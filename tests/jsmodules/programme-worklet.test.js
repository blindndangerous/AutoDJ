import { beforeEach, describe, expect, it, vi } from "vitest";

let Processor;
beforeEach(async () => {
  vi.resetModules();
  vi.stubGlobal("sampleRate", 44100);
  vi.stubGlobal("AudioWorkletProcessor", class {
    constructor() { this.port = { postMessage: vi.fn(), onmessage: null }; }
  });
  vi.stubGlobal("registerProcessor", (_name, processor) => { Processor = processor; });
  await import("../../src/autodj/static/programme-worklet.js");
});

describe("programme capture", () => {
  // ADR-0020-04
  it("samples the capture clock at a render boundary instead of a port callback", () => {
    vi.stubGlobal("currentTime", 1);
    const p = new Processor();
    p.port.onmessage({ data: { type: "watch", frames: 44100 } });
    expect(p.port.postMessage).not.toHaveBeenCalled();
    vi.stubGlobal("currentTime", 1 + 128 / 44100);
    p.process([[new Float32Array(128), new Float32Array(128)]], []);
    expect(p.port.postMessage.mock.calls[0][0]).toEqual({
      type: "clock", frame: 0, time: 1 + 128 / 44100,
    });
  });
  // ADR-0020-14
  it("waits for a fresh grant before starting its capture clock", () => {
    const p = new Processor({ processorOptions: { waitForStart: true } });
    const channels = [new Float32Array(128), new Float32Array(128)];
    for (let i = 0; i < 8; i++) p.process([channels], []);
    expect(p.port.postMessage).not.toHaveBeenCalled();
    p.port.onmessage({ data: { type: "start" } });
    for (let i = 0; i < 4; i++) p.process([channels], []);
    expect(p.port.postMessage.mock.calls[0][0].firstFrame).toBe(0);
  });
  // ADR-0020-04
  it("acknowledges future markers before PCM and silences only monitor on watchdog expiry", () => {
    vi.stubGlobal("currentTime", 0);
    const p = new Processor();
    p.port.onmessage({ data: { type: "watch", frames: 256 } });
    p.port.onmessage({ data: { type: "mark", event: { sequence: 0, frame: 128, kind: "started" } } });
    expect(p.port.postMessage.mock.calls.at(-1)[0].type).toBe("marked");
    const channels = [new Float32Array(128).fill(.25), new Float32Array(128).fill(-.5)];
    const output = [new Float32Array(128), new Float32Array(128)];
    p.process([channels], [output]);
    expect(output[0][0]).toBe(.25);
    p.process([channels], [output]);
    p.process([channels], [output]);
    expect(output[0][0]).toBe(0);
    expect(p.port.postMessage.mock.calls.filter(c => c[0].type === "failed")).toHaveLength(1);
  });

  // ADR-0020-14
  it("frames stereo without gain and bounds outstanding transfers to 32", () => {
    const p = new Processor();
    const left = new Float32Array(128).fill(0.25);
    const right = new Float32Array(128).fill(-0.5);
    for (let i = 0; i < 110; i++) p.process([[left, right]], []);
    const messages = p.port.postMessage.mock.calls.map(c => c[0]);
    expect(messages.filter(m => m.type === "pcm")).toHaveLength(31);
    expect(messages.some(m => m.type === "failed")).toBe(false);
    const pcm = new Float32Array(messages[0].buffer);
    expect(pcm.length).toBe(882);
    expect(Array.from(pcm.slice(0, 4))).toEqual([0.25, -0.5, 0.25, -0.5]);
    for (let i = 0; i < 20; i++) p.process([[left, right]], []);
    expect(p.port.postMessage.mock.calls.filter(c => c[0].type === "pcm")).toHaveLength(32);
    expect(p.port.postMessage.mock.calls.filter(c => c[0].type === "failed")).toHaveLength(1);
  });

  // ADR-0020-14
  it("recycles only loaned slots and rejects unsupported rates", () => {
    vi.stubGlobal("sampleRate", 48000);
    expect(() => new Processor()).toThrow("44,100");
    vi.stubGlobal("sampleRate", 44100);
    const p = new Processor();
    const channels = [new Float32Array(128), new Float32Array(128)];
    for (let i = 0; i < 4; i++) p.process([channels], []);
    const packet = p.port.postMessage.mock.calls[0][0];
    p.port.onmessage({ data: { type: "recycle", slot: packet.slot, buffer: packet.buffer } });
    for (let i = 0; i < 10; i++) p.process([channels], []);
    expect(p.port.postMessage.mock.calls.filter(c => c[0].type === "pcm")).toHaveLength(4);
    expect(p.port.postMessage.mock.calls.at(-1)[0].firstFrame).toBe(1323);
  });

  // ADR-0020-14
  it("survives 200 milliseconds of delayed recycling without growing or losing stereo frames", () => {
    const p = new Processor();
    const channels = [new Float32Array(441).fill(.25), new Float32Array(441).fill(-.5)];
    for (let i = 0; i < 25; i++) p.process([channels], []);
    let messages = p.port.postMessage.mock.calls.map(c => c[0]);
    expect(messages.filter(m => m.type === "pcm")).toHaveLength(25);
    expect(messages.some(m => m.type === "failed")).toBe(false);
    for (const packet of messages) {
      p.port.onmessage({ data: { type: "recycle", slot: packet.slot, buffer: packet.buffer } });
      // A duplicate return cannot create an additional free slot.
      p.port.onmessage({ data: { type: "recycle", slot: packet.slot, buffer: packet.buffer } });
    }
    p.port.onmessage({ data: { type: "recycle", slot: 32, buffer: new ArrayBuffer(3528) } });
    for (let i = 0; i < 33; i++) p.process([channels], []);
    messages = p.port.postMessage.mock.calls.map(c => c[0]);
    const packets = messages.filter(m => m.type === "pcm");
    expect(packets).toHaveLength(57);
    expect(packets.at(-1).firstFrame).toBe(56 * 441);
    expect(messages.filter(m => m.type === "failed")).toHaveLength(1);
  });
});
