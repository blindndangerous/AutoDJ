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

// Visible key for the coloured ticks.  The strip itself is
// pointer-events:none so there is no tooltip, and the only explanation
// of what a colour means lived in the visually-hidden #cue-summary --
// leaving a sighted user with eleven coloured marks and no way to read
// them (WCAG 1.4.1).  Only the types actually present are listed, so the
// legend stays short and matches what is on the bar.
export function renderCueLegend(legendEl, track) {
  if (!legendEl) return;
  const cues = _validCues(track);
  const seen = new Map();
  for (const cue of cues) {
    if (!seen.has(cue.type)) seen.set(cue.type, _cueColor(cue));
  }
  const key = JSON.stringify([...seen]);
  if (legendEl.dataset.cueKey === key) return;
  legendEl.dataset.cueKey = key;
  legendEl.replaceChildren();
  legendEl.hidden = seen.size === 0;
  for (const [type, color] of seen) {
    const item = legendEl.ownerDocument.createElement("span");
    item.className = "cue-key";
    const swatch = legendEl.ownerDocument.createElement("span");
    swatch.className = "cue-swatch";
    swatch.style.background = color;
    item.appendChild(swatch);
    item.appendChild(
      legendEl.ownerDocument.createTextNode(type.replace(/_/g, " ")),
    );
    legendEl.appendChild(item);
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
