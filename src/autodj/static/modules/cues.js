// Cue strip rendering + durable screen-reader summary.
// Sighted users see colored ticks on the progress bar; AT users can
// revisit matching static summary text whenever they need it.

export const CUE_COLORS = {
  drop:            "#ff5470",
  breakdown:       "#5a7bff",
  first_downbeat:  "#69d0ff",
  outro_downbeat:  "#ffb454",
  phrase:          "rgba(255,255,255,0.45)",
  user:            "#a4ff7a",
};

let _lastCueKey = "";
const CUE_SUMMARY_LIMIT = 3;
const SAFE_CSS_COLOR = /^(?:#[0-9a-f]{3,8}|[a-z]+|(?:rgb|hsl)a?\([0-9.,%+\-\s/]+\))$/i;

function _validCues(track) {
  const duration = track && Number.isFinite(track.length) && track.length > 0
    ? track.length
    : 0;
  if (!duration || !Array.isArray(track.cues)) return [];
  return track.cues
    .filter((cue) => cue && Number.isFinite(cue.time_s)
      && cue.time_s >= 0 && cue.time_s <= duration)
    .map((cue) => {
      const type = typeof cue.type === "string" && cue.type.trim()
        ? cue.type.trim()
        : "cue";
      const rawLabel = typeof cue.label === "string" ? cue.label.trim() : "";
      const label = rawLabel.toLocaleLowerCase() === type.toLocaleLowerCase()
        ? ""
        : rawLabel;
      return { ...cue, type, label };
    });
}

function _cueDescription(cue) {
  return cue.type.replace(/_/g, " ") + (cue.label ? `: ${cue.label}` : "");
}

function _cuePhrase(cue) {
  return `${_cueDescription(cue)} at ${Math.round(cue.time_s)} seconds`;
}

function _cueColor(cue) {
  const fallback = CUE_COLORS[cue.type] || CUE_COLORS.user;
  if (typeof cue.color !== "string") return fallback;
  const candidate = cue.color.trim();
  if (!SAFE_CSS_COLOR.test(candidate)) return fallback;
  const supports = globalThis.CSS?.supports;
  if (typeof supports === "function" && !supports("color", candidate)) {
    return fallback;
  }
  return candidate;
}

export function renderCueStrip(cueStripEl, track) {
  if (!cueStripEl) return;
  const cues = _validCues(track);
  const dur = track && Number.isFinite(track.length) ? track.length : 0;
  const key = JSON.stringify([
    track ? track.path : "",
    dur,
    cues.map((cue) => [cue.type, cue.label, cue.time_s, cue.color]),
  ]);
  if (key === _lastCueKey) return;
  _lastCueKey = key;
  cueStripEl.replaceChildren();
  if (!cues.length || dur <= 0) {
    return;
  }
  for (const cue of cues) {
    const marker = cueStripEl.ownerDocument.createElement("span");
    marker.className = "cue-mark";
    marker.style.left = `${((cue.time_s / dur) * 100).toFixed(2)}%`;
    marker.style.background = _cueColor(cue);
    marker.title = _cueDescription(cue);
    cueStripEl.appendChild(marker);
  }
}

export function applyCueSummary(track, element, detailsElement) {
  if (!element && !detailsElement) return;
  const cues = _validCues(track);
  const headline = `${cues.length} cue ${cues.length === 1 ? "point" : "points"}`;
  const phrases = cues.map(_cuePhrase);
  const detailText = cues.length ? `${headline}, ${phrases.join(", ")}` : "No cue points";
  const hiddenCount = Math.max(0, cues.length - CUE_SUMMARY_LIMIT);
  const summaryText = hiddenCount
    ? `${headline}, ${phrases.slice(0, CUE_SUMMARY_LIMIT).join(", ")}, and ${hiddenCount} more`
    : detailText;
  if (element && element.textContent !== summaryText) element.textContent = summaryText;
  if (detailsElement && detailsElement.textContent !== detailText) {
    detailsElement.textContent = detailText;
  }
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
