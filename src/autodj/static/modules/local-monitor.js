// Plays the server's shared, on-air mix bus in this browser.
// The wire format is interleaved stereo float32le at 44.1 kHz.

const SOURCE_RATE = 44100;
const CHANNELS = 2;

export const LOCAL_MONITOR_INSECURE_TEXT = "Local monitoring needs a secure connection: open AutoDJ over HTTPS, or on this computer at localhost.";
const LOCAL_MONITOR_UNSUPPORTED_TEXT = "This browser cannot play the local monitor. Try a current version of Firefox, Chrome, Edge or Safari.";

function socketProblem(code) {
  if (code === 4401) return "Sign in again to use local monitoring.";
  if (code === 4403) return "The server refused the local monitor connection from this page's address.";
  if (code === 4404) return "This station is not available for local monitoring.";
  if (code === 4409) return "Local monitoring is unavailable because this station is not playing on the server.";
  return "The local monitor connection closed.";
}

export function createLocalMonitor({
  AudioContextClass = () => globalThis.AudioContext || globalThis.webkitAudioContext,
  AudioWorkletNodeClass = () => globalThis.AudioWorkletNode,
  WebSocketClass = () => globalThis.WebSocket,
  secure = () => globalThis.isSecureContext === true
    || ["localhost", "127.0.0.1", "::1"].includes(globalThis.location?.hostname),
  location: loc = globalThis.location,
  workletUrl = "/local-monitor-worklet.js",
  onState = () => {},
  onError = () => {},
} = {}) {
  let session = null;
  let generation = 0;

  function supported() {
    const Context = AudioContextClass();
    return secure() && typeof Context === "function" && typeof WebSocketClass() === "function"
      && typeof AudioWorkletNodeClass() === "function";
  }

  function delaySeconds() {
    return session ? session.bufferedFrames / SOURCE_RATE : 0;
  }

  function cleanup(current) {
    if (!current || current.done) return;
    current.done = true;
    try { current.node?.port?.close?.(); } catch (_) { /* already closed */ }
    try { current.node?.disconnect?.(); } catch (_) { /* already disconnected */ }
    const socket = current.socket;
    if (socket && socket.readyState < 2) {
      try { socket.close(1000, "monitor stopped"); } catch (_) { /* closing */ }
    }
    try { void current.context?.close?.()?.catch?.(() => {}); } catch (_) { /* already closed */ }
    if (session === current) session = null;
    onState(false);
  }

  function fail(current, error) {
    cleanup(current);
    onError(error);
  }

  async function setSink(sinkId) {
    const current = session;
    if (!current || current.done) return false;
    if (typeof current.context?.setSinkId !== "function") {
      throw new Error("This browser cannot select an output device for local monitoring.");
    }
    try { await current.context.setSinkId(sinkId || ""); }
    catch (cause) { throw new Error("Could not select the chosen output device for local monitoring.", { cause }); }
    return session === current && !current.done;
  }

  function socketUrl(url) {
    const target = new URL(url, `${loc?.protocol === "https:" ? "wss:" : "ws:"}//${loc?.host}`);
    if (target.protocol === "http:") target.protocol = "ws:";
    if (target.protocol === "https:") target.protocol = "wss:";
    return target.href;
  }

  async function start({ url, volume = 1, muted = false, sinkId = "" } = {}) {
    stop();
    if (!secure()) throw new Error(LOCAL_MONITOR_INSECURE_TEXT);
    if (!supported()) throw new Error(LOCAL_MONITOR_UNSUPPORTED_TEXT);
    const Context = AudioContextClass();
    const Socket = WebSocketClass();
    const asked = ++generation;
    const current = { done: false, context: null, socket: null, node: null, inFlight: false, bufferedFrames: 0 };
    session = current;
    try {
      current.context = new Context();
      current.context.addEventListener?.("statechange", () => {
        if (!current.done && ["suspended", "interrupted"].includes(current.context.state)) {
          fail(current, new Error("Local monitoring stopped because browser audio was suspended."));
        }
      });
      if (current.context.state === "suspended") await current.context.resume();
      if (current.done || asked !== generation) return false;
      if (current.context.state === "suspended" || current.context.state === "interrupted") {
        throw new Error("The browser audio context is suspended. Resume playback and try local monitoring again.");
      }
      if (typeof current.context.audioWorklet?.addModule !== "function") throw new Error(LOCAL_MONITOR_UNSUPPORTED_TEXT);
      await current.context.audioWorklet.addModule(workletUrl);
      if (current.done || asked !== generation) return false;
      if (sinkId) {
        if (typeof current.context.setSinkId !== "function") throw new Error("This browser cannot select an output device for local monitoring.");
        try { await current.context.setSinkId(sinkId); }
        catch (cause) { throw new Error("Could not select the chosen output device for local monitoring.", { cause }); }
        if (current.done || asked !== generation) return false;
      }
      const Node = AudioWorkletNodeClass();
      current.node = new Node(current.context, "autodj-local-monitor", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2] });
      current.node.connect(current.context.destination);
      current.node.port.postMessage({ level: true, volume: Math.max(0, Math.min(1, Number(volume) || 0)), muted: Boolean(muted) });
      current.node.port.onmessage = (event) => {
        current.inFlight = false;
        current.bufferedFrames = event.data?.bufferedFrames || 0;
      };
      current.socket = new Socket(socketUrl(url));
      current.socket.binaryType = "arraybuffer";
      await new Promise((resolve, reject) => {
        const socket = current.socket;
        socket.addEventListener("open", resolve, { once: true });
        socket.addEventListener("error", () => reject(new Error("Could not connect to the local monitor.")), { once: true });
        socket.addEventListener("close", (event) => reject(new Error(socketProblem(event.code))), { once: true });
      });
      if (current.done || asked !== generation) return false;
      current.socket.addEventListener("message", (event) => {
        if (current.done || current.inFlight || !(event.data instanceof ArrayBuffer) || event.data.byteLength % (4 * CHANNELS)) return;
        current.inFlight = true;
        current.node.port.postMessage(event.data, [event.data]);
      });
      current.socket.addEventListener("close", (event) => { if (!current.done) fail(current, new Error(socketProblem(event.code))); });
      current.socket.addEventListener("error", () => {});
      onState(true);
      return true;
    } catch (error) {
      if (current.done || asked !== generation) return false;
      cleanup(current);
      throw error;
    }
  }

  function stop() {
    generation += 1;
    cleanup(session);
  }

  function setLevel(volume, muted = false) {
    session?.node?.port?.postMessage({ level: true, volume: Math.max(0, Math.min(1, Number(volume) || 0)), muted: Boolean(muted) });
  }

  function isListening() { return Boolean(session && !session.done); }

  return { start, stop, setLevel, setSink, isListening, delaySeconds, supported };
}

