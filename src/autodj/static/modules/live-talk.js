// Live DJ on the air and browser talkover (#48).
//
// The live line: "Live: DJ Alice since 8:15 PM" under the song title while
// a DJ's BUTT or Mixxx feeds the station.  It is plain text, never a live
// region; the change is said once, politely, through the page's status
// region when a DJ goes live and once when they hand back.  Switching
// stations or signing in again shows the new station's line in silence.
//
// Talk: hold to talk over the music.  The microphone is asked for only
// when Talk is pressed, and every track of it is stopped, the socket
// closed and the audio context closed as soon as it is let go.  Ways to
// hold it:
//   - the pointer: pointerdown starts, pointerup, pointercancel or losing
//     the pointer capture lets go;
//   - Space or Enter held on the focused button (auto-repeat ignored, and
//     the native click they would make is suppressed);
//   - Alt+M held anywhere on the page (see the key notes in hotkeys.js),
//     which says "Talking" and "Stopped talking" since focus is elsewhere.
//     Alt+M is hold-only: letting go always stops, however quick the press.
// A press of the button let go within LATCH_MS latches talk on until the
// next press, so a quick press works too: a screen reader in browse mode,
// a switch or a voice command sends a press and release at once, or only a
// click, which toggles talk the same way.  Latched talk is said once, shown
// in words under the button, and turns itself off after LATCH_LIMIT_MS
// (said once), so a microphone left on is never left on the air.  Leaving
// the page (blur, hidden tab) always stops.  aria-pressed says whether
// this page is talking; the name "Talk" never changes.
//
// Audio: an AudioWorklet (from an inline module, so no new static file)
// mixes the microphone to mono and sends little-endian 16-bit PCM at the
// context's own rate to /api/live/mic?rate=<rate> over a same-origin
// WebSocket, about 43 ms per message.  A slow link drops audio rather
// than letting it queue up behind the talker.

import { announceStatus } from "./live-region.js";
import { stationApi } from "./stations.js";

const SAMPLES_PER_MESSAGE = 2048;
// Audio queued in the socket past this is dropped: about 0.7 s at 48 kHz.
const MAX_BUFFERED_BYTES = 65536;
// A click this soon after a press of ours is that same press.
const CLICK_AFTER_HOLD_MS = 600;
// Let go sooner than this and talk stays on until the next press.
const LATCH_MS = 350;
// The server's talkover flag still names this page's own talk for a push
// or two after it ends.
const OWN_TALK_ECHO_MS = 3000;

// Latched talk turns itself off after this long without a press.
const LATCH_LIMIT_MS = 2 * 60 * 1000;
const LATCHED_TEXT = "Talking. Press Talk again to stop. Talk turns off by itself after 2 minutes.";
export const LATCHED_NOTE = "Talking until you press Talk again, for 2 minutes at most.";
const LATCH_TIMEOUT_TEXT = "Talk turned off after 2 minutes.";

export const TALK_INSECURE_TEXT = "Talking needs a secure connection: open AutoDJ over HTTPS, or on this computer at localhost.";
const TALK_UNSUPPORTED_TEXT = "This browser cannot send the microphone. Try a current version of Firefox, Chrome, Edge or Safari.";

const WORKLET_SOURCE = `
class AutodjTalkCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(${SAMPLES_PER_MESSAGE});
    this.count = 0;
  }

  push(value) {
    const sample = value > 1 ? 1 : value < -1 ? -1 : (value || 0);
    this.samples[this.count++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    if (this.count === this.samples.length) {
      this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
      this.samples = new Int16Array(${SAMPLES_PER_MESSAGE});
      this.count = 0;
    }
  }

  process(inputs) {
    const channels = inputs[0] || [];
    const frames = channels[0] ? channels[0].length : 128;
    for (let i = 0; i < frames; i += 1) {
      let sum = 0;
      for (const channel of channels) sum += channel[i];
      this.push(channels.length ? sum / channels.length : 0);
    }
    return true;
  }
}
registerProcessor("autodj-talk-capture", AutodjTalkCapture);
`;

// "8:15 PM" in the reader's own clock.
function localTime(iso, locale) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" });
}

// The live line for *live* ({dj, since} or null): "" when automation plays.
export function liveLine(live, locale = undefined) {
  if (!live || typeof live.dj !== "string" || !live.dj) return "";
  const since = localTime(live.since, locale);
  return since ? `Live: ${live.dj} since ${since}` : `Live: ${live.dj}`;
}

// Why the server closed the talk socket, in plain words; "" for a close
// this page asked for.
export function talkCloseProblem(code) {
  switch (code) {
    case 1000:
    case 1005:
      return "";
    case 4401:
      return "Talk stopped: sign in again, then hold Talk.";
    case 4403:
      return "Talk stopped: the server does not accept the microphone from this page's address.";
    case 4404:
      return "Talk stopped: this station is not available.";
    case 4409:
      return "Could not talk: someone else is talking on this station, or it is not playing on the server.";
    case 4000:
      return "Talk stopped: no microphone sound reached the server for 2 seconds.";
    case 1003:
    case 1009:
      return "Talk stopped: the server refused the microphone audio.";
    default:
      return "Talk stopped: the connection to the server dropped.";
  }
}

// Why the microphone could not start.
export function talkMicrophoneProblem(error) {
  switch (error?.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "The browser blocked the microphone. Allow this site to use the microphone, then hold Talk again.";
    case "NotFoundError":
    case "OverconstrainedError":
      return "No microphone was found. Connect one, then hold Talk again.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone could not start. Another program may be using it.";
    default:
      return "The microphone could not start.";
  }
}

export function installLiveDesk({
  doc = globalThis.document,
  srStatus = doc.getElementById("sr-status"),
  media = () => globalThis.navigator?.mediaDevices,
  AudioContextClass = () => globalThis.AudioContext || globalThis.webkitAudioContext,
  WebSocketClass = () => globalThis.WebSocket,
  secure = () => globalThis.isSecureContext !== false,
  location: loc = globalThis.location,
  now = () => Date.now(),
  // When a key or pointer event happened.  The event's own time, not the
  // time it is handled: a busy page handles a quick press's key up late,
  // and that must not turn the press into a hold.
  eventTime = (event) => event.timeStamp,
  setTimer = (callback, ms) => setTimeout(callback, ms),
  clearTimer = (id) => clearTimeout(id),
  locale = undefined,
} = {}) {
  const line = doc.getElementById("live-on-air");
  const desk = doc.getElementById("talk-desk");
  const button = doc.getElementById("btn-talk");
  const note = doc.getElementById("talk-note");
  const error = doc.getElementById("talk-error");
  const help = doc.getElementById("talk-help");
  const latchedNote = doc.getElementById("talk-latched");
  if (!line || !desk || !button || !note || !error || !help || !latchedNote) return null;
  const win = doc.defaultView || globalThis;

  let available = false;
  let reason = "";
  let busy = false;
  let lastKey = null;
  let session = null;
  let generation = 0;
  let workletUrl = null;
  let keyHeld = null;
  let hotkeyHeld = false;
  let pointerId = null;
  let pressedAt = -Infinity;
  let pressedStamp = -Infinity;
  let lastReleased = -Infinity;
  let pressStops = false;
  let ownEnded = -Infinity;

  const othersTalking = () => busy && !session && now() - ownEnded > OWN_TALK_ECHO_MS;
  const say = (message) => announceStatus(srStatus, message, { force: true, dwellMs: 5000 });

  function describe() {
    const ids = [help.id];
    if (!latchedNote.hidden && latchedNote.textContent) ids.push(latchedNote.id);
    if (!note.hidden && note.textContent) ids.push(note.id);
    if (!error.hidden && error.textContent) ids.push(error.id);
    if (doc.getElementById("station-context")) ids.push("station-context");
    button.setAttribute("aria-describedby", ids.join(" "));
  }

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
    node.hidden = !text;
  }

  function showError(text) {
    setText(error, text);
    describe();
    if (text) announceStatus(srStatus, text, { force: true, tone: "error", dwellMs: 6000 });
  }

  // Shown, not said: who may talk now.
  function renderAvailability() {
    // Talk shows only while the station can go live; Settings > Live DJ
    // says why it cannot.
    if (desk.hidden === available) desk.hidden = !available;
    const blocked = othersTalking();
    if (blocked) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
    setText(note, blocked ? "Someone else is talking on this station from another browser." : "");
    describe();
  }

  function renderPressed() {
    const talking = Boolean(session?.streaming);
    const value = talking ? "true" : "false";
    if (button.getAttribute("aria-pressed") !== value) button.setAttribute("aria-pressed", value);
    // Shown, not live: the latch was said when it began.
    setText(latchedNote, talking && session.latched ? LATCHED_NOTE : "");
    describe();
  }

  // Talk stays on after a quick press, for LATCH_LIMIT_MS at most.
  function latch(current) {
    if (!current || current.done || current.latched) return;
    current.latched = true;
    current.spoken = true;
    current.latchTimer = setTimer(() => {
      if (current.done) return;
      current.spoken = false;
      teardown(current);
      say(LATCH_TIMEOUT_TEXT);
    }, LATCH_LIMIT_MS);
    renderPressed();
  }

  function socketUrl(rate) {
    const path = stationApi(`/api/live/mic?rate=${encodeURIComponent(String(rate))}`);
    const scheme = loc?.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${loc?.host}${path}`;
  }

  function moduleUrl() {
    if (!workletUrl) {
      const blob = new win.Blob([WORKLET_SOURCE], { type: "text/javascript" });
      workletUrl = win.URL.createObjectURL(blob);
    }
    return workletUrl;
  }

  // Lets everything go: the socket, the microphone and the audio context.
  // Stops whatever *current* holds now; safe to call again, as a
  // microphone or socket that arrives after talk ended must go too.
  function freeResources(current) {
    try { current.node?.port?.close?.(); } catch (_) { /* gone */ }
    try { current.node?.disconnect?.(); } catch (_) { /* gone */ }
    try { current.source?.disconnect?.(); } catch (_) { /* gone */ }
    current.node = null;
    current.source = null;
    for (const track of current.stream?.getTracks?.() || []) {
      try { track.stop(); } catch (_) { /* already stopped */ }
    }
    current.stream = null;
    const socket = current.socket;
    if (socket && socket.readyState < 2) {
      try { socket.close(1000, "released"); } catch (_) { /* closing */ }
    }
    current.socket = null;
    try { void current.context?.close?.()?.catch?.(() => {}); } catch (_) { /* closed */ }
    current.context = null;
  }

  function teardown(current, problem = "") {
    if (!current) return;
    if (current.done) {
      freeResources(current);
      return;
    }
    current.done = true;
    if (current.latchTimer !== undefined) clearTimer(current.latchTimer);
    if (session === current) session = null;
    freeResources(current);
    const wasStreaming = current.streaming;
    current.streaming = false;
    if (wasStreaming) ownEnded = now();
    renderPressed();
    renderAvailability();
    if (problem) showError(problem);
    else if (wasStreaming && current.spoken) say("Stopped talking.");
  }

  async function start(how) {
    if (session) return;
    if (!available) {
      showError(reason || "Talk is not available on this station.");
      return;
    }
    if (othersTalking()) {
      showError("Could not talk: someone else is talking on this station from another browser.");
      return;
    }
    const devices = media();
    const Context = AudioContextClass();
    const Socket = WebSocketClass();
    if (typeof devices?.getUserMedia !== "function") {
      showError(secure() ? TALK_UNSUPPORTED_TEXT : TALK_INSECURE_TEXT);
      return;
    }
    if (typeof Context !== "function" || typeof Socket !== "function") {
      showError(TALK_UNSUPPORTED_TEXT);
      return;
    }
    showError("");
    const current = {
      how, done: false, streaming: false, spoken: how === "hotkey",
      stream: null, context: null, socket: null, source: null, node: null,
    };
    session = current;
    const asked = generation;
    try {
      // Made while the press is still the user's gesture, so it may run.
      current.context = new Context();
      void current.context.resume?.()?.catch?.(() => {});
    } catch (_) {
      teardown(current, TALK_UNSUPPORTED_TEXT);
      return;
    }
    try {
      current.stream = await devices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (problem) {
      teardown(current, current.done ? "" : talkMicrophoneProblem(problem));
      return;
    }
    if (current.done || asked !== generation) {
      teardown(current);
      return;
    }
    try {
      if (typeof current.context.audioWorklet?.addModule !== "function") throw new Error("no worklet");
      await current.context.audioWorklet.addModule(moduleUrl());
    } catch (_) {
      teardown(current, current.done ? "" : TALK_UNSUPPORTED_TEXT);
      return;
    }
    if (current.done || asked !== generation) {
      teardown(current);
      return;
    }
    let socket;
    try {
      socket = new Socket(socketUrl(current.context.sampleRate));
    } catch (_) {
      teardown(current, talkCloseProblem(0));
      return;
    }
    socket.binaryType = "arraybuffer";
    current.socket = socket;
    socket.addEventListener("open", () => {
      if (current.done) return;
      try {
        const WorkletNode = win.AudioWorkletNode;
        current.source = current.context.createMediaStreamSource(current.stream);
        // No outputs: nothing reaches the speakers, and a node with no
        // outputs is run for its input alone (Chrome's "automatic pull").
        current.node = new WorkletNode(current.context, "autodj-talk-capture", {
          numberOfInputs: 1, numberOfOutputs: 0,
        });
        current.source.connect(current.node);
        current.node.port.onmessage = (event) => {
          if (current.done || socket.readyState !== 1) return;
          if (socket.bufferedAmount > MAX_BUFFERED_BYTES) return;
          socket.send(event.data);
        };
      } catch (_) {
        teardown(current, TALK_UNSUPPORTED_TEXT);
        return;
      }
      current.streaming = true;
      renderPressed();
      if (current.latched) say(LATCHED_TEXT);
      else if (current.spoken) say("Talking.");
    });
    socket.addEventListener("close", (event) => {
      if (current.done) return;
      teardown(current, talkCloseProblem(event.code === 1000 ? 0 : event.code));
    });
    socket.addEventListener("error", () => {
      // A close event follows with its code.
    });
  }

  function stop() {
    if (session) teardown(session);
  }

  // A press of the button, Space, Enter or Alt+M.  A press while latched
  // talk is on stops it; otherwise it starts talking.
  function press(how, event) {
    pressedAt = now();
    pressedStamp = eventTime(event);
    if (session) {
      pressStops = true;
      if (session.latched || how === "toggle") stop();
      return;
    }
    pressStops = false;
    void start(how);
  }

  // Letting go: a short press latches talk on, a longer one was the hold.
  function release(event) {
    lastReleased = now();
    if (pressStops || !session) {
      pressStops = false;
      return;
    }
    // Alt+M is hold-only: a quick press never leaves the microphone on.
    if (session.how !== "hotkey" && eventTime(event) - pressedStamp < LATCH_MS) {
      const current = session;
      latch(current);
      if (current.streaming) say(LATCHED_TEXT);
      return;
    }
    stop();
  }

  // ---- The button: pointer, Space and Enter, or a bare click ----

  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || pointerId !== null) return;
    pointerId = event.pointerId;
    try { button.setPointerCapture?.(event.pointerId); } catch (_) { /* not capturable */ }
    event.preventDefault();
    button.focus?.();
    press("pointer", event);
  });
  const pointerEnded = (event) => {
    if (pointerId === null || event.pointerId !== pointerId) return;
    pointerId = null;
    release(event);
  };
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(type, pointerEnded);
  }
  button.addEventListener("keydown", (event) => {
    if (event.key !== " " && event.key !== "Enter") return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    if (event.repeat || keyHeld) return;
    keyHeld = event.key;
    press("key", event);
  });
  button.addEventListener("keyup", (event) => {
    if (keyHeld === null || event.key !== keyHeld) return;
    event.preventDefault();
    keyHeld = null;
    release(event);
  });
  button.addEventListener("blur", (event) => {
    if (keyHeld !== null) {
      keyHeld = null;
      release(event);
    }
  });
  // A click with no press of ours just before it (a screen reader in
  // browse mode, a switch, a voice command) turns talk on or off.
  button.addEventListener("click", (event) => {
    event.preventDefault();
    if (pointerId !== null || keyHeld !== null) return;
    if (now() - Math.max(pressedAt, lastReleased) < CLICK_AFTER_HOLD_MS) return;
    pressedAt = now();
    if (session) {
      stop();
      return;
    }
    void start("toggle");
    latch(session);
  });

  // ---- Alt+M anywhere ----

  doc.addEventListener("keydown", (event) => {
    if (event.code !== "KeyM" || !event.altKey) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (desk.hidden || doc.querySelector("dialog[open]")) return;
    event.preventDefault();
    if (event.repeat || hotkeyHeld) return;
    hotkeyHeld = true;
    press(doc.activeElement === button ? "key" : "hotkey", event);
  });
  doc.addEventListener("keyup", (event) => {
    if (!hotkeyHeld || (event.code !== "KeyM" && event.key !== "Alt")) return;
    event.preventDefault();
    hotkeyHeld = false;
    release(event);
  });

  const letGo = () => {
    keyHeld = null;
    hotkeyHeld = false;
    pointerId = null;
    stop();
  };
  win.addEventListener?.("blur", letGo);
  doc.addEventListener("visibilitychange", () => {
    if (doc.visibilityState === "hidden") letGo();
  });

  renderAvailability();
  renderPressed();

  return {
    // GET /api/live's answer (or null when it failed).
    setInfo(info) {
      available = Boolean(info?.available);
      reason = available ? "" : (typeof info?.reason === "string" ? info.reason : "");
      if (!available) letGo();
      if (info) this.applyState(info);
      else renderAvailability();
    },
    // A state push or status: {live, talkover}.
    applyState(state) {
      if (!state || typeof state !== "object") return;
      if ("talkover" in state) busy = Boolean(state.talkover);
      if ("live" in state) {
        const text = liveLine(state.live, locale);
        const key = text ? `${state.live.dj}\u0000${state.live.since}` : "";
        setText(line, text);
        if (lastKey !== null && key !== lastKey) {
          say(text ? `${text}.` : "The live DJ has left. Automation is back on the air.");
        }
        lastKey = key;
      }
      renderAvailability();
    },
    get talking() {
      return Boolean(session?.streaming);
    },
    stop: letGo,
    // A station switch or a session that ended: stop talking and forget
    // the line, so the next station's line is shown in silence.
    clear() {
      generation += 1;
      letGo();
      lastKey = null;
      busy = false;
      available = false;
      reason = "";
      setText(line, "");
      showError("");
      renderAvailability();
    },
  };
}
