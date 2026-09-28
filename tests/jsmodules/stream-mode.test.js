import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createStreamMode,
  createStreamSettings,
  ADDRESS_RETRY_MS,
  BITRATE_SETTLE_MS,
  LINK_CLAIM_TIMEOUT_MS,
  NOT_LOADED_NOTE,
  STREAM_DELAY_ESTIMATE_S,
} from "../../src/autodj/static/modules/stream-mode.js";

// happy-dom has no MediaError; the codes are fixed by the HTML standard.
globalThis.MediaError ??= {
  MEDIA_ERR_ABORTED: 1,
  MEDIA_ERR_NETWORK: 2,
  MEDIA_ERR_DECODE: 3,
  MEDIA_ERR_SRC_NOT_SUPPORTED: 4,
};

const IDLE_TEXT =
  "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";
const NOT_LOADED = "The stream did not load. It may be full or not running.";
const DROPPED = "The stream connection dropped.";
const STALLED = "The stream stopped responding.";

function mediaError(audio, code) {
  Object.defineProperty(audio, "error", { configurable: true, value: { code } });
}

function setup({ fetchInfo, beforePlay = vi.fn() } = {}) {
  document.body.innerHTML = `
    <audio id="stream-audio" hidden></audio>
    <button id="btn-listen" type="button" aria-pressed="false" hidden><span aria-hidden="true">🔊</span> Listen here</button>
    <p id="stream-idle-note" hidden></p>
    <button id="elsewhere" type="button">Elsewhere</button>
    <div id="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
  const audio = document.getElementById("stream-audio");
  audio.play = vi.fn(() => Promise.resolve());
  audio.pause = vi.fn();
  audio.load = vi.fn();
  const info = fetchInfo || vi.fn(async () => ({ path: "/stream/SECRET.mp3" }));
  const button = document.getElementById("btn-listen");
  const mode = createStreamMode({
    audio,
    button,
    idleNote: document.getElementById("stream-idle-note"),
    srStatus: document.getElementById("sr-status"),
    fetchInfo: info,
    beforePlay,
  });
  return { mode, audio, button, fetchInfo: info, sr: document.getElementById("sr-status") };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 10));

describe("stream mode", () => {
  beforeEach(() => vi.useRealTimers());

  it("stays hidden in browser mode", () => {
    setup().mode.apply({ stream_mode: false });
    expect(document.getElementById("btn-listen").hidden).toBe(true);
    expect(document.getElementById("stream-idle-note").hidden).toBe(true);
  });

  it("shows Listen here and the idle note when idle", () => {
    const { mode } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    const note = document.getElementById("stream-idle-note");
    expect(document.getElementById("btn-listen").hidden).toBe(false);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe(IDLE_TEXT);
    mode.apply({ stream_mode: true, stream_state: "playing" });
    expect(note.hidden).toBe(true);
    expect(document.getElementById("btn-listen").hidden).toBe(false);
  });

  it("leaves the button and note untouched on a repeated push", async () => {
    const { mode, button } = setup();
    const note = document.getElementById("stream-idle-note");
    const state = { stream_mode: true, stream_state: "idle", stream_listeners: 0 };
    mode.apply(state);
    const records = [];
    const observer = new window.MutationObserver((batch) => records.push(...batch));
    for (const node of [button, note]) {
      observer.observe(node, {
        attributes: true, childList: true, characterData: true, subtree: true,
      });
    }
    mode.apply(state);
    mode.apply({ ...state, stream_listeners: 3 });
    await Promise.resolve();
    expect(records).toHaveLength(0);
    observer.disconnect();
  });

  it("toggles listening with a fixed name and aria-pressed", async () => {
    const { mode, audio, fetchInfo, button } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    await mode.toggleListen();
    expect(fetchInfo).toHaveBeenCalledOnce();
    expect(audio.src).toContain("/stream/SECRET.mp3");
    expect(audio.play).toHaveBeenCalled();
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.textContent.trim()).toContain("Listen here");
    expect(mode.isListening()).toBe(true);
    await mode.toggleListen();
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.getAttribute("src")).toBeNull();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.textContent.trim()).toContain("Listen here");
    expect(mode.isListening()).toBe(false);
  });

  it("sets the page level before every play, the silent retry included", async () => {
    const order = [];
    const beforePlay = vi.fn(() => order.push("level"));
    const { mode, audio } = setup({ beforePlay });
    audio.play = vi.fn(() => {
      order.push("play");
      return Promise.resolve();
    });
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    audio.dispatchEvent(new Event("playing"));
    mediaError(audio, 2);
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(order).toEqual(["level", "play", "level", "play"]);
  });

  it("marks the button pressed at once and fetches a fresh link each time", async () => {
    let resolveInfo;
    const fetchInfo = vi.fn(() => new Promise((resolve) => { resolveInfo = resolve; }));
    const { mode, audio, button } = setup({ fetchInfo });
    mode.apply({ stream_mode: true, stream_state: "idle" });
    const pending = mode.toggleListen();
    expect(button.getAttribute("aria-pressed")).toBe("true");
    resolveInfo({ path: "/stream/ONE.mp3" });
    await pending;
    await mode.toggleListen();
    const again = mode.toggleListen();
    resolveInfo({ path: "/stream/TWO.mp3" });
    await again;
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(audio.src).toContain("/stream/TWO.mp3");
  });

  it("cancels a start that is still fetching when pressed again", async () => {
    let resolveInfo;
    const fetchInfo = vi.fn(() => new Promise((resolve) => { resolveInfo = resolve; }));
    const { mode, audio, button } = setup({ fetchInfo });
    mode.apply({ stream_mode: true, stream_state: "idle" });
    const first = mode.toggleListen();
    await mode.toggleListen();
    resolveInfo({ path: "/stream/LATE.mp3" });
    await first;
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(audio.play).not.toHaveBeenCalled();
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("reports a blocked start in plain words and releases the toggle", async () => {
    const { mode, audio, button, sr } = setup();
    audio.play = vi.fn(() => Promise.reject(new globalThis.DOMException(
      "play() failed because the user did not interact with the document first.",
      "NotAllowedError",
    )));
    mode.apply({ stream_mode: true, stream_state: "idle" });
    button.focus();
    await mode.toggleListen();
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe("The browser blocked playback. Press Listen here again.");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("never speaks raw browser error text", async () => {
    const cases = [
      [new globalThis.DOMException("The element has no supported sources.", "NotSupportedError"), NOT_LOADED],
      [new globalThis.DOMException("Some internal detail", "EncodingError"), NOT_LOADED],
      [new TypeError("NetworkError when attempting to fetch resource."), DROPPED],
    ];
    for (const [failure, expected] of cases) {
      const { mode, audio, sr } = setup();
      audio.play = vi.fn(() => Promise.reject(failure));
      mode.apply({ stream_mode: true, stream_state: "idle" });
      await mode.toggleListen();
      await tick();
      expect(sr.textContent).toBe(expected);
    }
  });

  it("reports a refused link lookup with the server's reason", async () => {
    const refusal = Object.assign(new Error("Stream mode is not enabled"), { status: 409 });
    const fetchInfo = vi.fn(async () => { throw refusal; });
    const { mode, button, sr } = setup({ fetchInfo });
    mode.apply({ stream_mode: true, stream_state: "idle" });
    await mode.toggleListen();
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe("Could not get the stream link: Stream mode is not enabled");
  });

  it("never speaks the listen state: aria-pressed carries it", async () => {
    const { mode, button, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    for (const focus of [button, document.getElementById("elsewhere")]) {
      focus.focus();
      await mode.toggleListen();
      await tick();
      expect(button.getAttribute("aria-pressed")).toBe("true");
      expect(sr.textContent).toBe("");
      await mode.toggleListen();
      await tick();
      expect(button.getAttribute("aria-pressed")).toBe("false");
      expect(sr.textContent).toBe("");
    }
  });

  it("reconnects once after a dropped stream, then gives up and says so", async () => {
    const { mode, audio, button, fetchInfo, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    button.focus();
    await mode.toggleListen();
    expect(fetchInfo).toHaveBeenCalledTimes(1);
    audio.dispatchEvent(new Event("playing"));

    mediaError(audio, 2);
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(sr.textContent).toBe("");

    audio.dispatchEvent(new Event("ended"));
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe(DROPPED);
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("reports a failed reconnect as a dropped connection", async () => {
    let calls = 0;
    const fetchInfo = vi.fn(async () => {
      calls += 1;
      if (calls > 1) throw new TypeError("Failed to fetch");
      return { path: "/stream/SECRET.mp3" };
    });
    const { mode, audio, button, sr } = setup({ fetchInfo });
    mode.apply({ stream_mode: true, stream_state: "playing" });
    button.focus();
    await mode.toggleListen();
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe(DROPPED);
  });

  it("allows a fresh reconnect after the stream played again", async () => {
    const { mode, audio, fetchInfo, button } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));
    await tick();
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).toHaveBeenCalledTimes(3);
    expect(button.getAttribute("aria-pressed")).toBe("true");
  });

  it("maps a stream that never played by its media error code, without retrying", async () => {
    for (const [code, expected] of [
      [4, NOT_LOADED], [3, NOT_LOADED], [2, DROPPED], [null, NOT_LOADED],
    ]) {
      const { mode, audio, button, fetchInfo, sr } = setup();
      mode.apply({ stream_mode: true, stream_state: "idle" });
      button.focus();
      await mode.toggleListen();
      if (code) mediaError(audio, code);
      audio.dispatchEvent(new Event("error"));
      await tick();
      expect(fetchInfo).toHaveBeenCalledOnce();
      expect(button.getAttribute("aria-pressed")).toBe("false");
      expect(sr.textContent).toBe(expected);
    }
  });

  it("ignores media errors while not listening", async () => {
    const { mode, audio, fetchInfo, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).not.toHaveBeenCalled();
    expect(sr.textContent).toBe("");
  });

  describe("stalled connection", () => {
    afterEach(() => vi.useRealTimers());

    it("stops and says so once when a stall lasts 15 seconds", async () => {
      vi.useFakeTimers();
      const { mode, audio, button, sr } = setup();
      const say = vi.spyOn(sr, "textContent", "set");
      mode.apply({ stream_mode: true, stream_state: "playing" });
      await mode.toggleListen();
      audio.dispatchEvent(new Event("playing"));
      audio.dispatchEvent(new Event("stalled"));
      await vi.advanceTimersByTimeAsync(10000);
      // A second stall signal does not restart or double the countdown.
      audio.dispatchEvent(new Event("waiting"));
      await vi.advanceTimersByTimeAsync(4999);
      expect(button.getAttribute("aria-pressed")).toBe("true");
      await vi.advanceTimersByTimeAsync(1);
      expect(button.getAttribute("aria-pressed")).toBe("false");
      expect(audio.getAttribute("src")).toBeNull();
      await vi.advanceTimersByTimeAsync(1);
      expect(sr.textContent).toBe(STALLED);

      audio.dispatchEvent(new Event("waiting"));
      await vi.advanceTimersByTimeAsync(20000);
      expect(say.mock.calls.filter(([text]) => text === STALLED)).toHaveLength(1);
    });

    it("keeps listening when audio plays again within 15 seconds", async () => {
      vi.useFakeTimers();
      const { mode, audio, button, sr } = setup();
      mode.apply({ stream_mode: true, stream_state: "playing" });
      await mode.toggleListen();
      audio.dispatchEvent(new Event("waiting"));
      await vi.advanceTimersByTimeAsync(14000);
      audio.dispatchEvent(new Event("playing"));
      await vi.advanceTimersByTimeAsync(20000);
      expect(button.getAttribute("aria-pressed")).toBe("true");
      expect(sr.textContent).toBe("");
    });

    it("forgets a pending stall when the user stops listening", async () => {
      vi.useFakeTimers();
      const { mode, audio, sr } = setup();
      mode.apply({ stream_mode: true, stream_state: "playing" });
      await mode.toggleListen();
      audio.dispatchEvent(new Event("stalled"));
      await mode.toggleListen();
      await vi.advanceTimersByTimeAsync(20000);
      expect(sr.textContent).toBe("");
    });
  });

  it("announces each station event once", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: { id: "a", seq: 0, name: "set_stopped" } });
    await tick();
    expect(sr.textContent).toBe("");
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("New set started.");
    sr.textContent = "";
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("");
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: { id: "a", seq: 2, name: "set_stopped" } });
    await tick();
    expect(sr.textContent).toBe("Set stopped. Nobody is listening.");
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: { id: "a", seq: 3, name: "link_changed" } });
    await tick();
    expect(sr.textContent).toBe("Stream link changed. Speakers using the old link have stopped.");
  });

  it("treats a restarted server's first event as new", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_event: { id: "old", seq: 4, name: "set_started" } });
    mode.apply({ stream_mode: true, stream_event: { id: "new", seq: 4, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("New set started.");
  });

  it("announces the first event when the page loaded before any event", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: null });
    mode.apply({ stream_mode: true, stream_state: "idle" });
    await tick();
    expect(sr.textContent).toBe("");
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("New set started.");
    sr.textContent = "";
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: null });
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("");
  });

  it("stops listening when the server leaves stream mode", async () => {
    const { mode, audio, button } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    mode.apply({ stream_mode: false });
    expect(audio.pause).toHaveBeenCalled();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.hidden).toBe(true);
  });

  it("stop() releases the stream for session teardown", async () => {
    const { mode, audio, button } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    mode.stop();
    expect(audio.getAttribute("src")).toBeNull();
    expect(button.getAttribute("aria-pressed")).toBe("false");
  });

  it("leaves the audio element alone on pushes while not listening", () => {
    const { mode, audio } = setup();
    mode.apply({ stream_mode: false });
    mode.apply({ stream_mode: false });
    mode.stop();
    expect(audio.pause).not.toHaveBeenCalled();
    expect(audio.load).not.toHaveBeenCalled();
  });

  it("uses the estimate unless listening", () => {
    const { mode } = setup();
    expect(STREAM_DELAY_ESTIMATE_S).toBe(3);
    expect(mode.delaySeconds({ elapsed: 50 })).toBe(STREAM_DELAY_ESTIMATE_S);
  });

  it("measures the delay from the audio buffer while listening", async () => {
    const { mode, audio } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    const buffered = (end) => ({ length: 1, end: () => end });
    Object.defineProperty(audio, "currentTime", { configurable: true, value: 10 });
    Object.defineProperty(audio, "buffered", { configurable: true, value: buffered(11.5) });
    expect(mode.delaySeconds()).toBeCloseTo(1.5);
    Object.defineProperty(audio, "buffered", { configurable: true, value: buffered(100) });
    expect(mode.delaySeconds()).toBe(STREAM_DELAY_ESTIMATE_S);
    Object.defineProperty(audio, "buffered", { configurable: true, value: { length: 0 } });
    expect(mode.delaySeconds()).toBe(STREAM_DELAY_ESTIMATE_S);
  });
});

describe("own link change", () => {
  it("skips the link_changed event this page claimed, once", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "set_started" } });
    mode.claimLinkChange();
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 2, name: "link_changed" } });
    await tick();
    expect(sr.textContent).toBe("");
    // A later change made on another page is spoken as usual.
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 3, name: "link_changed" } });
    await tick();
    expect(sr.textContent).toBe("Stream link changed. Speakers using the old link have stopped.");
  });

  it("does not let a claim swallow other station events", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_event: null });
    mode.claimLinkChange();
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(sr.textContent).toBe("New set started.");
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 2, name: "link_changed" } });
    await tick();
    expect(sr.textContent).toBe("New set started.");
  });

  it("releases a claim whose link change never happened", async () => {
    const { mode, sr } = setup();
    mode.apply({ stream_mode: true, stream_event: null });
    const release = mode.claimLinkChange();
    release();
    mode.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "link_changed" } });
    await tick();
    expect(sr.textContent).toBe("Stream link changed. Speakers using the old link have stopped.");
  });
});

const INFO = { path: "/stream/S.mp3", m3u_path: "/stream/S.m3u", bitrate: 320, listeners: 0, state: "idle" };
const NEW_INFO = { path: "/stream/NEW.mp3", m3u_path: "/stream/NEW.m3u", bitrate: 320, listeners: 0, state: "idle" };
const MADE_TEXT = "New stream link made. The old link no longer works.";
const COPY_FAIL = "Could not copy. Select the address and copy it yourself.";

function settingsDom() {
  document.body.innerHTML = `
    <details class="card" open><summary><h2>Settings</h2></summary>
    <div id="settings-status" role="status" aria-live="polite" aria-atomic="true"></div>
    <fieldset id="stream-settings" hidden>
      <legend>Stream</legend>
      <label for="stream-url">Stream address</label>
      <input id="stream-url" type="text" readonly aria-describedby="stream-url-desc">
      <button type="button" id="stream-copy">Copy address</button>
      <p id="stream-url-note" hidden></p>
      <p id="stream-loopback-note" hidden>This address only works on this computer.</p>
      <a id="stream-m3u" download="autodj.m3u">Download playlist file (.m3u)</a>
      <label for="stream-bitrate">Quality</label>
      <select id="stream-bitrate"><option value="128">128 kbps</option><option value="192">192 kbps</option><option value="256">256 kbps</option><option value="320">320 kbps</option></select>
      <button type="button" id="stream-rotate">Make new link</button>
      <p id="stream-listeners"></p>
    </fieldset>
    </details>
    <dialog id="stream-rotate-dialog" aria-labelledby="stream-rotate-title"><form method="dialog"><h2 id="stream-rotate-title">Make a new stream link?</h2><button value="cancel">Cancel</button><button value="confirm">Make new link</button></form></dialog>
    <div id="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
  const dialog = document.getElementById("stream-rotate-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return dialog;
}

function makeSettings(overrides = {}) {
  const opts = {
    doc: document,
    fetchInfo: vi.fn(async () => INFO),
    rotate: vi.fn(async () => NEW_INFO),
    saveBitrate: vi.fn(async () => true),
    claimLinkChange: vi.fn(() => vi.fn()),
    srStatus: document.getElementById("sr-status"),
    settingsStatus: document.getElementById("settings-status"),
    ...overrides,
  };
  return { ui: createStreamSettings(opts), opts };
}

const byId = (id) => document.getElementById(id);

function closeDialog(dialog, value) {
  dialog.returnValue = value;
  dialog.removeAttribute("open");
  dialog.dispatchEvent(new Event("close"));
}

describe("stream settings", () => {
  beforeEach(() => vi.useRealTimers());
  afterEach(() => {
    vi.useRealTimers();
    delete navigator.clipboard;
    delete document.execCommand;
  });

  it.each(["localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "127.1.2.3", "[::1]", "::1"])(
    "warns that a page opened as %s copies a link only this computer can use",
    (hostname) => {
      settingsDom();
      const note = byId("stream-loopback-note");
      makeSettings({ hostname });
      expect(note.hidden).toBe(false);
      expect(note.hasAttribute("aria-live")).toBe(false);
      expect(note.hasAttribute("role")).toBe(false);
      // Heard when tabbing to the address or Copy, not only in browse mode.
      expect(byId("stream-url").getAttribute("aria-describedby"))
        .toBe("stream-url-desc stream-loopback-note");
      expect(byId("stream-copy").getAttribute("aria-describedby")).toBe("stream-loopback-note");
    },
  );

  it.each(["192.168.1.20", "autodj.local", "10.0.0.5", "[fe80::1]", "127.example.com"])(
    "shows no loopback warning on %s",
    (hostname) => {
      settingsDom();
      makeSettings({ hostname });
      expect(byId("stream-loopback-note").hidden).toBe(true);
      expect(byId("stream-url").getAttribute("aria-describedby")).toBe("stream-url-desc");
      expect(byId("stream-copy").hasAttribute("aria-describedby")).toBe(false);
    },
  );

  it("reads the hostname from the page by default", () => {
    settingsDom();
    makeSettings();
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
    expect(byId("stream-loopback-note").hidden).toBe(!loopback);
  });

  it("stays hidden and fetches nothing outside stream mode", () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: false });
    expect(byId("stream-settings").hidden).toBe(true);
    expect(opts.fetchInfo).not.toHaveBeenCalled();
  });

  it("fills address, playlist link, quality and listener count", async () => {
    settingsDom();
    const info = { ...INFO, bitrate: 256, listeners: 2 };
    const { ui } = makeSettings({ fetchInfo: vi.fn(async () => info) });
    ui.apply({ stream_mode: true, stream_listeners: 2 });
    await ui.refresh();
    expect(byId("stream-settings").hidden).toBe(false);
    expect(byId("stream-url").value).toBe(`${location.origin}/stream/S.mp3`);
    expect(byId("stream-m3u").getAttribute("href")).toBe("/stream/S.m3u");
    expect(byId("stream-bitrate").value).toBe("256");
    expect(byId("stream-listeners").textContent).toBe("2 listeners");
  });

  it("fetches the address once on entering stream mode, not on every push", async () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledOnce();
    expect(byId("stream-url").value).toBe(`${location.origin}/stream/S.mp3`);
  });

  it("rewrites the listener count only when it changes and never makes it live", () => {
    settingsDom();
    const { ui } = makeSettings();
    const listeners = byId("stream-listeners");
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    expect(listeners.textContent).toBe("1 listener");
    const node = listeners.firstChild;
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    expect(listeners.firstChild).toBe(node);
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    expect(listeners.textContent).toBe("0 listeners");
    expect(listeners.hasAttribute("aria-live")).toBe(false);
    expect(listeners.hasAttribute("role")).toBe(false);
  });

  it("does not overwrite the quality while it has focus", async () => {
    settingsDom();
    const { ui } = makeSettings({ fetchInfo: vi.fn(async () => ({ ...INFO, bitrate: 128 })) });
    const select = byId("stream-bitrate");
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    select.focus();
    select.value = "192";
    await ui.refresh();
    expect(select.value).toBe("192");
  });

  it("refreshes the address when another page makes a new link", async () => {
    settingsDom();
    const fetchInfo = vi.fn(async () => INFO);
    const { ui } = makeSettings({ fetchInfo });
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    fetchInfo.mockResolvedValue(NEW_INFO);
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "set_started" } });
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 2, name: "set_stopped" } });
    await tick();
    expect(fetchInfo).toHaveBeenCalledOnce();
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 3, name: "link_changed" } });
    await tick();
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(byId("stream-url").value).toBe(`${location.origin}/stream/NEW.mp3`);
    expect(byId("stream-m3u").getAttribute("href")).toBe("/stream/NEW.m3u");
  });

  it("does not let a slow lookup overwrite a newer link", async () => {
    settingsDom();
    let resolveSlow;
    const fetchInfo = vi.fn(() => new Promise((resolve) => { resolveSlow = resolve; }));
    const { ui } = makeSettings({ fetchInfo });
    const dialog = byId("stream-rotate-dialog");
    ui.apply({ stream_mode: true });
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    await tick();
    resolveSlow(INFO);
    await tick();
    expect(byId("stream-url").value).toBe(`${location.origin}/stream/NEW.mp3`);
  });

  it("reports a failed address lookup once, in the settings region", async () => {
    settingsDom();
    const err = Object.assign(new Error("stream encoder failed"), { status: 503 });
    const { ui } = makeSettings({ fetchInfo: vi.fn(async () => { throw err; }) });
    ui.apply({ stream_mode: true });
    await tick();
    expect(byId("settings-status").textContent)
      .toBe("Could not load the stream address: stream encoder failed");
  });

  it("stays quiet when the lookup failed because sign-in expired", async () => {
    settingsDom();
    const err = Object.assign(new Error("Authentication required"), { name: "AuthenticationRequiredError" });
    const { ui } = makeSettings({ fetchInfo: vi.fn(async () => { throw err; }) });
    ui.apply({ stream_mode: true });
    await tick();
    expect(byId("settings-status").textContent).toBe("");
  });

  it("rotates only after confirmation and announces once", async () => {
    const dialog = settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    await ui.refresh();
    const rotateBtn = byId("stream-rotate");
    rotateBtn.focus();
    rotateBtn.click();
    expect(dialog.showModal).toHaveBeenCalled();
    closeDialog(dialog, "cancel");
    expect(opts.rotate).not.toHaveBeenCalled();
    expect(opts.claimLinkChange).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(rotateBtn);
    rotateBtn.click();
    closeDialog(dialog, "confirm");
    await tick();
    expect(opts.claimLinkChange).toHaveBeenCalledOnce();
    expect(opts.rotate).toHaveBeenCalledOnce();
    expect(byId("stream-url").value).toContain("/stream/NEW.mp3");
    expect(byId("stream-m3u").getAttribute("href")).toBe("/stream/NEW.m3u");
    expect(byId("sr-status").textContent).toBe(MADE_TEXT);
    expect(document.activeElement).toBe(rotateBtn);
  });

  it("treats Escape (no return value) as cancel", () => {
    const dialog = settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true });
    dialog.returnValue = "confirm";
    byId("stream-rotate").click();
    // Escape closes without a submitter, so returnValue keeps whatever was
    // there when the dialog opened: the click cleared it.
    dialog.removeAttribute("open");
    dialog.dispatchEvent(new Event("close"));
    expect(opts.rotate).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(byId("stream-rotate"));
  });

  it("claims the link change before asking the server", async () => {
    const dialog = settingsDom();
    const order = [];
    const { ui } = makeSettings({
      claimLinkChange: vi.fn(() => { order.push("claim"); return () => {}; }),
      rotate: vi.fn(async () => { order.push("rotate"); return NEW_INFO; }),
    });
    ui.apply({ stream_mode: true });
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    await tick();
    expect(order).toEqual(["claim", "rotate"]);
  });

  it("reports a failed rotation, releases the claim and keeps the old link", async () => {
    const dialog = settingsDom();
    const release = vi.fn();
    const { ui } = makeSettings({
      rotate: vi.fn(async () => { throw new Error("Could not save the new stream link"); }),
      claimLinkChange: vi.fn(() => release),
    });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    await tick();
    expect(release).toHaveBeenCalledOnce();
    expect(byId("stream-url").value).toContain("/stream/S.mp3");
    expect(byId("settings-status").textContent)
      .toBe("Could not make a new link: Could not save the new stream link");
    expect(byId("sr-status").textContent).toBe("");
    expect(document.activeElement).toBe(byId("stream-rotate"));
  });

  it("copies the address and reports failure", async () => {
    settingsDom();
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { ui } = makeSettings({ fetchInfo: vi.fn(async () => ({ ...INFO, listeners: 1 })) });
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    await ui.refresh();
    byId("stream-copy").click();
    await tick();
    expect(writeText).toHaveBeenCalledWith(`${location.origin}/stream/S.mp3`);
    expect(byId("sr-status").textContent).toBe("Stream address copied.");
    writeText.mockRejectedValueOnce(new Error("denied"));
    document.execCommand = vi.fn(() => false);
    byId("stream-copy").click();
    await tick();
    expect(byId("sr-status").textContent).toBe(COPY_FAIL);
    const url = byId("stream-url");
    expect(document.activeElement).toBe(url);
    expect(url.selectionStart).toBe(0);
    expect(url.selectionEnd).toBe(url.value.length);
  });

  it("says copied again on a second copy", async () => {
    settingsDom();
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn(async () => {}) }, configurable: true });
    const { ui } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    const sr = byId("sr-status");
    byId("stream-copy").click();
    await tick();
    expect(sr.textContent).toBe("Stream address copied.");
    byId("stream-copy").click();
    // Forced: the region is emptied first so NVDA hears it again.
    await Promise.resolve();
    await Promise.resolve();
    expect(sr.textContent).toBe("");
    await tick();
    expect(sr.textContent).toBe("Stream address copied.");
  });

  it("copies through the copy command without touching the address field", async () => {
    settingsDom();
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    const setData = vi.fn();
    let prevented = false;
    document.execCommand = vi.fn(() => {
      const event = new Event("copy", { cancelable: true });
      event.clipboardData = { setData };
      document.dispatchEvent(event);
      prevented = event.defaultPrevented;
      return true;
    });
    const { ui } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    const url = byId("stream-url");
    const addressFocus = vi.fn();
    url.addEventListener("focus", addressFocus);
    const copy = byId("stream-copy");
    copy.focus();
    copy.click();
    await tick();
    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(setData).toHaveBeenCalledWith("text/plain", `${location.origin}/stream/S.mp3`);
    expect(prevented).toBe(true);
    expect(byId("sr-status").textContent).toBe("Stream address copied.");
    // NVDA would read the whole secret address on focusing the field.
    expect(addressFocus).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(copy);
    // The one-off listener is gone: a later copy elsewhere is untouched.
    const later = new Event("copy", { cancelable: true });
    later.clipboardData = { setData: vi.fn() };
    document.dispatchEvent(later);
    expect(later.clipboardData.setData).not.toHaveBeenCalled();
  });

  it("asks the user to copy when every copy route fails", async () => {
    settingsDom();
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    document.execCommand = vi.fn(() => false);
    const { ui } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    byId("stream-copy").click();
    await tick();
    expect(byId("sr-status").textContent).toBe(COPY_FAIL);
    const url = byId("stream-url");
    expect(document.activeElement).toBe(url);
    expect(url.selectionEnd).toBe(url.value.length);
  });

  it("treats a copy command that never delivered the text as a failure", async () => {
    settingsDom();
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    // No copy event reaches the page (no user activation, for example).
    document.execCommand = vi.fn(() => true);
    const { ui } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    byId("stream-copy").click();
    await tick();
    expect(byId("sr-status").textContent).toBe(COPY_FAIL);
  });

  it("saves the quality once the choice settles and says 1 listener correctly", async () => {
    settingsDom();
    const { ui, opts } = makeSettings({ fetchInfo: vi.fn(async () => ({ ...INFO, listeners: 1 })) });
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    await ui.refresh();
    expect(byId("stream-listeners").textContent).toBe("1 listener");
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "192";
    select.dispatchEvent(new Event("change"));
    expect(opts.saveBitrate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(opts.saveBitrate).toHaveBeenCalledWith(192, select);
  });

  it("sends one save for quick arrow presses, with the last value", async () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    for (const value of ["256", "192", "128"]) {
      select.value = value;
      select.dispatchEvent(new Event("change"));
      await vi.advanceTimersByTimeAsync(200);
    }
    expect(opts.saveBitrate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(opts.saveBitrate).toHaveBeenCalledOnce();
    expect(opts.saveBitrate).toHaveBeenCalledWith(128, select);
  });

  it("sends nothing when the choice settles back on the saved quality", async () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    for (const value of ["256", "320"]) {
      select.value = value;
      select.dispatchEvent(new Event("change"));
    }
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS * 2);
    expect(opts.saveBitrate).not.toHaveBeenCalled();
  });

  it("follows a save in flight with exactly one save of the newest choice", async () => {
    settingsDom();
    const replies = [];
    const saveBitrate = vi.fn(() => new Promise((resolve) => replies.push(resolve)));
    const { ui } = makeSettings({ saveBitrate });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "256";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(saveBitrate).toHaveBeenCalledTimes(1);
    for (const value of ["192", "128"]) {
      select.value = value;
      select.dispatchEvent(new Event("change"));
      await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    }
    // Still one: the first save has not answered yet.
    expect(saveBitrate).toHaveBeenCalledTimes(1);
    replies[0](true);
    await vi.advanceTimersByTimeAsync(0);
    expect(saveBitrate).toHaveBeenCalledTimes(2);
    expect(saveBitrate).toHaveBeenLastCalledWith(128, select);
    replies[1](true);
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS * 2);
    expect(saveBitrate).toHaveBeenCalledTimes(2);
    expect(select.value).toBe("128");
  });

  it("skips the follow-up when the newest choice is what was just saved", async () => {
    settingsDom();
    const replies = [];
    const saveBitrate = vi.fn(() => new Promise((resolve) => replies.push(resolve)));
    const { ui } = makeSettings({ saveBitrate });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "256";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    select.value = "192";
    select.dispatchEvent(new Event("change"));
    select.value = "256";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    replies[0](true);
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(saveBitrate).toHaveBeenCalledOnce();
  });

  it("puts the quality back when saving it fails", async () => {
    settingsDom();
    const { ui } = makeSettings({ saveBitrate: vi.fn(async () => false) });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "128";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(select.value).toBe("320");
  });

  it("keeps a saved quality when a later save fails", async () => {
    settingsDom();
    const saveBitrate = vi.fn(async () => true);
    const { ui } = makeSettings({ saveBitrate });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "192";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    saveBitrate.mockResolvedValueOnce(false);
    select.value = "128";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(select.value).toBe("192");
  });

  it("drops an unsent quality change when stream mode ends", async () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "128";
    select.dispatchEvent(new Event("change"));
    ui.apply({ stream_mode: false });
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(opts.saveBitrate).not.toHaveBeenCalled();
  });

  it("follows the quality the server pushes", async () => {
    settingsDom();
    const { ui } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    const select = byId("stream-bitrate");
    const pushed = (value) => ({
      stream_mode: true, settings: { playback: { stream_bitrate: value } },
    });
    ui.apply(pushed(192));
    expect(select.value).toBe("192");
    // Never while it has focus...
    select.focus();
    ui.apply(pushed(256));
    expect(select.value).toBe("192");
    select.blur();
    ui.apply(pushed(256));
    expect(select.value).toBe("256");
    // ...nor while a change of it is waiting to be saved.
    vi.useFakeTimers();
    select.value = "128";
    select.dispatchEvent(new Event("change"));
    ui.apply(pushed(256));
    expect(select.value).toBe("128");
    // A push without the key leaves it alone.
    ui.apply({ stream_mode: true, settings: null });
    expect(select.value).toBe("128");
  });

  it("treats a pushed quality as the value to undo a failed save to", async () => {
    settingsDom();
    const { ui } = makeSettings({ saveBitrate: vi.fn(async () => false) });
    ui.apply({ stream_mode: true });
    await ui.refresh();
    ui.apply({ stream_mode: true, settings: { playback: { stream_bitrate: 192 } } });
    vi.useFakeTimers();
    const select = byId("stream-bitrate");
    select.value = "128";
    select.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(BITRATE_SETTLE_MS);
    expect(select.value).toBe("192");
  });

  it("shows a plain note and retries while the address is missing", async () => {
    settingsDom();
    vi.useFakeTimers();
    const err = Object.assign(new Error("stream encoder failed"), { status: 503 });
    const fetchInfo = vi.fn(async () => { throw err; });
    const { ui } = makeSettings({ fetchInfo });
    const note = byId("stream-url-note");
    ui.apply({ stream_mode: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe(NOT_LOADED_NOTE);
    expect(note.hasAttribute("aria-live")).toBe(false);
    expect(byId("settings-status").textContent)
      .toBe("Could not load the stream address: stream encoder failed");
    // Pushes inside the ten seconds do not look again.
    await vi.advanceTimersByTimeAsync(ADDRESS_RETRY_MS - 1000);
    ui.apply({ stream_mode: true });
    expect(fetchInfo).toHaveBeenCalledOnce();
    // After ten seconds a push looks again; a second failure is silent.
    await vi.advanceTimersByTimeAsync(1000);
    ui.apply({ stream_mode: true });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(byId("settings-status").textContent).toBe("");
    // Opening the Settings tab looks again at once.
    fetchInfo.mockResolvedValue(INFO);
    ui.retryIfMissing();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchInfo).toHaveBeenCalledTimes(3);
    expect(byId("stream-url").value).toBe(`${location.origin}/stream/S.mp3`);
    expect(note.hidden).toBe(true);
    // Loaded: no more lookups on pushes or tab opens.
    await vi.advanceTimersByTimeAsync(ADDRESS_RETRY_MS * 2);
    ui.apply({ stream_mode: true });
    ui.retryIfMissing();
    expect(fetchInfo).toHaveBeenCalledTimes(3);
  });

  it("does not look up the link again after its own rotation", async () => {
    const dialog = settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "set_started" } });
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledOnce();
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    await tick();
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 2, name: "link_changed" } });
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledOnce();
    expect(byId("stream-url").value).toContain("/stream/NEW.mp3");
    // A later change from another page is looked up as usual.
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 3, name: "link_changed" } });
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledTimes(2);
  });

  it("skips the lookup when the event beats the rotate reply", async () => {
    const dialog = settingsDom();
    let reply;
    const { ui, opts } = makeSettings({
      rotate: vi.fn(() => new Promise((resolve) => { reply = resolve; })),
    });
    ui.apply({ stream_mode: true, stream_event: null });
    await tick();
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "link_changed" } });
    reply(NEW_INFO);
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledOnce();
    expect(byId("stream-url").value).toContain("/stream/NEW.mp3");
  });

  it("gives up the link-change claim ten seconds after the rotate reply", async () => {
    const dialog = settingsDom();
    const release = vi.fn();
    const { ui, opts } = makeSettings({ claimLinkChange: vi.fn(() => release) });
    ui.apply({ stream_mode: true, stream_event: null });
    await tick();
    vi.useFakeTimers();
    byId("stream-rotate").click();
    closeDialog(dialog, "confirm");
    await vi.advanceTimersByTimeAsync(LINK_CLAIM_TIMEOUT_MS - 1);
    expect(release).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(release).toHaveBeenCalledOnce();
    // The expired claim no longer stops another page's change being fetched.
    ui.apply({ stream_mode: true, stream_event: { id: "a", seq: 1, name: "link_changed" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(opts.fetchInfo).toHaveBeenCalledTimes(2);
  });

  it("hides the section and moves focus out when stream mode ends", async () => {
    const dialog = settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true });
    await ui.refresh();
    byId("stream-rotate").focus();
    byId("stream-rotate").click();
    ui.apply({ stream_mode: false });
    expect(byId("stream-settings").hidden).toBe(true);
    expect(dialog.hasAttribute("open")).toBe(false);
    expect(opts.rotate).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(document.querySelector("summary"));
  });

  it("reset() wipes the secret address for sign-out", async () => {
    settingsDom();
    const { ui, opts } = makeSettings();
    ui.apply({ stream_mode: true, stream_listeners: 3 });
    await ui.refresh();
    ui.reset();
    expect(byId("stream-url").value).toBe("");
    expect(byId("stream-m3u").hasAttribute("href")).toBe(false);
    expect(byId("stream-listeners").textContent).toBe("");
    expect(byId("stream-settings").hidden).toBe(true);
    // The next stream-mode push after sign-in fetches the link again.
    ui.apply({ stream_mode: true });
    await tick();
    expect(opts.fetchInfo).toHaveBeenCalledTimes(3);
  });
});
