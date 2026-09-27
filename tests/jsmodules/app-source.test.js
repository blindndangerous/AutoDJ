import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

const moduleMocks = [
  "../../src/autodj/static/modules/library-jobs.js",
  "../../src/autodj/static/modules/liners.js",
  "../../src/autodj/static/modules/hotkeys.js",
  "../../src/autodj/static/modules/media-session.js",
  "../../src/autodj/static/modules/audio-engine.js",
];

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installDocument() {
  const html = readFileSync(
    join(process.cwd(), "src/autodj/static/index.html"), "utf8",
  );
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll('script, link[rel="stylesheet"]')
    .forEach((element) => element.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  const dialog = document.querySelector("#auth-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return dialog;
}

async function setupApp({
  audio = {},
  initialState = {},
  onAuthStatus = () => jsonResponse({ required: true, authenticated: true }),
  onInstallLiners = () => {},
  onRequest = (url) => {
    throw new Error(`Unexpected request: ${url}`);
  },
} = {}) {
  vi.resetModules();
  const dialog = installDocument();
  const linerModule = await vi.importActual(moduleMocks[1]);
  linerModule.resetLinerStateForTest();
  const stopAllDecks = audio.stopAllDecks || vi.fn();
  const resetTrackCaches = audio.resetTrackCaches || vi.fn();
  const resetTransitionCaches = audio.resetTransitionCaches || vi.fn();
  const loadCoverArt = audio.loadCoverArt || vi.fn();
  const unlockAndPlay = audio.unlockAndPlay || vi.fn().mockResolvedValue();
  const startCrossfade = audio.startCrossfade || vi.fn().mockResolvedValue(true);
  const setLastBrowserPlayback = audio.setLastBrowserPlayback || vi.fn();
  const updateMediaSession = vi.fn();
  const bumpLinerTrackCount = vi.fn(linerModule.bumpLinerTrackCount);
  const audioDefaults = {
    _beatmatchOnSkip: false,
    _ctx: null,
    _crossfadeSecondsCache: 3,
    _inBpmCache: 0,
    _lastBrowserPlayback: false,
    _nextTrackPathCache: null,
    _outBpmCache: 0,
    _volume: 1,
    activeIdx: 0,
    applyBrowserPlaybackState: vi.fn(),
    applyEqState: vi.fn(),
    applyTransitionFx: vi.fn(),
    crossfading: false,
    deckActive: vi.fn(),
    decks: [],
    deckStandby: vi.fn(),
    ensureAudioGraph: vi.fn(),
    eqValueLabel: vi.fn(),
    loadCoverArt,
    playbackEnabled: false,
    playOnDeck: vi.fn(),
    postEq: vi.fn(),
    resetTrackCaches,
    resetTransitionCaches,
    setApplyState: vi.fn(),
    setLastBrowserPlayback,
    setSrcOnDeck: vi.fn(),
    setVolume: vi.fn(),
    startCrossfade,
    stopAllDecks,
    unlockAndPlay,
    ...audio,
  };
  vi.doMock(moduleMocks[0], () => ({
    applyLibraryJobState: vi.fn(),
    installLibraryJobs: vi.fn(),
  }));
  vi.doMock(moduleMocks[1], () => ({
    bumpLinerTrackCount,
    installLiners: vi.fn(onInstallLiners),
  }));
  vi.doMock(moduleMocks[2], () => ({
    installHotkeys: vi.fn(),
    toggleShortcutsModal: vi.fn(),
  }));
  vi.doMock(moduleMocks[3], () => ({
    installMediaActionHandlers: vi.fn(),
    updateMediaSession,
  }));
  vi.doMock(moduleMocks[4], () => audioDefaults);

  const state = {
    browser_playback: false,
    current_track: null,
    discovery_available: false,
    duration: 0,
    elapsed: 0,
    eq: {},
    is_muted: false,
    is_paused: false,
    next_track: null,
    queue: [],
    settings: null,
    volume: 1,
    ...initialState,
  };
  const fetchImpl = vi.fn((url, options) => {
    if (url === "/api/auth/status") {
      return Promise.resolve(onAuthStatus());
    }
    if (url === "/api/status") return Promise.resolve(jsonResponse(state));
    if (url === "/api/version") {
      return Promise.resolve(jsonResponse({
        version: "test", commit: "test", built_at: "2026-01-01T00:00:00Z",
      }));
    }
    return Promise.resolve(onRequest(url, options));
  });
  const webSocket = { close: vi.fn(), readyState: 1, send: vi.fn() };
  const WebSocketImpl = vi.fn(function WebSocketMock() {
    return webSocket;
  });
  WebSocketImpl.CONNECTING = 0;
  WebSocketImpl.OPEN = 1;
  vi.stubGlobal("fetch", fetchImpl);
  vi.stubGlobal("WebSocket", WebSocketImpl);

  await import("../../src/autodj/static/app.js");
  try {
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledOnce());
  } catch (error) {
    throw new Error(`${error.message}; connection=${document.querySelector("#conn-status")?.textContent}; requests=${fetchImpl.mock.calls.map(([url]) => url).join(",")}`, {
      cause: error,
    });
  }
  return {
    bumpLinerTrackCount,
    dialog,
    fetchImpl,
    getLinerTrackCountForTest: linerModule.getLinerTrackCountForTest,
    loadCoverArt,
    resetTrackCaches,
    resetTransitionCaches,
    setLastBrowserPlayback,
    startCrossfade,
    stopAllDecks,
    unlockAndPlay,
    updateMediaSession,
    webSocket,
    WebSocketImpl,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  for (const modulePath of moduleMocks) vi.doUnmock(modulePath);
  document.body.replaceChildren();
});

describe("durable playback semantics", () => {
  it("exposes deliberate live regions and durable non-live descriptions", () => {
    installDocument();
    const queueAnnounce = document.querySelector("#queue-announce");
    const settingsStatus = document.querySelector("#settings-status");
    const linerStatus = document.querySelector("#ln-status");
    const queueList = document.querySelector("#queue-list");
    const metadata = document.querySelector("#now-playing-meta");
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    const progress = document.querySelector("#progress-track");

    expect(queueAnnounce.getAttribute("role")).toBe("status");
    expect(queueAnnounce.getAttribute("aria-live")).toBe("polite");
    expect(queueAnnounce.getAttribute("aria-atomic")).toBe("true");
    expect(settingsStatus.getAttribute("aria-atomic")).toBe("true");
    expect(linerStatus.getAttribute("role")).toBe("status");
    expect(linerStatus.getAttribute("aria-live")).toBe("polite");
    expect(linerStatus.getAttribute("aria-atomic")).toBe("true");

    expect(metadata.hasAttribute("aria-hidden")).toBe(false);
    expect(metadata.hasAttribute("role")).toBe(false);
    expect(metadata.hasAttribute("aria-live")).toBe(false);
    expect(cueSummary).not.toBeNull();
    expect(cueSummary.hasAttribute("role")).toBe(false);
    expect(cueSummary.hasAttribute("aria-live")).toBe(false);
    expect(progress.contains(cueSummary)).toBe(false);
    expect(cueDetails).not.toBeNull();
    expect(cueDetails.hasAttribute("role")).toBe(false);
    expect(cueDetails.hasAttribute("aria-live")).toBe(false);
    expect(progress.contains(cueDetails)).toBe(false);
    const describedBy = progress.getAttribute("aria-describedby").split(/\s+/);
    expect(describedBy).toContain("cue-summary");
    expect(describedBy).not.toContain("cue-details");

    expect(queueList.getAttribute("role")).toBe("list");
    expect(queueList.getAttribute("aria-label")).toBe("Queued tracks");
    expect(queueList.getAttribute("tabindex")).toBe("-1");
    expect(queueList.hasAttribute("aria-live")).toBe(false);
  });

  it("renders complete metadata and cue text without rewriting unchanged ticks", async () => {
    const track = {
      album: "Night Drive",
      bpm: 128,
      cues: [{ type: "drop", time_s: 30 }],
      energy: 0.7,
      key_label: "8A",
      length: 120,
      path: "current.mp3",
      title: "Current",
    };
    const { webSocket } = await setupApp({
      initialState: { current_track: track, duration: 120 },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ lyrics: [], path: track.path })
        : jsonResponse({ ok: true }),
    });
    const metadata = document.querySelector("#now-playing-meta");
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    expect(cueSummary).not.toBeNull();
    expect(cueDetails).not.toBeNull();
    expect(metadata.textContent).toBe(
      "Album Night Drive · BPM 128 · Key 8A · Energy 0.70",
    );
    expect(cueSummary.textContent).toBe("1 cue point, drop at 30 seconds");
    expect(cueDetails.textContent).toBe("1 cue point, drop at 30 seconds");
    let metadataWrites = 0;
    let cueWrites = 0;
    let cueDetailWrites = 0;
    const descriptor = Object.getOwnPropertyDescriptor(
      globalThis.Node.prototype,
      "textContent",
    );
    const countWrites = (element, count) => {
      let storedText = element.textContent;
      Object.defineProperty(element, "textContent", {
        configurable: true,
        get() { return storedText; },
        set(value) {
          count();
          storedText = String(value);
          descriptor.set.call(this, value);
        },
      });
    };
    countWrites(metadata, () => { metadataWrites += 1; });
    countWrites(cueSummary, () => { cueWrites += 1; });
    countWrites(cueDetails, () => { cueDetailWrites += 1; });

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: track,
      discovery_available: false,
      duration: 120,
      elapsed: 1,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(metadataWrites).toBe(0);
    expect(cueWrites).toBe(0);
    expect(cueDetailWrites).toBe(0);
  });

  it("resets the cue summary when applied state has no track", async () => {
    const { webSocket } = await setupApp({
      initialState: {
        current_track: {
          cues: [{ type: "drop", time_s: 10 }],
          length: 60,
          path: "current.mp3",
          title: "Current",
        },
        duration: 60,
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    const metadata = document.querySelector("#now-playing-meta");
    expect(cueSummary).not.toBeNull();
    expect(cueDetails).not.toBeNull();
    expect(cueSummary.textContent).toContain("drop at 10 seconds");

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(cueSummary.textContent).toBe("No cue points");
    expect(cueDetails.textContent).toBe("No cue points");
    expect(metadata.textContent).toBe("");
  });
});

describe("app request behavior", () => {
  it("continues applying unrelated state while pointer seeking", async () => {
    const { setLastBrowserPlayback, webSocket } = await setupApp({
      initialState: {
        current_track: { path: "same.flac", title: "Same" },
        duration: 120,
        elapsed: 0,
      },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ path: "same.flac", lyrics: [] })
        : jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ left: 0, width: 100 });
    const down = new Event("pointerdown", { bubbles: true, cancelable: true });
    Object.defineProperties(down, {
      button: { value: 0 },
      clientX: { value: 50 },
      isPrimary: { value: true },
      pointerId: { value: 23 },
    });
    progress.dispatchEvent(down);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: true,
      current_track: { path: "same.flac", title: "Same" },
      discovery_available: false,
      duration: 120,
      elapsed: 30,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(document.querySelector("#progress-fill").style.width).toBe("50.0%");
    expect(setLastBrowserPlayback).toHaveBeenLastCalledWith(true);
  });

  it("cancels an active seek whenever applied state has no track", async () => {
    const { fetchImpl, webSocket } = await setupApp({
      initialState: { duration: 120, elapsed: 0 },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ left: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        isPrimary: { value: true },
        pointerId: { value: 24 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 120,
      elapsed: 30,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });
    dispatchPointer("pointerup", 100);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#progress-fill").style.width).toBe("25.0%");
    const seekBodies = fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body));
    expect(seekBodies).toEqual([]);
  });

  it("cancels an active seek when the current track changes", async () => {
    const pathFromLyricsUrl = (url) => decodeURIComponent(url.split("path=")[1]);
    const deckAudio = { currentTime: 10, duration: 120 };
    const { fetchImpl, webSocket } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: {
        current_track: { path: "first.flac", title: "First" },
        duration: 120,
      },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ path: pathFromLyricsUrl(url), lyrics: [] })
        : jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ left: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        isPrimary: { value: true },
        pointerId: { value: 29 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: { path: "second.flac", title: "Second" },
      discovery_available: false,
      duration: 200,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });
    dispatchPointer("pointerup", 100);
    await new Promise((resolve) => setTimeout(resolve, 0));

    const seekBodies = fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body));
    expect(seekBodies).toEqual([]);
    expect(deckAudio.currentTime).toBe(10);
  });

  it("passes every applied state through the liner cadence hook", async () => {
    const initialTrack = { path: "current.mp3", title: "Current" };
    const { bumpLinerTrackCount, webSocket } = await setupApp({
      initialState: { current_track: initialTrack },
    });
    expect(bumpLinerTrackCount).toHaveBeenCalledWith(
      expect.objectContaining({ current_track: initialTrack }),
    );

    const nextState = {
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };
    webSocket.onmessage({ data: JSON.stringify(nextState) });

    expect(bumpLinerTrackCount).toHaveBeenLastCalledWith(nextState);
  });

  it("reports a failing saved sink once, not once per devicechange", async () => {
    const deck = {
      audio: { setSinkId: vi.fn().mockRejectedValue(new Error("device gone")) },
    };
    let deviceChange = null;
    vi.stubGlobal("navigator", {
      mediaDevices: {
        enumerateDevices: vi.fn().mockResolvedValue([
          { deviceId: "usb-dac", kind: "audiooutput", label: "USB DAC" },
        ]),
        addEventListener: (type, handler) => {
          if (type === "devicechange") deviceChange = handler;
        },
      },
    });
    // happy-dom exposes localStorage on window, not on globalThis.
    const store = new Map([["autodj.sinkId", "usb-dac"]]);
    vi.stubGlobal("localStorage", {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, value),
      removeItem: (key) => store.delete(key),
    });
    vi.stubGlobal("HTMLMediaElement", { prototype: { setSinkId() {} } });

    await setupApp({
      audio: { decks: [deck] },
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const status = document.querySelector("#settings-status");
    const records = [];
    await vi.waitFor(() =>
      expect(status.textContent).toContain("Could not switch audio device"));
    new window.MutationObserver((r) => records.push(...r))
      .observe(status, { childList: true, characterData: true, subtree: true });

    // Three USB plug events, same failure every time.
    for (let i = 0; i < 3; i += 1) {
      deviceChange();
      await vi.waitFor(() => expect(deck.audio.setSinkId).toHaveBeenCalled());
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(0);

    // The user picking the device is a fresh action and must be reported,
    // even though the sentence has not changed.
    document.querySelector("#audio-device").value = "usb-dac";
    document.querySelector("#audio-device")
      .dispatchEvent(new window.Event("change"));
    await vi.waitFor(() =>
      expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(1));

  });

  it("announces a lost link once, however many retries it takes", async () => {
    const { webSocket } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const pill = document.querySelector("#conn-status");
    const records = [];
    new window.MutationObserver((r) => records.push(...r))
      .observe(pill, { childList: true, characterData: true, subtree: true });
    vi.useFakeTimers();

    // Ten failed cycles.  The old loop wrote "Connecting...", "Error" and
    // "Disconnected" once per cycle -- thirty announcements.
    let delay = 3000;
    for (let cycle = 0; cycle < 10; cycle += 1) {
      webSocket.onclose({ code: 1006, wasClean: false });
      webSocket.onerror();
      await vi.advanceTimersByTimeAsync(delay);
      delay = Math.min(delay * 2, 60000);
    }

    expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(1);
    expect(pill.textContent).toBe("Disconnected, retrying");
  });

  it("backs off between retries instead of hammering every three seconds", async () => {
    const { webSocket, WebSocketImpl } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(2999);
    expect(WebSocketImpl).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(WebSocketImpl).toHaveBeenCalledTimes(2);

    // Second failure waits six seconds, not three.
    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    expect(WebSocketImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(WebSocketImpl).toHaveBeenCalledTimes(3);
  });

  it("cancels a scheduled transient auth probe after REST expiry", async () => {
    let authStatusCalls = 0;
    const { dialog, webSocket, WebSocketImpl } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onAuthStatus: () => {
        authStatusCalls += 1;
        return jsonResponse({ required: true, authenticated: true });
      },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(3000);

    expect(authStatusCalls).toBe(1);
    expect(WebSocketImpl).toHaveBeenCalledOnce();
  });

  it("ignores an in-flight transient probe failure after REST expiry", async () => {
    let authStatusCalls = 0;
    let rejectProbe;
    const { dialog, webSocket, WebSocketImpl } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onAuthStatus: () => {
        authStatusCalls += 1;
        if (authStatusCalls === 1) {
          return jsonResponse({ required: true, authenticated: true });
        }
        return new Promise((_resolve, reject) => { rejectProbe = reject; });
      },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    expect(authStatusCalls).toBe(2);
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    rejectProbe(new Error("transient probe failed"));
    await vi.advanceTimersByTimeAsync(0);

    expect(WebSocketImpl).toHaveBeenCalledOnce();
  });

  it("stops transient socket activity without clearing recoverable session data", async () => {
    let linerDeps;
    const source = {
      addEventListener: vi.fn(),
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const audioContext = {
      createBufferSource: vi.fn(() => source),
      createGain: vi.fn(() => ({ gain: { value: 1 }, connect: vi.fn() })),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 1 }),
      destination: {},
    };
    const stopAllDecks = vi.fn();
    const resetTrackCaches = vi.fn();
    const resetTransitionCaches = vi.fn();
    const {
      dialog, loadCoverArt, webSocket, WebSocketImpl,
    } = await setupApp({
      audio: {
        _ctx: audioContext,
        decks: [{ gain: { gain: {
          cancelScheduledValues: vi.fn(),
          linearRampToValueAtTime: vi.fn(),
          setValueAtTime: vi.fn(),
          value: 1,
        } } }],
        playbackEnabled: true,
        resetTrackCaches,
        resetTransitionCaches,
        stopAllDecks,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
      onRequest: () => jsonResponse({ lyrics: [] }),
    });
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    const history = document.querySelector("#history-list");
    for (let index = 2; index <= 5; index += 1) {
      webSocket.onmessage({ data: JSON.stringify({
        browser_playback: true,
        current_track: { path: `track-${index}.mp3`, title: `Track ${index}` },
        discovery_available: false,
        duration: 0,
        elapsed: 0,
        eq: {},
        is_muted: false,
        is_paused: false,
        next_track: null,
        queue: [],
        settings: null,
        volume: 1,
      }) });
    }
    expect(history.children).toHaveLength(5);
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });

    expect(stopAllDecks).toHaveBeenCalledOnce();
    expect(source.stop).toHaveBeenCalledOnce();
    expect(resetTrackCaches).not.toHaveBeenCalled();
    expect(resetTransitionCaches).toHaveBeenCalledOnce();
    expect(loadCoverArt).not.toHaveBeenCalledWith(null);
    expect(history.children).toHaveLength(5);
    expect(dialog.showModal).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(2));
    expect(dialog.showModal).not.toHaveBeenCalled();
  });

  it("owns first-click unlock until it settles and restores Pause", async () => {
    let resolveUnlock;
    const unlockAndPlay = vi.fn(() => new Promise((resolve) => {
      resolveUnlock = resolve;
    }));
    await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: false, unlockAndPlay },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
    });
    const pause = document.querySelector("#btn-pause");

    pause.click();
    pause.click();
    expect(unlockAndPlay).toHaveBeenCalledOnce();
    expect(pause.disabled).toBe(true);
    resolveUnlock();
    await vi.waitFor(() => expect(pause.disabled).toBe(false));
  });

  it("keeps Skip owned through a browser crossfade and ignores double click", async () => {
    let resolveCrossfade;
    const startCrossfade = vi.fn(() => new Promise((resolve) => {
      resolveCrossfade = resolve;
    }));
    const { fetchImpl } = await setupApp({
      audio: {
        _ctx: {},
        _lastBrowserPlayback: true,
        _nextTrackPathCache: "next.mp3",
        crossfading: false,
        decks: [{ audio: { duration: Number.NaN } }],
        playbackEnabled: true,
        startCrossfade,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
        next_track: { path: "next.mp3", title: "Next" },
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const skip = document.querySelector("#btn-skip");

    skip.click();
    skip.click();
    await vi.waitFor(() => expect(startCrossfade).toHaveBeenCalledOnce());
    expect(skip.disabled).toBe(true);
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/skip")).toBe(false);
    resolveCrossfade(true);
    await vi.waitFor(() => expect(skip.disabled).toBe(false));
  });

  it("sends an absolute seek with the exact seconds payload", async () => {
    const { fetchImpl } = await setupApp({
      initialState: { duration: 100 },
      onRequest: () => jsonResponse({ elapsed: 99 }),
    });
    const seek = document.querySelector("#progress-track");

    seek.dispatchEvent(new KeyboardEvent("keydown", {
      key: "End", bubbles: true, cancelable: true,
    }));
    await vi.waitFor(() => expect(fetchImpl.mock.calls.some(
      ([url]) => url === "/api/seek",
    )).toBe(true));
    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/seek");
    expect(JSON.parse(options.body)).toEqual({ seconds: 99 });
  });

  it("steps the focused seek slider from the real position, not its frozen value", async () => {
    const baseState = {
      current_track: { path: "a.mp3", title: "A" }, duration: 200, elapsed: 50,
    };
    const { fetchImpl, webSocket } = await setupApp({
      initialState: baseState,
      onRequest: () => jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.focus();
    const seeks = () => fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body).seconds);
    const press = (key) => seek.dispatchEvent(new KeyboardEvent("keydown", {
      key, bubbles: true, cancelable: true,
    }));

    // The playhead moved on while the slider held focus; its attribute did not.
    webSocket.onmessage({ data: JSON.stringify({ ...baseState, elapsed: 80 }) });
    expect(seek.getAttribute("aria-valuetext")).toBe("0:50 of 3:20");
    press("ArrowRight");
    await vi.waitFor(() => expect(seeks()).toEqual([85]));
    expect(seek.getAttribute("aria-valuetext")).toBe("1:25 of 3:20");

    // A stale tick lands before the server applies the seek; the next
    // press still builds on the position just requested.
    webSocket.onmessage({ data: JSON.stringify({ ...baseState, elapsed: 81 }) });
    press("ArrowRight");
    await vi.waitFor(() => expect(seeks()).toEqual([85, 90]));
    expect(seek.getAttribute("aria-valuetext")).toBe("1:30 of 3:20");
  });

  it("saves key notation without announcing into the hidden Now Playing panel", async () => {
    const { fetchImpl } = await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const notation = document.querySelector("#key-notation");
    notation.value = "musical";
    notation.dispatchEvent(new Event("change"));
    const flats = document.querySelector("#key-prefer-flats");
    flats.checked = true;
    flats.dispatchEvent(new Event("change"));

    await vi.waitFor(() => expect(fetchImpl.mock.calls
      .filter(([url]) => url === "/api/playback-settings")).toHaveLength(2));
    const nowPanel = document.querySelector("#panel-now").textContent;
    expect(nowPanel).not.toContain("Key notation");
    expect(nowPanel).not.toContain("key spelling");
  });

  it("gives Pause a changing label and Mute a fixed label with pressed state", async () => {
    const { webSocket } = await setupApp({
      initialState: { current_track: { path: "a.mp3", title: "A" } },
    });
    const pause = document.querySelector("#btn-pause");
    const mute = document.querySelector("#btn-mute");
    const label = (button) => button.textContent.replace(/[^A-Za-z]/g, "");

    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "a.mp3", title: "A" }, is_paused: true, is_muted: true,
      queue: [], eq: {}, volume: 1,
    }) });
    expect(label(pause)).toBe("Resume");
    expect(pause.hasAttribute("aria-pressed")).toBe(false);
    expect(label(mute)).toBe("Mute");
    expect(mute.getAttribute("aria-pressed")).toBe("true");

    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "a.mp3", title: "A" }, is_paused: false, is_muted: false,
      queue: [], eq: {}, volume: 1,
    }) });
    expect(label(pause)).toBe("Pause");
    expect(pause.hasAttribute("aria-pressed")).toBe(false);
    expect(label(mute)).toBe("Mute");
    expect(mute.getAttribute("aria-pressed")).toBe("false");
  });

  it("leaves unchanged transport buttons untouched on a repeated push", async () => {
    const tick = {
      current_track: { path: "a.mp3", title: "A" }, is_paused: false, is_muted: true,
      discovery_available: true, discovery_enabled: true,
      queue: [], eq: {}, volume: 1,
    };
    const { webSocket } = await setupApp({ initialState: tick });
    webSocket.onmessage({ data: JSON.stringify(tick) });
    const records = [];
    const observer = new window.MutationObserver((batch) => records.push(...batch));
    for (const id of ["#btn-pause", "#btn-mute", "#btn-discovery"]) {
      observer.observe(document.querySelector(id), {
        attributes: true, childList: true, characterData: true, subtree: true,
      });
    }

    webSocket.onmessage({ data: JSON.stringify(tick) });
    await Promise.resolve();
    expect(records).toHaveLength(0);

    webSocket.onmessage({ data: JSON.stringify({ ...tick, is_paused: true }) });
    await Promise.resolve();
    expect(records.length).toBeGreaterThan(0);
    observer.disconnect();
  });

  it("announces a track change once, key included, with Up Next silent", async () => {
    const { webSocket } = await setupApp();
    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "a.mp3", artist: "Artist", title: "Song", bpm: 128, key_label: "8A" },
      next_track: { path: "b.mp3", artist: "Other", title: "Next" },
      queue: [], eq: {}, volume: 1,
    }) });

    const title = document.querySelector("#now-playing-announce");
    expect(title.getAttribute("aria-live")).toBe("polite");
    expect(title.textContent).toBe("Artist — Song, 128 BPM, key 8A");
    expect(title.querySelector(".visually-hidden").textContent).toBe(", key 8A");
    const upNext = document.querySelector("#next-track-text");
    expect(upNext.textContent).toBe("Other — Next");
    expect(upNext.hasAttribute("aria-live")).toBe(false);
    expect(upNext.closest("[aria-live]")).toBeNull();
    expect(document.querySelector("#badges-announce")).toBeNull();
  });

  it("speaks volume once: from the slider when focused, the region otherwise", async () => {
    await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const slider = document.querySelector("#vol");
    const region = document.querySelector("#vol-announce");

    slider.focus();
    slider.value = "95";
    slider.dispatchEvent(new Event("input"));
    expect(slider.getAttribute("aria-valuetext")).toBe("95%");
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(region.textContent).toBe("");

    // A shortcut press while focus is elsewhere still gets spoken.
    slider.blur();
    slider.value = "90";
    slider.dispatchEvent(new Event("input"));
    expect(slider.getAttribute("aria-valuetext")).toBe("90%");
    await vi.waitFor(() => expect(region.textContent).toBe("Volume 90%."));
  });

  it("keeps pointer previews local and sends only the final absolute seek", async () => {
    let resolveSeek;
    const deckAudio = { currentTime: 10, duration: 100 };
    const { fetchImpl } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: { duration: 100 },
      onRequest: (url) => url === "/api/seek"
        ? new Promise((resolve) => { resolveSeek = resolve; })
        : jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.getBoundingClientRect = () => ({ left: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        isPrimary: { value: true },
        pointerId: { value: 55 },
      });
      seek.dispatchEvent(event);
    };

    dispatchPointer("pointerdown", 20);
    dispatchPointer("pointermove", 80);
    expect(document.querySelector("#progress-fill").style.width).toBe("20.0%");
    expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/seek")).toHaveLength(0);
    expect(deckAudio.currentTime).toBe(10);

    dispatchPointer("pointerup", 90);
    await vi.waitFor(() => expect(fetchImpl.mock.calls.filter(
      ([url]) => url === "/api/seek",
    )).toHaveLength(1));
    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/seek");
    expect(JSON.parse(options.body)).toEqual({ seconds: 90 });
    expect(document.querySelector("#progress-fill").style.width).toBe("90.0%");
    expect(deckAudio.currentTime).toBe(90);

    resolveSeek(jsonResponse({ elapsed: 90 }));
  });

  it("keeps browser audio at its start position on pointer lifecycle cancellation", async () => {
    const deckAudio = { currentTime: 10, duration: 100 };
    const { fetchImpl } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: { duration: 100 },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.getBoundingClientRect = () => ({
      bottom: 20, left: 0, right: 100, top: 0, width: 100,
    });
    const pointer = (type, clientX = 80) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 57 },
      });
      seek.dispatchEvent(event);
    };
    const cancellations = [
      () => pointer("pointercancel"),
      () => pointer("lostpointercapture"),
      () => window.dispatchEvent(new Event("blur")),
      () => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true, value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
        Object.defineProperty(document, "visibilityState", {
          configurable: true, value: "visible",
        });
      },
      () => pointer("pointerup", 101),
    ];

    for (const cancel of cancellations) {
      deckAudio.currentTime = 10;
      pointer("pointerdown");
      expect(deckAudio.currentTime).toBe(10);
      cancel();
      expect(deckAudio.currentTime).toBe(10);
    }
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);
  });

  it("loads history on a direct #history visit and pages without losing focus", async () => {
    // App instances left by earlier tests still listen for hashchange;
    // let them react against an inert fetch rather than the network.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const pageOf = (url) => Number(new URL(url, "http://x").searchParams.get("page"));
    try {
      const { fetchImpl } = await setupApp({
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({
            total: 60, page: pageOf(url), pages: 2,
            items: [{ played_at: "2026-01-01T00:00:00Z", title: `T${pageOf(url)}`, artist: "A", duration: 60 }],
          })
          : jsonResponse({ ok: true }),
      });
      const historyCalls = () => fetchImpl.mock.calls
        .filter(([url]) => url.startsWith("/api/history"));
      await vi.waitFor(() => expect(historyCalls()).toHaveLength(1));
      document.dispatchEvent(new Event("DOMContentLoaded"));
      const prev = document.querySelector("#hist-prev");
      const next = document.querySelector("#hist-next");
      const status = document.querySelector("#sr-status");
      await vi.waitFor(() => expect(prev.getAttribute("aria-disabled")).toBe("true"));
      // The automatic first load is not announced; only user paging is.
      expect(status.textContent).toBe("");

      next.focus();
      next.click();
      await vi.waitFor(() => expect(status.textContent).toBe("Page 2 of 2"));
      expect(next.getAttribute("aria-disabled")).toBe("true");
      expect(next.disabled).toBe(false);
      expect(document.activeElement).toBe(next);

      // Next is unavailable on the last page, so nothing asks for page 3.
      next.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(historyCalls().some(([url]) => pageOf(url) === 3)).toBe(false);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("clears stale history and exposes a current load failure", async () => {
    await setupApp({
      onRequest: (url) => url.startsWith("/api/history")
        ? jsonResponse({ detail: "History unavailable" }, 503)
        : jsonResponse({ ok: true }),
    });
    document.dispatchEvent(new Event("DOMContentLoaded"));
    const tbody = document.querySelector("#history-tbody");
    const table = document.querySelector("#history-table");
    const pagination = document.querySelector("#history-pagination");
    const empty = document.querySelector("#history-empty");
    tbody.innerHTML = "<tr><td>stale</td><td>track</td></tr>";
    table.removeAttribute("hidden");
    pagination.removeAttribute("hidden");
    document.querySelector("#hist-goto").value = "1";

    document.querySelector("#hist-go").click();
    await vi.waitFor(() => expect(empty.textContent).toContain("History unavailable"));
    expect(tbody.children).toHaveLength(0);
    expect(table.hasAttribute("hidden")).toBe(true);
    expect(pagination.hasAttribute("hidden")).toBe(true);
    expect(empty.hasAttribute("hidden")).toBe(false);
  });

  it("tears down buffered decks and liners before showing auth on REST 401", async () => {
    let linerDeps;
    const deckAudio = { currentTime: 10, duration: 100 };
    const source = {
      addEventListener: vi.fn(),
      buffer: null,
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const gainParam = {
      cancelScheduledValues: vi.fn(),
      linearRampToValueAtTime: vi.fn(),
      setValueAtTime: vi.fn(),
      value: 1,
    };
    const audioContext = {
      createBufferSource: vi.fn(() => source),
      createGain: vi.fn(() => ({ gain: { value: 1 }, connect: vi.fn() })),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 1 }),
      destination: {},
    };
    const stopAllDecks = vi.fn();
    const resetTrackCaches = vi.fn();
    const { dialog, fetchImpl, updateMediaSession, webSocket } = await setupApp({
      audio: {
        _ctx: audioContext,
        _lastBrowserPlayback: true,
        decks: [{
          audio: deckAudio,
          gain: { gain: gainParam },
        }],
        playbackEnabled: true,
        resetTrackCaches,
        stopAllDecks,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
        duration: 100,
        next_track: { path: "next.mp3", title: "Next" },
        queue: [{ path: "queued.mp3", title: "Queued secret" }],
        settings: {
          available_presets: ["Secret preset"],
          bpm_range: {},
          discovery_every: null,
          djmix: {},
          playback: {},
          preset: "Secret preset",
          transition: "echo_out",
        },
      },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    expect(document.querySelector("#history-list").children.length).toBeGreaterThan(0);
    expect(document.querySelector("#next-track-text").textContent).toContain("Next");
    expect(document.querySelector("#queue-list").textContent).toContain("Queued secret");
    expect(document.querySelector("#preset-select").textContent).toContain("Secret preset");

    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ left: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        isPrimary: { value: true },
        pointerId: { value: 61 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);
    expect(document.querySelector("#progress-fill").style.width).toBe("50.0%");

    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    expect(stopAllDecks).toHaveBeenCalledOnce();
    expect(source.stop).toHaveBeenCalledOnce();
    expect(resetTrackCaches).toHaveBeenCalledOnce();
    expect(webSocket.close).toHaveBeenCalledOnce();
    expect(document.querySelector("#history-list").children).toHaveLength(0);
    expect(document.querySelector("#now-playing-announce").textContent).not.toContain("Current");
    expect(document.querySelector("#now-playing-meta").textContent).toBe("");
    const cueSummary = document.querySelector("#cue-summary");
    expect(cueSummary).not.toBeNull();
    if (cueSummary) expect(cueSummary.textContent).toBe("No cue points");
    const cueDetails = document.querySelector("#cue-details");
    expect(cueDetails).not.toBeNull();
    if (cueDetails) expect(cueDetails.textContent).toBe("No cue points");
    expect(document.querySelector("#next-track-text").textContent).toBe("—");
    expect(document.querySelector("#queue-list").textContent).not.toContain("Queued secret");
    expect(document.querySelector("#preset-select").textContent).not.toContain("Secret preset");
    expect(updateMediaSession).toHaveBeenLastCalledWith({ current_track: null });

    dispatchPointer("pointermove", 90);
    dispatchPointer("pointerup", 90);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector("#progress-fill").style.width).toBe("0%");
    expect(document.querySelector("#progress-track").getAttribute("aria-valuenow")).toBe("0");
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);
    expect(deckAudio.currentTime).toBe(10);
  });

  it("does not apply a shuffle response completed after confirmed expiry", async () => {
    let resolveShuffle;
    const { dialog, updateMediaSession } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onRequest: (url) => {
        if (url === "/api/random-track") {
          return new Promise((resolve) => { resolveShuffle = resolve; });
        }
        if (url === "/api/pause") {
          return jsonResponse({ detail: "Authentication required" }, 401);
        }
        return jsonResponse({ ok: true });
      },
    });

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(resolveShuffle).toEqual(expect.any(Function)));
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    resolveShuffle(jsonResponse({
      browser_playback: false,
      current_track: { path: "late.mp3", title: "Late secret" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#now-playing-announce").textContent)
      .not.toContain("Late secret");
    expect(updateMediaSession).toHaveBeenLastCalledWith({ current_track: null });
  });

  it("does not apply a delayed shuffle response after newer WebSocket state", async () => {
    let resolveShuffle;
    const {
      bumpLinerTrackCount,
      getLinerTrackCountForTest,
      webSocket,
    } = await setupApp({
      initialState: {
        current_track: { path: "a.mp3", title: "Track A" },
      },
      onRequest: (url) => url === "/api/random-track"
        ? new Promise((resolve) => { resolveShuffle = resolve; })
        : jsonResponse({ ok: true }),
    });
    const stateC = {
      browser_playback: false,
      current_track: { path: "c.mp3", title: "Track C" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(resolveShuffle).toEqual(expect.any(Function)));
    webSocket.onmessage({ data: JSON.stringify(stateC) });
    resolveShuffle(jsonResponse({
      ...stateC,
      current_track: { path: "late-b.mp3", title: "Late Track B" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#now-playing-announce").textContent)
      .toContain("Track C");
    expect(document.querySelector("#now-playing-announce").textContent)
      .not.toContain("Late Track B");
    webSocket.onmessage({ data: JSON.stringify(stateC) });
    expect(getLinerTrackCountForTest()).toBe(1);
    expect(bumpLinerTrackCount.mock.calls.map(
      ([state]) => state.current_track?.path ?? null,
    )).toEqual(["a.mp3", "c.mp3", "c.mp3"]);
  });

  it("applies an ordinary shuffle response when no newer state arrived", async () => {
    const shuffledState = {
      browser_playback: false,
      current_track: { path: "b.mp3", title: "Track B" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };
    const {
      bumpLinerTrackCount,
      getLinerTrackCountForTest,
    } = await setupApp({
      initialState: {
        current_track: { path: "a.mp3", title: "Track A" },
      },
      onRequest: (url) => url === "/api/random-track"
        ? jsonResponse(shuffledState)
        : jsonResponse({ ok: true }),
    });

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(document.querySelector(
      "#now-playing-announce",
    ).textContent).toContain("Track B"));

    expect(getLinerTrackCountForTest()).toBe(1);
    expect(bumpLinerTrackCount).toHaveBeenLastCalledWith(shuffledState);
  });
});

describe("stream mode", () => {
  const streamState = {
    browser_playback: false,
    current_track: { path: "a.mp3", artist: "Artist", title: "Song", bpm: 128 },
    duration: 200,
    elapsed: 50,
    stream_event: { id: "boot", seq: 1, name: "set_started" },
    stream_listeners: 1,
    stream_mode: true,
    stream_state: "playing",
  };
  const lyricsFor = (path) => jsonResponse({
    path,
    lyrics: [
      { time_s: 0, text: "One" },
      { time_s: 46, text: "Two" },
      { time_s: 49, text: "Three" },
    ],
  });
  const hotkeyOptions = async () => {
    const hotkeys = await import("../../src/autodj/static/modules/hotkeys.js");
    return hotkeys.installHotkeys.mock.calls[0][0];
  };

  it("never starts the deck engine in stream mode and hides seek", async () => {
    const applyBrowserPlaybackState = vi.fn();
    // browser_playback is false in stream mode; true here proves stream wins.
    const app = await setupApp({
      audio: { applyBrowserPlaybackState },
      initialState: {
        ...streamState, browser_playback: true, current_track: null, stream_state: "idle",
      },
    });
    expect(app.unlockAndPlay).not.toHaveBeenCalled();
    expect(applyBrowserPlaybackState).not.toHaveBeenCalled();
    expect(app.setLastBrowserPlayback).toHaveBeenLastCalledWith(false);
    expect(document.getElementById("progress-track").hidden).toBe(true);
    expect(document.getElementById("btn-listen").hidden).toBe(false);
    expect(document.getElementById("stream-idle-note").hidden).toBe(false);
    // The slider is gone, so the visible clock becomes the readable one.
    expect(document.getElementById("progress-bar-label").hasAttribute("aria-hidden"))
      .toBe(false);
  });

  it("leaves browser mode unchanged", async () => {
    await setupApp();
    expect(document.getElementById("btn-listen").hidden).toBe(true);
    expect(document.getElementById("stream-idle-note").hidden).toBe(true);
    expect(document.getElementById("progress-track").hidden).toBe(false);
    expect(document.getElementById("progress-bar-label").getAttribute("aria-hidden"))
      .toBe("true");
  });

  it("shows the server clock minus the stream delay, lyrics included", async () => {
    const { webSocket } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    await vi.waitFor(() => expect(
      document.querySelectorAll("#lyrics-list li"),
    ).toHaveLength(3));
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, has_lyrics: true, lyric_index: 2, lyric_text: "Three",
    }) });
    expect(document.getElementById("progress-bar-label").textContent).toBe("0:47 / 3:20");
    expect(document.getElementById("lyric-announce").textContent).toBe("Two");
  });

  it("starts and stops listening from the button with a fresh link", async () => {
    const { fetchImpl } = await setupApp({
      initialState: streamState,
      onRequest: (url) => url === "/api/stream"
        ? jsonResponse({ path: "/stream/SECRET.mp3", m3u_path: "/stream/SECRET.m3u" })
        : lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");

    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());
    expect(audio.getAttribute("src")).toBe("/stream/SECRET.mp3");
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/stream")).toHaveLength(1);

    listen.click();
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("refuses seeking and answers status keys from the server clock", async () => {
    const { fetchImpl } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const options = await hotkeyOptions();

    options.seekDelta(5);
    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toBe("Seeking is not available while streaming."));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);

    expect(options.getBpm()).toBe(128);
    expect(options.getRemaining()).toBe(153);
  });

  it("stops listening when the session expires", async () => {
    let expire = false;
    await setupApp({
      initialState: streamState,
      onRequest: (url) => {
        if (expire) return jsonResponse({ detail: "Authentication required" }, 401);
        return url === "/api/stream"
          ? jsonResponse({ path: "/stream/SECRET.mp3" })
          : lyricsFor("a.mp3");
      },
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());

    expire = true;
    document.getElementById("btn-skip").click();
    await vi.waitFor(() => expect(document.getElementById("conn-status").textContent)
      .toBe("Authentication required"));
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(audio.getAttribute("src")).toBeNull();
  });
});
