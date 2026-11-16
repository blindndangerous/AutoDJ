// History tab: shared filters, page queries and the optional Type column.
// The nonmusic choice remains remembered in this browser for continuity.

import { readStorage, writeStorage } from "./safe-storage.js";

export const SHOW_LINERS_KEY = "autodj.history.showLiners";
const PER_PAGE = 50;

const KIND_LABELS = {
  song: "Song",
  live: "Live DJ",
  liner: "Liner",
  ad: "Ad",
  matched_intro: "Matched intro",
  time_check: "Time check",
  cart: "Cart",
  voice_track: "Voice track",
  dj: "DJ break",
  news: "News",
  weather: "Weather",
  music_news: "Music news",
  speech: "Speech",
};

export function readShowLiners(storage) {
  return readStorage(storage, SHOW_LINERS_KEY) === "true";
}

export function writeShowLiners(storage, value) {
  writeStorage(storage, SHOW_LINERS_KEY, value ? "true" : "false");
}

export function historyKindLabel(kind) {
  if (!kind) return KIND_LABELS.song;
  if (KIND_LABELS[kind]) return KIND_LABELS[kind];
  const words = String(kind).replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function historyQuery({ page, from, to, q = "", nonmusic = false, anchor = null }) {
  const query = new URLSearchParams();
  if (from) query.set("from", from);
  if (to) query.set("to", to);
  if (q.trim()) query.set("q", q.trim());
  query.set("nonmusic", String(Boolean(nonmusic)));
  query.set("page", String(page));
  query.set("per_page", String(PER_PAGE));
  if (anchor) query.set("anchor", anchor);
  return query.toString();
}

export function historyResultsMessage(data) {
  const total = Number(data?.total) || 0;
  if (!total) return "No plays match these filters.";
  return `${total} ${total === 1 ? "play" : "plays"}.`;
}

export function historyFiltersKey({ from = "", to = "", q = "", nonmusic = false }) {
  return JSON.stringify([from, to, q.trim(), Boolean(nonmusic)]);
}

// A filter result may be followed by a passive track refresh before its
// delayed announcement is spoken. Carry one pending result across those
// refreshes; a new user action cancels it.
export function createHistoryFilterFeedback(selection, say) {
  let pending = null;

  function cancel() {
    pending = null;
    selection.cancel();
  }

  function start(filtersKey) {
    cancel();
    const token = {};
    pending = { filtersKey, token, finish: selection.start() };
    return token;
  }

  function complete(filtersKey, data) {
    if (!pending || pending.filtersKey !== filtersKey) return false;
    pending.finish(historyResultsMessage(data), pending.token);
    return true;
  }

  function deliver(message, token) {
    if (!pending || pending.token !== token) return false;
    pending = null;
    selection.cancel();
    say(message);
    return true;
  }

  return { cancel, start, complete, deliver };
}

export function applyHistoryFilterOnEnter(event, apply) {
  if (event.isComposing || event.keyCode === 229 || event.key !== "Enter") return false;
  event.preventDefault();
  apply();
  return true;
}

// The Type column sits after Duration, so the time and duration columns keep
// their places and styles.  Added and removed rather than hidden: table
// navigation reads a cell's heading by its position among the headings.
export function setHistoryTypeColumn(table, shown) {
  if (!table) return;
  const doc = table.ownerDocument;
  const headRow = table.querySelector("thead tr");
  const existing = headRow?.querySelector("th[data-column=type]");
  const col = table.querySelector("col.hist-type");
  table.classList.toggle("has-type", shown);
  if (!shown) {
    existing?.remove();
    col?.remove();
    return;
  }
  if (!existing && headRow) {
    const heading = doc.createElement("th");
    heading.scope = "col";
    heading.dataset.column = "type";
    heading.textContent = "Type";
    headRow.children[3]?.after(heading);
  }
  if (!col) {
    const added = doc.createElement("col");
    added.className = "hist-type";
    table.querySelector("col.hist-duration")?.after(added);
  }
}
