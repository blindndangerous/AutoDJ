// Lyrics rendering: timestamped LRC scroll + plain-text fallback.
// Nothing here speaks by itself.  Lyrics are read on demand in the
// Lyrics card, where the active line carries aria-current so a screen
// reader says "current" on it, or with Shift+L (currentLyricLine below).
// The auto-read live region was removed in 0.18.0: lines are not to be
// spoken over the music.

import { escHtml } from "./dom-helpers.js";
import { requestJson } from "./api-client.js";

const state = {
  cached: [],          // full list, used by the visible scroll
  lastIndex: null,     // skip the highlight work when the line is unchanged
  plain: false,        // untimed lyrics: there is no current line
};
// The lyrics request in flight: a newer one aborts it.
let lyricsRequest = null;

export function resetLyricState(elements) {
  lyricsRequest?.abort();
  state.lastIndex = null;
  state.cached = [];
  state.plain = false;
  if (elements) renderLyricsList(elements);
}

// What Shift+L says, once, on request.  A blank timed line is an
// instrumental break; before the first line or on a blank one, the next
// sung line is given too.
export function currentLyricLine() {
  if (state.plain) return "Lyrics for this track are not timed.";
  if (state.cached.length === 0) return "No lyrics for this track.";
  const text = (i) => String(state.cached[i].text || "").trim();
  const idx = state.lastIndex;
  if (idx !== null && text(idx)) return text(idx);
  for (let i = idx === null ? 0 : idx + 1; i < state.cached.length; i++) {
    if (text(i)) return `Instrumental. Next line: ${text(i)}`;
  }
  return "Instrumental.";
}

// Belt and braces for the raw-timestamp defect.  The server parses LRC
// out of sidecars and embedded tags now, but any plain text that still
// arrives carrying cue syntax must not print it on screen or read it
// aloud.  Mirrors audio_meta.strip_lyric_timestamps exactly:
//   * only a LEADING stamp run is removed, so "meet me at [10:30]
//     tonight" survives intact instead of becoming "meet me at tonight";
//   * a line that is nothing but an "[xx:value]" header is dropped;
//   * enhanced per-word "<mm:ss.xx>" stamps go, because readers say them.
const LRC_LEADING_STAMP_RE = /^(?:\[\d{1,3}:\d{1,2}(?:\.\d{1,3})?\]\s*)+/;
const LRC_METADATA_LINE_RE = /^\[[A-Za-z_]{2,10}:[^\]]*\]$/;
const LRC_WORD_TAG_RE = /<\d{1,3}:\d{1,2}(?:\.\d{1,3})?>/g;
// U+FEFF is a format character, not whitespace: trim() leaves it behind.
const BOM_RE = /^\uFEFF/;

export function stripLyricTimestamps(text) {
  if (typeof text !== "string") return text;
  const kept = [];
  for (const raw of text.replace(BOM_RE, "").split(/\r?\n/)) {
    const line = raw.trim();
    if (LRC_METADATA_LINE_RE.test(line)) continue;
    kept.push(line
      .replace(LRC_LEADING_STAMP_RE, "")
      .replace(LRC_WORD_TAG_RE, "")
      .trim());
  }
  while (kept.length && !kept[kept.length - 1]) kept.pop();
  while (kept.length && !kept[0]) kept.shift();
  return kept.join("\n");
}

function hasPlainFallback(elements) {
  return elements.lyricsList.querySelector(".plain-lyrics") !== null;
}

function clearCurrentLine(lyricsList) {
  lyricsList.querySelectorAll("li").forEach((li) => {
    li.classList.remove("active");
    li.removeAttribute("aria-current");
  });
}

// Scroll ONLY the lyrics box.  Element.scrollIntoView walks every
// scrollable ancestor including the document, so with real synced
// lyrics it yanked the whole page every few seconds -- which is exactly
// the case the embedded-LRC fix has just made common.  The box scrolls
// smoothly, or at once under prefers-reduced-motion (app.css).
function scrollActiveLineIntoView(lyricsList, li) {
  const lineBox = li.getBoundingClientRect();
  const listBox = lyricsList.getBoundingClientRect();
  const top = lyricsList.scrollTop
    + (lineBox.top - listBox.top)
    - (listBox.height - lineBox.height) / 2;
  lyricsList.scrollTop = Math.max(0, top);
}

export async function loadLyrics(path, elements) {
  lyricsRequest?.abort();
  const request = lyricsRequest = new AbortController();
  state.cached = [];
  state.lastIndex = null;
  renderLyricsList(elements);
  try {
    const encodedPath = encodeURIComponent(path);
    const data = await requestJson(`/api/lyrics?path=${encodedPath}`, {
      signal: request.signal,
    });
    if (request.signal.aborted) return;
    if (data.path !== path) {
      throw new Error("Lyrics response did not match the requested track");
    }
    state.cached = Array.isArray(data.lyrics) ? data.lyrics : [];
    if (state.cached.length || !hasPlainFallback(elements)) {
      renderLyricsList(elements);
    }
    state.lastIndex = null;
  } catch (_) {
    // A failed load leaves the card as it was: hidden, or showing the
    // plain-text fallback.
    if (request.signal.aborted) return;
    state.cached = [];
    if (!hasPlainFallback(elements)) renderLyricsList(elements);
  }
}

// A blank timed line is an instrumental break.  NVDA has no name for the
// note glyph, so the line says so in words.
const INSTRUMENTAL_LINE =
  '<span aria-hidden="true">♫</span><span class="visually-hidden">Instrumental</span>';

export function renderLyricsList({ lyricsCard, lyricsList }) {
  if (state.cached.length === 0) {
    lyricsCard.hidden = true;
    lyricsList.innerHTML = "";
    return;
  }
  lyricsCard.hidden = false;
  lyricsList.innerHTML = state.cached
    .map((ll, i) => `<li data-i="${i}">${ll.text ? escHtml(ll.text) : INSTRUMENTAL_LINE}</li>`)
    .join("");
}

// Index of the last line whose timestamp has passed, or null before the
// first one.  Binary search: a long song is a few thousand lines and this
// runs on every websocket tick.
function lineIndexAt(lines, elapsed) {
  if (!Array.isArray(lines) || lines.length === 0) return null;
  if (!Number.isFinite(elapsed) || elapsed < Number(lines[0].time_s)) return null;
  let lo = 0;
  let hi = lines.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (Number(lines[mid].time_s) <= elapsed) lo = mid; else hi = mid - 1;
  }
  return lo;
}

// `localClock` means the browser owns the audio clock.  In that mode the
// server's own `elapsed` is 0.0 forever, so `lyric_index` never leaves
// null and the highlight could never move.  The deck's currentTime is the real
// position, so resolve the line here instead of trusting the server.
export function applyLyricsState(
  s,
  { lyricsCard, lyricsList },
  { elapsed = null, localClock = false } = {},
) {
  // Plain (unsynced) beets lyrics fallback -- show as a single block
  // when we have no timestamped .lrc list.  Updated on every track
  // change.
  state.plain = !s.has_lyrics && Boolean(s.lyrics_plain);
  if (state.plain) {
    clearCurrentLine(lyricsList);
    if (state.cached.length || lyricsList.querySelector(".plain-lyrics") === null) {
      state.cached = [];
      lyricsCard.hidden = false;
      lyricsList.innerHTML =
        `<li class="plain-lyrics" style="white-space:pre-wrap;list-style:none;padding-left:0">${escHtml(stripLyricTimestamps(s.lyrics_plain))}</li>`;
    }
    state.lastIndex = null;
    return;
  }
  if (!s.has_lyrics) {
    clearCurrentLine(lyricsList);
    state.lastIndex = null;
    return;
  }
  const local = localClock && state.cached.length
    ? lineIndexAt(state.cached, elapsed)
    : null;
  const idx = local !== null ? local : s.lyric_index;
  if (idx === state.lastIndex) return;
  state.lastIndex = idx;

  const items = lyricsList.querySelectorAll("li");
  items.forEach((li) => {
    li.classList.remove("active");
    li.removeAttribute("aria-current");
  });
  if (idx !== null && idx >= 0 && idx < items.length) {
    const li = items[idx];
    li.classList.add("active");
    li.setAttribute("aria-current", "true");
    scrollActiveLineIntoView(lyricsList, li);
  }
}
