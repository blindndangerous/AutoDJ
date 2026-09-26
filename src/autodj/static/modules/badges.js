// Now-playing badges row: BPM / Camelot key / energy / beatmatch.
//
// The visible row is aria-hidden and re-rendered every WS tick.  Speech
// comes from trackChangeDetails(), which app.js folds into the single
// track-change announcement in #now-playing-announce.

import { escHtml } from "./dom-helpers.js";

// The spoken tail of a track change: ", 128 BPM, key 8A, beatmatched
// 1.02 times".  One announcement per track: the key used to follow the
// title 800 ms later from a second live region, which NVDA could speak
// on top of the title or of the Up Next line.
export function trackChangeDetails(s) {
  const t = s && s.current_track;
  if (!t) return "";
  const phrases = [];
  if (t.bpm) phrases.push(`${Math.round(t.bpm)} BPM`);
  const key = typeof t.key_label === "string" ? t.key_label.trim() : "";
  if (key && key !== "--") phrases.push(`key ${key}`);
  if (s.beatmatch_ratio && Math.abs(s.beatmatch_ratio - 1.0) > 0.005) {
    // Spell "times" for beatmatch (per a11y review).
    phrases.push(`beatmatched ${s.beatmatch_ratio.toFixed(2)} times`);
  }
  return phrases.map((phrase) => `, ${phrase}`).join("");
}

export function formatPersistentMetadata(track) {
  if (!track) return "";
  const t = track;
  const album = typeof t.album === "string" && t.album.trim()
    ? t.album.trim()
    : "unknown";
  const bpm = Number.isFinite(t.bpm) && t.bpm > 0
    ? Math.round(t.bpm)
    : "unknown";
  const keyValue = typeof t.key_label === "string" ? t.key_label.trim() : "";
  const key = keyValue && keyValue !== "--"
    ? keyValue
    : "unknown";
  const energy = Number.isFinite(t.energy) && t.energy > 0
    ? t.energy.toFixed(2)
    : "unknown";
  return `Album ${album} · BPM ${bpm} · Key ${key} · Energy ${energy}`;
}

export function applyBadges(s, els, { renderCueStrip }) {
  const { badgesRow } = els;
  const t = s.current_track;
  if (!t) {
    if (badgesRow) badgesRow.innerHTML = "";
    if (typeof renderCueStrip === "function") renderCueStrip(null);
    return;
  }
  const out = [];
  if (t.bpm) out.push(`<span class="badge">${Math.round(t.bpm)} BPM</span>`);
  // Notation-aware display label; key_label is the only key field
  // browsers should read for display.
  const _kl = t.key_label;
  if (_kl && _kl !== "--") {
    out.push(`<span class="badge badge-key">Key ${escHtml(_kl)}</span>`);
  }
  if (t.energy && t.energy > 0) {
    out.push(`<span class="badge">Energy ${t.energy.toFixed(2)}</span>`);
  }
  if (s.beatmatch_ratio && Math.abs(s.beatmatch_ratio - 1.0) > 0.005) {
    out.push(
      `<span class="badge badge-stretch">Beatmatch ${s.beatmatch_ratio.toFixed(3)}x</span>`,
    );
  }
  if (badgesRow) badgesRow.innerHTML = out.join("");
  if (typeof renderCueStrip === "function") renderCueStrip(t);
}
