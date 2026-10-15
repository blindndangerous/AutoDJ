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

const POLL_TICKS = 5;
const WARN_SECONDS = 30;
const MAX_LISTED = 50;
// Room for the form around the file, under the station's upload cap.
const FORM_BYTES = 4096;
const WAV_HEADER_BYTES = 44;

export const NO_RECORDER_TEXT = "This browser cannot record audio. Try a current version of Firefox, Chrome, Edge or Safari.";
export const INSECURE_TEXT = "Recording needs a secure connection: open AutoDJ over HTTPS, or on this computer at localhost.";

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

// The longest take the browser records.  Booking decodes the whole take
// in the page (decodeAudioData has no streaming form), so the length is
// what bounds the page's memory: three minutes of stereo at 44.1 kHz is
// about 64 MB decoded, plus 16 MB of WAV.  The station's own limit
// applies too, when it is shorter.
export const BROWSER_MAX_SECONDS = 180;
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
function writeFrames(view, channels, from, to) {
  for (let i = from; i < to; i += 1) {
    let sum = 0;
    for (const channel of channels) sum += channel[i];
    const value = sum / channels.length;
    const sample = Math.max(-1, Math.min(1, Number.isFinite(value) ? value : 0));
    view.setInt16(WAV_HEADER_BYTES + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
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
) {
  const audio = await new Context(1, 1, rate).decodeAudioData(await blob.arrayBuffer());
  const channels = Array.from({ length: audio.numberOfChannels }, (_, i) => audio.getChannelData(i));
  const frames = audio.length;
  const buffer = new ArrayBuffer(WAV_HEADER_BYTES + frames * 2);
  const view = new DataView(buffer);
  wavHeader(view, frames, audio.sampleRate);
  for (let from = 0; from < frames; from += WAV_BLOCK_FRAMES) {
    writeFrames(view, channels, from, Math.min(frames, from + WAV_BLOCK_FRAMES));
    if (from + WAV_BLOCK_FRAMES < frames) await pause();
  }
  return new Blob([buffer], { type: "audio/wav" });
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
  const closes = el("vt-closes");
  const airs = el("vt-airs");
  const recordButton = el("vt-record");
  const timer = el("vt-timer");
  const playback = el("vt-playback");
  const placement = el("vt-placement");
  const placementDesc = el("vt-placement-desc");
  const allowOutro = el("vt-allow-outro");
  const listenButton = el("vt-listen");
  const bookButton = el("vt-book");
  const discardButton = el("vt-discard");
  const error = el("vt-error");
  const status = el("vt-status");
  const list = el("vt-list");
  const none = el("vt-none");
  const listHeading = el("vt-booked-heading");

  let data = null;
  let fetchedAt = 0;
  let requestEpoch = 0;
  let ticks = 0;
  let take = null;
  let session = null;
  let starting = false;
  let generation = 0;
  let linked = null;
  let wasShown = false;
  const rows = new Map();

  const shown = () => !panel.hidden;
  // The tracker's own region while its tab shows; the page-wide one when
  // the tab has been left, since a region inside a hidden panel says nothing.
  const say = (message, { tone = "info", mirror = true } = {}) => announceStatus(
    shown() ? status : (doc.getElementById("sr-status") || status),
    message,
    { force: true, tone, mirror, dwellMs: 5000 },
  );

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

  function fail(control, message) {
    clearError();
    error.textContent = message;
    error.hidden = false;
    link(control, true);
    linked = control;
    say(message, { tone: "error" });
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
    let text;
    if (placement.value === "standalone") {
      text = "The ending song fades, your voice plays, then the next song starts.";
    } else if (placement.value === "talk_up") {
      text = slot && slot.upcoming_intro_s == null
        ? "The next song has no intro point, so your voice plays standalone."
        : "Your voice plays over the next song's intro and ends as its vocal starts.";
    } else if (!slot) {
      text = "Talks up the next song's intro when it is long enough, otherwise plays between the songs.";
    } else if (slot.auto_placement === "talk_up") {
      text = `Here, a talk-up: the next song's intro is ${introWords(slot.upcoming_intro_s)}.`;
    } else {
      text = `Here, standalone between the songs${slot.auto_note ? `: ${slot.auto_note}` : ""}.`;
    }
    setText(placementDesc, text);
  }

  function renderTimer() {
    if (session) {
      const elapsed = (now() - session.started) / 1000;
      setText(timer, `Recording ${clock(elapsed)} of ${clock(limitSeconds())}.`);
    } else if (take) {
      setText(timer, `Recorded ${clock(take.seconds)}, not booked yet.`);
    } else {
      setText(timer, `Not recording. A voice track may be up to ${clock(limitSeconds())} long.`);
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
    try {
      const response = await requestJson("/api/voice-tracks");
      if (epoch === requestEpoch) apply(response);
    } catch (problem) {
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
    try {
      // Unprocessed: what the microphone hears, as a studio would want it.
      stream = await devices.getUserMedia({
        audio: {
          channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false,
        },
      });
    } catch (problem) {
      starting = false;
      fail(recordButton, microphoneProblem(problem));
      return;
    }
    if (asked !== generation) {
      // Signed out while the browser asked for the microphone.
      starting = false;
      stopTracks(stream);
      return;
    }
    if (!shown() || doc.visibilityState === "hidden") {
      starting = false;
      stopTracks(stream);
      say("Recording did not start: the Voice tracks tab is not showing.");
      return;
    }
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
    starting = false;
    recordButton.setAttribute("aria-pressed", "true");
    renderTimer();
    say(`Recording${replaced ? ", the earlier take discarded" : ""}. Press Record or Alt+R to stop.`);
  }

  function stop(note = "") {
    const current = session;
    if (!current || current.stopping) return;
    current.stopping = true;
    current.note = note;
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
    if (!blob.size) {
      renderTimer();
      fail(recordButton, "Nothing was recorded. Check the microphone, then press Record again.");
      return;
    }
    showTake(blob, seconds, current.slot);
    renderTimer();
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
    if (!session || session.stopping) return;
    if (!shown()) stop("You left the Voice tracks tab, so recording stopped. ");
    else if (doc.visibilityState === "hidden") stop("The page was hidden, so recording stopped. ");
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
    await withDisabled(button, async () => {
      let wav;
      try {
        wav = await toWav(take.blob, rate());
      } catch (_) {
        fail(bookButton, "This browser could not read the recording back. Record it again.");
        return;
      }
      if (wav.size + FORM_BYTES > data.limits.max_bytes) {
        fail(bookButton, "The recording is too large for this station. Record a shorter one.");
        return;
      }
      const form = new FormData();
      form.append("slot_id", slot.slot_id);
      form.append("ending_id", slot.ending.id);
      form.append("upcoming_id", slot.upcoming.id);
      form.append("placement", placement.value);
      form.append("allow_outro", allowOutro.checked ? "true" : "false");
      form.append("file", new File([wav], "voice-track.wav", { type: "audio/wav" }));
      let booked;
      try {
        booked = await requestJson("/api/voice-tracks", { method: "POST", body: form });
      } catch (problem) {
        if (problem.status === 409) await refresh();
        const latest = data?.slot;
        if (problem.status === 409 && take && latest && latest.slot_id !== slot.slot_id) {
          moved(latest);
        } else {
          fail(bookButton, `Could not book the voice track. ${problem.message}`);
        }
        return;
      }
      dropTake();
      await refresh();
      renderTimer();
      const when = booked.airs_in_s != null ? ` It airs in about ${roughly(booked.airs_in_s)}.` : "";
      say(`Booked ${booked.label || "the voice track"}.${when}`);
    });
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
  listenButton.addEventListener("click", () => void listen());
  playback.addEventListener("ended", () => listenButton.setAttribute("aria-pressed", "false"));
  bookButton.addEventListener("click", (event) => void book(event.currentTarget));
  discardButton.addEventListener("click", () => discard());
  placement.addEventListener("change", renderPlacement);
  // Alt+R: start or stop from anywhere in the Voice tracks tab, focus staying put.
  doc.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.repeat || event.code !== "KeyR") return;
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (!root.isConnected || !shown() || doc.querySelector("dialog[open]")) return;
    event.preventDefault();
    void toggle();
  });
  doc.addEventListener("visibilitychange", guard);
  if (typeof globalThis.MutationObserver === "function") {
    new globalThis.MutationObserver(guard).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  }
  interval(tick, 1000);
  renderSlot();
  renderTimer();

  return {
    refresh,
    toggle,
    clear() {
      requestEpoch += 1;
      generation += 1;
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
