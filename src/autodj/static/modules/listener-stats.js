// Settings > Stream > Listener statistics (#50): listeners now, peaks,
// sessions and listening time, by format and by day, as text and tables.
//
// Speech rules:
//   - The summary, the tables and the session lengths are plain text and
//     tables, never live regions.  They change only when the person asks
//     (opening Settings, Refresh statistics, Show, Days to keep), never on
//     the once-a-second push, so reading a table is never disturbed.
//   - Each of those actions says what it did once, in the settings status
//     region; failures with the error tone.
//   - Download keeps focus on its button.

import { requestFile, requestJson } from "./api-client.js";
import { fmtTime, focusRevealed } from "./dom-helpers.js";
import { saveFile } from "./history-export.js";
import { announceStatus } from "./live-region.js";

const SPANS = { 7: "the last 7 days", 30: "the last 30 days", 90: "the last 90 days" };

export function spanText(days) {
  return SPANS[days] || "every day kept";
}

function plural(count, one, many = `${one}s`) {
  return `${count} ${count === 1 ? one : many}`;
}

// Listening time in hours, one decimal: "4.2".
export function hoursText(seconds) {
  return (Math.round((Number(seconds) || 0) / 360) / 10).toFixed(1);
}

// "Tue 13 Oct" from "2026-10-13", read as a calendar day.
export function dayText(iso, locale) {
  const [year, month, day] = String(iso).split("-").map(Number);
  const date = new Date(year, month - 1, day, 12);
  return date.toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "short" });
}

function formatLabel(name) {
  return { mp3: "MP3", aac: "AAC", opus: "Opus" }[name] || name;
}

// The plain-text summary over the tables.
export function summaryText(stats, locale) {
  const now = stats.now || { total: 0, formats: {} };
  const parts = Object.entries(now.formats || {})
    .filter(([, count]) => count > 0)
    .map(([name, count]) => `${formatLabel(name)} ${count}`);
  let text = `Listening now: ${now.total}${parts.length ? ` (${parts.join(", ")})` : ""}.`;
  if (!stats.keep_days) {
    return `${text} No statistics are kept: Days to keep statistics is 0.`;
  }
  const today = (stats.days || []).find((row) => row.date === stats.today);
  if (today && (today.peak || today.sessions)) {
    text += ` Today: peak ${today.peak}${today.peak_at ? ` at ${today.peak_at}` : ""}, `
      + `${plural(today.sessions, "session")}, ${hoursText(today.seconds)} listening hours.`;
  } else {
    text += " Nobody has listened yet today.";
  }
  const period = stats.period;
  const span = spanText(stats.days_shown);
  if (!period || (!period.sessions && !period.peak)) {
    return `${text} No listening recorded in ${span}.`;
  }
  text += ` In ${span}: ${plural(period.sessions, "session")}, `
    + `${hoursText(period.seconds)} listening hours`;
  if (period.peak_date) {
    text += `, peak ${period.peak} on ${dayText(period.peak_date, locale)}`
      + `${period.peak_at ? ` at ${period.peak_at}` : ""}`;
  }
  if (period.longest) text += `, longest session ${fmtTime(period.longest)}`;
  return `${text}.`;
}

function cell(doc, tag, text, scope) {
  const element = doc.createElement(tag);
  element.textContent = text;
  if (scope) element.setAttribute("scope", scope);
  return element;
}

function fillBody(doc, table, rows) {
  table.tBodies[0].replaceChildren(...rows.map(([head, ...rest]) => {
    const tr = doc.createElement("tr");
    tr.append(cell(doc, "th", head, "row"), ...rest.map((text) => cell(doc, "td", text)));
    return tr;
  }));
}

export function installListenerStats({
  doc,
  request = requestJson,
  download = requestFile,
  save = saveFile,
  saveKeepDays,
  settingsStatus,
  locale = undefined,
}) {
  const summary = doc.getElementById("listener-stats-summary");
  const span = doc.getElementById("listener-stats-span");
  const refreshButton = doc.getElementById("listener-stats-refresh");
  const formats = doc.getElementById("listener-stats-formats");
  const formatsCaption = doc.getElementById("listener-stats-formats-caption");
  const days = doc.getElementById("listener-stats-days");
  const daysCaption = doc.getElementById("listener-stats-days-caption");
  const lengthsTitle = doc.getElementById("listener-stats-lengths-title");
  const lengths = doc.getElementById("listener-stats-lengths");
  const keep = doc.getElementById("listener-stats-keep");
  const exportButton = doc.getElementById("listener-stats-export");
  let active = false;
  let loaded = false;
  let sequence = 0;

  function say(text, tone = "info") {
    announceStatus(settingsStatus, text, { dwellMs: 5000, force: true, tone });
  }

  function render(stats) {
    summary.textContent = summaryText(stats, locale);
    const period = spanText(stats.days_shown);
    const names = new Set([
      ...Object.keys(stats.now?.formats || {}),
      ...Object.keys(stats.period?.formats || {}),
    ]);
    const formatRows = ["mp3", "aac", "opus"].filter((name) => names.has(name)).map((name) => {
      const totals = stats.period?.formats?.[name] || { peak: 0, sessions: 0, seconds: 0 };
      return [
        formatLabel(name),
        String(stats.now?.formats?.[name] ?? 0),
        String(totals.peak),
        String(totals.sessions),
        hoursText(totals.seconds),
      ];
    });
    formatsCaption.textContent = `Listeners by format, ${period}`;
    fillBody(doc, formats, formatRows);
    formats.hidden = formatRows.length === 0;
    const dayRows = (stats.days || []).map((row) => [
      dayText(row.date, locale),
      row.peak ? `${row.peak}${row.peak_at ? ` at ${row.peak_at}` : ""}` : "0",
      String(row.sessions),
      hoursText(row.seconds),
      row.sessions ? fmtTime(row.seconds / row.sessions) : "None",
      row.sessions ? fmtTime(row.longest) : "None",
    ]);
    daysCaption.textContent = `Listeners by day, ${period}, newest first`;
    fillBody(doc, days, dayRows);
    days.hidden = dayRows.length === 0;
    const bands = stats.period?.bands || [];
    const labels = stats.bands || [];
    const anySessions = bands.some((count) => count > 0);
    lengths.replaceChildren(...(anySessions ? labels : []).map((label, index) => {
      const item = doc.createElement("li");
      item.textContent = `${label}: ${plural(bands[index] || 0, "session")}`;
      return item;
    }));
    lengths.hidden = !anySessions;
    lengthsTitle.hidden = !anySessions;
    if (doc.activeElement !== keep) keep.value = String(stats.keep_days);
  }

  async function refresh({ spoken = "" } = {}) {
    if (!active) return false;
    const mine = ++sequence;
    try {
      const stats = await request(`/api/stream/stats?days=${encodeURIComponent(span.value)}`);
      if (!active || mine !== sequence) return false;
      loaded = true;
      render(stats);
      if (spoken) say(spoken);
      return true;
    } catch (err) {
      if (!active || mine !== sequence || err?.name === "AuthenticationRequiredError") return false;
      say(`Could not load listener statistics: ${err.message}`, "error");
      return false;
    }
  }

  function apply(state) {
    active = Boolean(state?.stream_mode);
    if (!active) return;
    const pushed = state.settings?.playback?.stream_stats_days;
    if (pushed != null && doc.activeElement !== keep && !keep.disabled) keep.value = String(pushed);
    if (!loaded) {
      loaded = true;
      void refresh();
    }
  }

  function reset() {
    active = false;
    loaded = false;
    sequence += 1;
    summary.textContent = "Statistics not loaded yet.";
    fillBody(doc, formats, []);
    fillBody(doc, days, []);
    formats.hidden = true;
    days.hidden = true;
    lengths.replaceChildren();
    lengths.hidden = true;
    lengthsTitle.hidden = true;
  }

  refreshButton.addEventListener("click", () => {
    void refresh({ spoken: "Listener statistics updated." });
  });
  span.addEventListener("change", () => {
    void refresh({ spoken: `Showing ${spanText(Number(span.value))}.` });
  });
  keep.addEventListener("change", async () => {
    const value = Number(keep.value);
    if (!Number.isInteger(value) || value < 0 || value > 3650 || keep.value.trim() === "") {
      say("Days to keep statistics must be a whole number from 0 to 3650.", "error");
      focusRevealed(keep);
      return;
    }
    if (!await saveKeepDays(value, keep)) return;
    await refresh({
      spoken: value
        ? `Listener statistics are kept for ${plural(value, "day")}.`
        : "Listener statistics are off, and those kept are deleted.",
    });
  });
  exportButton.addEventListener("click", async () => {
    try {
      const response = await download("/api/stream/stats/export", ["text/csv"]);
      const blob = await response.blob();
      const match = /filename="([^"/\\]+)"/.exec(response.headers.get("Content-Disposition") || "");
      save(doc, blob, match ? match[1] : "autodj-listeners.csv");
      say("Listener statistics downloaded.");
    } catch (err) {
      say(`Could not download listener statistics: ${err.message}`, "error");
    }
  });

  return { apply, refresh, reset };
}
