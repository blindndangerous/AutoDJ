// Voice tracker (#128): record a DJ break in the browser for the open song
// change and book it, as an announcer program books one (#88).
//
// Everything is found inside the root element.  The server names the open
// song change (the one after the next song: the next one is already
// rendered), how long is left to book it, and the voice tracks booked on
// this station; the page asks again every few seconds while the tab shows.
//
// Recording: the microphone is asked for only when Record is pressed, and
// every track of the stream is stopped as soon as the recording ends, is
// discarded or the page signs out.  Leaving the tab or hiding the page
// stops a recording too, keeping the take.  A take remembers the two songs
// it was recorded for: it is booked on other songs only after it has been
// listened to again and Book pressed again.  The take stays in the page, to listen
// back, record again or discard; Book voice track turns it into a mono WAV
// at the playout rate (the browser decodes its own recording) and uploads
// it through the signed-in session.  The server checks, caps and decodes it
// as it does an announcer's break.
//
// Speech: the recording state is said once, politely, through the tracker's
// status region, at start and stop, then once a minute and 30 seconds
// before the length limit or the song change closing.  The timer and the
// time left are on the page and never live.  Focus stays on the control
// that was used; Alt+R starts and stops recording from anywhere while the
// Voice tracks tab shows, without moving focus.  Errors are words on the
// page, linked to the control they are about, and said once.

import { requestJson, withDisabled } from "./api-client.js";
import { fmtTime, nextRowControl, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { defaultStorage, readStorage, removeStorage, writeStorage } from "./safe-storage.js";

const POLL_TICKS = 5;
const WARN_SECONDS = 30;
const MAX_LISTED = 50;
// Room for the form around the file, under the station's upload cap.
const FORM_BYTES = 4096;
const WAV_HEADER_BYTES = 44;
const FOCUS_SETTLE_MS = 150;
const FOCUS_WAIT_MS = 3000;

export const NO_RECORDER_TEXT = "This browser cannot record audio. Try a current version of Firefox, Chrome, Edge or Safari.";
export const INSECURE_TEXT = "Recording needs a secure connection: open AutoDJ over HTTPS, or on this computer at localhost.";
const MICROPHONE_KEY = "autodj.voiceMicrophoneId";
const MICROPHONE_LABEL_KEY = "autodj.voiceMicrophoneLabel";

// 65.4 seconds is "1:05".
export function clock(seconds) {
  return fmtTime(Math.round(Number(seconds) || 0));
}

// Round longer waits to whole minutes, so a list
// that is read again a few seconds later says the same thing.
function roughly(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  return clock(value >= 90 ? Math.round(value / 60) * 60 : value);
}

// An intro's length to a tenth of a second: "8.5 seconds", "1 second".
function introWords(seconds) {
  const value = Math.round(Math.max(0, Number(seconds) || 0) * 10) / 10;
  return `${value} second${value === 1 ? "" : "s"}`;
}

// Why the microphone could not start, in plain words.
export function microphoneProblem(error) {
  switch (error?.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "The browser blocked the microphone. Allow this site to use the microphone, then press Record again.";
    case "NotFoundError":
    case "OverconstrainedError":
      return "No microphone was found. Connect one, then press Record again.";
    case "NotReadableError":
    case "AbortError":
      return "The microphone could not start. Another program may be using it.";
    default:
      return "The microphone could not start.";
  }
}

// Booking decodes the take in the page (decodeAudioData has no streaming form).
export const BROWSER_MAX_SECONDS = 600;
// Frames converted between pauses that let the page handle input.
const WAV_BLOCK_FRAMES = 44100 * 10;

function wavHeader(view, frames, rate) {
  const text = (offset, value) => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + frames * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, frames * 2, true);
}

// Frames *from* to *to* of *channels*, averaged to one, as 16-bit samples
// straight into the WAV: no mono copy of the take is made.
function writeFrames(view, channels, from, to, offset = WAV_HEADER_BYTES) {
  for (let i = from; i < to; i += 1) {
    let sum = 0;
    for (const channel of channels) sum += channel[i];
    const value = sum / channels.length;
    const sample = Math.max(-1, Math.min(1, Number.isFinite(value) ? value : 0));
    view.setInt16(offset + (i - from) * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
}

// A mono 16-bit PCM WAV file at *rate* Hz of *channels* (arrays of floats
// from -1 to 1, all the same length), averaged to one channel.
export function encodeWav(channels, rate) {
  const frames = channels[0]?.length || 0;
  const buffer = new ArrayBuffer(WAV_HEADER_BYTES + frames * 2);
  const view = new DataView(buffer);
  wavHeader(view, frames, rate);
  writeFrames(view, channels, 0, frames);
  return buffer;
}

// The recording as a WAV file at *rate* Hz.  The browser decodes its own
// recording (WebM, Ogg or MP4) and resamples it to the context's rate;
// the channels are then averaged into the WAV ten seconds at a time,
// with a pause between blocks so the page stays responsive.
export async function recordingToWav(
  blob, rate, Context = globalThis.OfflineAudioContext, pause = () => new Promise((resolve) => setTimeout(resolve, 0)),
  onDecoded = () => {},
) {
  const audio = await new Context(1, 1, rate).decodeAudioData(await blob.arrayBuffer());
  const channels = Array.from({ length: audio.numberOfChannels }, (_, i) => audio.getChannelData(i));
  const frames = audio.length;
  onDecoded(speechLength(channels, audio.sampleRate));
  const parts = [];
  const header = new ArrayBuffer(WAV_HEADER_BYTES);
  wavHeader(new DataView(header), frames, audio.sampleRate);
  parts.push(header);
  for (let from = 0; from < frames; from += WAV_BLOCK_FRAMES) {
    const to = Math.min(frames, from + WAV_BLOCK_FRAMES);
    const buffer = new ArrayBuffer((to - from) * 2);
    const view = new DataView(buffer);
    writeFrames(view, channels, from, to, 0);
    parts.push(new Blob([buffer]));
    if (from + WAV_BLOCK_FRAMES < frames) await pause();
  }
  return new Blob(parts, { type: "audio/wav" });
}

// Match the server's 20 ms, relative -45 dB speech-edge estimate; pauses stay.
export function speechLength(channels, rate) {
  const frame = Math.max(1, Math.round(rate * 0.02));
  const levels = [];
  let peak = 0;
  for (let from = 0; from < (channels[0]?.length || 0); from += frame) {
    let level = 0;
    for (const channel of channels) {
      let sum = 0;
      for (let i = from; i < Math.min(channel.length, from + frame); i += 1) sum += channel[i] ** 2;
      level = Math.max(level, Math.sqrt(sum / frame));
    }
    levels.push(level);
    peak = Math.max(peak, level);
  }
  if (peak < 1e-4) return { speech: 0 };
  const threshold = peak * 10 ** (-45 / 20);
  const first = levels.findIndex((level) => level >= threshold);
  const last = levels.findLastIndex((level) => level >= threshold);
  const start = Math.max(0, first * frame / rate - 0.05);
  const segue = Math.min(channels[0].length, (last + 1) * frame) / rate;
  const end = Math.min(channels[0].length / rate, segue + 0.4);
  return { speech: segue - start, clip: end - start, tail: end - segue };
}

// *text* as a sentence: the server's reasons have no full stop.
function sentence(text) {
  const trimmed = String(text || "").trim();
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

function closedWords(reason) {
  return `No song change can take a voice track now. ${sentence(reason)}`;
}

function songWords(song) {
  if (!song) return "an unknown song";
  if (song.artist && song.title) return `${song.artist}, ${song.title}`;
  return song.title || song.artist || "an untitled song";
}

function pairWords(slot) {
  return `${slot?.ending?.title || "an untitled song"} into ${slot?.upcoming?.title || "an untitled song"}`;
}

function stateWords(track, slot, wallNow) {
  const kind = track.mixed_as === "talk_up" ? "a talk-up" : "a standalone break";
  switch (track.state) {
    case "queued": {
      const airs = slot && slot.slot_id === track.slot_id ? `, airs in about ${roughly(slot.airs_in_s)}` : "";
      return `booked, waiting for its song change${airs}`;
    }
    case "rendered": {
      const airs = track.eta != null ? `, airs in about ${roughly(track.eta - wallNow)}` : "";
      return `mixed in as ${kind}${airs}`;
    }
    case "on_air":
      return "on the air now";
    case "aired":
      return "aired";
    case "dropped":
      return `dropped: ${track.reason || "it will not air"}`;
    case "expired":
      return `expired: ${track.reason || "it was too late to air"}`;
    default:
      return track.state;
  }
}

export function installVoiceTracker(root = document.getElementById("voice-tracker"), {
  media = () => globalThis.navigator?.mediaDevices,
  Recorder = () => globalThis.MediaRecorder,
  secure = () => globalThis.isSecureContext !== false,
  toWav = recordingToWav,
  now = () => globalThis.performance.now(),
  wallClock = () => Date.now() / 1000,
  interval = setInterval,
  objectUrl = (blob) => URL.createObjectURL(blob),
  revokeUrl = (url) => URL.revokeObjectURL(url),
  storage = defaultStorage(),
} = {}) {
  if (!root) return null;
  const doc = root.ownerDocument;
  const el = (id) => root.querySelector(`#${id}`);
  const panel = root.closest("[role='tabpanel']") || root;
  const unavailable = el("vt-unavailable");
  const slotList = el("vt-slot");
  const ending = el("vt-ending");
  const upcoming = el("vt-upcoming");
  const intro = el("vt-intro");
  const outro = el("vt-outro");
  const windowText = el("vt-window");
  const closes = el("vt-closes");
  const airs = el("vt-airs");
  const recordButton = el("vt-record");
  const microphoneSelect = el("vt-microphone");
  const microphoneRefresh = el("vt-microphone-refresh");
  const microphoneStatus = el("vt-microphone-status");
  const testButton = el("vt-microphone-test");
  const testPlayback = el("vt-microphone-playback");
  const timer = el("vt-timer");
  const playback = el("vt-playback");
  const placement = el("vt-placement");
  const listenButton = el("vt-listen");
  const bookButton = el("vt-book");
  const discardButton = el("vt-discard");
  const error = el("vt-error");
  const status = el("vt-status");
  const list = el("vt-list");
  const none = el("vt-none");
  const listHeading = el("vt-booked-heading");
  const transition = el("vt-transition");
  const transitionSummary = el("vt-transition-summary");
  const transitionItems = el("vt-transition-items");

  let data = null;
  let fetchedAt = 0;
  let requestEpoch = 0;
  let ticks = 0;
  let take = null;
  let session = null;
  let starting = false;
  let testing = null;
  let testStarting = false;
  let pendingTestStream = null;
  let testGeneration = 0;
  let microphoneChoices = [];
  let microphoneListKnown = false;
  let microphoneEpoch = 0;
  let booking = false;
  let bookingAbort = null;
  let selectedMicrophone = {
    id: readStorage(storage, MICROPHONE_KEY) || "",
    label: readStorage(storage, MICROPHONE_LABEL_KEY) || "",
  };
  const stoppedTracks = new WeakSet();
  let generation = 0;
  let linked = null;
  let wasShown = false;
  const rows = new Map();
  const transitionChoices = new Map();
  let transitionSlotId = null;

  const shown = () => !panel.hidden;
  // The tracker's own region while its tab shows; the page-wide one when
  // the tab has been left, since a region inside a hidden panel says nothing.
  const say = (message, { tone = "info", mirror = true } = {}) => announceStatus(
    shown() ? status : (doc.getElementById("sr-status") || status),
    message,
    { force: true, tone, mirror, dwellMs: 5000 },
  );

  function afterFocus() {
    const view = doc.defaultView;
    const pause = () => new Promise((resolve) => setTimeout(resolve, FOCUS_SETTLE_MS));
    if (!view || typeof doc.hasFocus !== "function" || doc.hasFocus()) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        view.removeEventListener("focus", done);
        resolve(pause());
      };
      const timer = setTimeout(done, FOCUS_WAIT_MS);
      view.addEventListener("focus", done, { once: true });
    });
  }

  // ---- Errors: words on the page, linked to the control, said once ----

  function link(control, on) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== error.id);
    if (on) ids.unshift(error.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  function clearError() {
    if (linked) link(linked, false);
    linked = null;
    error.textContent = "";
    error.hidden = true;
  }

  function microphoneLabel(id, label = "") {
    if (!id) return "System default";
    return microphoneChoices.find((item) => item.deviceId === id)?.label
      || label || "the selected microphone";
  }

  function renderMicrophones(missing = null) {
    if (!microphoneSelect) return;
    const choices = [{ id: "", label: "System default" }, ...microphoneChoices.map((item) => ({
      id: item.deviceId,
      label: item.label || `Microphone ${microphoneChoices.indexOf(item) + 1}`,
    }))];
    if (selectedMicrophone.id && !choices.some((item) => item.id === selectedMicrophone.id)) {
      choices.push({
        id: selectedMicrophone.id,
        label: `${selectedMicrophone.label || "Saved microphone"}${missing === null ? "" : ` (${missing ? "not connected" : "unavailable"})`}`,
      });
    }
    microphoneSelect.replaceChildren(...choices.map((choice) => {
      const option = doc.createElement("option");
      option.value = choice.id;
      option.textContent = choice.label;
      return option;
    }));
    microphoneSelect.value = selectedMicrophone.id;
  }

  function microphoneNote(message) {
    setText(microphoneStatus, message);
    if (microphoneStatus) microphoneStatus.hidden = !message;
    if (microphoneSelect) {
      if (message) microphoneSelect.setAttribute("aria-describedby", "vt-microphone-status");
      else microphoneSelect.removeAttribute("aria-describedby");
    }
  }

  async function refreshMicrophones() {
    const epoch = ++microphoneEpoch;
    let devices;
    try {
      devices = await media()?.enumerateDevices?.();
    } catch {
      devices = null;
    }
    if (epoch !== microphoneEpoch) return;
    if (Array.isArray(devices)) {
      microphoneChoices = devices.filter((item) => item.kind === "audioinput" && item.deviceId && item.deviceId !== "default");
      microphoneListKnown ||= devices.some((item) => item.kind === "audioinput" && item.label);
    }
    const present = !selectedMicrophone.id || microphoneChoices.some((item) => item.deviceId === selectedMicrophone.id);
    renderMicrophones(microphoneListKnown ? !present : null);
    if (selectedMicrophone.id && microphoneListKnown && !present) {
      microphoneNote("The selected microphone is not connected. Choose another microphone or System default.");
    } else if (!testing && !session) microphoneNote("");
    const activeId = testing?.stream?.getAudioTracks?.()[0]?.getSettings?.()?.deviceId;
    if (testing && activeId && microphoneListKnown && !microphoneChoices.some((item) => item.deviceId === activeId)) {
      stopMicrophoneTest("The microphone disconnected, so the microphone test stopped. Choose another input.");
      microphoneNote("The microphone disconnected during the test. Choose another input or System default.");
    }
  }

  function saveMicrophone(id) {
    const device = microphoneChoices.find((item) => item.deviceId === id);
    selectedMicrophone = { id, label: id ? (device?.label || (selectedMicrophone.id === id ? selectedMicrophone.label : "")) : "" };
    if (id) writeStorage(storage, MICROPHONE_KEY, id);
    else removeStorage(storage, MICROPHONE_KEY);
    if (id && selectedMicrophone.label) writeStorage(storage, MICROPHONE_LABEL_KEY, selectedMicrophone.label);
    else removeStorage(storage, MICROPHONE_LABEL_KEY);
    renderMicrophones();
    microphoneNote("");
  }

  function requestedMicrophone() {
    const audio = {
      channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false,
    };
    if (selectedMicrophone.id) audio.deviceId = { exact: selectedMicrophone.id };
    return { audio };
  }

  function microphoneWords(stream, prefix) {
    const settings = stream?.getAudioTracks?.()[0]?.getSettings?.();
    const actual = settings?.deviceId;
    const confirmed = actual && actual !== "default";
    const label = confirmed && (microphoneChoices.find((item) => item.deviceId === actual)?.label
      || (actual === selectedMicrophone.id ? selectedMicrophone.label : ""));
    return confirmed
      ? label ? `${prefix} ${label}.` : `${prefix} microphone; the browser confirmed its device ID but cannot provide its name.`
      : prefix === "Recording from"
        ? "The browser cannot confirm which microphone was used."
        : `${prefix} The browser cannot confirm which microphone is active.`;
  }

  function reportMicrophone(stream, prefix) {
    microphoneNote(microphoneWords(stream, prefix));
  }

  function fail(control, message, gate = null) {
    clearError();
    error.textContent = message;
    error.hidden = false;
    link(control, true);
    linked = control;
    if (gate) void Promise.resolve(gate).then(() => say(message, { tone: "error" }));
    else say(message, { tone: "error" });
  }

  // ---- What the server says ----

  const rate = () => data?.limits?.sample_rate || 44100;

  // The longest recording that fits both the station's length and its
  // upload cap as a 16-bit mono WAV.
  function limitSeconds() {
    const limits = data?.limits;
    if (!limits) return BROWSER_MAX_SECONDS;
    const bySize = Math.floor((limits.max_bytes - FORM_BYTES - WAV_HEADER_BYTES) / (2 * rate()));
    return Math.max(1, Math.min(limits.max_seconds, bySize, BROWSER_MAX_SECONDS));
  }

  // Seconds left to book the open change, counting down from the last answer.
  function closesLeft() {
    if (!data?.slot) return null;
    return Math.max(0, data.slot.closes_in_s - (now() - fetchedAt) / 1000);
  }

  function setText(node, value) {
    if (node && node.textContent !== value) node.textContent = value;
  }

  function renderSlot() {
    const slot = data?.slot;
    renderTransition(slot);
    if (!slot) {
      slotList.hidden = true;
      unavailable.hidden = false;
      setText(unavailable, data?.reason ? closedWords(data.reason) : "Loading the open song change.");
      renderPlacement();
      return;
    }
    unavailable.hidden = true;
    slotList.hidden = false;
    setText(ending, songWords(slot.ending));
    setText(upcoming, songWords(slot.upcoming));
    setText(intro, slot.upcoming_intro_s == null ? "No intro point" : introWords(slot.upcoming_intro_s));
    renderTimes();
    renderPlacement();
  }

  function renderTransition(slot) {
    if (!transition || !transitionItems || !transitionSummary) return;
    if (transitionSlotId !== slot?.slot_id) {
      transitionChoices.clear();
      transitionSlotId = slot?.slot_id || null;
    }
    const active = doc.activeElement;
    const hadFocus = transitionItems.contains(active);
    const items = slot?.transition_items || [];
    transition.hidden = !items.length;
    const wanted = new Set(items.map((item) => item.id));
    for (const row of [...transitionItems.children]) {
      if (!wanted.has(row.dataset.transitionId)) {
        transitionChoices.delete(row.dataset.transitionId);
        row.remove();
      }
    }
    for (const item of items) {
      let row = [...transitionItems.children].find((child) => child.dataset.transitionId === item.id);
      if (!row) {
        row = doc.createElement("li");
        row.dataset.transitionId = item.id;
        const text = doc.createElement("span");
        const label = doc.createElement("label");
        const select = doc.createElement("select");
        select.id = `vt-transition-choice-${item.id}`;
        label.htmlFor = select.id;
        for (const [value, title] of [["replace", "Replace"], ["before", "Before"], ["after", "After"]]) {
          const option = doc.createElement("option");
          option.value = value;
          option.textContent = title;
          select.append(option);
        }
        select.addEventListener("change", () => {
          transitionChoices.set(item.id, select.value);
          renderTransition(data?.slot);
        });
        row.append(text, doc.createTextNode(" "), label, doc.createTextNode(" "), select);
        transitionItems.append(row);
      }
      const text = row.querySelector("span");
      const label = row.querySelector("label");
      const select = row.querySelector("select");
      setText(label, `Position for ${item.label || "transition audio"}:`);
      const action = transitionChoices.get(item.id) || item.current_action || item.default_action || "replace";
      const deferred = item.editable && action === "replace";
      const retained = item.source === "queued" || item.source === "clockwheel";
      const state = deferred
        ? retained ? " (deferred; remains queued)" : " (skipped in this transition)"
        : item.committed ? " (committed)" : " (predicted)";
      setText(text, `${item.label || "Transition audio"}${state}`);
      select.hidden = !item.editable;
      label.hidden = !item.editable;
      if (item.editable) {
        if (!transitionChoices.has(item.id)) transitionChoices.set(item.id, action);
        select.value = transitionChoices.get(item.id);
      }
    }
    const planned = effectiveTransitionOrder(slot);
    if (planned) {
      const rows = new Map([...transitionItems.children].map((row) => [row.dataset.transitionId, row]));
      let next = null;
      const order = [...planned.order, ...items.filter((item) =>
        item.editable && actionFor(item) === "replace").map((item) => item.id)];
      for (const id of order.toReversed()) {
        const row = rows.get(id);
        if (!row) continue;
        // Moving the focused native select resets WebKit's keyboard state.
        if (!row.contains(active) && row.nextElementSibling !== next) {
          transitionItems.insertBefore(row, next);
        }
        next = row;
      }
    }
    if (hadFocus && doc.activeElement !== active && active.isConnected) active.focus();
    updateTransitionSummary(slot);
  }

  function actionFor(item) {
    return transitionChoices.get(item.id) || item.current_action || item.default_action || "replace";
  }

  function effectiveTransitionOrder(slot) {
    if (!slot) return null;
    const items = slot.transition_items || [];
    const byId = new Map(items.map((item) => [item.id, item]));
    const kept = items.filter((item) => !item.editable || actionFor(item) !== "replace");
    const order = (slot.transition_order || items.map((item) => item.id))
      .filter((id) => byId.has(id) && kept.includes(byId.get(id)));
    for (const item of kept) {
      if (!order.includes(item.id)) order.push(item.id);
    }
    let lower = 0;
    let upper = order.length;
    for (const item of items.filter((candidate) => candidate.editable && actionFor(candidate) !== "replace")) {
      const index = order.indexOf(item.id);
      const action = actionFor(item);
      if (action === "after") lower = Math.max(lower, index + 1);
      if (action === "before") upper = Math.min(upper, index);
    }
    if (lower > upper) return null;
    order.splice(lower, 0, "new-voice-track");
    return { order };
  }

  function updateTransitionSummary(slot) {
    if (!slot || !transitionSummary) return;
    const planned = effectiveTransitionOrder(slot);
    if (!planned) {
      setText(transitionSummary, "These choices conflict. Choose a consistent order before booking.");
      return;
    }
    const byId = new Map((slot.transition_items || []).map((item) => [item.id, item]));
    const labels = planned.order.map((id) => id === "new-voice-track"
      ? "Your new voice track" : byId.get(id)?.label || "Booked break");
    setText(transitionSummary, `Planned order: ${labels.join(", ")}.`);
  }

  function renderTimes() {
    const slot = data?.slot;
    if (!slot) return;
    const left = closesLeft();
    setText(closes, left > 0 ? clock(left) : "Closing now");
    const elapsed = (now() - fetchedAt) / 1000;
    setText(airs, clock(Math.max(0, slot.airs_in_s - elapsed)));
  }

  function renderPlacement() {
    const slot = data?.slot;
    const available = (slot?.ending_outro_s || 0) + (slot?.upcoming_intro_s || 0);
    setText(outro, introWords(slot?.ending_outro_s || 0));
    setText(windowText, introWords(available));
    const fit = el("vt-fit");
    if (fit) fit.hidden = !take;
    if (take) {
      const speech = take.cues?.speech ?? take.seconds;
      const forecast = slot?.voice_forecast;
      const estimate = forecast
        ? `The voice starts ${introWords(forecast.recording_start_s)} into the ending song; the next song starts ${introWords(forecast.music_entry_s)} after its segue. Full chain speech: ${introWords(forecast.chain_speech_s)}; ${introWords(forecast.speech_without_music_s)} is between songs without music.`
        : "The playout timing estimate is pending.";
      setText(el("vt-fit"), `${take.cues ? "Detected speech" : "Recording (speech estimate pending)"}: ${introWords(speech)}${take.cues?.tail ? ` plus ${introWords(take.cues.tail)} tail` : ""}. ${estimate} Original recording: ${introWords(take.seconds)}.`);
    } else setText(fit, "");
  }

  function renderTimer() {
    if (session) {
      const elapsed = (now() - session.started) / 1000;
      setText(timer, `Recording ${clock(elapsed)} of ${clock(limitSeconds())}.`);
    } else if (take) {
      setText(timer, `Recorded ${clock(take.seconds)}, not booked yet.`);
    } else {
      setText(timer, `${clock(limitSeconds())} max`);
    }
  }

  function trackText(track) {
    return `${track.label || "Voice track"}: ${stateWords(track, data?.slot, wallClock())}`;
  }

  // Rows are kept and changed in place, so a refresh never takes focus off
  // a Cancel button that is still there.
  function renderTracks(tracks) {
    const shownTracks = tracks.slice(0, MAX_LISTED);
    const active = doc.activeElement;
    const hadFocus = list.contains(active);
    const focusIndex = hadFocus ? [...list.querySelectorAll("button")].indexOf(active) : -1;
    const keep = new Set(shownTracks.map((track) => track.break_id));
    for (const [id, row] of rows) {
      if (!keep.has(id)) {
        row.item.remove();
        rows.delete(id);
      }
    }
    shownTracks.forEach((track, index) => {
      let row = rows.get(track.break_id);
      if (!row) {
        const item = doc.createElement("li");
        const text = doc.createElement("span");
        item.append(text);
        row = { item, text, button: null };
        rows.set(track.break_id, row);
      }
      row.track = track;
      setText(row.text, trackText(track));
      const cancellable = track.state === "queued";
      if (cancellable && !row.button) {
        const current = row;
        row.button = rowButton(doc, "Cancel", `Cancel voice track ${track.label || ""}`.trim(),
          (button) => void cancel(current.track, button));
        row.item.append(" ", row.button);
      } else if (!cancellable && row.button) {
        row.button.previousSibling?.remove();
        row.button.remove();
        row.button = null;
      }
      if (list.children[index] !== row.item) list.insertBefore(row.item, list.children[index] || null);
    });
    none.hidden = shownTracks.length > 0;
    if (hadFocus && doc.activeElement !== active) {
      if (active.isConnected) active.focus();
      else {
        const next = nextRowControl(list, "button", Math.max(0, focusIndex), listHeading);
        if (next === listHeading) listHeading.setAttribute("tabindex", "-1");
        next.focus();
      }
    }
  }

  function apply(response) {
    const before = data?.slot?.slot_id;
    data = response;
    fetchedAt = now();
    renderSlot();
    renderTimer();
    renderTracks(response.tracks || []);
    const after = data.slot?.slot_id;
    // Someone with a recording on the go hears that the change moved on.
    if (before && after && before !== after && (session || take)) {
      say(`The open song change is now ${pairWords(data.slot)}.`);
    }
  }

  async function refresh() {
    const epoch = ++requestEpoch;
    let url = "/api/voice-tracks";
    let transitionForecast = false;
    if (take?.cues?.speech > 0) {
      const query = new URLSearchParams({
        speech_s: String(take.cues.speech),
        tail_s: String(take.cues.tail || 0),
        placement: placement.value,
      });
      const slot = data?.slot;
      if (slot?.transition_revision && Array.isArray(slot.transition_items) && transitionItems) {
        const choices = slot.transition_items.filter((item) => item.editable).map((item) => {
          const row = [...transitionItems.children]
            .find((candidate) => candidate.dataset.transitionId === item.id);
          const select = row?.querySelector("select");
          return {
            id: item.id,
            action: select?.value || item.current_action || item.default_action || "replace",
          };
        });
        query.set("transition_revision", slot.transition_revision);
        query.set("transition_choices", JSON.stringify(choices));
        transitionForecast = true;
        slot.voice_forecast = null;
        renderPlacement();
      }
      url += `?${query}`;
    }
    try {
      const response = await requestJson(url);
      if (epoch === requestEpoch) apply(response);
    } catch (problem) {
      if (epoch === requestEpoch && transitionForecast && problem.status === 409) {
        const latestEpoch = ++requestEpoch;
        try {
          const latest = await requestJson("/api/voice-tracks");
          if (latestEpoch === requestEpoch) apply(latest);
        } catch (_) {
          if (latestEpoch === requestEpoch) {
            setText(el("vt-fit"), "The transition changed. Refresh the voice tracker to get a new estimate.");
          }
        }
        return;
      }
      // Shown, not said: the page asks again every few seconds.
      if (epoch === requestEpoch && !data) {
        unavailable.hidden = false;
        slotList.hidden = true;
        setText(unavailable, `Could not load the voice tracker. ${problem.message}`);
      }
    }
  }

  // ---- Recording ----

  function stopTracks(stream) {
    for (const track of stream?.getTracks?.() || []) {
      if (stoppedTracks.has(track)) continue;
      stoppedTracks.add(track);
      try { track.stop(); } catch (_) { /* already stopped */ }
    }
  }

  // A take remembers the two songs it was recorded for.  *retarget* is a
  // song change with other songs that it may go to only once it has been
  // listened to again (*heard*) and Book is pressed again.
  function showTake(blob, seconds, slot) {
    take = {
      blob, seconds, slot, endingId: slot.ending?.id, upcomingId: slot.upcoming?.id,
      retarget: null, heard: false, url: objectUrl(blob),
    };
    playback.src = take.url;
    const recorded = take;
    recorded.preparing = toWav(blob, rate(), undefined, undefined, (cues) => { recorded.cues = cues; })
      .then((wav) => {
        if (take === recorded) {
          recorded.wav = wav;
          renderPlacement();
          void refresh();
        }
      }).catch(() => {
        if (take === recorded) fail(bookButton, "This browser could not prepare the recording. Record it again.");
      });
  }

  const samePair = (open) => open.ending?.id === take.endingId && open.upcoming?.id === take.upcomingId;

  // The take's song change has gone and *open* is the one open now: say why
  // the take is not booked.  The same two songs only need Book again.
  function moved(open) {
    const was = pairWords(take.slot);
    if (samePair(open)) {
      take.slot = open;
      fail(bookButton, `The song change for ${was} was planned again with the same songs. Press Book voice track again to book the take.`);
      return;
    }
    take.retarget = open;
    take.heard = false;
    fail(bookButton, `The song change you recorded for, ${was}, has closed, and the open one, ${pairWords(open)}, has other songs, so the take was not booked.`);
  }

  // The song change the take may be booked for now, or null after saying why not.
  function target(open) {
    if (open.slot_id === take.slot.slot_id) return open;
    if (samePair(open)) {
      take.slot = open;
      return open;
    }
    if (take.retarget?.slot_id === open.slot_id) {
      if (take.heard) return open;
      fail(bookButton, `Listen back to the take first: it was recorded for ${pairWords(take.slot)}, not ${pairWords(open)}. If it still fits, press Book voice track again after listening; if not, Record again.`);
      return null;
    }
    moved(open);
    return null;
  }

  function stopListening() {
    try { playback.pause(); } catch (_) { /* not playing */ }
    listenButton.setAttribute("aria-pressed", "false");
  }

  function dropTake() {
    if (!take) return;
    stopListening();
    playback.removeAttribute("src");
    revokeUrl(take.url);
    take = null;
  }

  // Listen back: a toggle, pressed while the take plays.
  async function listen() {
    clearError();
    if (booking) {
      fail(listenButton, "Wait for booking to finish first.");
      return;
    }
    if (testing || testStarting || starting) {
      fail(listenButton, testing || testStarting ? "Stop the microphone test first." : "Wait for microphone access to finish first.");
      return;
    }
    if (session) {
      fail(listenButton, "Stop recording first: press Record or Alt+R.");
      return;
    }
    if (!take) {
      fail(listenButton, "Record a voice track first.");
      return;
    }
    if (listenButton.getAttribute("aria-pressed") === "true") {
      stopListening();
      return;
    }
    listenButton.setAttribute("aria-pressed", "true");
    try {
      playback.currentTime = 0;
      await playback.play();
      // Heard again: it may now go to the other songs it was offered for.
      if (take?.retarget) take.heard = true;
    } catch (_) {
      listenButton.setAttribute("aria-pressed", "false");
      fail(listenButton, "This browser could not play the recording back.");
    }
  }

  async function start() {
    clearError();
    if (booking) {
      fail(recordButton, "Wait for booking to finish first.");
      return;
    }
    if (testing || testStarting) {
      fail(recordButton, "Stop the microphone test before recording.");
      return;
    }
    const slot = data?.slot;
    if (!slot) {
      fail(recordButton, data?.reason
        ? closedWords(data.reason)
        : "The open song change has not loaded yet. Try again in a moment.");
      return;
    }
    const devices = media();
    const Rec = Recorder();
    if (typeof devices?.getUserMedia !== "function") {
      fail(recordButton, secure() ? NO_RECORDER_TEXT : INSECURE_TEXT);
      return;
    }
    if (typeof Rec !== "function") {
      fail(recordButton, NO_RECORDER_TEXT);
      return;
    }
    starting = true;
    const asked = generation;
    let stream;
    let focusGate = null;
    try {
      // Unprocessed: what the microphone hears, as a studio would want it.
      stream = await devices.getUserMedia(requestedMicrophone());
      focusGate = afterFocus();
    } catch (problem) {
      if (asked !== generation) return;
      starting = false;
      if (selectedMicrophone.id) microphoneNote(`Could not use ${microphoneLabel(selectedMicrophone.id, selectedMicrophone.label)}. Choose another microphone or System default. ${microphoneProblem(problem)}`);
      fail(recordButton, microphoneProblem(problem), afterFocus());
      return;
    }
    if (asked !== generation) {
      // Signed out while the browser asked for the microphone.
      stopTracks(stream);
      return;
    }
    if (!root.isConnected || !shown() || doc.visibilityState === "hidden") {
      starting = false;
      stopTracks(stream);
      say("Recording did not start: the Voice tracks tab is not showing.");
      return;
    }
    const labelRefresh = refreshMicrophones();
    reportMicrophone(stream, "Recording from");
    let recorder;
    try {
      recorder = new Rec(stream);
    } catch (_) {
      starting = false;
      stopTracks(stream);
      fail(recordButton, NO_RECORDER_TEXT);
      return;
    }
    const replaced = Boolean(take);
    dropTake();
    stopListening();
    const current = {
      recorder, stream, chunks: [], started: now(), slot, minutes: 0,
      microphone: microphoneWords(stream, "Recording from"),
      warned: false, closing: false, stopping: false, done: false, cancelled: false, note: "",
    };
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data?.size) current.chunks.push(event.data);
    });
    recorder.addEventListener("stop", () => finish(current));
    recorder.addEventListener("error", () => {
      current.failed = true;
      finish(current);
    });
    try {
      recorder.start(1000);
    } catch (_) {
      starting = false;
      stopTracks(stream);
      fail(recordButton, "The recording could not start.");
      return;
    }
    session = current;
    void labelRefresh.then(() => {
      if (session === current) {
        current.microphone = microphoneWords(stream, "Recording from");
        reportMicrophone(stream, "Recording from");
      }
    });
    starting = false;
    recordButton.setAttribute("aria-pressed", "true");
    renderTimer();
    void Promise.resolve(focusGate).then(() => {
      if (session === current) say(`Recording${replaced ? ", the earlier take discarded" : ""}. Press Record or Alt+R to stop.`);
    });
  }

  function stop(note = "") {
    const current = session;
    if (!current || current.stopping) return;
    current.stopping = true;
    current.note = note;
    stopTracks(current.stream);
    try {
      current.recorder.stop();
    } catch (_) {
      finish(current);
    }
  }

  function finish(current) {
    if (current.done) return;
    current.done = true;
    stopTracks(current.stream);
    if (session === current) session = null;
    recordButton.setAttribute("aria-pressed", "false");
    const seconds = Math.min(limitSeconds(), (now() - current.started) / 1000);
    if (current.cancelled) {
      renderTimer();
      return;
    }
    if (current.failed) {
      renderTimer();
      fail(recordButton, "The recording stopped because the microphone failed.");
      return;
    }
    const blob = new Blob(current.chunks, { type: current.recorder.mimeType || "audio/webm" });
    current.chunks = [];
    if (!blob.size) {
      renderTimer();
      fail(recordButton, "Nothing was recorded. Check the microphone, then press Record again.");
      return;
    }
    showTake(blob, seconds, current.slot);
    microphoneNote(current.microphone.replace(/^Recording from/, "Last recording used"));
    renderTimer();
    renderPlacement();
    say(`${current.note}Recording stopped at ${clock(seconds)}. Listen back, then Book voice track, or Record again to replace it.`);
  }

  async function toggle() {
    if (session) {
      stop();
      return;
    }
    if (!starting) await start();
  }

  // Recording only goes on while the tab shows and the page is in view:
  // leaving either stops it, keeps the take and lets the microphone go.
  function guard() {
    const reason = !root.isConnected ? "The voice tracker closed" : !shown() ? "You left the Voice tracks tab" : doc.visibilityState === "hidden" ? "The page was hidden" : "";
    if (!reason) return;
    if (session && !session.stopping) stop(`${reason}, so recording stopped. `);
    if (testing) stopMicrophoneTest(`${reason}, so the microphone test stopped.`);
    if (testStarting) stopMicrophoneTest("");
    if (starting) {
      generation += 1;
      starting = false;
      testGeneration += 1;
    }
  }

  function tick() {
    guard();
    const current = session;
    if (current && !current.stopping) {
      const elapsed = (now() - current.started) / 1000;
      const limit = limitSeconds();
      if (elapsed >= limit) {
        stop(`Stopped at the ${fmtTime(limit)} limit. `);
      } else {
        const minutes = Math.floor(elapsed / 60);
        if (minutes > current.minutes) {
          current.minutes = minutes;
          // The timer on the page shows it already.
          say(`${minutes} minute${minutes === 1 ? "" : "s"}.`, { mirror: false });
        }
        if (!current.warned && limit > 2 * WARN_SECONDS && limit - elapsed <= WARN_SECONDS) {
          current.warned = true;
          say(`${WARN_SECONDS} seconds left before the length limit.`);
        }
        const left = closesLeft();
        if (!current.closing && left !== null && left <= WARN_SECONDS
            && data.slot.slot_id === current.slot.slot_id) {
          current.closing = true;
          say(left > 0
            ? `The song change closes in about ${fmtTime(left)}.`
            : "The song change is closing now.");
        }
      }
    }
    renderTimer();
    renderTimes();
    ticks += 1;
    // Asked again every few seconds while the tab shows, and at once when
    // it comes back into view.
    const showing = shown() && doc.visibilityState !== "hidden";
    if (showing && (!wasShown || ticks % POLL_TICKS === 0)) void refresh();
    wasShown = showing;
  }

  // ---- Booking ----

  async function book(button) {
    clearError();
    if (testing || testStarting || starting) {
      fail(bookButton, "Stop microphone access before booking a voice track.");
      return;
    }
    if (booking) return;
    if (session) {
      fail(bookButton, "Stop recording first: press Record or Alt+R.");
      return;
    }
    if (!take) {
      fail(bookButton, "Record a voice track first.");
      return;
    }
    const open = data?.slot;
    if (!open) {
      fail(bookButton, "No song change is open now. Keep the recording and try again in a moment.");
      return;
    }
    const slot = target(open);
    if (!slot) return;
    booking = true;
    const recorded = take;
    const asked = generation;
    const abort = new AbortController();
    bookingAbort = abort;
    stopListening();
    try { await withDisabled(button, async () => {
      let wav;
      try {
        await recorded.preparing;
        if (asked !== generation || take !== recorded) return;
        wav = recorded.wav;
        if (!wav) throw new Error("Recording preparation failed");
      } catch (_) {
        if (asked !== generation) return;
        fail(bookButton, "This browser could not read the recording back. Record it again.");
        return;
      }
      if (asked !== generation || take !== recorded) return;
      if (wav.size + FORM_BYTES > data.limits.max_bytes) {
        fail(bookButton, "The recording is too large for this station. Record a shorter one.");
        return;
      }
      const form = new FormData();
      form.append("slot_id", slot.slot_id);
      form.append("ending_id", slot.ending.id);
      form.append("upcoming_id", slot.upcoming.id);
      form.append("placement", placement.value);
      form.append("allow_outro", placement.value === "auto" ? "true" : "false");
      if (slot.transition_revision) {
        form.append("transition_revision", slot.transition_revision);
        form.append("transition_choices", JSON.stringify((slot.transition_items || [])
          .filter((item) => item.editable)
          .map((item) => ({
            id: item.id,
            action: transitionChoices.get(item.id) || item.current_action || item.default_action || "replace",
          }))));
      }
      form.append("file", new File([wav], "voice-track.wav", { type: "audio/wav" }));
      let booked;
      try {
        booked = await requestJson("/api/voice-tracks", { method: "POST", body: form, signal: abort.signal });
        if (asked !== generation || take !== recorded) return;
      } catch (problem) {
        if (asked !== generation || abort.signal.aborted) return;
        if (problem.status === 409) await refresh();
        const latest = data?.slot;
        if (problem.status === 409 && take && latest && latest.slot_id !== slot.slot_id) {
          moved(latest);
        } else {
          fail(bookButton, `Could not book the voice track. ${problem.message}`);
        }
        return;
      }
      if (asked !== generation || take !== recorded) return;
      dropTake();
      await refresh();
      renderTimer();
      const when = booked.airs_in_s != null ? ` It airs in about ${roughly(booked.airs_in_s)}.` : "";
      say(`Booked ${booked.label || "the voice track"}.${when}`);
    }); } finally {
      if (bookingAbort === abort) { booking = false; bookingAbort = null; }
    }
  }

  function discard() {
    clearError();
    if (session) {
      session.cancelled = true;
      stop();
      say("Recording discarded.");
      return;
    }
    if (!take) {
      say("There is no recording to discard.");
      return;
    }
    dropTake();
    renderTimer();
    say("Recording discarded.");
  }

  function stopMicrophoneTest(message = "Microphone test stopped.") {
    testGeneration += 1;
    const current = testing;
    testing = null;
    testStarting = false;
    try { testPlayback.pause(); } catch (_) { /* not playing */ }
    testPlayback.srcObject = null;
    stopTracks(current?.stream || pendingTestStream);
    pendingTestStream = null;
    testButton.setAttribute("aria-pressed", "false");
    testButton.textContent = "Test microphone";
    if (message && current) say(message);
    if (!session) microphoneNote("");
  }

  async function toggleMicrophoneTest() {
    clearError();
    if (testing || testStarting) {
      stopMicrophoneTest();
      return;
    }
    if (session || starting || booking || listenButton.getAttribute("aria-pressed") === "true") {
      fail(testButton, "Stop recording or listen-back before testing the microphone.");
      return;
    }
    const devices = media();
    if (typeof devices?.getUserMedia !== "function") {
      fail(testButton, secure() ? NO_RECORDER_TEXT : INSECURE_TEXT);
      return;
    }
    testStarting = true;
    const asked = ++testGeneration;
    let stream;
    try {
      stream = await devices.getUserMedia(requestedMicrophone());
      if (asked !== testGeneration) {
        stopTracks(stream);
        return;
      }
      if (!root.isConnected || !shown() || doc.visibilityState === "hidden") {
        stopTracks(stream);
        testStarting = false;
        return;
      }
      pendingTestStream = stream;
      const focusGate = afterFocus();
      const labelRefresh = refreshMicrophones();
      testPlayback.srcObject = stream;
      await testPlayback.play();
      if (asked !== testGeneration) {
        stopTracks(stream);
        if (pendingTestStream === stream) pendingTestStream = null;
        if (testPlayback.srcObject === stream) testPlayback.srcObject = null;
        return;
      }
      pendingTestStream = null;
      testing = { stream };
      testStarting = false;
      stream.getAudioTracks?.()[0]?.addEventListener?.("ended", () => {
        if (testing?.stream === stream) stopMicrophoneTest("The microphone stopped, so the microphone test ended.");
      }, { once: true });
      testButton.setAttribute("aria-pressed", "true");
      testButton.textContent = "Stop microphone test";
      reportMicrophone(stream, "Testing");
      void labelRefresh.then(() => { if (testing?.stream === stream) reportMicrophone(stream, "Testing"); });
      void Promise.resolve(focusGate).then(() => {
        if (testing?.stream === stream) say("Microphone test started. Use headphones to avoid feedback. Press Stop microphone test when finished.");
      });
    } catch (problem) {
      stopTracks(stream);
      if (asked !== testGeneration) return;
      pendingTestStream = null;
      testPlayback.srcObject = null;
      testStarting = false;
      if (selectedMicrophone.id) microphoneNote(`Could not use ${microphoneLabel(selectedMicrophone.id, selectedMicrophone.label)}. Choose another microphone or System default. ${microphoneProblem(problem)}`);
      fail(testButton, microphoneProblem(problem), afterFocus());
    }
  }

  async function cancel(track, button) {
    await withDisabled(button, async () => {
      try {
        await requestJson(`/api/voice-tracks/${encodeURIComponent(track.break_id)}`, { method: "DELETE" });
        say(`Cancelled ${track.label || "the voice track"}.`);
      } catch (problem) {
        // Shown under Record and linked to this Cancel button.
        fail(button, `Could not cancel ${track.label || "the voice track"}. ${problem.message}`);
      }
    });
    await refresh();
  }

  recordButton.addEventListener("click", () => void toggle());
  microphoneSelect?.addEventListener("change", () => saveMicrophone(microphoneSelect.value));
  microphoneRefresh?.addEventListener("click", () => void refreshMicrophones());
  testButton?.addEventListener("click", () => void toggleMicrophoneTest());
  listenButton.addEventListener("click", () => void listen());
  playback.addEventListener("ended", () => listenButton.setAttribute("aria-pressed", "false"));
  bookButton.addEventListener("click", (event) => void book(event.currentTarget));
  discardButton.addEventListener("click", () => discard());
  placement.addEventListener("change", () => {
    renderPlacement();
    if (take?.cues) void refresh();
  });
  transitionItems?.addEventListener("change", (event) => {
    if (event.target instanceof doc.defaultView.HTMLSelectElement && take?.cues) void refresh();
  });
  // Alt+R: start or stop from anywhere in the Voice tracks tab, focus staying put.
  doc.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.repeat || event.code !== "KeyR") return;
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!root.isConnected || !shown() || doc.querySelector("dialog[open]")) return;
    event.preventDefault();
    void toggle();
  });
  doc.addEventListener("visibilitychange", guard);
  media()?.addEventListener?.("devicechange", () => { void refreshMicrophones(); });
  if (typeof globalThis.MutationObserver === "function") {
    new globalThis.MutationObserver(guard).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  }
  interval(tick, 1000);
  renderSlot();
  renderTimer();
  void refreshMicrophones();

  return {
    refresh,
    toggle,
    // A recording on the go or a take not booked yet: a station switch
    // drops either, so the page asks first.
    hasUnsavedWork: () => Boolean(session || take),
    clear() {
      requestEpoch += 1;
      generation += 1;
      starting = false;
      bookingAbort?.abort();
      bookingAbort = null;
      booking = false;
      testGeneration += 1;
      if (testing) stopMicrophoneTest("");
      else if (testStarting) stopMicrophoneTest("");
      if (session) {
        session.cancelled = true;
        stop();
      }
      dropTake();
      clearError();
      data = null;
      for (const row of rows.values()) row.item.remove();
      rows.clear();
      none.hidden = false;
      status.textContent = "";
      renderSlot();
      renderTimer();
    },
  };
}
