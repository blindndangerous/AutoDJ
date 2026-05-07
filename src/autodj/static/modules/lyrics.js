// Lyrics rendering: timestamped LRC scroll + plain-text fallback.
// Active line announced via the polite #lyric-announce live region;
// pattern verified by accessibility-lead -- aria-live="polite" +
// aria-atomic="true" is the right ARIA mechanism for time-coded line
// updates that fire every few seconds.

import { escHtml } from "./dom-helpers.js";

const state = {
  cached: [],          // full list, used by the visible scroll
  lastIndex: null,     // suppress repeated lyric announcements
};

export function getCachedLyrics() {
  return state.cached;
}

export function resetLyricState() {
  state.lastIndex = null;
  state.cached = [];
}

export async function loadLyrics(elements) {
  try {
    const res = await fetch("/api/lyrics");
    const data = await res.json();
    state.cached = data.lyrics || [];
  } catch (_) {
    state.cached = [];
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
      lyricAnnounce.textContent = s.lyric_text;
    }
  }
}
