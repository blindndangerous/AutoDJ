// Stream mode: the server mixes and streams MP3, so this page is a remote
// that can also listen through one plain <audio> element.  The browser
// mixing engine (decks, worklets, prefetch) is never started here.
//
// Speech rules (docs/accessibility-testing.md, Announcement contract):
//   - "Listen here" keeps a fixed name; only aria-pressed changes, so NVDA
//     says "pressed" / "not pressed" and nothing on the 1 Hz push.  The
//     listen state is never also spoken through a live region.
//   - Failures are spoken in plain words, never as raw browser error text.
//   - The idle note is plain text, not a live region: the station event
//     "Set stopped" already said it once.
//   - A station event is spoken once per (id, seq).  The first event seen
//     after page load is only remembered, so a reload never repeats it.

import { announceStatus } from "./live-region.js";

export const STREAM_DELAY_ESTIMATE_S = 3;
// A buffer reading beyond this is a stalled or bogus element, not a delay.
const MAX_MEASURED_DELAY_S = 30;
const IDLE_TEXT =
  "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";
const EVENT_TEXT = {
  set_started: "New set started.",
  set_stopped: "Set stopped. Nobody is listening.",
  link_changed: "Stream link changed. Speakers using the old link have stopped.",
};
const BLOCKED_TEXT = "The browser blocked playback. Press Listen here again.";
const NOT_LOADED_TEXT = "The stream did not load. It may be full or not running.";
const DROPPED_TEXT = "The stream connection dropped.";
const STALLED_TEXT = "The stream stopped responding.";
// A stalled or waiting stream that has not played again by then is dead.
const STALL_TIMEOUT_MS = 15000;
// MediaError codes (numeric so this works where MediaError is undefined).
const MEDIA_ERR_NETWORK = 2;

// A failed start, from play() or the link lookup, in plain words.
function startFailureText(err) {
  const name = err && err.name;
  if (name === "NotAllowedError") return BLOCKED_TEXT;
  // An API refusal carries a status and a server-written reason.
  if (err && typeof err.status === "number" && err.message) {
    return `Could not get the stream link: ${err.message}`;
  }
  // fetch() rejects with a TypeError when the network is down.
  if (name === "TypeError" || name === "NetworkError") return DROPPED_TEXT;
  return NOT_LOADED_TEXT;
}

// A media element error or end, by MediaError code.  Without a code, a
// stream that already played has dropped and one that never did failed
// to load.
function mediaFailureText(audio, hasPlayed) {
  const code = audio.error && audio.error.code;
  if (code === MEDIA_ERR_NETWORK) return DROPPED_TEXT;
  if (code) return NOT_LOADED_TEXT;
  return hasPlayed ? DROPPED_TEXT : NOT_LOADED_TEXT;
}

export function createStreamMode({ audio, button, idleNote, srStatus, fetchInfo }) {
  let listening = false;
  // Bumped by every start and stop, so a start still waiting on the link
  // lookup can tell that the user has pressed the button again since.
  let generation = 0;
  let retried = false;
  let hasPlayed = false;
  let stallTimer = null;
  let seenFirstApply = false;
  let lastEventKey = null;

  function say(text, tone = "info") {
    announceStatus(srStatus, text, { dwellMs: 4000, force: true, tone });
  }

  function setListening(value) {
    listening = value;
    const pressed = value ? "true" : "false";
    if (button.getAttribute("aria-pressed") !== pressed) {
      button.setAttribute("aria-pressed", pressed);
    }
  }

  function release() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load?.();
  }

  function clearStall() {
    if (stallTimer === null) return;
    clearTimeout(stallTimer);
    stallTimer = null;
  }

  // Only a real listen is torn down; a browser-mode push is a no-op.
  // Bumping the generation cancels a start still waiting on the link.
  function stop() {
    if (!listening) return;
    generation += 1;
    clearStall();
    setListening(false);
    release();
  }

  function fail(text) {
    stop();
    say(text, "error");
  }

  // Always asks for the link again: "Make new link" retires the old one.
  // A failed silent retry is reported as the dropped connection it follows.
  async function connect({ retry = false } = {}) {
    const mine = ++generation;
    setListening(true);
    try {
      const info = await fetchInfo();
      if (mine !== generation) return;
      audio.src = info.path;
      await audio.play();
    } catch (err) {
      if (mine !== generation) return;
      fail(retry ? DROPPED_TEXT : startFailureText(err));
    }
  }

  // A stream that never played is a failed start (for example the
  // listener limit).  Once it has played, a drop gets one silent retry
  // (a link change or a blip); a second drop before audio plays again is
  // reported.
  function onDropped() {
    if (!listening) return;
    if (!hasPlayed || retried) {
      fail(mediaFailureText(audio, hasPlayed));
      return;
    }
    retried = true;
    void connect({ retry: true });
  }
  audio.addEventListener("error", onDropped);
  audio.addEventListener("ended", onDropped);
  audio.addEventListener("playing", () => {
    hasPlayed = true;
    retried = false;
    clearStall();
  });
  // A connection can hang without ever raising an error.  The first stall
  // signal starts one countdown; audio playing again cancels it.
  function onStalled() {
    if (!listening || stallTimer !== null) return;
    stallTimer = setTimeout(() => {
      stallTimer = null;
      if (listening) fail(STALLED_TEXT);
    }, STALL_TIMEOUT_MS);
  }
  audio.addEventListener("stalled", onStalled);
  audio.addEventListener("waiting", onStalled);

  async function toggleListen() {
    if (listening) {
      stop();
      return;
    }
    retried = false;
    hasPlayed = false;
    await connect();
  }

  function announceEvent(event) {
    const key = event ? `${event.id}|${event.seq}` : null;
    if (!seenFirstApply) {
      seenFirstApply = true;
      lastEventKey = key;
      return;
    }
    if (!event || key === lastEventKey) return;
    lastEventKey = key;
    const text = EVENT_TEXT[event.name];
    if (text) say(text);
  }

  function apply(state) {
    const active = Boolean(state && state.stream_mode);
    if (button.hidden === active) button.hidden = !active;
    const idle = active && state.stream_state === "idle";
    if (idle && idleNote.textContent !== IDLE_TEXT) idleNote.textContent = IDLE_TEXT;
    if (idleNote.hidden === idle) idleNote.hidden = !idle;
    if (!active) {
      stop();
      return;
    }
    announceEvent(state.stream_event);
  }

  // Seconds the listener hears behind the server's mix position.  While
  // listening that is the audio buffered ahead of the playhead; otherwise
  // (a speaker elsewhere) a fixed estimate.
  function delaySeconds() {
    if (listening) {
      try {
        const ranges = audio.buffered;
        if (ranges && ranges.length) {
          const ahead = ranges.end(ranges.length - 1) - audio.currentTime;
          if (Number.isFinite(ahead) && ahead >= 0 && ahead < MAX_MEASURED_DELAY_S) {
            return ahead;
          }
        }
      } catch (_) { /* no buffer information yet */ }
    }
    return STREAM_DELAY_ESTIMATE_S;
  }

  return { apply, toggleListen, delaySeconds, stop, isListening: () => listening };
}
