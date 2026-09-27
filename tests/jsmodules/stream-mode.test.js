import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createStreamMode,
  STREAM_DELAY_ESTIMATE_S,
} from "../../src/autodj/static/modules/stream-mode.js";

const IDLE_TEXT =
  "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";

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
    doc: document,
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

  it("reports a failed start once and releases the toggle", async () => {
    const { mode, audio, button, sr } = setup();
    audio.play = vi.fn(() => Promise.reject(new Error("Autoplay blocked")));
    mode.apply({ stream_mode: true, stream_state: "idle" });
    button.focus();
    await mode.toggleListen();
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe("Could not start listening: Autoplay blocked");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("reports a failed link lookup the same way", async () => {
    const fetchInfo = vi.fn(async () => { throw new Error("Request failed (409)"); });
    const { mode, button, sr } = setup({ fetchInfo });
    mode.apply({ stream_mode: true, stream_state: "idle" });
    await mode.toggleListen();
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe("Could not start listening: Request failed (409)");
  });

  it("speaks the new state only when the button itself is not focused", async () => {
    const { mode, button, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });

    button.focus();
    await mode.toggleListen();
    await tick();
    expect(sr.textContent).toBe("");
    await mode.toggleListen();
    await tick();
    expect(sr.textContent).toBe("");

    document.getElementById("elsewhere").focus();
    await mode.toggleListen();
    await tick();
    expect(sr.textContent).toBe("Listen here on.");
    await mode.toggleListen();
    await tick();
    expect(sr.textContent).toBe("Listen here off.");
  });

  it("reconnects once after a dropped stream, then gives up and says so", async () => {
    const { mode, audio, button, fetchInfo, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    button.focus();
    await mode.toggleListen();
    expect(fetchInfo).toHaveBeenCalledTimes(1);

    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(sr.textContent).toBe("");

    audio.dispatchEvent(new Event("ended"));
    await tick();
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(sr.textContent).toBe("Listening stopped: the stream connection was lost.");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("allows a fresh reconnect after the stream played again", async () => {
    const { mode, audio, fetchInfo, button } = setup();
    mode.apply({ stream_mode: true, stream_state: "playing" });
    await mode.toggleListen();
    audio.dispatchEvent(new Event("error"));
    await tick();
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).toHaveBeenCalledTimes(3);
    expect(button.getAttribute("aria-pressed")).toBe("true");
  });

  it("ignores media errors while not listening", async () => {
    const { mode, audio, fetchInfo, sr } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    audio.dispatchEvent(new Event("error"));
    await tick();
    expect(fetchInfo).not.toHaveBeenCalled();
    expect(sr.textContent).toBe("");
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
