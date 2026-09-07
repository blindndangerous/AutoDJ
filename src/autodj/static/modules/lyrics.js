// Lyrics rendering: timestamped LRC scroll + plain-text fallback.
// Active line announced via the polite #lyric-announce live region;
// pattern verified by accessibility-lead -- aria-live="polite" +
// aria-atomic="true" is the right ARIA mechanism for time-coded line
// updates that fire every few seconds.

import { escHtml } from "./dom-helpers.js";
import { requestJson } from "./api-client.js";
import { createLatestRequestOwner } from "./latest-request.js";

const state = {
  cached: [],          // full list, used by the visible scroll
  lastIndex: null,     // suppress repeated lyric announcements
  loadStatus: null,    // request status currently owned by the live region
  currentLineAnnouncement: null,
};
const lyricsRequestOwner = createLatestRequestOwner();

export function resetLyricState(elements) {
  lyricsRequestOwner.cancel();
  state.lastIndex = null;
  state.cached = [];
  state.loadStatus = null;
  state.currentLineAnnouncement = null;
  if (elements) {
    renderLyricsList(elements);
    if (elements.lyricAnnounce) elements.lyricAnnounce.textContent = "";
  }
}

function beginLoadStatus(request, elements) {
  state.currentLineAnnouncement = null;
  state.loadStatus = { message: "Loading lyrics", request };
  if (elements.lyricAnnounce) {
    elements.lyricAnnounce.textContent = state.loadStatus.message;
  }
}

function finishLoadStatus(request, elements, message) {
  const owned = state.loadStatus;
  if (!owned || owned.request !== request) return;
  state.loadStatus = message === "No lyrics available"
    ? { message, request }
    : null;
  const announce = elements.lyricAnnounce;
  if (announce && announce.textContent === owned.message) {
    announce.textContent = message;
  }
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

function claimPlainLyricsStatus(elements) {
  const owned = state.loadStatus;
  state.loadStatus = null;
  const announce = elements.lyricAnnounce;
  if (announce && owned && announce.textContent === owned.message) {
    announce.textContent = "Lyrics loaded";
  }
}

function clearCurrentLine({ lyricsList, lyricAnnounce }) {
  lyricsList.querySelectorAll("li").forEach((li) => {
    li.classList.remove("active");
    li.removeAttribute("aria-current");
  });
  if (lyricAnnounce
      && state.currentLineAnnouncement !== null
      && lyricAnnounce.textContent === state.currentLineAnnouncement) {
    lyricAnnounce.textContent = "";
  }
  state.currentLineAnnouncement = null;
}

// Scroll ONLY the lyrics box.  Element.scrollIntoView walks every
// scrollable ancestor including the document, so with real synced
// lyrics it yanked the whole page every few seconds -- which is exactly
// the case the embedded-LRC fix has just made common.
function scrollActiveLineIntoView(lyricsList, li) {
  if (!lyricsList || typeof li.getBoundingClientRect !== "function") return;
  const lineBox = li.getBoundingClientRect();
  const listBox = lyricsList.getBoundingClientRect();
  const top = lyricsList.scrollTop
    + (lineBox.top - listBox.top)
    - (listBox.height - lineBox.height) / 2;
  const behavior = prefersReducedMotion() ? "auto" : "smooth";
  if (typeof lyricsList.scrollTo === "function") {
    lyricsList.scrollTo({ top: Math.max(0, top), behavior });
  } else {
    lyricsList.scrollTop = Math.max(0, top);
  }
}

function prefersReducedMotion() {
  try {
    if (typeof globalThis.matchMedia !== "function") return true;
    return globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (_) {
    return true;
  }
}

export async function loadLyrics(path, elements) {
  const request = lyricsRequestOwner.begin();
  state.cached = [];
  state.lastIndex = null;
  renderLyricsList(elements);
  beginLoadStatus(request, elements);
  try {
    const encodedPath = encodeURIComponent(path);
    const data = await requestJson(`/api/lyrics?path=${encodedPath}`, {
      signal: request.signal,
    });
    if (!lyricsRequestOwner.isCurrent(request)) return;
    if (data.path !== path) {
      throw new Error("Lyrics response did not match the requested track");
    }
    state.cached = Array.isArray(data.lyrics) ? data.lyrics : [];
    if (state.cached.length || !hasPlainFallback(elements)) {
      renderLyricsList(elements);
    }
    state.lastIndex = null;
    finishLoadStatus(
      request,
      elements,
      state.cached.length ? "Lyrics loaded" : "No lyrics available",
    );
  } catch (errorValue) {
    if (!lyricsRequestOwner.isCurrent(request)) return;
    state.cached = [];
    if (!hasPlainFallback(elements)) renderLyricsList(elements);
    finishLoadStatus(
      request,
      elements,
      `Could not load lyrics: ${errorValue.message || errorValue}`,
    );
  } finally {
    lyricsRequestOwner.finish(request);
  }
}

export function renderLyricsList({ lyricsCard, lyricsList }) {
  if (state.cached.length === 0) {
    lyricsCard.hidden = true;
    lyricsList.innerHTML = "";
    return;
  }
  lyricsCard.hidden = false;
  lyricsList.innerHTML = state.cached
    .map((ll, i) => `<li data-i="${i}">${escHtml(ll.text || "♫")}</li>`)
    .join("");
}

export function applyLyricsState(s, { lyricsCard, lyricsList, lyricAnnounce }) {
  // Plain (unsynced) beets lyrics fallback -- show as a single block
  // when we have no timestamped .lrc list.  Updated on every track
  // change.
  if (!s.has_lyrics && s.lyrics_plain) {
    clearCurrentLine({ lyricsList, lyricAnnounce });
    if (state.cached.length || lyricsList.querySelector(".plain-lyrics") === null) {
      state.cached = [];
      lyricsCard.hidden = false;
      lyricsList.innerHTML =
        `<li class="plain-lyrics" style="white-space:pre-wrap;list-style:none;padding-left:0">${escHtml(stripLyricTimestamps(s.lyrics_plain))}</li>`;
    }
    state.lastIndex = null;
    claimPlainLyricsStatus({ lyricsList, lyricAnnounce });
    return;
  }
  if (!s.has_lyrics) {
    clearCurrentLine({ lyricsList, lyricAnnounce });
    state.lastIndex = null;
    return;
  }
  const idx = s.lyric_index;
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
    if (s.lyric_text && lyricAnnounce) {
      state.loadStatus = null;
      state.currentLineAnnouncement = s.lyric_text;
      lyricAnnounce.textContent = s.lyric_text;
    }
  }
}
