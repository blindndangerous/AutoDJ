// Cue strip rendering + screen-reader summary.
// Sighted users see colored ticks on the progress bar; AT users get
// the summariseCues() text pushed into the polite #badges-announce
// live region by the WS state-change handler.

import { escHtml } from "./dom-helpers.js";

export const CUE_COLORS = {
  drop:            "#ff5470",
  breakdown:       "#5a7bff",
  first_downbeat:  "#69d0ff",
  outro_downbeat:  "#ffb454",
  phrase:          "rgba(255,255,255,0.45)",
  user:            "#a4ff7a",
};

let _lastCueKey = "";

export function renderCueStrip(cueStripEl, track) {
  if (!cueStripEl) return;
  const cues = (track && Array.isArray(track.cues)) ? track.cues : [];
  const dur = track && track.length ? track.length : 0;
  // Cheap dedupe: same path + cue-count = no rebuild.
  const key = `${track ? track.path : ""}#${cues.length}`;
  if (key === _lastCueKey) return;
  _lastCueKey = key;
  // The persistent sr-only navigable list (#cue-list-summary) was
  // removed at user request.  AT users still get a polite live-region
  // summary on every track change via summariseCues() -> #badges-announce.
  if (!cues.length || dur <= 0) {
    cueStripEl.innerHTML = "";
    return;
  }
  const html = cues
    .filter(c => c.time_s >= 0 && c.time_s <= dur)
    .map(c => {
      const pct = (c.time_s / dur) * 100;
      const color = c.color || CUE_COLORS[c.type] || CUE_COLORS.user;
      return `<span class="cue-mark" style="left:${pct.toFixed(2)}%;background:${color};"
              title="${escHtml(c.type)}${c.label ? ': ' + escHtml(c.label) : ''}"></span>`;
    })
    .join("");
  cueStripEl.innerHTML = html;
}

export function summariseCues(cues) {
  // Compact, screen-reader-friendly summary: count + up to first 3 markers
  // formatted as "drop at 1 minute 23, breakdown at 2 minutes 10".
  const fmt = (sec) => {
    const m = Math.floor(sec / 60);
    const s = Math.round(sec - m * 60);
    if (m <= 0) return `${s} seconds`;
    return `${m} minute${m === 1 ? "" : "s"} ${s}`;
  };
  const headline = `${cues.length} cue ${cues.length === 1 ? "point" : "points"}`;
  const interesting = cues
    .filter(c => c.type !== "phrase")
    .slice(0, 3);
  if (!interesting.length) return headline;
  const phrases = interesting.map(c => `${c.type.replace(/_/g, " ")} at ${fmt(c.time_s)}`);
  return `${headline}: ${phrases.join(", ")}`;
}
