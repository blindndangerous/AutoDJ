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
// (speakSummary) says it from anywhere through the page's spoken status.
// Times and lengths use the same numeric text for everyone. Fixed or
// predicted is a word in the Status column of every row, not a colour.
//
// On a slow server the dry run can reach its time limit before the end of
// the span.  The server then goes on from its last pick in the background
// and the view says so (view.filling); the card asks again every few
// seconds, quietly, and the new rows join the table in place.  When a
// preview someone asked for finishes, that is said once.
import { requestJson } from "./api-client.js";
import { srSpeak } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { installTableNavigation } from "./table-navigation.js";

const ENDPOINT = "/api/hour-preview";
const REFRESH_MS = 30000;
// While the server is still working out the rest of the preview.
const FILLING_REFRESH_MS = 5000;
// After a song change the station renders the next song; give it a moment.
const SONG_CHANGE_DELAY_MS = 2000;
// Shift+H reuses a preview this recent instead of asking again.
const FRESH_MS = 20000;

// Said after the summary while the server is still working out the rest.
function fillingNote(view) {
  return view?.filling ? " Still working out the rest of the preview." : "";
}

// The words Shift+H and Refresh preview say for *view*.
export function spokenSummary(view) {
  if (!view) return "No hour preview yet.";
  if (view.reason) return view.reason;
  return `${view.summary || "No hour preview yet."}${fillingNote(view)}`;
}

export function rowCells(item) {
  return [
    item.starts_text,
    item.title,
    item.length_text,
    item.status_text,
  ];
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
      for (let column = 0; column < 5; column += 1) {
        const cell = doc.createElement("td");
        cell.setAttribute("role", "gridcell");
        if (column === 1) cell.dataset.name = "";
        row.append(cell);
      }
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
      if (td.textContent !== cell) td.textContent = cell;
    });
    const actionCell = row.children[4];
    const path = item.kind === "song" || item.kind === "liner"
      ? item.path
      : item.kind === "intro" && item.details_kind === "liner" ? item.details_path : null;
    let button = actionCell.querySelector("button");
    if (!path) {
      button?.remove();
      const unavailable = item.kind === "intro" ? "Track info unavailable" : "";
      if (actionCell.textContent !== unavailable) actionCell.textContent = unavailable;
    } else {
      if (!button) {
        actionCell.textContent = "";
        button = doc.createElement("button");
        button.type = "button";
        button.textContent = "Track info";
        actionCell.append(button);
      }
      button.dataset.path = path;
      button.dataset.kind = item.kind === "intro"
        ? item.details_kind
        : item.kind === "liner" ? "liner" : "music";
      button.setAttribute("aria-label", `Track info for ${item.title}`);
    }
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
  const tableNavigation = installTableNavigation(tbody);
  const notes = get("notes");
  const status = get("status");
  const nowPanel = doc.getElementById("panel-now");
  if (!card || !span || !refreshButton || !summary || !table || !tbody || !status) {
    return {
      refresh: async () => null,
      resume() {},
      noteState() {},
      noteSeek() {},
      speakSummary: async () => {},
      clear() {},
    };
  }
  tbody.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-path]");
    if (!button || !tbody.contains(button)) return;
    doc.dispatchEvent(new CustomEvent("autodj:edit-points", {
      detail: { path: button.dataset.path, kind: button.dataset.kind, opener: button },
    }));
  });
  let lastView = null;
  let lastAt = 0;
  let timer = null;
  let songTimer = null;
  let songKey = null;
  // User asks supersede other user asks. Quiet refreshes have their own ticket
  // so they cannot cancel an explicit Refresh preview confirmation.
  let session = 0;
  let latestUser = 0;
  let latestQuiet = 0;
  let nextRequest = 0;
  let renderedRequest = 0;
  // The failure of a quiet refresh last said, so it is said once.
  let quietError = null;
  // The user's ask whose preview is still filling: its end is said once.
  let fillingAsk = null;

  const showing = () => card.open && !(nowPanel && nowPanel.hidden)
    && doc.visibilityState !== "hidden";

  function show(view) {
    lastView = view;
    lastAt = now();
    // The first backtiming line below says the next landing, so the card's
    // line holds only the counts.
    const text = view.reason || view.counts_text || view.summary || "";
    if (summary.textContent !== text) summary.textContent = text;
    const stopText = view.stopped || view.filling || "";
    if (stopped.textContent !== stopText) stopped.textContent = stopText;
    stopped.hidden = !stopText;
    const items = view.items || [];
    renderRows(doc, tbody, items);
    tableNavigation.refresh();
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
  async function load({ force = false, quiet = false } = {}) {
    const mine = session;
    const requestSequence = ++nextRequest;
    const userTicket = quiet ? latestUser : ++latestUser;
    const quietTicket = quiet ? ++latestQuiet : null;
    const minutes = Number(span.value) || 60;
    let view;
    try {
      view = await request(`${ENDPOINT}?minutes=${minutes}${force ? "&refresh=true" : ""}`);
    } catch (error) {
      if (
        mine !== session
        || (quiet
          ? quietTicket !== latestQuiet || userTicket !== latestUser
          : userTicket !== latestUser)
      ) return null;
      throw error;
    }
    if (
      mine !== session
      || (quiet
        ? quietTicket !== latestQuiet || userTicket !== latestUser
        : userTicket !== latestUser)
    ) return null;
    // Keep timeline display ordered by request start, while allowing a still
    // current explicit request to announce the latest visible result.
    if (requestSequence < renderedRequest) return quiet ? null : lastView;
    renderedRequest = requestSequence;
    show(view);
    return view;
  }

  // A refresh nobody asked for: on its timer, a song change or the card
  // opening.  It says nothing, except a failure, once, until one succeeds.
  async function quietRefresh(force = false) {
    try {
      const view = await load({ force, quiet: true });
      if (view) {
        quietError = null;
        if (fillingAsk !== null && !view.filling) {
          // The preview someone asked for has finished filling in: say so once.
          if (fillingAsk === latestUser) {
            announceStatus(status, `Hour preview finished. ${view.stopped || spokenSummary(view)}`,
              { force: true, dwellMs: 8000, mirror: false });
          }
          fillingAsk = null;
        }
        // Come back sooner while the server is still filling it in.
        if (view.filling && card.open) schedule();
      }
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
      if (card.open && timer === null) schedule();
    }, lastView?.filling ? FILLING_REFRESH_MS : REFRESH_MS);
  }

  function stop() {
    if (timer !== null) clearTimer(timer);
    if (songTimer !== null) clearTimer(songTimer);
    timer = null;
    songTimer = null;
    fillingAsk = null;
  }

  // Ask on the user's behalf and say the outcome once, even when it is the
  // same as before.  Only the newest ask is said: one overtaken by a later
  // one (another span chosen meanwhile) says nothing.
  async function askAndSay(lead, { force = false, words = spokenSummary } = {}) {
    let view;
    try {
      view = await load({ force });
    } catch (error) {
      announceStatus(status, `The hour preview could not be loaded: ${error.message}`,
        { force: true, dwellMs: 8000, tone: "error" });
      return null;
    }
    if (view) {
      quietError = null;
      fillingAsk = view.filling ? latestUser : null;
      announceStatus(status, `${lead} ${words(view)}`.trim(),
        { force: true, dwellMs: 8000, mirror: false });
    }
    if (card.open) schedule();
    return view;
  }

  // Refresh preview: the new summary is said once.
  function refresh() {
    return askAndSay("Hour preview updated.", { force: true });
  }

  // Show: a new span says only its counts, once it has loaded.
  function spanChanged() {
    return askAndSay("", {
      words: (view) => (view.reason
        || (view.counts_text ? `${view.counts_text}${fillingNote(view)}` : spokenSummary(view))),
    });
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

  function noteSeek() {
    if (!card.open) return;
    if (songTimer !== null) clearTimer(songTimer);
    songTimer = setTimer(() => {
      songTimer = null;
      if (showing()) void quietRefresh(true);
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
  // Back on Now Playing, or back in this browser tab: catch up at once.
  doc.addEventListener("autodj:view", (event) => {
    if (event.detail?.view === "now" && showing()) void quietRefresh();
  });
  doc.addEventListener("visibilitychange", () => {
    if (showing()) void quietRefresh();
  });

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

  return { refresh, resume, noteState, noteSeek, speakSummary, clear };
}
