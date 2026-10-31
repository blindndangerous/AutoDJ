import { afterEach, describe, expect, it, vi } from "vitest";
import { mediaReady, ProgrammeOwner } from "../../src/autodj/static/modules/local-programme.js";
import { selectStation } from "../../src/autodj/static/modules/stations.js";
import { ProgrammeTransport } from "../../src/autodj/static/modules/programme-transport.js";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("local programme browser performance", () => {
  // ADR-0019-01
  it("ignores an old worklet load failure after acquisition was replaced", async () => {
    let failModule;
    const engine = { prepareLocalProgramme: vi.fn(), endLocalProgramme: vi.fn(),
      _ctx: { resume: vi.fn().mockResolvedValue(), audioWorklet: {
        addModule: () => new Promise((_resolve, reject) => { failModule = reject; }),
      } } };
    const owner = new ProgrammeOwner({ engine });
    const taking = owner.take();
    await Promise.resolve();
    owner.run++;
    failModule(new Error("Old module request failed"));
    await expect(taking).resolves.toBeUndefined();
  });
  // ADR-0019-04
  it("renews the monitor watchdog for at most 750 milliseconds", async () => {
    const owner = new ProgrammeOwner();
    owner.node = { port: { postMessage: vi.fn() } };
    await owner.receive({ type: "credit", limit_frame: 9000 });
    await owner.receive({ type: "credit", limit_frame: 9000 });
    expect(owner.node.port.postMessage).toHaveBeenCalledWith({ type: "watch", frames: 33075 });
    expect(owner.node.port.postMessage).toHaveBeenCalledTimes(1);
  });
  // ADR-0019-01
  it("cancels acquisition when the station changes during context preparation", async () => {
    vi.stubGlobal("AudioWorkletNode", vi.fn(function () { return { port: {} }; }));
    vi.stubGlobal("WebSocket", vi.fn(function () { return { close: vi.fn() }; }));
    let resume;
    const engine = { prepareLocalProgramme: vi.fn(), endLocalProgramme: vi.fn(),
      _ctx: { resume: () => new Promise(done => { resume = done; }),
        audioWorklet: { addModule: vi.fn().mockResolvedValue() } } };
    const owner = new ProgrammeOwner({ engine });
    const taking = owner.take();
    selectStation("second");
    resume();
    await taking;
    expect(owner.socket).toBe(null);
    expect(owner.taking).toBe(false);
    selectStation("default");
    vi.unstubAllGlobals();
  });
  // ADR-0019-01
  it("serializes pause markers and cancels the old pause timer before resume", async () => {
    vi.useFakeTimers();
    const audio = new window.EventTarget();
    Object.assign(audio, { readyState: 3, play: vi.fn().mockResolvedValue(), pause: vi.fn() });
    const owner = new ProgrammeOwner({ engine: { _ctx: { currentTime: 1 },
      decks: [{ audio, gain: { gain: { setValueAtTime: vi.fn() } } }] } });
    owner.grant = { generation: 1 }; owner.current = { id: "one" };
    let acknowledge;
    owner.marker = vi.fn(() => new Promise(done => { acknowledge = done; }));
    const first = owner.togglePause();
    const duplicate = owner.togglePause();
    expect(owner.marker).toHaveBeenCalledTimes(1);
    acknowledge(); await first; await duplicate;
    owner.marker.mockResolvedValue();
    await owner.togglePause();
    await vi.advanceTimersByTimeAsync(200);
    expect(owner.paused).toBe(false);
    expect(audio.pause).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
  // ADR-0019-04
  it("waits for an observed render clock before preparing the first audible marker", async () => {
    const owner = new ProgrammeOwner();
    let ready = false;
    const waiting = owner.renderClock().then(() => { ready = true; });
    await Promise.resolve();
    expect(ready).toBe(false);
    owner.capture({ type: "clock", frame: 128, time: 1 });
    await waiting;
    expect(ready).toBe(true);
  });
  // ADR-0019-01
  it("does not apply old render acknowledgements to a replacement owner", async () => {
    const audio = new window.EventTarget();
    Object.assign(audio, { readyState: 3, play: vi.fn().mockResolvedValue(), pause: vi.fn() });
    const gain = { cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() };
    const owner = new ProgrammeOwner({ engine: { _ctx: { currentTime: 1 },
      decks: [{ audio, gain: { gain } }], setSrcOnDeck: vi.fn() } });
    owner.grant = { generation: 1 };
    owner.occurrences = [{ id: "old", path: "same.wav" }];
    owner.marker = vi.fn(async () => { owner.run++; owner.current = { id: "new" }; });
    await expect(owner.transition()).resolves.toBeUndefined();
    expect(gain.linearRampToValueAtTime).not.toHaveBeenCalled();
    expect(audio.pause).not.toHaveBeenCalled();
    expect(owner.current.id).toBe("new");
  });
  // ADR-0019-01
  it("does not report an old pause failure against a replacement owner", async () => {
    const owner = new ProgrammeOwner({ engine: { _ctx: { currentTime: 1 },
      decks: [{ audio: {}, gain: { gain: {} } }] } });
    owner.current = { id: "old" };
    owner.marker = vi.fn(async () => { owner.run++; throw new Error("Old marker cancelled"); });
    await expect(owner.togglePause()).resolves.toBe(false);
  });
  // ADR-0019-17
  it("rewinds a reused media deck for a new occurrence of the same song", async () => {
    vi.useFakeTimers();
    const audio = new window.EventTarget();
    audio.currentTime = 19;
    audio.readyState = 3;
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const gain = { cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() };
    const owner = new ProgrammeOwner({ engine: { _ctx: { currentTime: 1 },
      decks: [{ audio, gain: { gain } }], setSrcOnDeck: vi.fn() } });
    owner.grant = { generation: 1 };
    owner.node = {};
    owner.renderClock = vi.fn().mockResolvedValue();
    owner.occurrences = [{ id: "repeat", path: "same.wav" }];
    owner.marker = vi.fn().mockResolvedValue();
    await owner.transition();
    expect(owner.renderClock).toHaveBeenCalledWith(true);
    expect(audio.currentTime).toBe(0);
    await vi.advanceTimersByTimeAsync(600);
    vi.useRealTimers();
  });
  // ADR-0019-17
  it("does not start media after its ownership operation was cancelled", async () => {
    const abort = new window.AbortController(); abort.abort();
    const audio = new window.EventTarget();
    audio.play = vi.fn().mockResolvedValue();
    await expect(mediaReady(audio, abort.signal)).rejects.toThrow();
    expect(audio.play).not.toHaveBeenCalled();
  });
  // ADR-0019-14
  it("catches up after timer quantization without exceeding a two packet sender burst", () => {
    let now = 0, timer = null;
    vi.spyOn(globalThis.performance, "now").mockImplementation(() => now);
    vi.stubGlobal("setTimeout", (callback, delay) => { timer = { callback, due: now + delay }; return 1; });
    const sent = [];
    const owner = new ProgrammeOwner();
    owner.transport = { send: (_buffer, frame) => { sent.push({time: now, frame}); return true; } };
    owner.node = { port: { postMessage: vi.fn() } };
    const pending = [];
    for (let frame = 0; frame < 100; frame++) {
      now = frame * 10;
      pending.push({type: "pcm", slot: frame % 32, firstFrame: frame * 441, buffer: new ArrayBuffer(3528)});
      if (frame >= 30 && frame < 36) continue; // 60 ms event-loop interruption.
      if (timer && timer.due <= now) { const run = timer.callback; timer = null; run(); }
      for (const packet of pending.splice(0)) owner.capture(packet);
    }
    // Rounded 10 ms wakeups must recover the backlog while capture continues.
    expect(sent.length).toBeGreaterThanOrEqual(99);
    for (const time of new Set(sent.map(item => item.time))) {
      expect(sent.filter(item => item.time === time).length).toBeLessThanOrEqual(2);
    }
    expect(sent.map(item => item.frame)).toEqual(Array.from({length:sent.length}, (_, i) => i * 441));
  });

  // ADR-0019-14
  it("paces batched render messages within the 32 buffer pool", async () => {
    vi.useFakeTimers();
    const owner = new ProgrammeOwner({engine: { endLocalProgramme: vi.fn(), _master: {} }});
    owner.transport = { send: vi.fn().mockReturnValue(true) };
    owner.node = { port: { postMessage: vi.fn() }, disconnect: vi.fn() };
    owner.sum = { connect: vi.fn(), disconnect: vi.fn() };
    const transport = owner.transport, node = owner.node;
    for (let slot = 0; slot < 32; slot++) {
      owner.capture({ type: "pcm", slot, firstFrame: slot * 441, buffer: new ArrayBuffer(3528) });
    }
    expect(transport.send).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(257);
    expect(transport.send).toHaveBeenCalledTimes(32);
    expect(node.port.postMessage).toHaveBeenCalledTimes(32);
    vi.useRealTimers();
  });

  // ADR-0019-14
  it("ends the session without stranded live loans on every sender failure path", () => {
    for (const failure of ["send throws", "socket closed", "backpressure", "credit", "invalid frame", "invalid buffer", "already failed", "sender missing", "recycle throws"]) {
      vi.useFakeTimers();
      const engine = { endLocalProgramme: vi.fn(), _master: {} };
      const owner = new ProgrammeOwner({ engine });
      const socket = { readyState: 1, bufferedAmount: 0, send: vi.fn(), close: vi.fn() };
      const node = { port: { postMessage: vi.fn(), onmessage: vi.fn() }, disconnect: vi.fn() };
      owner.socket = socket; owner.node = node;
      owner.sum = { connect: vi.fn(), disconnect: vi.fn() };
      owner.grant = { generation: 1, limit_frame: 8820 };
      owner.transport = new ProgrammeTransport(socket, owner.grant);
      // One queued loan and one newly delivered loan must both end with the session.
      owner.captureQueue.push({ type: "pcm", slot: 0, firstFrame: 0, buffer: new ArrayBuffer(3528) });
      if (failure === "send throws") socket.send.mockImplementation(() => { throw new Error("send failed"); });
      if (failure === "socket closed") socket.readyState = 3;
      if (failure === "backpressure") socket.bufferedAmount = 22000;
      if (failure === "credit") owner.transport.limitFrame = 0;
      if (failure === "invalid frame") owner.transport.nextFrame = 441;
      if (failure === "invalid buffer") owner.captureQueue[0].buffer = new ArrayBuffer(8);
      if (failure === "already failed") owner.transport.fail("prior failure");
      if (failure === "sender missing") owner.transport = null;
      if (failure === "recycle throws") node.port.postMessage.mockImplementation(() => { throw new Error("port failed"); });
      expect(() => owner.capture({ type: "pcm", slot: 1, firstFrame: 441, buffer: new ArrayBuffer(3528) }), failure).not.toThrow();
      expect(owner.owned).toBe(false);
      expect(owner.socket).toBe(null);
      expect(owner.node).toBe(null);
      expect(owner.captureQueue).toEqual([]);
      expect(owner.sendTimer).toBe(null);
      expect(socket.close).toHaveBeenCalled();
      expect(node.disconnect).toHaveBeenCalled();
      expect(engine.endLocalProgramme).toHaveBeenCalled();
      vi.useRealTimers();
    }
  });
  // ADR-0019-17
  it("rejects failed play and requires observed media readiness", async () => {
    const audio = new window.EventTarget();
    audio.readyState = 0;
    audio.play = vi.fn().mockRejectedValue(new Error("blocked"));
    await expect(mediaReady(audio)).rejects.toThrow("blocked");
    audio.play.mockResolvedValue();
    let ready = false;
    const waiting = mediaReady(audio).then(() => { ready = true; });
    await Promise.resolve();
    expect(ready).toBe(false);
    audio.readyState = 3;
    audio.dispatchEvent(new Event("playing"));
    await waiting;
    expect(ready).toBe(true);
  });

  // ADR-0019-04
  it("does not open a deck or emit started when play rejects", async () => {
    const gain = { cancelScheduledValues: vi.fn(), setValueAtTime: vi.fn(),
      linearRampToValueAtTime: vi.fn(), value: 0 };
    const audio = new window.EventTarget();
    audio.readyState = 0;
    audio.play = vi.fn().mockRejectedValue(new Error("decode failed"));
    audio.pause = vi.fn();
    const engine = { decks: [{ audio, gain: { gain } }], _ctx: { currentTime: 1 },
      setSrcOnDeck: vi.fn(), endLocalProgramme: vi.fn() };
    const owner = new ProgrammeOwner({ engine });
    owner.grant = { generation: 1 };
    owner.socket = { readyState: 1, send: vi.fn(), close: vi.fn() };
    owner.occurrences = [{ id: "one", path: "a.wav" }];
    await expect(owner.transition()).rejects.toThrow("decode failed");
    expect(gain.linearRampToValueAtTime).not.toHaveBeenCalled();
    expect(owner.socket.send).not.toHaveBeenCalled();
    expect(owner.current).toBe(null);
  });
});
