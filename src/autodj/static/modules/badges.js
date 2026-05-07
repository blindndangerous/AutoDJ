// Now-playing badges row: BPM / Camelot key / energy / beatmatch.
//
// Visible row pre-rendered every WS tick (cheap dedupe in a parent),
// plus a polite live-region announce that fires only on real track
// change so the speech viewer doesn't repeat key/BPM each second.

import { escHtml } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";

let _lastBadgeKey = null;

export function applyBadges(s, els, { lastTrackKey, renderCueStrip }) {
  const { badgesRow, badgesAnnounce } = els;
  const t = s.current_track;
  if (!t) {
    if (badgesRow) badgesRow.innerHTML = "";
    return;
  }
  const out = [];
  if (t.bpm) out.push(`<span class="badge">${Math.round(t.bpm)} BPM</span>`);
  if (t.camelot && t.camelot !== "--") {
    out.push(`<span class="badge badge-key">Key ${escHtml(t.camelot)}</span>`);
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

  // Announce key + BPM only on track change (not on every WS tick).
  // The caller has already updated lastTrackKey when the title changed,
  // so only fire when WE see a fresh track AND there is a key/BPM to
  // read.  Spell "times" for beatmatch (per a11y review).
  if (s.current_track.path === lastTrackKey &&
      _lastBadgeKey !== s.current_track.path) {
    _lastBadgeKey = s.current_track.path;
    const phrases = [];
    if (t.camelot && t.camelot !== "--") phrases.push(`Key ${t.camelot}`);
    if (t.bpm) phrases.push(`BPM ${Math.round(t.bpm)}`);
    if (s.beatmatch_ratio && Math.abs(s.beatmatch_ratio - 1.0) > 0.005) {
      phrases.push(`beatmatched ${s.beatmatch_ratio.toFixed(2)} times`);
    }
    // Cue-point summary intentionally NOT announced -- key + BPM is
    // what users want on track change.  Cue strip on the progress bar
    // still conveys the markers visually for sighted users.
    if (phrases.length && badgesAnnounce) {
      // Slight delay so the title aria-live region speaks first.
      setTimeout(() => {
        badgesAnnounce.textContent = phrases.join(", ");
        clearLiveRegionLater(badgesAnnounce);
      }, 800);
    }
  }
  if (typeof renderCueStrip === "function") renderCueStrip(t);
}
