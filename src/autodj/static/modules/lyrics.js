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
        `<li class="plain-lyrics" style="white-space:pre-wrap;list-style:none;padding-left:0">${escHtml(s.lyrics_plain)}</li>`;
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
    li.scrollIntoView({
      behavior: prefersReducedMotion() ? "auto" : "smooth",
      block: "center",
    });
    if (s.lyric_text && lyricAnnounce) {
      state.loadStatus = null;
      state.currentLineAnnouncement = s.lyric_text;
      lyricAnnounce.textContent = s.lyric_text;
    }
  }
}
