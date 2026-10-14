import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LATCHED_NOTE,
  TALK_INSECURE_TEXT,
  installLiveDesk,
  liveLine,
  talkCloseProblem,
  talkMicrophoneProblem,
} from "../../src/autodj/static/modules/live-talk.js";

const { EventTarget, MouseEvent } = globalThis;
const byId = (id) => document.getElementById(id);

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

// A microphone, an audio context with a worklet and a socket, all fake.
function fakes() {
  const log = { stopped: 0, closedContexts: 0, sockets: [], sent: [], nodes: [], destinations: 0 };
  const track = { stop: () => { log.stopped += 1; } };
  const media = { getUserMedia: vi.fn(async () => ({ getTracks: () => [track] })) };
  class Context {
    constructor() {
      this.sampleRate = 48000;
      this.destination = {};
      this.audioWorklet = { addModule: vi.fn(async () => {}) };
    }

    resume() { return Promise.resolve(); }

    createMediaStreamSource() { return { connect: vi.fn(), disconnect: vi.fn() }; }

    close() { log.closedContexts += 1; return Promise.resolve(); }
  }
  class Node {
    constructor(_context, name, options) {
      this.name = name;
      this.options = options;
      this.port = { onmessage: null, close: vi.fn() };
      this.disconnect = vi.fn();
      log.nodes.push(this);
    }
  }
  class Socket extends EventTarget {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.bufferedAmount = 0;
      this.closedWith = null;
      log.sockets.push(this);
    }

    open() {
      this.readyState = 1;
      this.dispatchEvent(new Event("open"));
    }

    send(data) { log.sent.push(data); }

    close(code) {
      this.closedWith = code;
      this.readyState = 3;
    }

    serverClose(code) {
      this.readyState = 3;
      const event = new Event("close");
      event.code = code;
      this.dispatchEvent(event);
    }
  }
  window.AudioWorkletNode = Node;
  return { log, media, Context, Socket };
}

let clockNow = 0;

function install(extra = {}) {
  installIndex();
  const fake = fakes();
  const desk = installLiveDesk({
    doc: document,
    media: () => fake.media,
    AudioContextClass: () => fake.Context,
    WebSocketClass: () => fake.Socket,
    location: { protocol: "https:", host: "radio.example:8443" },
    now: () => clockNow,
    eventTime: () => clockNow,
    locale: "en-US",
    ...extra,
  });
  desk.setInfo({ available: true, live: null, talkover: false });
  return { desk, ...fake };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

async function opened(log) {
  await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
  log.sockets[0].open();
  return log.sockets[0];
}

function key(type, init, target = byId("btn-talk")) {
  const event = new KeyboardEvent(type, { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

afterEach(() => {
  clockNow = 0;
  document.body.replaceChildren();
  delete window.AudioWorkletNode;
});

describe("live line", () => {
  it("names the DJ and the local start time, and nothing for automation", () => {
    expect(liveLine({ dj: "DJ Alice", since: "2026-10-13T20:15:00" }, "en-US")).toBe("Live: DJ Alice since 8:15 PM");
    expect(liveLine({ dj: "DJ Alice", since: "not a time" })).toBe("Live: DJ Alice");
    expect(liveLine(null)).toBe("");
    expect(liveLine({ dj: "" })).toBe("");
  });

  it("is said once when a DJ goes live and once when they leave, never on a switch", async () => {
    vi.useFakeTimers();
    try {
      const { desk } = install();
      const line = byId("live-on-air");
      const status = byId("sr-status");
      // The first state after install or a switch is shown in silence.
      desk.clear();
      desk.setInfo({ available: true, live: { dj: "DJ Alice", since: "2026-10-13T20:15:00" }, talkover: false });
      vi.runAllTimers();
      expect(line.hidden).toBe(false);
      expect(line.textContent).toBe("Live: DJ Alice since 8:15 PM");
      expect(status.textContent).toBe("");
      expect(line.closest("[aria-live], [role=status]")).toBeNull();
      // The same DJ on every push: nothing more.
      for (let i = 0; i < 3; i += 1) desk.applyState({ live: { dj: "DJ Alice", since: "2026-10-13T20:15:00" } });
      vi.runAllTimers();
      expect(status.textContent).toBe("");
      desk.applyState({ live: null, talkover: false });
      vi.advanceTimersByTime(1);
      expect(status.textContent).toBe("The live DJ has left. Automation is back on the air.");
      expect(line.hidden).toBe(true);
      vi.runAllTimers();
      desk.applyState({ live: { dj: "DJ Bob", since: "2026-10-13T21:00:00" } });
      vi.advanceTimersByTime(1);
      expect(status.textContent).toBe("Live: DJ Bob since 9:00 PM.");
      // A push without the key (an older server) changes nothing.
      vi.runAllTimers();
      desk.applyState({ current_track: null });
      expect(line.textContent).toBe("Live: DJ Bob since 9:00 PM");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("talk", () => {
  it("is hidden while the station cannot go live", () => {
    const { desk } = install();
    expect(byId("talk-desk").hidden).toBe(false);
    desk.setInfo({ available: false, reason: "Live DJ needs stream mode." });
    expect(byId("talk-desk").hidden).toBe(true);
    desk.setInfo(null);
    expect(byId("talk-desk").hidden).toBe(true);
  });

  it("asks for the microphone only on a press, talks while held and lets everything go", async () => {
    const { log, media } = install();
    const button = byId("btn-talk");
    expect(media.getUserMedia).not.toHaveBeenCalled();
    button.focus();
    clockNow = 1000;
    const down = key("keydown", { key: " " });
    expect(down.defaultPrevented).toBe(true);
    // Auto-repeat while held asks for nothing more.
    key("keydown", { key: " ", repeat: true });
    const socket = await opened(log);
    expect(media.getUserMedia).toHaveBeenCalledOnce();
    expect(media.getUserMedia.mock.calls[0][0].audio).toMatchObject({
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    });
    expect(socket.url).toBe("wss://radio.example:8443/api/live/mic?rate=48000");
    expect(button.getAttribute("aria-pressed")).toBe("true");
    // The worklet has no outputs: nothing reaches the speakers.
    expect(log.nodes[0].options.numberOfOutputs).toBe(0);
    log.nodes[0].port.onmessage({ data: new ArrayBuffer(4096) });
    expect(log.sent).toHaveLength(1);
    // A socket backed up past its limit drops audio rather than queue it.
    socket.bufferedAmount = 1 << 20;
    log.nodes[0].port.onmessage({ data: new ArrayBuffer(4096) });
    expect(log.sent).toHaveLength(1);
    clockNow = 3000;
    key("keyup", { key: " " });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(socket.closedWith).toBe(1000);
    expect(log.stopped).toBe(1);
    expect(log.closedContexts).toBe(1);
    expect(document.activeElement).toBe(button);
    // The click Space would make after the hold is the same press.
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await settle();
    expect(log.sockets).toHaveLength(1);
  });

  it("latches on after a quick press and stops on the next press", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { log } = install();
      const button = byId("btn-talk");
      button.focus();
      clockNow = 1000;
      key("keydown", { key: "Enter" });
      clockNow = 1100;
      key("keyup", { key: "Enter" });
      await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
      log.sockets[0].open();
      vi.advanceTimersByTime(1);
      expect(button.getAttribute("aria-pressed")).toBe("true");
      expect(byId("sr-status").textContent)
        .toBe("Talking. Press Talk again to stop. Talk turns off by itself after 2 minutes.");
      // Shown in words under the button too, and describing it.
      const note = byId("talk-latched");
      expect(note.hidden).toBe(false);
      expect(note.textContent).toBe(LATCHED_NOTE);
      expect(note.textContent).toBe("Talking until you press Talk again, for 2 minutes at most.");
      expect(note.closest("[aria-live], [role=status]")).toBeNull();
      expect(button.getAttribute("aria-describedby").split(" ")).toContain("talk-latched");
      clockNow = 5000;
      key("keydown", { key: "Enter" });
      expect(button.getAttribute("aria-pressed")).toBe("false");
      clockNow = 5100;
      key("keyup", { key: "Enter" });
      expect(log.stopped).toBe(1);
      expect(log.sockets).toHaveLength(1);
      expect(byId("talk-latched").hidden).toBe(true);
      expect(button.getAttribute("aria-describedby").split(" ")).not.toContain("talk-latched");
    } finally {
      vi.useRealTimers();
    }
  });

  it("turns latched talk off after 2 minutes, saying so once", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { log } = install();
      const button = byId("btn-talk");
      button.focus();
      clockNow = 1000;
      key("keydown", { key: " " });
      clockNow = 1100;
      key("keyup", { key: " " });
      await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
      log.sockets[0].open();
      vi.advanceTimersByTime(1);
      expect(button.getAttribute("aria-pressed")).toBe("true");
      vi.advanceTimersByTime(119 * 1000);
      expect(button.getAttribute("aria-pressed")).toBe("true");
      vi.advanceTimersByTime(1000);
      expect(button.getAttribute("aria-pressed")).toBe("false");
      expect(log.stopped).toBe(1);
      expect(log.sockets[0].closedWith).toBe(1000);
      expect(byId("talk-latched").hidden).toBe(true);
      vi.advanceTimersByTime(1);
      expect(byId("sr-status").textContent).toBe("Talk turned off after 2 minutes.");
      expect(document.activeElement).toBe(button);
      // Said once: nothing more follows.
      vi.runAllTimers();
      expect(byId("sr-status").textContent).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });

  it("never latches on a quick Alt+M: letting go always stops", async () => {
    const { log } = install();
    const other = byId("btn-skip");
    other.focus();
    clockNow = 1000;
    key("keydown", { code: "KeyM", key: "m", altKey: true }, other);
    const socket = await opened(log);
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("true");
    clockNow = 1050;
    key("keyup", { code: "KeyM", key: "m", altKey: true }, other);
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
    expect(socket.closedWith).toBe(1000);
    expect(log.stopped).toBe(1);
    expect(byId("talk-latched").hidden).toBe(true);
  });

  it("does not latch a quick Alt+M that let go before the microphone was ready", async () => {
    const { log } = install();
    clockNow = 1000;
    key("keydown", { code: "KeyM", key: "m", altKey: true }, byId("btn-skip"));
    clockNow = 1050;
    key("keyup", { code: "KeyM", key: "m", altKey: true }, byId("btn-skip"));
    await settle();
    await settle();
    expect(log.sockets).toHaveLength(0);
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
    expect(log.stopped).toBe(1);
  });

  it("toggles on a bare click, as a screen reader in browse mode sends", async () => {
    const { log } = install();
    const button = byId("btn-talk");
    clockNow = 10000;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    const socket = await opened(log);
    expect(button.getAttribute("aria-pressed")).toBe("true");
    clockNow = 20000;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(socket.closedWith).toBe(1000);
  });

  it("talks while Alt+M is held anywhere, says so, and leaves focus alone", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const { log } = install();
      const other = byId("btn-skip");
      other.focus();
      clockNow = 1000;
      const down = key("keydown", { code: "KeyM", key: "m", altKey: true }, other);
      expect(down.defaultPrevented).toBe(true);
      await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
      log.sockets[0].open();
      vi.advanceTimersByTime(1);
      expect(byId("sr-status").textContent).toBe("Talking.");
      expect(document.activeElement).toBe(other);
      vi.runAllTimers();
      clockNow = 4000;
      // Letting go of Alt first ends it too.
      key("keyup", { key: "Alt", code: "AltLeft" }, other);
      vi.advanceTimersByTime(1);
      expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
      expect(byId("sr-status").textContent).toBe("Stopped talking.");
      expect(document.activeElement).toBe(other);
      // AltGr (Control+Alt) and Shift+Alt+M are not the shortcut.
      key("keydown", { code: "KeyM", key: "µ", altKey: true, ctrlKey: true }, other);
      key("keydown", { code: "KeyM", key: "M", altKey: true, shiftKey: true }, other);
      await Promise.resolve();
      expect(log.sockets).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops when the window loses focus or the page is hidden", async () => {
    const { log } = install();
    clockNow = 1000;
    key("keydown", { key: " " });
    const socket = await opened(log);
    window.dispatchEvent(new Event("blur"));
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
    expect(socket.closedWith).toBe(1000);
    expect(log.stopped).toBe(1);
  });

  it("says why the server stopped it, in words linked to Talk", async () => {
    const { log } = install();
    clockNow = 1000;
    key("keydown", { key: " " });
    const socket = await opened(log);
    socket.serverClose(4409);
    const error = byId("talk-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe(talkCloseProblem(4409));
    expect(byId("btn-talk").getAttribute("aria-describedby").split(" ")).toContain("talk-error");
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
    expect(log.stopped).toBe(1);
  });

  it("explains a refused microphone and an insecure page", async () => {
    const { log, media } = install();
    media.getUserMedia.mockRejectedValueOnce(Object.assign(new Error("no"), { name: "NotAllowedError" }));
    clockNow = 1000;
    key("keydown", { key: " " });
    await vi.waitFor(() => expect(byId("talk-error").hidden).toBe(false));
    expect(byId("talk-error").textContent).toBe(talkMicrophoneProblem({ name: "NotAllowedError" }));
    expect(log.sockets).toHaveLength(0);
    expect(log.closedContexts).toBe(1);

    const insecure = install({ media: () => undefined, secure: () => false });
    clockNow = 9000;
    key("keydown", { key: " " });
    expect(byId("talk-error").textContent).toBe(TALK_INSECURE_TEXT);
    expect(insecure.log.closedContexts).toBe(0);
  });

  it("is unavailable while another browser talks, without moving focus", () => {
    const { desk, media } = install();
    const button = byId("btn-talk");
    button.focus();
    desk.applyState({ talkover: true });
    expect(button.getAttribute("aria-disabled")).toBe("true");
    expect(document.activeElement).toBe(button);
    expect(byId("talk-note").textContent).toMatch(/Someone else is talking/);
    expect(button.getAttribute("aria-describedby").split(" ")).toContain("talk-note");
    clockNow = 1000;
    key("keydown", { key: " " });
    expect(media.getUserMedia).not.toHaveBeenCalled();
    expect(byId("talk-error").hidden).toBe(false);
  });

  it("lets go of the microphone when the station changes", async () => {
    const { desk, log } = install();
    clockNow = 1000;
    key("keydown", { key: " " });
    await opened(log);
    desk.clear();
    expect(log.stopped).toBe(1);
    expect(byId("btn-talk").getAttribute("aria-pressed")).toBe("false");
    expect(byId("talk-desk").hidden).toBe(true);
  });

  it("names every close code a person may meet", () => {
    for (const code of [4401, 4403, 4404, 4409, 4000, 1003, 1009, 1006]) {
      expect(talkCloseProblem(code)).toMatch(/\.$/);
    }
    expect(talkCloseProblem(1000)).toBe("");
  });
});
