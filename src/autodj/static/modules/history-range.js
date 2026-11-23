// History presets use rolling instants. Custom dates keep the existing
// inclusive calendar-date behavior shared by the table and export.

const PRESET_DURATIONS = new Map([
  ["last-day", 24 * 60 * 60 * 1000],
  ["last-7-days", 7 * 24 * 60 * 60 * 1000],
  ["last-30-days", 30 * 24 * 60 * 60 * 1000],
  ["last-90-days", 90 * 24 * 60 * 60 * 1000],
]);

let selectedRange = null;
let selectedPreset = null;

export function setHistoryRangePreset(preset, now = new Date()) {
  selectedPreset = PRESET_DURATIONS.has(preset) ? preset : null;
  const duration = PRESET_DURATIONS.get(preset);
  selectedRange = duration == null ? null : {
    from: new Date(now.getTime() - duration).toISOString(),
    to: now.toISOString(),
  };
  return selectedRange;
}

export function refreshHistoryRange(now = new Date()) {
  return selectedPreset ? setHistoryRangePreset(selectedPreset, now) : null;
}

export function historyDateRange(from, to) {
  return selectedRange || { from, to };
}
