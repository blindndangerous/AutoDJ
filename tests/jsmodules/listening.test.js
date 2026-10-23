import { afterEach, describe, expect, it, vi } from "vitest";

import { createListening } from "../../src/autodj/static/modules/listening.js";

function setup({ savedRoute = null, localAvailable = true, streamMode = true,
  station = "nightowl", pinnedStation = station, onState = vi.fn() } = {}) {
  document.body.innerHTML = `
    <select id="route"><option value="local">Local</option><option value="stream">Stream</option></select>
    <button id="listen" type="button"></button><p id="note"></p><p id="status"></p>`;
  const select = document.getElementById("route");
  const note = document.getElementById("note");
  const button = document.getElementById("listen");
  const storage = {
    getItem: vi.fn(() => savedRoute),
    setItem: vi.fn(),
  };
  const calls = [];
  let localListening = false;
  let streamListening = false;
  let streamEnabled = false;
  let finishLocalStart;
  const local = {
    supported: vi.fn(() => true),
    isListening: () => localListening,
    start: vi.fn((_options) => {
      calls.push("local.start");
      localListening = true;
      return new Promise((resolve) => { finishLocalStart = resolve; });
    }),
    stop: vi.fn(() => { calls.push("local.stop"); localListening = false; }),
    setLevel: vi.fn(),
    setSink: vi.fn(async () => true),
    delaySeconds: () => 0.02,
  };
  const stream = {
    isListening: () => streamListening,
    toggleListen: vi.fn(async () => {
      calls.push("stream.start");
      if (!streamEnabled) throw new Error("stream route is unavailable");
      streamListening = true;
    }),
    stop: vi.fn(() => { calls.push("stream.stop"); streamListening = false; }),
    apply: vi.fn((value) => { streamEnabled = Boolean(value?.stream_mode); }),
    delaySeconds: () => 3,
    forgetEvents: vi.fn(),
    setLinkUp: vi.fn(),
  };
  const monitorUrl = vi.fn(() => `/api/stations/${pinnedStation}/monitor`);
  const listening = createListening({
    select, note, button, srStatus: document.getElementById("status"),
    monitorUrl,
    onState,
    level: () => ({ volume: 0.25, muted: true }),
    sinkId: () => "headphones",
    storage,
    makeLocal: (options) => { local.callbacks = options; return local; },
    makeStream: () => stream,
  });
  listening.apply({ monitor_available: localAvailable, stream_mode: streamMode, station }, station);
  return { listening, local, stream, calls, storage, select, note, button, monitorUrl, onState,
    finishLocal: (value = false) => finishLocalStart?.(value) };
}

afterEach(() => { document.body.replaceChildren(); });

describe("listening route", () => {
  it("restores and persists the selected route, and disables unavailable routes", () => {
    const ui = setup({ savedRoute: "stream", localAvailable: false, streamMode: false });
    expect(ui.select.value).toBe("stream");
    expect(ui.select.querySelector('[value="local"]').disabled).toBe(true);
    expect(ui.select.querySelector('[value="stream"]').disabled).toBe(true);
    expect(ui.button.disabled).toBe(true);
    expect(ui.note.textContent).toContain("not broadcasting");

    ui.select.value = "local";
    ui.select.dispatchEvent(new Event("change"));
    expect(ui.storage.setItem).toHaveBeenLastCalledWith("autodj.listenRoute", "local");
    expect(ui.monitorUrl).not.toHaveBeenCalled();
    expect(ui.calls).toEqual(["local.stop", "stream.stop"]);
    expect(ui.local.start).not.toHaveBeenCalled();
    expect(ui.stream.toggleListen).not.toHaveBeenCalled();
  });

  it("stops the pending route before starting the new route and ignores stale completion", async () => {
    const ui = setup();
    const firstStart = ui.listening.toggleListen();
    expect(ui.local.start).toHaveBeenCalledWith({
      url: "/api/stations/nightowl/monitor", volume: 0.25, muted: true, sinkId: "headphones",
    });
    expect(ui.listening.isListening()).toBe(true);

    ui.select.value = "stream";
    ui.select.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(ui.stream.toggleListen).toHaveBeenCalledOnce());
    expect(ui.calls.indexOf("local.stop")).toBeLessThan(ui.calls.indexOf("stream.start"));
    expect(ui.listening.isListening()).toBe(true);
    expect(ui.listening.delaySeconds()).toBe(3);

    ui.finishLocal(true);
    await firstStart;
    expect(ui.select.value).toBe("stream");
    expect(ui.listening.isListening()).toBe(true);
    expect(ui.stream.isListening()).toBe(true);
  });

  it("keeps local volume controls device-only and forwards stream state without mutation", async () => {
    const ui = setup();
    ui.listening.apply({ monitor_available: true, stream_mode: true, station: "another-station" });
    expect(ui.stream.apply).toHaveBeenLastCalledWith({ monitor_available: true, stream_mode: true, station: "another-station" });
    ui.listening.setLevel(0.6, false);
    expect(ui.local.setLevel).toHaveBeenCalledWith(0.6, false);
    expect(ui.stream.toggleListen).not.toHaveBeenCalled();
    expect(ui.monitorUrl).not.toHaveBeenCalled();
  });

  it("clears stale local availability and refreshes app context after a monitor failure", () => {
    const ui = setup();
    ui.listening.setMonitorAvailable(false);
    expect(ui.select.querySelector('[value="local"]').disabled).toBe(true);
    expect(ui.button.disabled).toBe(true);
    expect(ui.note.textContent).toContain("unavailable while this station is stopped");

    ui.onState.mockClear();
    ui.local.callbacks.onError(new Error("monitor disconnected"));
    expect(ui.onState).toHaveBeenCalledWith(false);
  });

  it("keeps the listening station's route available while controls move to another station", async () => {
    const ui = setup({ savedRoute: "stream", station: "station-a", pinnedStation: "station-a" });
    await ui.listening.toggleListen();
    ui.listening.apply({ monitor_available: false, stream_mode: false }, "station-b");
    ui.listening.setMonitorAvailable(false, "station-b");
    expect(ui.select.querySelector('[value="local"]').disabled).toBe(false);

    ui.select.value = "local";
    ui.select.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(ui.local.start).toHaveBeenCalledOnce());
    expect(ui.calls.indexOf("stream.stop")).toBeLessThan(ui.calls.indexOf("local.start"));
    expect(ui.local.start).toHaveBeenCalledWith({
      url: "/api/stations/station-a/monitor", volume: 0.25, muted: true, sinkId: "headphones",
    });
    expect(ui.listening.isListening()).toBe(true);
    ui.listening.stop();
    expect(ui.select.querySelector('[value="local"]').disabled).toBe(true);
  });

  it("restores the pinned stream capability when switching from Local after selecting a non-streaming station", async () => {
    const ui = setup({ station: "station-a", pinnedStation: "station-a" });
    const pendingLocalStart = ui.listening.toggleListen();
    ui.finishLocal(true);
    await pendingLocalStart;
    ui.listening.apply({ monitor_available: false, stream_mode: false }, "station-b");

    ui.select.value = "stream";
    ui.select.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(ui.stream.isListening()).toBe(true));

    expect(ui.stream.apply).toHaveBeenLastCalledWith({
      monitor_available: true, stream_mode: true, station: "station-a",
    });
    expect(ui.calls.indexOf("local.stop")).toBeLessThan(ui.calls.indexOf("stream.start"));
    expect(ui.listening.isListening()).toBe(true);
  });
});
