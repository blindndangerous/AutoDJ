// The public status page (/status, rendered by status_page.py): keeps its
// plain-text values current by polling the public now-playing feed.
//
// Speech rules (docs/accessibility-testing.md, Announcement contract):
//   - Only the now-playing line is a live region (polite, atomic), and it
//     is rewritten only when the song changes, so a poll that finds the
//     same song says nothing.
//   - Listener counts, the peak, the uptime, the next song, the show on
//     air and the update note are plain text: they change without speaking.
//   - The wording matches status_page.py, so the page reads the same with
//     and without JavaScript.
//   - Pause updates (WCAG 2.2.2) keeps a fixed name; only aria-pressed
//     changes, so a screen reader says "pressed" or "not pressed".

import { requestJson } from "./api-client.js";

const FEED_URL = "/api/nowplaying";
const POLL_MS = 15000;
export const NOTHING_PLAYING = "Nothing is playing right now.";
export const UPDATING_TEXT = "Updates every 15 seconds.";
export const UNREACHABLE_TEXT = "Could not reach the station. Trying again every 15 seconds.";
export const PAUSED_TEXT = "Updates paused.";

function count(value) {
  const number = Math.floor(Number(value));
  return Number.isFinite(number) && number > 0 ? number : 0;
}

// "Title by Artist, from the album Album."
export function trackText(track, empty) {
  if (!track) return empty;
  const title = String(track.title || "").trim() || "Unknown title";
  const artist = String(track.artist || "").trim() || "an unknown artist";
  let text = `${title} by ${artist}`;
  const album = String(track.album || "").trim();
  if (album) text += `, from the album ${album}`;
  return `${text}.`;
}

// "On air: Remembering the 2000s, until 21:00."  The end time is read
// from the station-zone ISO time as written, as status_page.py does.
export function showText(show) {
  if (!show) return "";
  const name = String(show.name || "").trim();
  if (!name) return "";
  const ends = String(show.ends_at || "");
  const until = ends.length >= 16 && ends[13] === ":" ? `, until ${ends.slice(11, 16)}` : "";
  return `On air: ${name}${until}.`;
}

export function listenersText(value) {
  const number = count(value);
  return `${number} listener${number === 1 ? "" : "s"}`;
}

// To the minute: "2 days, 3 hours, 1 minute".
export function uptimeText(seconds) {
  const total = count(seconds);
  if (total < 60) return "less than a minute";
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return [[days, "day"], [hours, "hour"], [minutes, "minute"]]
    .filter(([value]) => value)
    .map(([value, unit]) => `${value} ${unit}${value === 1 ? "" : "s"}`)
    .join(", ");
}

function trackKey(track) {
  if (!track) return "";
  return [track.artist, track.title, track.album, track.started_at].join("\u0000");
}

// The feed sends Cache-Control: no-cache, so each poll revalidates.
function fetchFeed() {
  return requestJson(FEED_URL, { cache: "no-cache", headers: { Accept: "application/json" } });
}

export function startStatusPage({
  doc = document,
  load = fetchFeed,
  setTimer = (callback, ms) => setTimeout(callback, ms),
  clearTimer = (id) => clearTimeout(id),
} = {}) {
  const els = {
    station: doc.getElementById("status-station"),
    now: doc.getElementById("status-now"),
    show: doc.getElementById("status-show"),
    nextSection: doc.getElementById("status-next-section"),
    next: doc.getElementById("status-next"),
    listeners: doc.getElementById("status-listeners"),
    peak: doc.getElementById("status-peak"),
    uptime: doc.getElementById("status-uptime"),
    updated: doc.getElementById("status-updated"),
    pause: doc.getElementById("status-pause"),
  };
  if (!els.now) return null;
  // The song the server rendered; unknown until the first poll, which
  // then only remembers it unless the text differs.
  let lastKey = null;
  let timer = null;
  let stopped = false;
  let paused = false;

  function setText(element, text) {
    if (element && element.textContent !== text) element.textContent = text;
  }

  function apply(feed) {
    const station = String(feed.station_name || "AutoDJ");
    setText(els.station, station);
    const title = `${station}: station status`;
    if (doc.title !== title) doc.title = title;
    const key = trackKey(feed.current_track);
    if (key !== lastKey) {
      lastKey = key;
      // A live region speaks only when its text changes.
      setText(els.now, trackText(feed.current_track, NOTHING_PLAYING));
    }
    const show = showText(feed.show);
    if (els.show) {
      setText(els.show, show);
      if (els.show.hidden === Boolean(show)) els.show.hidden = !show;
    }
    const next = feed.next_track;
    if (els.nextSection && els.nextSection.hidden === Boolean(next)) els.nextSection.hidden = !next;
    if (next) setText(els.next, trackText(next, ""));
    setText(els.listeners, listenersText(feed.listener_count));
    setText(els.peak, listenersText(feed.peak_listeners_today));
    setText(els.uptime, uptimeText(feed.uptime_seconds));
  }

  // Each poll owns the loop until a newer one starts (Resume, or the tab
  // shown again), so two loops never run side by side.
  let run = 0;

  async function poll() {
    const mine = ++run;
    timer = null;
    if (stopped || paused) return;
    if (!doc.hidden) {
      let feed = null;
      try {
        feed = await load();
      } catch (_) {
        feed = null;
      }
      if (mine !== run || stopped || paused) return;
      if (feed) {
        apply(feed);
        setText(els.updated, UPDATING_TEXT);
      } else {
        setText(els.updated, UNREACHABLE_TEXT);
      }
    }
    timer = setTimer(poll, POLL_MS);
  }

  if (els.pause) {
    els.pause.hidden = false;
    els.pause.addEventListener("click", () => {
      paused = !paused;
      els.pause.setAttribute("aria-pressed", paused ? "true" : "false");
      if (timer !== null) clearTimer(timer);
      timer = null;
      if (paused) setText(els.updated, PAUSED_TEXT);
      else void poll();
    });
  }

  // A hidden tab skips its polls; showing it again updates at once.
  doc.addEventListener("visibilitychange", () => {
    if (doc.hidden || stopped || paused) return;
    if (timer !== null) clearTimer(timer);
    void poll();
  });

  void poll();
  return {
    poll,
    stop() {
      stopped = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}

if (typeof document !== "undefined" && document.getElementById("status-now")) {
  startStatusPage();
}
