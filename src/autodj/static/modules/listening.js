// Listening changes this browser's audio route, never station transport.
import { createStreamMode } from "./stream-mode.js";
import { createLocalMonitor } from "./local-monitor.js";
import { announceStatus } from "./live-region.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

const ROUTE_KEY = "autodj.listenRoute";

export function createListening({
  select, note, button, srStatus, monitorUrl, level, sinkId,
  onState = () => {}, storage = defaultStorage(), makeLocal = createLocalMonitor,
  makeStream = createStreamMode, ...streamOptions
}) {
  let state = null;
  let selectedStation = null;
  let listeningState = null;
  let listeningStation = null;
  let generation = 0;
  let route = readStorage(storage, ROUTE_KEY) === "stream" ? "stream" : "local";
  select.value = route;
  const local = makeLocal({
    onState: () => {
      clearListeningStateWhenStopped();
      render();
    },
    onError: (message) => {
      clearListeningStateWhenStopped();
      render();
      announceStatus(srStatus, message.message || String(message), { force: true, tone: "error" });
    },
  });
  const stream = makeStream({ ...streamOptions, button, srStatus,
    manageButton: false, onListening: () => {
      clearListeningStateWhenStopped();
      render();
    } });

  function isListening() { return local.isListening() || stream.isListening(); }
  function clearListeningStateWhenStopped() {
    if (!isListening()) {
      listeningState = null;
      listeningStation = null;
    }
  }
  function render() {
    const listening = isListening();
    const routeState = listening && listeningState ? listeningState : state;
    const localAvailable = Boolean(routeState?.monitor_available) && local.supported();
    const streamAvailable = Boolean(routeState?.stream_mode);
    select.querySelector('[value="local"]').disabled = !localAvailable;
    select.querySelector('[value="stream"]').disabled = !streamAvailable;
    button.hidden = false;
    button.disabled = !listening && !(route === "local" ? localAvailable : streamAvailable);
    button.setAttribute("aria-pressed", String(listening));
    note.textContent = listening || !button.disabled ? ""
      : route === "stream" ? "This station is not broadcasting a stream."
        : !local.supported() ? "Local listening needs HTTPS or localhost and a browser with AudioWorklet support."
          : "Local listening is unavailable while this station is stopped.";
    onState(listening);
  }
  function stop() {
    generation += 1;
    listeningState = null;
    listeningStation = null;
    local.stop();
    stream.stop();
    render();
  }
  async function toggleListen(pinned = null) {
    if (isListening()) { stop(); return; }
    const startState = pinned?.state ?? state;
    const startStation = pinned?.station ?? selectedStation;
    const available = route === "local"
      ? Boolean(startState?.monitor_available) && local.supported()
      : Boolean(startState?.stream_mode);
    if (!available) return;
    const mine = ++generation;
    listeningState = startState;
    listeningStation = startStation;
    render();
    try {
      if (route === "stream") {
        // Re-apply the pinned station's stream capability before starting. The
        // controls may currently show a different, non-streaming station.
        stream.apply({ ...startState, stream_mode: true });
        await stream.toggleListen();
      }
      else await local.start({ url: monitorUrl(), ...level(), sinkId: sinkId() });
    } catch (error) {
      if (mine === generation) announceStatus(srStatus, error.message,
        { force: true, tone: "error" });
    }
    if (mine === generation) {
      clearListeningStateWhenStopped();
      render();
    }
  }
  select.addEventListener("change", () => {
    const resume = isListening();
    const pinned = resume ? { state: listeningState || state, station: listeningStation ?? selectedStation } : null;
    stop();
    route = select.value;
    writeStorage(storage, ROUTE_KEY, route);
    render();
    if (resume) void toggleListen(pinned);
  });
  return {
    apply(value, station = selectedStation) {
      state = value;
      selectedStation = station;
      if (listeningStation === station && listeningState) listeningState = value;
      // A stream remains pinned while the controls show another station.
      stream.apply(stream.isListening() ? { ...value, stream_mode: true } : value);
      render();
    },
    setMonitorAvailable(available, station = selectedStation) {
      if (station !== selectedStation) {
        selectedStation = station;
        state = null;
      }
      if (state) state = { ...state, monitor_available: Boolean(available) };
      if (listeningState && listeningStation === station) {
        listeningState = { ...listeningState, monitor_available: Boolean(available) };
      }
      render();
    },
    toggleListen, stop, isListening,
    delaySeconds: () => route === "local" ? local.delaySeconds() : stream.delaySeconds(),
    setLevel: (volume, muted) => local.setLevel(volume, muted),
    setSink: (id) => local.setSink(id),
    forgetEvents: () => stream.forgetEvents(),
    setLinkUp: (up) => stream.setLinkUp(up),
  };
}
