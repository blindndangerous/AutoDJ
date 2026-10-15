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
//   - While the page's own connection to AutoDJ is down, a stream that
//     drops or stalls is not reported: the connection status already says
//     the link is down.  Listen here stays on (pressed) and asks for the
//     stream again until the link is back, and at once when it is; only a
//     stream that still fails then is reported and turned off.

import { announceStatus } from "./live-region.js";

export const STREAM_DELAY_ESTIMATE_S = 3;
// A buffer reading beyond this is a stalled or bogus element, not a delay.
const MAX_MEASURED_DELAY_S = 30;
const IDLE_TEXT =
  "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";
const EVENT_TEXT = {
  set_started: "New set started.",
  set_stopped: "Set stopped. Nobody is listening.",
};
const BLOCKED_TEXT = "The browser blocked playback. Press Listen here again.";
const NOT_LOADED_TEXT = "The stream did not load. It may be full or not running.";
const DROPPED_TEXT = "The stream connection dropped.";
const STALLED_TEXT = "The stream stopped responding.";
// A stalled or waiting stream whose playback has not moved on by then is
// dead.
const STALL_TIMEOUT_MS = 15000;
// Playback that moved less than this over the timeout has not moved.
const STALL_PROGRESS_S = 1;
// HTMLMediaElement.HAVE_FUTURE_DATA: enough is buffered to keep playing.
const HAVE_FUTURE_DATA = 3;
// The silent retry after a drop asks for the link again.  When that
// request cannot reach AutoDJ (the network or a tunnel in front of it is
// down for a moment) it is tried again after each of these waits, which
// together stay inside the server's 30 s idle grace, before the drop is
// reported.  While the page's link is down the last wait repeats until
// the link is back.
const LOOKUP_RETRY_MS = [2000, 4000, 8000, 12000];

// A link lookup that never reached AutoDJ, as opposed to one it refused.
function isUnreachable(err) {
  const name = err && err.name;
  if (name === "TypeError" || name === "NetworkError") return true;
  return Boolean(err) && err.unreachable === true;
}

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
  let lookupRetries = 0;
  let lookupTimer = null;
  let seenFirstApply = false;
  let lastEventKey = null;
  // Whether the page's websocket to AutoDJ is up (see setLinkUp).
  let linkUp = true;

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

  function clearLookupRetry() {
    lookupRetries = 0;
    if (lookupTimer === null) return;
    clearTimeout(lookupTimer);
    lookupTimer = null;
  }

  // Only a real listen is torn down; a browser-mode push is a no-op.
  // Bumping the generation cancels a start still waiting on the link.
  function stop() {
    if (!listening) return;
    generation += 1;
    clearStall();
    clearLookupRetry();
    setListening(false);
    release();
  }

  function fail(text) {
    stop();
    say(text, "error");
  }

  // Always asks for the current link, including after a dropped connection.
  // A failed silent retry is reported as the dropped connection it follows.
  async function connect({ retry = false } = {}) {
    const mine = ++generation;
    setListening(true);
    try {
      const info = await fetchInfo();
      if (mine !== generation) return;
      // Firefox can reuse an old stream offset after reload, even with no-store.
      const source = new URL(info.path, audio.ownerDocument.baseURI);
      const connection = `${Date.now()}-${mine}-${Math.random().toString(36).slice(2)}`;
      source.searchParams.set("connection", connection);
      audio.src = source.href;
      beforePlay();
      await audio.play();
    } catch (err) {
      if (mine !== generation) return;
      if (retry && isUnreachable(err) && (!linkUp || lookupRetries < LOOKUP_RETRY_MS.length)) {
        // Still listening, quietly: the page's connection status already
        // says the link is down.
        retryLater();
        return;
      }
      fail(retry ? DROPPED_TEXT : startFailureText(err));
    }
  }

  function retryLater() {
    const mine = generation;
    const delay = LOOKUP_RETRY_MS[Math.min(lookupRetries, LOOKUP_RETRY_MS.length - 1)];
    lookupRetries += 1;
    lookupTimer = setTimeout(() => {
      lookupTimer = null;
      if (mine === generation && listening) void connect({ retry: true });
    }, delay);
  }

  // A stream that never played is a failed start (for example the
  // listener limit).  Once it has played, a drop gets one silent retry
  // (a bitrate change or a blip); a second drop before audio plays again is
  // reported, unless the page's link is down too.
  function onDropped() {
    if (!listening) return;
    // The stall countdown belongs to the connection that dropped; the
    // retry gets its own.
    clearStall();
    if (!hasPlayed || (retried && linkUp)) {
      fail(mediaFailureText(audio, hasPlayed));
      return;
    }
    reconnect();
  }

  // The quiet retry of a stream that played.  The first goes at once; one
  // that failed too, while the link is down, waits like a lookup, so a
  // stream that keeps failing is not asked for in a tight loop.
  function reconnect() {
    const again = retried;
    retried = true;
    if (lookupTimer !== null) {
      clearTimeout(lookupTimer);
      lookupTimer = null;
    }
    if (again) retryLater();
    else void connect({ retry: true });
  }
  audio.addEventListener("error", onDropped);
  audio.addEventListener("ended", onDropped);
  audio.addEventListener("playing", () => {
    hasPlayed = true;
    retried = false;
    clearStall();
    clearLookupRetry();
  });
  // A connection can hang without ever raising an error.  The first stall
  // signal starts one countdown; audio playing again cancels it.  A
  // "stalled" event only says the download paused, and it fires while the
  // buffer still covers playback (with no "waiting", so no "playing"
  // after it), so the countdown fails the stream only when playback
  // itself has not moved on and nothing is buffered to play.
  function position() {
    try {
      return audio.currentTime;
    } catch (_) {
      return 0;
    }
  }
  function playingWell() {
    try {
      return !audio.paused && audio.readyState >= HAVE_FUTURE_DATA;
    } catch (_) {
      return false;
    }
  }
  function armStall() {
    const startedAt = position();
    stallTimer = setTimeout(() => {
      stallTimer = null;
      if (!listening || playingWell()) return;
      // Still moving, just not well: watch it for another round.
      if (position() - startedAt >= STALL_PROGRESS_S) {
        armStall();
        return;
      }
      // A stream that played is asked for again, like a drop: AutoDJ
      // stopping or the network going away often hangs the connection
      // without an error.
      if (hasPlayed && (!retried || !linkUp)) {
        reconnect();
        return;
      }
      fail(STALLED_TEXT);
    }, STALL_TIMEOUT_MS);
  }
  function onStalled() {
    // While a retry waits on the link, the old connection's stalls are
    // already being dealt with.
    if (!listening || stallTimer !== null || lookupTimer !== null) return;
    armStall();
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

  // Told by app.js when the page's websocket opens (true) or drops
  // (false).  A retry waiting on the link goes at once when it is back.
  function setLinkUp(up) {
    linkUp = Boolean(up);
    if (!linkUp || !listening || lookupTimer === null) return;
    clearLookupRetry();
    void connect({ retry: true });
  }

  // Another station's pushes follow (#70): its last station event is
  // remembered, not spoken, as on a fresh page.
  function forgetEvents() {
    seenFirstApply = false;
    lastEventKey = null;
  }

  return {
    apply, toggleListen, delaySeconds, stop, forgetEvents, setLinkUp,
    isListening: () => listening,
  };
}

// ----------------------------------------------------------------
// Settings > Stream: the address, the playlist file and the quality.
//
// Speech rules:
//   - The listener count and the "not loaded yet" note are plain text,
//     never live regions, and are only rewritten when they change.
//   - The "only works on this computer" note is fixed plain text, shown
//     once at start-up when the page was opened on a loopback address,
//     and then also describes the address field and Copy address.
//   - Copy is a user action, so its confirmation is forced: pressing it
//     twice is heard twice.
//   - Copy never moves focus through the address field on success: NVDA
//     would read the whole address aloud.
//   - Failures of a user action go to the settings region with the error
//     tone, like every other settings failure.
//   - The quality select is never rewritten while it has focus or while
//     a change of it is still being saved.
// ----------------------------------------------------------------

const COPIED_TEXT = "Stream address copied.";
const COPY_FAILED_TEXT = "Could not copy. Select the address and copy it yourself.";
export const NOT_LOADED_NOTE = "Stream address not loaded yet.";
// Arrowing through a closed select fires change on every step, and each
// save restarts the encoder and drops every speaker, so saves wait for
// the choice to settle.
export const BITRATE_SETTLE_MS = 600;
// While the address is missing, a state push retries at most this often.
export const ADDRESS_RETRY_MS = 10000;
// The copied address is built from this page's origin, so a page opened
// as localhost copies a link that a speaker on the network cannot reach.
export function isLoopbackHost(hostname) {
  const host = String(hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  return host === "localhost" || host.endsWith(".localhost")
    || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);
}

function listenerText(count) {
  return `${count} listener${count === 1 ? "" : "s"}`;
}

const RELAY_TYPE_LABELS = {
  icecast: "Icecast",
  shoutcast1: "Shoutcast 1",
  shoutcast2: "Shoutcast 2",
};

// One sentence per relay, read in order by a screen reader: who, where,
// then the state, with the last error while it is not connected.
export function relayText(relay) {
  const type = RELAY_TYPE_LABELS[relay.type] || relay.type;
  let state;
  if (relay.state === "live") state = "live";
  else if (relay.state === "connecting") state = "connecting";
  else if (relay.state === "retrying") {
    state = `not connected, trying again. Last error: ${relay.error || "unknown"}`;
  } else if (relay.state === "off") state = "off in the configuration";
  else state = "stopped";
  let text = `${relay.name}, ${type} at ${relay.target}: ${state}.`;
  if (relay.title_error) text += ` Song titles are not updating: ${relay.title_error}.`;
  return text;
}

// A 401 already opened the sign-in dialog; saying more would only talk
// over it.
function isAuthFailure(err) {
  return Boolean(err) && err.name === "AuthenticationRequiredError";
}

export function createStreamSettings({
  doc,
  fetchInfo,
  saveBitrate,
  saveContinuous = async () => false,
  saveStationName = async () => false,
  onRelays = () => {},
  // Told when the Stream settings appear and go, so the Settings subtab
  // for them can follow.
  setAvailable = () => {},
  // Where focus goes when the Stream settings vanish while focus is in
  // them.  The default suits a page that keeps them in a <details> card.
  focusAway = null,
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
  const continuous = doc.getElementById("stream-continuous");
  const stationName = doc.getElementById("stream-station-name");
  const listeners = doc.getElementById("stream-listeners");
  const relayBox = doc.getElementById("stream-relays");
  const relayList = doc.getElementById("stream-relays-list");
  let active = false;
  // Bumped by every source of stream details, so a slow lookup cannot
  // overwrite a newer link.
  let generation = 0;
  // The lookup in flight, if any (fill() bumps the generation, so the
  // generation cannot say when a lookup has finished).
  let lookup = null;
  let lastAttempt = -Infinity;
  let failureSpoken = false;
  // Quality saves: the value the server last confirmed, the newest
  // choice not yet sent, the settle timer and whether a save is out.
  let savedBitrate = bitrate.value;
  // The station name the server last confirmed.
  let savedStationName = stationName?.value ?? "";
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
    if (doc.activeElement !== continuous) continuous.checked = Boolean(info.continuous);
    showStationName(info.station_name);
    for (const [field, key] of [["genre", "genre"], ["description", "description"], ["url", "url"]]) {
      const input = doc.getElementById(`stream-station-${field}`);
      if (input && doc.activeElement !== input && typeof info[key] === "string") input.value = info[key];
    }
  }

  function showStationName(name) {
    if (!stationName || typeof name !== "string") return;
    savedStationName = name;
    if (doc.activeElement !== stationName && stationName.value !== name) stationName.value = name;
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
    stopBitrateSaves();
    fieldset.hidden = true;
    setAvailable(false);
  }

  // Sign-out: clear the address along with the rest of the session state.
  function reset() {
    hide();
    url.value = "";
    m3u.removeAttribute("href");
    listeners.textContent = "";
    showRelays([]);
    failureSpoken = false;
    lastAttempt = -Infinity;
  }

  // Plain text, not a live region: a relay retrying every minute would
  // otherwise interrupt whatever the user is doing.  Items are rebuilt
  // only when some text changed, so reading the list is not disturbed.
  function showRelays(relays) {
    onRelays(relays);
    if (!relayBox || !relayList) return;
    const lines = Array.isArray(relays) ? relays.map(relayText) : [];
    const current = [...relayList.children].map((item) => item.textContent);
    if (lines.join("\n") !== current.join("\n")) {
      relayList.replaceChildren(...lines.map((line) => {
        const item = doc.createElement("li");
        item.textContent = line;
        return item;
      }));
    }
    if (relayBox.hidden !== (lines.length === 0)) relayBox.hidden = lines.length === 0;
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
      const hadFocus = fieldset.contains(doc.activeElement);
      hide();
      if (hadFocus) {
        if (focusAway) focusAway();
        else fieldset.closest("details")?.querySelector("summary")?.focus();
      }
      return;
    }
    if (fieldset.hidden) {
      fieldset.hidden = false;
      setAvailable(true);
    }
    const text = listenerText(Number(state.stream_listeners) || 0);
    if (listeners.textContent !== text) listeners.textContent = text;
    showRelays(state.stream_relays);
    applyPushedBitrate(state);
    showStationName(state.settings?.playback?.stream_station_name);
    if (!active) {
      active = true;
      showNote(!url.value);
      void refresh();
      return;
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
  continuous.addEventListener("change", async () => {
    const chosen = continuous.checked;
    continuous.disabled = true;
    try {
      if (!await saveContinuous(chosen, continuous)) continuous.checked = !chosen;
    } finally {
      continuous.disabled = false;
    }
  });
  stationName?.addEventListener("change", async () => {
    const chosen = stationName.value.trim();
    if (chosen === savedStationName) {
      stationName.value = savedStationName;
      return;
    }
    // The server refuses a blank name and says so; the field goes back.
    if (await saveStationName(chosen, stationName)) savedStationName = chosen;
    else stationName.value = savedStationName;
  });

  return {
    apply,
    refresh,
    reset,
    retryIfMissing: () => retryIfMissing(),
  };
}
