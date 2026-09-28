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
//   - "Make new link" on this page speaks its own confirmation, so the
//     link_changed event its rotation causes is not spoken again here
//     (claimLinkChange).  Other open pages still hear the event once.

import { confirmAction } from "./confirm-dialog.js";
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
  if (code === MediaError.MEDIA_ERR_NETWORK) return DROPPED_TEXT;
  if (code) return NOT_LOADED_TEXT;
  return hasPlayed ? DROPPED_TEXT : NOT_LOADED_TEXT;
}

// beforePlay puts the page's volume and mute on the element; it runs
// before every play(), so the stream never starts at the element's
// default full volume.
export function createStreamMode({
  audio, button, idleNote, srStatus, fetchInfo, beforePlay,
}) {
  let listening = false;
  // Bumped by every start and stop, so a start still waiting on the link
  // lookup can tell that the user has pressed the button again since.
  let generation = 0;
  let retried = false;
  let hasPlayed = false;
  let stallTimer = null;
  let seenFirstApply = false;
  let lastEventKey = null;
  // Set while this page's own "Make new link" is in flight.  The token
  // lets a late release leave a newer claim alone.
  let linkClaim = null;

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
    audio.load();
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
      beforePlay();
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
    if (event.name === "link_changed" && linkClaim) {
      linkClaim = null;
      return;
    }
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

  // Marks the next link_changed event as already announced by this page.
  // Returns a release for a rotation that failed, so a later change made
  // elsewhere is still spoken.
  function claimLinkChange() {
    const token = {};
    linkClaim = token;
    return () => {
      if (linkClaim === token) linkClaim = null;
    };
  }

  return {
    apply, toggleListen, delaySeconds, stop, claimLinkChange,
    isListening: () => listening,
  };
}

// ----------------------------------------------------------------
// Settings > Stream: the address, the playlist file, the quality and
// "Make new link".
//
// Speech rules:
//   - The listener count and the "not loaded yet" note are plain text,
//     never live regions, and are only rewritten when they change.
//   - The "only works on this computer" note is fixed plain text, shown
//     once at start-up when the page was opened on a loopback address,
//     and then also describes the address field and Copy address.
//   - Copy and "Make new link" are user actions, so their confirmations
//     are forced: pressing Copy twice is heard twice.
//   - Copy never moves focus through the address field on success: NVDA
//     would read the whole secret address aloud.
//   - Failures of a user action go to the settings region with the error
//     tone, like every other settings failure.
//   - The quality select is never rewritten while it has focus or while
//     a change of it is still being saved.
// ----------------------------------------------------------------

const LINK_MADE_TEXT = "New stream link made. The old link no longer works.";
const COPIED_TEXT = "Stream address copied.";
const COPY_FAILED_TEXT = "Could not copy. Select the address and copy it yourself.";
export const NOT_LOADED_NOTE = "Stream address not loaded yet.";
// Arrowing through a closed select fires change on every step, and each
// save restarts the encoder and drops every speaker, so saves wait for
// the choice to settle.
export const BITRATE_SETTLE_MS = 600;
// While the address is missing, a state push retries at most this often.
export const ADDRESS_RETRY_MS = 10000;
// A claimed link_changed that has not arrived by then never will.
export const LINK_CLAIM_TIMEOUT_MS = 10000;

// The copied address is built from this page's origin, so a page opened
// as localhost copies a link that a speaker on the network cannot reach.
function isLoopbackHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host.endsWith(".localhost")
    || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);
}

function listenerText(count) {
  return `${count} listener${count === 1 ? "" : "s"}`;
}

// A 401 already opened the sign-in dialog; saying more would only talk
// over it.
function isAuthFailure(err) {
  return Boolean(err) && err.name === "AuthenticationRequiredError";
}

function eventKey(event) {
  return event ? `${event.id}|${event.seq}` : null;
}

export function createStreamSettings({
  doc,
  fetchInfo,
  rotate,
  saveBitrate,
  claimLinkChange = () => () => {},
  srStatus,
  settingsStatus,
  hostname = doc.location.hostname,
}) {
  const fieldset = doc.getElementById("stream-settings");
  const loopbackNote = doc.getElementById("stream-loopback-note");
  if (loopbackNote && isLoopbackHost(hostname)) {
    loopbackNote.hidden = false;
    // Part of the address field's and Copy's descriptions, so tabbing to
    // either says the address only works here.  Added only when shown: a
    // description reads hidden text too.
    for (const control of [doc.getElementById("stream-url"), doc.getElementById("stream-copy")]) {
      if (!control) continue;
      const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      if (!ids.includes(loopbackNote.id)) ids.push(loopbackNote.id);
      control.setAttribute("aria-describedby", ids.join(" "));
    }
  }
  const url = doc.getElementById("stream-url");
  const note = doc.getElementById("stream-url-note");
  const copy = doc.getElementById("stream-copy");
  const m3u = doc.getElementById("stream-m3u");
  const bitrate = doc.getElementById("stream-bitrate");
  const rotateBtn = doc.getElementById("stream-rotate");
  const listeners = doc.getElementById("stream-listeners");
  let active = false;
  let lastEventKey = null;
  // Bumped by every source of stream details, so a slow lookup cannot
  // overwrite a newer link.
  let generation = 0;
  // The lookup in flight, if any (fill() bumps the generation, so the
  // generation cannot say when a lookup has finished).
  let lookup = null;
  let lastAttempt = -Infinity;
  let failureSpoken = false;
  // Set from "Make new link" until its link_changed event arrives, so
  // that event does not fetch what the rotate reply already gave us.
  let ownRotation = null;
  // True while the "Make new link" confirmation is open.
  let confirming = false;
  // Quality saves: the value the server last confirmed, the newest
  // choice not yet sent, the settle timer and whether a save is out.
  let savedBitrate = bitrate.value;
  let pendingBitrate = null;
  let settleTimer = null;
  let saving = false;

  function showNote(missing) {
    if (!note) return;
    if (missing && note.textContent !== NOT_LOADED_NOTE) note.textContent = NOT_LOADED_NOTE;
    if (note.hidden === missing) note.hidden = !missing;
  }

  function bitrateBusy() {
    return saving || settleTimer !== null || pendingBitrate !== null;
  }

  function fill(info) {
    generation += 1;
    url.value = `${doc.location.origin}${info.path}`;
    m3u.setAttribute("href", info.m3u_path);
    showNote(false);
    failureSpoken = false;
    if (bitrateBusy()) return;
    savedBitrate = String(info.bitrate);
    if (doc.activeElement !== bitrate) bitrate.value = savedBitrate;
  }

  async function refresh() {
    if (!active) return;
    const mine = ++generation;
    const token = {};
    lookup = token;
    lastAttempt = Date.now();
    try {
      const info = await fetchInfo();
      if (mine === generation && active) fill(info);
    } catch (err) {
      if (mine !== generation || !active || isAuthFailure(err)) return;
      // Retries repeat every ten seconds; the failure is spoken once.
      if (failureSpoken) return;
      failureSpoken = true;
      announceStatus(settingsStatus, `Could not load the stream address: ${err.message}`,
        { dwellMs: 6000, tone: "error" });
    } finally {
      if (lookup === token) lookup = null;
    }
  }

  // The first lookup can fail while another tab shows, where the
  // failure is not heard; a missing address is looked up again.
  function retryIfMissing({ throttle = false } = {}) {
    if (!active || lookup || url.value) return;
    if (throttle && Date.now() - lastAttempt < ADDRESS_RETRY_MS) return;
    void refresh();
  }

  function stopBitrateSaves() {
    clearTimeout(settleTimer);
    settleTimer = null;
    pendingBitrate = null;
  }

  function hide() {
    active = false;
    generation += 1;
    lookup = null;
    lastEventKey = null;
    ownRotation = null;
    stopBitrateSaves();
    if (confirming) doc.getElementById("confirm-dialog")?.close();
    fieldset.hidden = true;
  }

  // Sign-out: the address is a secret, so it leaves the page.
  function reset() {
    hide();
    url.value = "";
    m3u.removeAttribute("href");
    listeners.textContent = "";
    failureSpoken = false;
    lastAttempt = -Infinity;
  }

  function applyPushedBitrate(state) {
    const pushed = state.settings?.playback?.stream_bitrate;
    if (pushed == null || bitrateBusy()) return;
    savedBitrate = String(pushed);
    if (doc.activeElement !== bitrate && bitrate.value !== savedBitrate) {
      bitrate.value = savedBitrate;
    }
  }

  function apply(state) {
    if (!state || !state.stream_mode) {
      if (!active) return;
      // A server restart without --stream: focus inside the section
      // would fall to the page top, so it goes to the Settings heading.
      const hadFocus = fieldset.contains(doc.activeElement) || confirming;
      hide();
      if (hadFocus) fieldset.closest("details")?.querySelector("summary")?.focus();
      return;
    }
    if (fieldset.hidden) fieldset.hidden = false;
    const text = listenerText(Number(state.stream_listeners) || 0);
    if (listeners.textContent !== text) listeners.textContent = text;
    applyPushedBitrate(state);
    const key = eventKey(state.stream_event);
    if (!active) {
      active = true;
      lastEventKey = key;
      showNote(!url.value);
      void refresh();
      return;
    }
    if (key !== lastEventKey) {
      lastEventKey = key;
      if (state.stream_event?.name === "link_changed") {
        // This page's own rotation already filled the new link.
        if (ownRotation) {
          ownRotation = null;
          return;
        }
        // Another page made a new link: the address shown here is dead.
        void refresh();
        return;
      }
    }
    retryIfMissing({ throttle: true });
  }

  // Plain http is not a secure context, so it has no clipboard API.  The
  // older copy command still works there; a one-off copy listener hands
  // it the address, so nothing needs selecting and focus stays put.
  function copyByCommand() {
    let wrote = false;
    const onCopy = (event) => {
      if (!event.clipboardData) return;
      event.clipboardData.setData("text/plain", url.value);
      event.preventDefault();
      wrote = true;
    };
    doc.addEventListener("copy", onCopy);
    try {
      return typeof doc.execCommand === "function" && doc.execCommand("copy") === true && wrote;
    } catch (_) {
      return false;
    } finally {
      doc.removeEventListener("copy", onCopy);
    }
  }

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url.value);
    } catch (_err) {
      if (!copyByCommand()) {
        // Focus moves to the selected address, ready for Ctrl+C.
        url.focus();
        url.select();
        announceStatus(srStatus, COPY_FAILED_TEXT, { dwellMs: 6000, force: true, tone: "error" });
        return;
      }
    }
    announceStatus(srStatus, COPIED_TEXT, { dwellMs: 3000, force: true });
  });

  // Sends the newest settled choice, one save at a time.  A choice made
  // while a save is out waits for it, then goes only if it still differs
  // from what the server has.
  async function flushBitrate() {
    settleTimer = null;
    if (saving || pendingBitrate === null) return;
    const chosen = pendingBitrate;
    pendingBitrate = null;
    if (chosen === savedBitrate) return;
    saving = true;
    let ok = false;
    try {
      ok = await saveBitrate(Number(chosen), bitrate);
    } finally {
      saving = false;
    }
    if (!active) return;
    if (ok) savedBitrate = chosen;
    if (pendingBitrate !== null) {
      if (settleTimer === null) void flushBitrate();
      return;
    }
    // postSettings has said why; the select shows what the server has.
    if (!ok && bitrate.value === chosen) bitrate.value = savedBitrate;
  }

  bitrate.addEventListener("change", () => {
    pendingBitrate = bitrate.value;
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => void flushBitrate(), BITRATE_SETTLE_MS);
  });

  // Claims the link_changed event this rotation causes, both for speech
  // (stream mode) and for the address lookup (here).
  function claimOwnRotation() {
    const releaseSpeech = claimLinkChange();
    const token = {};
    ownRotation = token;
    return () => {
      releaseSpeech();
      if (ownRotation === token) ownRotation = null;
    };
  }

  rotateBtn.addEventListener("click", async () => {
    confirming = true;
    const confirmed = await confirmAction(doc, {
      title: "Make a new stream link?",
      message: "Every speaker using the current link stops, and you will need to add the new link to them.",
      confirmLabel: "Make new link",
    });
    confirming = false;
    if (!active) return;
    // Cancel, Escape and confirm all leave focus on the button.
    rotateBtn.focus();
    if (!confirmed) return;
    const release = claimOwnRotation();
    let info;
    try {
      info = await rotate();
    } catch (err) {
      release();
      if (!active || isAuthFailure(err)) return;
      announceStatus(settingsStatus, `Could not make a new link: ${err.message}`,
        { dwellMs: 6000, force: true, tone: "error" });
      return;
    }
    // The event normally arrives within a push or two; a claim that
    // outlives that would swallow a later change made elsewhere.
    setTimeout(release, LINK_CLAIM_TIMEOUT_MS);
    if (!active) return;
    fill(info);
    announceStatus(srStatus, LINK_MADE_TEXT, { dwellMs: 5000, force: true });
  });

  return {
    apply,
    refresh,
    reset,
    retryIfMissing: () => retryIfMissing(),
  };
}
