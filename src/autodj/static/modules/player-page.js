// The public player pages (/player and /embed, and each station's
// /stations/<slug>/player and /embed, rendered by player_page.py): a Play
// button and a Volume slider for the station's stream, with its song kept
// current by the status page's polling loop (status-page.js).
//
// Rules (docs/accessibility-testing.md, Announcement contract):
//   - Nothing plays until the listener presses Play: the <audio> element
//     has preload="none" and no autoplay, and this script never calls
//     play() on its own.
//   - Play and Stop are one native button whose name says what it will do
//     ("Play" or "Stop"); it has no aria-pressed, so the name alone is the
//     state.  What happened is said once in #player-state (role="status"):
//     connecting, playing, stopped, or why it could not play.
//   - Stop drops the connection, so a stopped page holds no listener slot,
//     and Play joins the live stream again rather than resuming old audio.
//   - The Volume slider is a native range input with a visible label; its
//     value is read as a percentage and remembered in this browser only.
//     Where the browser will not let a page set the volume (iOS), the
//     slider is hidden and a note says the device's own volume applies.
//   - A stall while playing says "Buffering." once, and "Playing." again
//     when the sound is back; a stream that has not started 15 seconds
//     after Play stops and says it could not be played.
//   - The song line is the only other live region (status-page.js rewrites
//     it only when the song changes); the recent songs list is plain text.
//   - Focus never moves.

import { makeSay } from "./live-region.js";
import { startStatusPage, trackText } from "./status-page.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

export const CONNECTING_TEXT = "Connecting to the stream.";
export const PLAYING_TEXT = "Playing.";
export const STOPPED_TEXT = "Stopped.";
export const PLAY_FAILED_TEXT =
  "The stream could not be played. The station may be off air or full. Try again later.";
export const ENDED_TEXT = "The stream ended. Press Play to listen again.";
export const BUFFERING_TEXT = "Buffering.";
// Play gives up when nothing has started this long after it was pressed.
export const START_TIMEOUT_MS = 15000;
export const VOLUME_KEY = "autodj.player.volume";
const HISTORY_EMPTY_TEXT = "No songs have finished playing yet.";

// The station's stream: an .mp3 path on this site from data-stream, or
// null.  As for the feed (status-page.js feedUrl), anything that could
// reach another host is refused; the page's media-src 'self' policy
// refuses it as well.
export function streamUrl(doc) {
  const stream = doc.body?.dataset?.stream || "";
  if (!stream.startsWith("/") || stream.startsWith("//") || /\\|%5c/i.test(stream)) return null;
  const here = doc.location?.href || "http://localhost/";
  let url;
  try {
    url = new URL(stream, here);
  } catch {
    return null;
  }
  if (url.origin !== new URL(here).origin || !url.pathname.endsWith(".mp3")) return null;
  return url.pathname;
}

function storedVolume(storage) {
  const stored = readStorage(storage, VOLUME_KEY);
  const value = Number(stored);
  return stored !== null && stored !== "" && Number.isFinite(value) ? value : 100;
}

// Whether this browser lets a page change an element's volume: iOS keeps
// it at 1 whatever is set.
function volumeSettable(audio) {
  const before = audio.volume;
  try {
    audio.volume = before === 0.5 ? 0.25 : 0.5;
  } catch {
    return false;
  }
  const settable = audio.volume !== before;
  try {
    audio.volume = before;
  } catch { /* unchanged */ }
  return settable;
}

export function startPlayer({
  doc = document,
  storage = defaultStorage(),
  setTimer = (callback, ms) => setTimeout(callback, ms),
  clearTimer = (id) => clearTimeout(id),
} = {}) {
  const audio = doc.getElementById("player-audio");
  const controls = doc.getElementById("player-controls");
  const toggle = doc.getElementById("player-toggle");
  const volume = doc.getElementById("player-volume");
  const volumeValue = doc.getElementById("player-volume-value");
  const state = doc.getElementById("player-state");
  const volumeGroup = doc.getElementById("player-volume-group");
  const volumeNote = doc.getElementById("player-volume-note");
  const src = streamUrl(doc);
  if (!audio || !controls || !toggle || !src) return null;

  // The script's own controls replace the browser's.  Dropping the
  // rendered src and reloading leaves the element empty (no source chosen,
  // nothing fetched) until Play.
  audio.removeAttribute("controls");
  audio.removeAttribute("src");
  audio.load();
  controls.hidden = false;
  // Whether the listener has asked to listen, and whether sound has
  // started since.
  let wanted = false;
  let started = false;
  let startTimer = null;

  function clearStartTimer() {
    if (startTimer !== null) clearTimer(startTimer);
    startTimer = null;
  }

  const say = makeSay(state, { force: false, mirror: false });

  function setToggle(label) {
    if (toggle.textContent !== label) toggle.textContent = label;
  }

  // Close the connection: no buffered audio, no listener slot held.
  function release() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }

  function stop(message) {
    wanted = false;
    started = false;
    clearStartTimer();
    release();
    setToggle("Play");
    say(message);
  }

  function play() {
    wanted = true;
    setToggle("Stop");
    say(CONNECTING_TEXT);
    audio.src = src;
    clearStartTimer();
    startTimer = setTimer(() => {
      startTimer = null;
      if (wanted && !started) stop(PLAY_FAILED_TEXT);
    }, START_TIMEOUT_MS);
    let playing;
    try {
      playing = audio.play();
    } catch {
      stop(PLAY_FAILED_TEXT);
      return;
    }
    playing?.catch?.((error) => {
      // Stop pressed before it started: nothing to report.
      if (!wanted || error?.name === "AbortError") return;
      stop(PLAY_FAILED_TEXT);
    });
  }

  function applyVolume(value) {
    const level = Math.min(100, Math.max(0, Math.round(Number(value) || 0)));
    audio.volume = level / 100;
    const text = `${level}%`;
    if (volume) {
      if (volume.value !== String(level)) volume.value = String(level);
      volume.setAttribute("aria-valuetext", text);
    }
    if (volumeValue) volumeValue.textContent = text;
    return level;
  }

  if (volume && !volumeSettable(audio)) {
    // The slider would do nothing: say so instead of offering it.
    if (volumeGroup) volumeGroup.hidden = true;
    if (volumeNote) volumeNote.hidden = false;
  } else {
    applyVolume(storedVolume(storage));
  }
  volume?.addEventListener("input", () => {
    const level = applyVolume(volume.value);
    writeStorage(storage, VOLUME_KEY, String(level));
  });

  toggle.addEventListener("click", () => {
    if (wanted) stop(STOPPED_TEXT);
    else play();
  });
  audio.addEventListener("playing", () => {
    if (!wanted) return;
    started = true;
    clearStartTimer();
    say(PLAYING_TEXT);
  });
  // A stall once sound has started; before that it is still connecting.
  for (const name of ["waiting", "stalled"]) {
    audio.addEventListener(name, () => {
      if (wanted && started) say(BUFFERING_TEXT);
    });
  }
  audio.addEventListener("error", () => {
    if (wanted) stop(PLAY_FAILED_TEXT);
  });
  audio.addEventListener("ended", () => {
    if (wanted) stop(ENDED_TEXT);
  });
  // Paused from outside the page (a media key, the system's media
  // controls): a live stream cannot resume where it paused, so stop.  The
  // end of the stream also pauses, just before "ended".
  audio.addEventListener("pause", () => {
    if (wanted) stop(audio.ended ? ENDED_TEXT : STOPPED_TEXT);
  });

  return {
    get playing() { return wanted; },
    play,
    stop: () => stop(STOPPED_TEXT),
  };
}

function historyKey(history) {
  return history.map((row) => [row.artist, row.title, row.played_at].join("\u0000")).join("\u0001");
}

// The recent songs list, rebuilt only when the songs in it change.  Plain
// text: a new song is never announced from here.
export function historyRenderer(doc = document) {
  const list = doc.getElementById("player-history");
  const empty = doc.getElementById("player-history-empty");
  let lastKey = null;
  return (feed) => {
    if (!list) return;
    const history = Array.isArray(feed?.history)
      ? feed.history.filter((row) => row && typeof row === "object")
      : [];
    const key = historyKey(history);
    if (key === lastKey) return;
    lastKey = key;
    const items = history.map((row) => {
      const item = doc.createElement("li");
      item.textContent = trackText(row, "");
      return item;
    });
    const same = items.length === list.children.length
      && items.every((item, index) => item.textContent === list.children[index].textContent);
    if (!same) list.replaceChildren(...items);
    list.hidden = items.length === 0;
    if (empty) {
      empty.hidden = items.length > 0;
      if (empty.textContent !== HISTORY_EMPTY_TEXT) empty.textContent = HISTORY_EMPTY_TEXT;
    }
  };
}

export function startPlayerPage(doc = document) {
  const page = doc.body?.dataset.page;
  if (page !== "player" && page !== "embed") return null;
  const player = startPlayer({ doc });
  const feed = startStatusPage({
    doc,
    title: (station) => (page === "player" ? `${station}: listen live` : `${station} player`),
    onFeed: historyRenderer(doc),
  });
  return { player, feed };
}

if (typeof document !== "undefined") startPlayerPage();
