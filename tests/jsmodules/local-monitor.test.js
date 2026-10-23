import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  createLocalMonitor,
  LOCAL_MONITOR_INSECURE_TEXT,
} from "../../src/autodj/static/modules/local-monitor.js";

const { EventTarget: EventTargetImpl } = globalThis;

function fakes({ addModule = vi.fn(async () => {}), sinkId = vi.fn(async () => {}) } = {}) {
  const log = { contexts: [], sockets: [], nodes: [], messages: [], states: [], errors: [] };
  class Context extends EventTargetImpl {
    constructor() {
      super();
      this.state = "running";
      this.destination = {};
      this.sampleRate = 48000;
      this.audioWorklet = { addModule };
      this.setSinkId = sinkId;
      this.close = vi.fn(async () => { this.state = "closed"; });
      this.resume = vi.fn(async () => {});
      log.contexts.push(this);
    }
    suspend() {
      this.state = "suspended";
      this.dispatchEvent(new Event("statechange"));
    }
  }
  class Node {
    constructor(_context, name, options) {
      this.name = name;
      this.options = options;
      this.port = {
        onmessage: null,
        postMessage: vi.fn((data) => log.messages.push(data)),
        close: vi.fn(),
      };
      this.connect = vi.fn();
      this.disconnect = vi.fn();
      log.nodes.push(this);
    }
  }
  class Socket extends EventTargetImpl {
    constructor(url) {
      super();
      this.url = url;
      this.readyState = 0;
      this.bufferedAmount = 0;
      log.sockets.push(this);
    }
    open() { this.readyState = 1; this.dispatchEvent(new Event("open")); }
    message(data) {
      const event = new Event("message");
      event.data = data;
      this.dispatchEvent(event);
    }
    close(code = 1000) {
      this.readyState = 3;
      this.closedCode = code;
      const event = new Event("close");
      event.code = code;
      this.dispatchEvent(event);
    }
  }
  const monitor = createLocalMonitor({
    AudioContextClass: () => Context,
    AudioWorkletNodeClass: () => Node,
    WebSocketClass: () => Socket,
    secure: () => true,
    location: { protocol: "https:", host: "radio.example" },
    onState: (state) => log.states.push(state),
    onError: (error) => log.errors.push(error),
  });
  return { monitor, log, addModule, sinkId };
}

afterEach(() => vi.restoreAllMocks());

describe("local monitor", () => {
  it("plays the connected station socket, applies local level and closes all resources", async () => {
    const { monitor, log, sinkId } = fakes();
    const started = monitor.start({ url: "/api/stations/nightowl/monitor", volume: 0.4, muted: true, sinkId: "phones" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    expect(log.sockets[0].url).toBe("wss://radio.example/api/stations/nightowl/monitor");
    log.sockets[0].open();
    await expect(started).resolves.toBe(true);
    expect(log.contexts[0].audioWorklet.addModule).toHaveBeenCalledWith("/local-monitor-worklet.js");
    expect(sinkId).toHaveBeenCalledWith("phones");
    expect(log.nodes[0].options.outputChannelCount).toEqual([2]);
    expect(log.nodes[0].connect).toHaveBeenCalledWith(log.contexts[0].destination);
    expect(log.messages[0]).toMatchObject({ level: true, volume: 0.4, muted: true });
    expect(monitor.isListening()).toBe(true);

    monitor.setLevel(0.8, false);
    expect(log.messages[1]).toMatchObject({ level: true, volume: 0.8, muted: false });
    monitor.stop();
    expect(monitor.isListening()).toBe(false);
    expect(log.sockets[0].closedCode).toBe(1000);
    expect(log.contexts[0].close).toHaveBeenCalledOnce();
    expect(log.states).toEqual([true, false]);
  });

  it("uses the latest page level when it changes while the worklet loads", async () => {
    let finishModule;
    const addModule = vi.fn(() => new Promise((resolve) => { finishModule = resolve; }));
    const { monitor, log } = fakes({ addModule });
    const started = monitor.start({ url: "/api/monitor", volume: 0.2, muted: false });
    await vi.waitFor(() => expect(addModule).toHaveBeenCalledOnce());

    monitor.setLevel(0.65, true);
    finishModule();
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    expect(log.messages[0]).toMatchObject({ level: true, volume: 0.65, muted: true });
    log.sockets[0].open();
    await expect(started).resolves.toBe(true);
    monitor.stop();
  });

  it("bounds worklet messages to one outstanding chunk and drops malformed audio", async () => {
    const { monitor, log } = fakes();
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    log.sockets[0].open();
    await started;
    const first = new ArrayBuffer(16);
    log.sockets[0].message(first);
    log.sockets[0].message(new ArrayBuffer(16));
    log.sockets[0].message(new ArrayBuffer(7));
    expect(log.nodes[0].port.postMessage).toHaveBeenCalledTimes(2);
    expect(log.nodes[0].port.postMessage).toHaveBeenLastCalledWith(first, [first]);
    log.nodes[0].port.onmessage({ data: { bufferedFrames: 4 } });
    expect(monitor.delaySeconds()).toBeCloseTo(4 / 44100);
    log.sockets[0].message(new ArrayBuffer(16));
    expect(log.nodes[0].port.postMessage).toHaveBeenCalledTimes(3);
    monitor.stop();
  });

  it("keeps the processor ring at 200 ms and linearly resamples to the context rate", async () => {
    const { monitor, log } = fakes();
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    log.sockets[0].open();
    await started;

    let Processor;
    class WorkletBase {
      constructor() { this.port = { postMessage: vi.fn(), onmessage: null }; }
    }
    const source = readFileSync(join(process.cwd(), "src/autodj/static/local-monitor-worklet.js"), "utf8");
    new Function("AudioWorkletProcessor", "registerProcessor", "sampleRate", source)(
      WorkletBase,
      (_name, implementation) => { Processor = implementation; },
      48000,
    );
    const processor = new Processor();
    const samples = new Float32Array(9000 * 2);
    for (let frame = 0; frame < 9000; frame += 1) {
      samples[frame * 2] = frame / 10000;
      samples[frame * 2 + 1] = -samples[frame * 2];
    }
    processor.port.onmessage({ data: samples.buffer });
    expect(processor.port.postMessage).toHaveBeenLastCalledWith({ bufferedFrames: 8820 });

    const left = new Float32Array(128);
    const right = new Float32Array(128);
    processor.process([], [[left, right]]);
    expect(left[0]).toBeCloseTo(0.018);
    expect(left[1]).toBeCloseTo(0.018091875);
    expect(right[0]).toBeCloseTo(-0.018);
    monitor.stop();
  });

  it("stops a pending startup before creating its socket", async () => {
    let finishModule;
    const addModule = vi.fn(() => new Promise((resolve) => { finishModule = resolve; }));
    const { monitor, log } = fakes({ addModule });
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(addModule).toHaveBeenCalledOnce());
    monitor.stop();
    finishModule();
    await expect(started).resolves.toBe(false);
    expect(log.sockets).toHaveLength(0);
    expect(log.contexts[0].close).toHaveBeenCalledOnce();
  });

  it("reports unsupported secure contexts clearly", async () => {
    const { monitor } = fakes();
    const insecure = createLocalMonitor({
      AudioContextClass: () => class {},
      WebSocketClass: () => class {},
      AudioWorkletNodeClass: () => class {},
      secure: () => false,
    });
    expect(monitor.supported()).toBe(true);
    expect(insecure.supported()).toBe(false);
    await expect(insecure.start({ url: "/api/monitor" })).rejects.toThrow(LOCAL_MONITOR_INSECURE_TEXT);
  });

  it("fails honestly when the requested output device cannot be selected", async () => {
    const sinkId = vi.fn(async () => { throw new Error("denied"); });
    const { monitor, log } = fakes({ sinkId });
    await expect(monitor.start({ url: "/api/monitor", sinkId: "phones" })).rejects.toThrow("Could not select the chosen output device");
    expect(log.sockets).toHaveLength(0);
    expect(log.contexts[0].close).toHaveBeenCalledOnce();
    expect(log.errors).toHaveLength(0);
  });

  it("silently cancels a websocket that has not opened yet", async () => {
    const { monitor, log } = fakes();
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    monitor.stop();
    await expect(started).resolves.toBe(false);
    expect(log.sockets[0].closedCode).toBe(1000);
    expect(log.contexts[0].close).toHaveBeenCalledOnce();
    expect(log.errors).toHaveLength(0);
  });

  it("reports a runtime audio suspension and releases the active monitor", async () => {
    const { monitor, log } = fakes();
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    log.sockets[0].open();
    await started;

    log.contexts[0].suspend();
    expect(monitor.isListening()).toBe(false);
    expect(log.sockets[0].closedCode).toBe(1000);
    expect(log.contexts[0].close).toHaveBeenCalledOnce();
    expect(log.errors).toHaveLength(1);
    expect(log.errors[0].message).toContain("browser audio was suspended");
  });

  it("keeps playback active when a later output-device change fails", async () => {
    const { monitor, log, sinkId } = fakes();
    const started = monitor.start({ url: "/api/monitor" });
    await vi.waitFor(() => expect(log.sockets).toHaveLength(1));
    log.sockets[0].open();
    await started;

    sinkId.mockRejectedValueOnce(new Error("device removed"));
    await expect(monitor.setSink("missing-device")).rejects.toThrow("Could not select the chosen output device");
    expect(sinkId).toHaveBeenCalledWith("missing-device");
    expect(monitor.isListening()).toBe(true);
    expect(log.errors).toHaveLength(0);
    monitor.stop();
  });
});
