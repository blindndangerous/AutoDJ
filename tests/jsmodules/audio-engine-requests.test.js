import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function binaryResponse(type = "audio/mpeg") {
  return new globalThis.Response(new Uint8Array([1, 2, 3]), {
    status: 200,
    headers: { "Content-Type": type },
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

function gainParam(value = 0) {
  return {
    cancelScheduledValues: vi.fn(),
    exponentialRampToValueAtTime: vi.fn(),
    linearRampToValueAtTime: vi.fn(),
    setValueAtTime: vi.fn(),
    value,
  };
}

function installDom() {
  document.body.innerHTML = `
    <input id="eq-low" type="range" value="100">
    <input id="eq-mid" type="range" value="100">
    <input id="eq-high" type="range" value="100">
    <span id="eq-low-value"></span>
    <span id="eq-mid-value"></span>
    <span id="eq-high-value"></span>
    <div id="eq-announce"></div>
    <button id="btn-eq-reset"></button>
    <div class="volume-row"><input id="vol" type="range" value="100"></div>
    <button id="btn-pause"></button>
    <img id="cover-art">
    <div id="now-playing-announce"></div>
    <audio id="browser-player"></audio>
    <audio id="browser-player-b"></audio>
  `;
  for (const audio of document.querySelectorAll("audio")) {
    audio.play = vi.fn().mockResolvedValue(undefined);
    audio.pause = vi.fn();
    audio.load = vi.fn();
  }
}

function installAudioContext({ decodeAudioData = vi.fn().mockResolvedValue({}) } = {}) {
  const context = {
    createAnalyser: vi.fn(() => ({ fftSize: 0 })),
    createGain: vi.fn(() => ({
      connect: vi.fn(),
      disconnect: vi.fn(),
      gain: gainParam(),
    })),
    createMediaElementSource: vi.fn(() => ({
      connect: vi.fn(),
      disconnect: vi.fn(),
    })),
    currentTime: 2,
    decodeAudioData,
    destination: {},
    resume: vi.fn().mockResolvedValue(undefined),
    state: "running",
  };
  vi.stubGlobal("AudioContext", vi.fn(() => context));
  window.AudioContext = globalThis.AudioContext;
  return context;
}

async function importEngine(options) {
  installDom();
  const context = installAudioContext(options);
  const engine = await import("../../src/autodj/static/modules/audio-engine.js");
  return { context, engine };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("audio engine request recovery", () => {
  it("clears transient transition markers without destructive session reset", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { engine } = await importEngine();
    engine.applyBrowserPlaybackState({
      browser_playback: true,
      current_track: {
        outro_len: 4,
        outro_start_s: 100,
        path: "current.mp3",
      },
      is_muted: false,
      is_paused: false,
      next_track: { intro_end_s: 3, path: "next.mp3" },
      settings: { playback: {} },
    });
    expect(engine._currentOutroLenCache).toBe(4);
    expect(engine._currentOutroStartCache).toBe(100);
    expect(engine._nextTrackIntroEndCache).toBe(3);
    expect(engine._nextTrackPathCache).toBe("next.mp3");

    engine.resetTransitionCaches();

    expect(engine._currentOutroLenCache).toBeNull();
    expect(engine._currentOutroStartCache).toBeNull();
    expect(engine._nextTrackIntroEndCache).toBeNull();
    expect(engine._nextTrackPathCache).toBeNull();
  });

  it("retries advance after a rejected single-flight request settles", async () => {
    const first = deferred();
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchImpl);
    const { engine } = await importEngine();

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    first.resolve(jsonResponse({ detail: "Advance failed" }, 503));
    await vi.waitFor(() => expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("Advance failed"));

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
  });

  it("retries repick after failure and clears its pending owner", async () => {
    const first = deferred();
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchImpl);
    const { engine } = await importEngine();
    const standby = engine.decks[1];
    Object.defineProperty(standby.audio, "error", {
      configurable: true,
      value: { code: 3 },
    });
    standby.path = "bad.mp3";

    standby.audio.dispatchEvent(new Event("error"));
    standby.path = "bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    first.resolve(jsonResponse({ detail: "Repick failed" }, 503));
    await vi.waitFor(() => expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("Repick failed"));

    standby.path = "bad-again.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
  });

  it("does not give different repick blacklists the first request result", async () => {
    const first = deferred();
    const second = deferred();
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    vi.stubGlobal("fetch", fetchImpl);
    const { engine } = await importEngine();
    const standby = engine.decks[1];
    Object.defineProperty(standby.audio, "error", {
      configurable: true,
      value: { code: 3 },
    });

    standby.path = "first-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    standby.path = "second-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));

    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ blacklist: "first-bad.mp3" });
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({ blacklist: "second-bad.mp3" });
    first.resolve(jsonResponse({ ok: true, next_track: { path: "first.mp3" } }));
    second.resolve(jsonResponse({ ok: true, next_track: { path: "second.mp3" } }));
  });

  it("lets only the newest distinct repick response update state", async () => {
    const first = deferred();
    const second = deferred();
    vi.stubGlobal("fetch", vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise));
    const { engine } = await importEngine();
    const applyState = vi.fn();
    engine.setApplyState(applyState);
    const standby = engine.decks[1];
    Object.defineProperty(standby.audio, "error", {
      configurable: true,
      value: { code: 3 },
    });
    standby.path = "old-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    standby.path = "new-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));

    second.resolve(jsonResponse({ ok: true, next_track: { path: "new.mp3" } }));
    await vi.waitFor(() => expect(applyState).toHaveBeenCalledOnce());
    first.resolve(jsonResponse({ ok: true, next_track: { path: "old.mp3" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(applyState).toHaveBeenCalledOnce();
    expect(applyState).toHaveBeenCalledWith({
      ok: true, next_track: { path: "new.mp3" },
    });
  });

  it("drops an advance response completed after authenticated expiry", async () => {
    const pending = deferred();
    vi.stubGlobal("fetch", vi.fn(() => pending.promise));
    const { engine } = await importEngine();
    const apiClient = await import("../../src/autodj/static/modules/api-client.js");
    const applyState = vi.fn();
    engine.setApplyState(applyState);

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    apiClient.invalidateAuthenticatedRequestEpoch?.();
    pending.resolve(jsonResponse({ ok: true, current_track: { path: "late.mp3" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(applyState).not.toHaveBeenCalled();
  });

  it("drops an advance completed after a transient playback stop and accepts a new one", async () => {
    const stale = deferred();
    const current = deferred();
    vi.stubGlobal("fetch", vi.fn()
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => current.promise));
    const { engine } = await importEngine();
    const applyState = vi.fn();
    engine.setApplyState(applyState);

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    engine.stopAllDecks();
    engine.resetTransitionCaches();
    stale.resolve(jsonResponse({ ok: true, current_track: { path: "stale.mp3" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(applyState).not.toHaveBeenCalled();

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    current.resolve(jsonResponse({ ok: true, current_track: { path: "current.mp3" } }));
    await vi.waitFor(() => expect(applyState).toHaveBeenCalledOnce());
    expect(applyState).toHaveBeenCalledWith({
      ok: true, current_track: { path: "current.mp3" },
    });
  });

  it("drops a repick completed after a transient playback stop and accepts a new one", async () => {
    const stale = deferred();
    const current = deferred();
    vi.stubGlobal("fetch", vi.fn()
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => current.promise));
    const { engine } = await importEngine();
    const applyState = vi.fn();
    engine.setApplyState(applyState);
    const standby = engine.decks[1];
    Object.defineProperty(standby.audio, "error", {
      configurable: true,
      value: { code: 3 },
    });

    standby.path = "stale-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    engine.stopAllDecks();
    engine.resetTransitionCaches();
    stale.resolve(jsonResponse({ ok: true, next_track: { path: "stale.mp3" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(applyState).not.toHaveBeenCalled();

    standby.path = "current-bad.mp3";
    standby.audio.dispatchEvent(new Event("error"));
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    current.resolve(jsonResponse({ ok: true, next_track: { path: "current.mp3" } }));
    await vi.waitFor(() => expect(applyState).toHaveBeenCalledOnce());
    expect(applyState).toHaveBeenCalledWith({
      ok: true, next_track: { path: "current.mp3" },
    });
  });

  it("does not promote a queued advance into a new authenticated epoch", async () => {
    const pending = deferred();
    vi.stubGlobal("fetch", vi.fn(() => pending.promise));
    const { engine } = await importEngine();
    const apiClient = await import("../../src/autodj/static/modules/api-client.js");
    const applyState = vi.fn();
    engine.setApplyState(applyState);

    engine.decks[0].audio.dispatchEvent(new Event("ended"));
    apiClient.invalidateAuthenticatedRequestEpoch();
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (fetch.mock.calls.length > 0) {
      pending.resolve(jsonResponse({ ok: true, current_track: { path: "late.mp3" } }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    expect(applyState).not.toHaveBeenCalled();
  });

  it("reports rejected EQ updates", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ detail: "EQ unavailable" }, 503),
    ));
    const { engine } = await importEngine();

    engine.postEq();
    await vi.advanceTimersByTimeAsync(121);
    await flushPromises();
    expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("EQ unavailable");
  });

  it("reports non-audio binary responses without decoding them", async () => {
    const decodeAudioData = vi.fn().mockResolvedValue({});
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(binaryResponse("text/html")));
    const { engine } = await importEngine({ decodeAudioData });
    engine.ensureAudioGraph();

    engine.setSrcOnDeck(engine.decks[0], "bad.mp3");
    await vi.waitFor(() => expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("unexpected content type"));
    expect(decodeAudioData).not.toHaveBeenCalled();
  });

  it("announces status failures while unlocking playback", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      jsonResponse({ detail: "Status unavailable" }, 503),
    ));
    const { engine } = await importEngine();

    await expect(engine.unlockAndPlay()).rejects.toThrow("Status unavailable");
    expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("Status unavailable");
  });

  it("aborts stale art probes and reports a current non-image response", async () => {
    const oldRequest = deferred();
    const newRequest = deferred();
    const invalidRequest = deferred();
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)
      .mockImplementationOnce(() => invalidRequest.promise);
    vi.stubGlobal("fetch", fetchImpl);
    const { engine } = await importEngine();

    engine.loadCoverArt("old.mp3");
    engine.loadCoverArt("new.mp3");
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true);
    newRequest.resolve(binaryResponse("image/jpeg"));
    await vi.waitFor(() => expect(document.querySelector("#cover-art").src)
      .toContain("new.mp3"));
    oldRequest.resolve(binaryResponse("image/jpeg"));
    await flushPromises();
    expect(document.querySelector("#cover-art").src).toContain("new.mp3");

    engine.loadCoverArt("invalid.mp3");
    invalidRequest.resolve(binaryResponse("application/json"));
    await vi.waitFor(() => expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("unexpected content type"));
    expect(document.querySelector("#cover-art").hidden).toBe(true);
  });

  it("does not let an old decode rejection delete a newer same-path owner", async () => {
    const oldRequest = deferred();
    const newRequest = deferred();
    const fetchImpl = vi.fn()
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)
      .mockResolvedValue(binaryResponse());
    vi.stubGlobal("fetch", fetchImpl);
    const { engine } = await importEngine();
    engine.ensureAudioGraph();

    engine.setSrcOnDeck(engine.decks[0], "same.mp3");
    engine.resetTrackCaches();
    engine.setSrcOnDeck(engine.decks[1], "same.mp3");
    oldRequest.reject(new Error("old request failed"));
    await flushPromises();
    engine.setSrcOnDeck({ audio: {}, path: null }, "same.mp3");
    await flushPromises();

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    newRequest.resolve(binaryResponse());
  });

  it("keeps crossfade ownership pending until teardown completes", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ok: true })));
    const { engine } = await importEngine();
    engine.ensureAudioGraph();
    engine.decks[0].path = "current.mp3";
    engine.decks[1].path = "next.mp3";

    const operation = engine.startCrossfade("next.mp3", 1);
    expect(operation).toBeInstanceOf(Promise);
    let settled = false;
    operation.then(() => { settled = true; });
    await vi.advanceTimersByTimeAsync(1000);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(101);
    expect(settled).toBe(true);
  });

  it("cancels an owned crossfade without delayed playback resuming", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ok: true })));
    const { engine } = await importEngine();
    engine.ensureAudioGraph();
    engine.decks[0].path = "current.mp3";
    engine.decks[1].path = "next.mp3";

    const operation = engine.startCrossfade("next.mp3", 1);
    const activeBefore = engine.activeIdx;
    const playCounts = engine.decks.map((deck) => deck.audio.play.mock.calls.length);
    engine.stopAllDecks();
    await vi.advanceTimersByTimeAsync(2000);

    await expect(operation).resolves.toBe(false);
    expect(engine.activeIdx).toBe(activeBefore);
    expect(engine.crossfading).toBe(false);
    expect(engine.decks.map((deck) => deck.audio.play.mock.calls.length)).toEqual(playCounts);
  });
});
