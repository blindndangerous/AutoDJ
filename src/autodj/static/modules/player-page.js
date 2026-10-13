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
//   - The song line is the only other live region (status-page.js rewrites
//     it only when the song changes); the recent songs list is plain text.
//   - Focus never moves.

import { startStatusPage, trackText } from "./status-page.js";

export const CONNECTING_TEXT = "Connecting to the stream.";
export const PLAYING_TEXT = "Playing.";
export const STOPPED_TEXT = "Stopped.";
export const PLAY_FAILED_TEXT =
  "The stream could not be played. The station may be off air or full. Try again later.";
export const ENDED_TEXT = "The stream ended. Press Play to listen again.";
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
  let stored = null;
  try {
    stored = storage?.getItem(VOLUME_KEY) ?? null;
  } catch { /* storage blocked: the default */ }
  const value = Number(stored);
  return stored !== null && stored !== "" && Number.isFinite(value) ? value : 100;
}

function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function startPlayer({ doc = document, storage = defaultStorage() } = {}) {
  const audio = doc.getElementById("player-audio");
  const controls = doc.getElementById("player-controls");
  const toggle = doc.getElementById("player-toggle");
  const volume = doc.getElementById("player-volume");
  const volumeValue = doc.getElementById("player-volume-value");
  const state = doc.getElementById("player-state");
  const src = streamUrl(doc);
  if (!audio || !controls || !toggle || !src) return null;

  // The script's own controls replace the browser's.  Dropping the
  // rendered src and reloading leaves the element empty (no source chosen,
  // nothing fetched) until Play.
  audio.removeAttribute("controls");
  audio.removeAttribute("src");
  audio.load();
  controls.hidden = false;
  // Whether the listener has asked to listen.
  let wanted = false;

  function say(text) {
    if (state && state.textContent !== text) state.textContent = text;
  }

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
    release();
    setToggle("Play");
    say(message);
  }

  function play() {
    wanted = true;
    setToggle("Stop");
    say(CONNECTING_TEXT);
    audio.src = src;
    let started;
    try {
      started = audio.play();
    } catch {
      stop(PLAY_FAILED_TEXT);
      return;
    }
    started?.catch?.((error) => {
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

  applyVolume(storedVolume(storage));
  volume?.addEventListener("input", () => {
    const level = applyVolume(volume.value);
    try {
      storage?.setItem(VOLUME_KEY, String(level));
    } catch { /* storage blocked: this visit only */ }
  });

  toggle.addEventListener("click", () => {
    if (wanted) stop(STOPPED_TEXT);
    else play();
  });
  audio.addEventListener("playing", () => {
    if (wanted) say(PLAYING_TEXT);
  });
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
