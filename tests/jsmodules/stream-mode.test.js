import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createStreamMode,
  STREAM_DELAY_ESTIMATE_S,
} from "../../src/autodj/static/modules/stream-mode.js";

const IDLE_TEXT =
  "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";
const NOT_LOADED = "The stream did not load. It may be full or not running.";
const DROPPED = "The stream connection dropped.";
const STALLED = "The stream stopped responding.";

function mediaError(audio, code) {
  Object.defineProperty(audio, "error", { configurable: true, value: { code } });
}

function setup({ fetchInfo } = {}) {
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
