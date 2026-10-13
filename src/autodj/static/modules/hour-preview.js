// Now Playing > Hour preview (#126): what plays next, when it is expected
// to start, and how the hour lands.
//
// The server works the preview out on a worker thread of its own
// (hour_preview.py): the fixed part read from the station, then a dry run
// of the picker for the rest.  This module asks for it only while the card
// is open, and shows it as a table with column headers plus a list of
// backtiming sentences.
//
// Speech: a refresh nobody asked for says nothing.  The table refreshes
// every 30 seconds while the card is open and Now Playing is showing, and
// soon after the song changes; each refresh changes only the cells whose
// text changed, in place, so the reading position and focus stay put.  If
// those refreshes start failing, the failure is said once (not again until
// one succeeds).  Refresh preview, and choosing another span under Show,
// say the new summary once through #hour-preview-status, and Shift+H
// (speakSummary) says it from anywhere through the page's spoken status.  Times and lengths are shown as "2:04:30 PM" and "3:45" but read
// as "2:04 PM and 30 seconds" and "3 minutes 45 seconds".  Fixed or
// predicted is a word in the Status column of every row, not a colour.
import { requestJson } from "./api-client.js";
import { srSpeak } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

const ENDPOINT = "/api/hour-preview";
const REFRESH_MS = 30000;
// After a song change the station renders the next song; give it a moment.
const SONG_CHANGE_DELAY_MS = 2000;
// Shift+H reuses a preview this recent instead of asking again.
const FRESH_MS = 20000;

// The words Shift+H and Refresh preview say for *view*.
export function spokenSummary(view) {
  if (!view) return "No hour preview yet.";
  return view.reason || view.summary || "No hour preview yet.";
}

// The cells of one row: [shown, read] for the time and the length, so a
// screen reader gets words where the screen shows figures.
export function rowCells(item) {
  return [
    { shown: item.starts_text, read: item.starts_words },
    { text: item.title },
    { shown: item.length_text, read: item.length_words },
    { text: item.status_text },
  ];
}

function cellKey(cell) {
  return cell.text !== undefined ? `t:${cell.text}` : `s:${cell.shown}\n${cell.read}`;
}

function fillCell(doc, td, cell) {
  if (cell.text !== undefined) {
    td.textContent = cell.text;
    return;
  }
  const shown = doc.createElement("span");
  shown.setAttribute("aria-hidden", "true");
  shown.textContent = cell.shown;
  const read = doc.createElement("span");
  read.className = "visually-hidden";
  read.textContent = cell.read;
  td.replaceChildren(shown, read);
}

// Bring *tbody* in line with *items*, changing only what differs: rows are
// reused by position and a cell is rewritten only when its text changed.
export function renderRows(doc, tbody, items) {
  // children, not rows and cells: the same in every engine and in tests.
  const rows = [...tbody.children];
  items.forEach((item, index) => {
    let row = rows[index];
    if (!row) {
      row = doc.createElement("tr");
      for (let column = 0; column < 4; column += 1) row.append(doc.createElement("td"));
      tbody.append(row);
    }
    const kind = item.fixed ? "is-fixed" : "is-predicted";
    const onAir = item.status === "on_air";
    if (row.dataset.kind !== `${kind}${onAir ? " is-on-air" : ""}`) {
      row.className = `${kind}${onAir ? " is-on-air" : ""}`;
      row.dataset.kind = row.className;
    }
    rowCells(item).forEach((cell, column) => {
      const td = row.children[column];
      const key = cellKey(cell);
      if (td.dataset.key !== key) {
        fillCell(doc, td, cell);
        td.dataset.key = key;
      }
    });
  });
  for (const row of rows.slice(items.length)) row.remove();
}

// The backtiming sentences, one list item each, rewritten only when changed.
export function renderPoints(doc, list, points) {
  const items = [...list.children];
  points.forEach((point, index) => {
    let item = items[index];
    if (!item) {
      item = doc.createElement("li");
      list.append(item);
    }
    if (item.textContent !== point.text) item.textContent = point.text;
  });
  for (const item of items.slice(points.length)) item.remove();
}

export function installHourPreview({
  doc = document,
  request = requestJson,
  speak = srSpeak,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  now = () => Date.now(),
} = {}) {
  const get = (id) => doc.getElementById(`hour-preview-${id}`);
  const card = get("card");
  const span = get("span");
  const refreshButton = get("refresh");
  const summary = get("summary");
  const stopped = get("stopped");
  const backtiming = get("backtiming");
  const points = get("points");
  const pointsNote = get("points-note");
  const table = get("table");
  const tbody = get("tbody");
  const notes = get("notes");
  const status = get("status");
  const nowPanel = doc.getElementById("panel-now");
  if (!card || !span || !refreshButton || !summary || !table || !tbody || !status) {
    return {
      refresh: async () => null,
      resume() {},
      noteState() {},
      speakSummary: async () => {},
      clear() {},
    };
  }
  let lastView = null;
  let lastAt = 0;
  let timer = null;
  let songTimer = null;
  let songKey = null;
  // Bumped by clear() and by each new request: an older answer is dropped.
  let session = 0;
  let latest = 0;
  // The failure of a quiet refresh last said, so it is said once.
  let quietError = null;

  const showing = () => card.open && !(nowPanel && nowPanel.hidden)
    && doc.visibilityState !== "hidden";

  function show(view) {
    lastView = view;
    lastAt = now();
    const text = view.reason || view.summary || "";
    if (summary.textContent !== text) summary.textContent = text;
    const stopText = view.stopped || "";
    if (stopped.textContent !== stopText) stopped.textContent = stopText;
    stopped.hidden = !stopText;
    const items = view.items || [];
    renderRows(doc, tbody, items);
    table.hidden = items.length === 0;
    renderPoints(doc, points, view.points || []);
    backtiming.hidden = !(view.points || []).length;
    if (pointsNote) {
      const note = view.points_note || "";
      if (pointsNote.textContent !== note) pointsNote.textContent = note;
      pointsNote.hidden = !note;
    }
    if (notes) {
      renderPoints(doc, notes, (view.notes || []).map((note) => ({ text: note })));
      notes.hidden = items.length === 0;
    }
  }

  // Ask for the preview; resolves to the view, or null when it failed or an
  // answer for a newer request (or a cleared session) superseded it.
  async function load() {
    const mine = session;
    latest += 1;
    const ticket = latest;
    const minutes = Number(span.value) || 60;
    let view;
    try {
      view = await request(`${ENDPOINT}?minutes=${minutes}`);
    } catch (error) {
      if (mine !== session || ticket !== latest) return null;
      throw error;
    }
    if (mine !== session || ticket !== latest) return null;
    show(view);
    return view;
  }

  // A refresh nobody asked for: on its timer, a song change or the card
  // opening.  It says nothing, except a failure, once, until one succeeds.
  async function quietRefresh() {
    try {
      const view = await load();
      if (view) quietError = null;
      return view;
    } catch (error) {
      const text = `The hour preview could not be loaded: ${error.message}`;
      if (summary.textContent !== text) summary.textContent = text;
      if (quietError !== text) {
        quietError = text;
        announceStatus(status, text, { dwellMs: 8000, tone: "error" });
      }
      return null;
    }
  }

  function schedule() {
    if (timer !== null) clearTimer(timer);
    timer = setTimer(async () => {
      timer = null;
      if (showing()) await quietRefresh();
      if (card.open) schedule();
    }, REFRESH_MS);
  }

  function stop() {
    if (timer !== null) clearTimer(timer);
    if (songTimer !== null) clearTimer(songTimer);
    timer = null;
    songTimer = null;
  }

  // Ask on the user's behalf and say the outcome once, even when it is the
  // same as before.  Only the newest ask is said: one overtaken by a later
  // one (another span chosen meanwhile) says nothing.
  async function askAndSay(lead) {
    let view;
    try {
      view = await load();
    } catch (error) {
      announceStatus(status, `The hour preview could not be loaded: ${error.message}`,
        { force: true, dwellMs: 8000, tone: "error" });
      return null;
    }
    if (view) {
      quietError = null;
      announceStatus(status, `${lead} ${spokenSummary(view)}`,
        { force: true, dwellMs: 8000, mirror: false });
    }
    if (card.open) schedule();
    return view;
  }

  // Refresh preview: the new summary is said once.
  function refresh() {
    return askAndSay("Hour preview updated.");
  }

  // Show: a new span is said with its summary, once it has loaded.
  function spanChanged() {
    const chosen = span.options[span.selectedIndex]?.textContent || "the chosen span";
    return askAndSay(`Showing the ${chosen.toLowerCase()}.`);
  }

  // Shift+H: a recent preview, or a fresh one, said through the page's
  // spoken status.
  async function speakSummary() {
    if (lastView && now() - lastAt < FRESH_MS) {
      speak(spokenSummary(lastView));
      return;
    }
    try {
      const view = await load();
      speak(view ? spokenSummary(view) : spokenSummary(lastView));
    } catch (error) {
      speak(`The hour preview could not be loaded: ${error.message}`);
    }
  }

  // From each state push: a new song on the air, a new next song or a
  // queue edit refreshes the open preview shortly after.
  function noteState(state) {
    const key = [
      state?.current_track?.path || "",
      state?.next_track?.path || "",
      (state?.queue || []).map((item) => item?.path || "").join("\n"),
    ].join("\u0000");
    if (key === songKey) return;
    const first = songKey === null;
    songKey = key;
    if (first || !card.open) return;
    if (songTimer !== null) clearTimer(songTimer);
    songTimer = setTimer(() => {
      songTimer = null;
      if (showing()) void quietRefresh();
    }, SONG_CHANGE_DELAY_MS);
  }

  // A toggle event can arrive twice for one change; act on real changes only.
  let wasOpen = card.open;
  card.addEventListener("toggle", () => {
    if (card.open === wasOpen) return;
    wasOpen = card.open;
    if (card.open) {
      void quietRefresh();
      schedule();
    } else {
      stop();
    }
  });
  span.addEventListener("change", () => {
    if (card.open) void spanChanged();
  });
  refreshButton.addEventListener("click", () => { void refresh(); });

  // After sign-out: forget the preview, so another person signing in on
  // this page does not see it.
  function clear() {
    session += 1;
    stop();
    lastView = null;
    lastAt = 0;
    songKey = null;
    tbody.replaceChildren();
    points.replaceChildren();
    quietError = null;
    if (pointsNote) {
      pointsNote.textContent = "";
      pointsNote.hidden = true;
    }
    if (notes) {
      notes.replaceChildren();
      notes.hidden = true;
    }
    table.hidden = true;
    backtiming.hidden = true;
    stopped.hidden = true;
    stopped.textContent = "";
    summary.textContent = "Open this card to see the hour ahead.";
  }

  // After a sign-in or a reconnect: refresh an open card quietly.
  function resume() {
    if (!card.open) return;
    void quietRefresh();
    schedule();
  }

  return { refresh, resume, noteState, speakSummary, clear };
}
