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
  loadStatus: "",      // request error currently owned by the live region
};
const lyricsRequestOwner = createLatestRequestOwner();

export function getCachedLyrics() {
  return state.cached;
}

export function resetLyricState() {
  lyricsRequestOwner.cancel();
  state.lastIndex = null;
  state.cached = [];
}

function setLoadStatus(elements, message) {
  const announce = elements.lyricAnnounce;
  if (!message) {
    if (announce && state.loadStatus && announce.textContent === state.loadStatus) {
      announce.textContent = "";
    }
    state.loadStatus = "";
    return;
  }
  state.loadStatus = message;
  if (announce) announce.textContent = message;
}

export async function loadLyrics(elements) {
  const request = lyricsRequestOwner.begin();
  try {
    const data = await requestJson("/api/lyrics", { signal: request.signal });
    if (!lyricsRequestOwner.isCurrent(request)) return;
    state.cached = data.lyrics || [];
    setLoadStatus(elements, "");
  } catch (errorValue) {
    if (!lyricsRequestOwner.isCurrent(request)) return;
    state.cached = [];
    setLoadStatus(
      elements,
      `Could not load lyrics: ${errorValue.message || errorValue}`,
    );
  } finally {
    lyricsRequestOwner.finish(request);
  }
  renderLyricsList(elements);
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
    if (state.cached.length || lyricsList.querySelector(".plain-lyrics") === null) {
      state.cached = [];
      lyricsCard.hidden = false;
      lyricsList.innerHTML =
        `<li class="plain-lyrics" style="white-space:pre-wrap;list-style:none;padding-left:0">${escHtml(s.lyrics_plain)}</li>`;
    }
    state.lastIndex = null;
    return;
  }
  if (!s.has_lyrics) {
    if (state.cached.length || lyricsList.children.length) {
      state.cached = [];
      renderLyricsList({ lyricsCard, lyricsList });
    }
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
    li.scrollIntoView({ behavior: "smooth", block: "center" });
    if (s.lyric_text && lyricAnnounce) {
      state.loadStatus = "";
      lyricAnnounce.textContent = s.lyric_text;
    }
  }
}
