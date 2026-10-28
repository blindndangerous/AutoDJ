// History tab: the "Show liners" choice (#601).  Off, History lists the
// songs and live DJ sets; on, everything that aired, with a Type column:
// liners, ads, matched intros, time checks and the rest.  The choice is
// remembered in this browser only.  On a change the page asks for the page
// holding the first row shown (the anchor), so the listener stays near the
// same place, and says once how many items the view now has.

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

export function historyQuery({ page, showLiners, anchor = null }) {
  const query = new URLSearchParams({ page: String(page), per_page: String(PER_PAGE) });
  if (showLiners) query.set("liners", "true");
  if (anchor) query.set("anchor", anchor);
  return query.toString();
}

// The first row on the page.  The server finds it, or the next older row the
// other view shows, so hiding liners lands on the song before a top liner.
export function historyAnchor(tbody) {
  return tbody?.querySelector("tr[data-history-id]")?.dataset.historyId || null;
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

export function linersMessage(showLiners, data) {
  const start = showLiners ? "Liners shown." : "Liners hidden.";
  const total = Number(data?.total) || 0;
  if (!total) return `${start}  No items.`;
  return `${start}  ${total} ${total === 1 ? "item" : "items"}, page ${data.page} of ${data.pages}.`;
}
