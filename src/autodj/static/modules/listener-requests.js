// Queue & Search > Listener requests (#49): the requests listeners sent
// from the public request page, for the operator to approve, play next or
// turn down.
//
// The card shows only when [requests] is on for the station.  While it is
// open (and the tab is showing) it refreshes every 15 seconds, silently:
// the summary and the lists are plain text.  A refresh updates each
// request's row in place, adds new rows and removes decided ones, so the
// list is never rebuilt under the operator: a row that holds focus stays
// until focus leaves it, and nothing a refresh does moves focus.
//
// After Approve, Play next or Turn down, focus moves to the next request's
// Approve button (its first button when Approve is not offered), or to the
// card's summary when none is left.  The result ("Queued Song by Artist.")
// is said once through #requests-status a moment later, so the speech for
// the newly focused button does not cut it off.

import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const POLL_MS = 15000;
// Long enough for a screen reader to start on the focused button first.
export const ANNOUNCE_DELAY_MS = 600;
const ACTIONS = [
  ["approve", "Approve"],
  ["play-next", "Play next"],
  ["reject", "Turn down"],
];

export function songText(request) {
  const title = String(request.title || "").trim() || "Unknown title";
  const artist = String(request.artist || "").trim();
  return artist ? `${title} by ${artist}` : title;
}

export function requestedText(request, formatTime) {
  const who = String(request.name || "").trim();
  const when = formatTime(request.requested_at);
  return `${who ? `Requested by ${who}` : "Requested"} at ${when}.`;
}

export function summaryText(state) {
  const waiting = state.pending.length;
  const mode = state.approval === "auto"
    ? "Requests that keep the rules join the queue at once."
    : "Each request waits for you.";
  const count = waiting === 0
    ? "No requests waiting."
    : `${waiting} request${waiting === 1 ? "" : "s"} waiting.`;
  return `${count} ${state.today} of ${state.daily_limit} taken today. ${mode}`;
}

const STATUS_WORDS = {
  queued: "Queued",
  rejected: "Turned down",
  expired: "Expired",
};

function defaultTime(seconds) {
  return new Date(Number(seconds) * 1000).toLocaleTimeString([], {
    hour: "numeric", minute: "2-digit",
  });
}

function setText(node, text) {
  if (node && node.textContent !== text) node.textContent = text;
}

export function installListenerRequests({
  doc = document,
  request = requestJson,
  setTimer = (callback, ms) => setInterval(callback, ms),
  later = (callback, ms) => setTimeout(callback, ms),
  formatTime = defaultTime,
} = {}) {
  const card = doc.getElementById("requests-card");
  const heading = doc.getElementById("requests-heading");
  const summary = doc.getElementById("requests-summary");
  const list = doc.getElementById("requests-list");
  const recent = doc.getElementById("requests-recent");
  const recentHeading = doc.getElementById("requests-recent-heading");
  const status = doc.getElementById("requests-status");
  const panel = doc.getElementById("panel-queue");
  if (!card || !summary || !list || !recent || !status) return { refresh: async () => {} };
  let lastJson = null;
  let lastState = null;
  let busy = false;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  const rowOf = (id) => [...list.children].find((li) => li.dataset.id === id) || null;

  function makeButton(item, action, label) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    button.dataset.id = item.id;
    // The visible word first, so speech input matches it (2.5.3).
    button.append(label);
    const which = doc.createElement("span");
    which.className = "visually-hidden";
    which.textContent = ` ${songText(item)}`;
    button.append(which);
    return button;
  }

  function makeRow(item) {
    const li = doc.createElement("li");
    li.dataset.id = item.id;
    const song = doc.createElement("p");
    const strong = doc.createElement("strong");
    strong.className = "request-song";
    const when = doc.createElement("span");
    when.className = "request-when";
    song.append(strong, " ", when);
    const fit = doc.createElement("p");
    fit.className = "request-fit";
    const buttons = doc.createElement("p");
    buttons.className = "request-actions";
    li.append(song, fit, buttons);
    updateRow(li, item);
    return li;
  }

  // Changes only what differs, so a refresh never replaces a focused button.
  function updateRow(li, item) {
    setText(li.querySelector(".request-song"), songText(item));
    setText(li.querySelector(".request-when"), requestedText(item, formatTime));
    setText(li.querySelector(".request-fit"), item.fit || "");
    const buttons = li.querySelector(".request-actions");
    ACTIONS.forEach(([action, label], index) => {
      const offered = action !== "approve" || item.can_approve !== false;
      const button = buttons.querySelector(`[data-action="${action}"]`);
      if (offered && !button) {
        const after = ACTIONS.slice(index + 1)
          .map(([other]) => buttons.querySelector(`[data-action="${other}"]`))
          .find(Boolean);
        buttons.insertBefore(makeButton(item, action, label), after || null);
      } else if (!offered && button && doc.activeElement !== button) {
        button.remove();
      }
    });
  }

  function renderPending(items) {
    const holding = list.contains(doc.activeElement) ? doc.activeElement.closest("li") : null;
    const wanted = new Set(items.map((item) => item.id));
    for (const li of [...list.children]) {
      if (!wanted.has(li.dataset.id) && li !== holding) li.remove();
    }
    let previous = null;
    for (const item of items) {
      let li = rowOf(item.id);
      if (li) {
        updateRow(li, item);
      } else {
        li = makeRow(item);
        list.insertBefore(li, previous ? previous.nextSibling : list.firstChild);
      }
      previous = li;
    }
    list.hidden = list.children.length === 0;
  }

  function recentLine(item) {
    const word = STATUS_WORDS[item.status] || item.status;
    return `${word}: ${songText(item)}. ${item.note || ""}`.trim();
  }

  function render(state) {
    lastState = state;
    const json = JSON.stringify(state);
    if (json === lastJson) return;
    lastJson = json;
    setText(summary, summaryText(state));
    renderPending(state.pending);
    // The decisions list has no buttons, so it may be rebuilt when it changes.
    const lines = state.recent.map(recentLine);
    if (lines.join("\n") !== [...recent.children].map((li) => li.textContent).join("\n")) {
      recent.replaceChildren(...lines.map((line) => {
        const li = doc.createElement("li");
        li.textContent = line;
        return li;
      }));
    }
    recent.hidden = lines.length === 0;
    if (recentHeading) recentHeading.hidden = recent.hidden;
  }

  // A row kept only because it held focus goes once focus has left it (the
  // render keeps only the row that holds focus now, if any).
  list.addEventListener("focusout", () => {
    later(() => {
      if (!lastState) return;
      lastJson = null;
      render(lastState);
    }, 0);
  });

  async function refresh() {
    let state;
    try {
      state = await request("/api/requests", { headers: { Accept: "application/json" } });
    } catch {
      return;
    }
    if (!state?.enabled) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    render({ ...state, pending: state.pending || [], recent: state.recent || [] });
  }

  function neighbours(id) {
    const items = [...list.children];
    const index = items.findIndex((li) => li.dataset.id === id);
    return [items[index + 1], items[index - 1]].filter(Boolean).map((li) => li.dataset.id);
  }

  function focusTarget(ids, state) {
    for (const id of ids) {
      const item = state.pending.find((pending) => pending.id === id);
      const li = rowOf(id);
      if (!item || !li) continue;
      const approve = item.can_approve !== false && li.querySelector('[data-action="approve"]');
      return approve || li.querySelector("button");
    }
    return heading;
  }

  list.addEventListener("click", async (event) => {
    const button = event.target.closest?.("button[data-action]");
    if (!button || busy) return;
    const { id, action } = button.dataset;
    const song = button.querySelector(".visually-hidden")?.textContent.trim() || "the song";
    const next = neighbours(id);
    busy = true;
    button.setAttribute("aria-disabled", "true");
    try {
      const reply = await request(`/api/requests/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      const state = { ...reply, pending: reply.pending || [], recent: reply.recent || [] };
      // Move focus first, then let the decided row go, then say the result.
      focusTarget(next, state)?.focus();
      lastJson = null;
      render(state);
      const done = action === "reject" ? `Turned down ${song}.` : `Queued ${song}.`;
      later(() => say(done), ANNOUNCE_DELAY_MS);
    } catch (error) {
      button.removeAttribute("aria-disabled");
      say(error?.message || "That did not work.", "error");
    } finally {
      busy = false;
    }
  });

  const showing = () => card.open && !card.hidden && !doc.hidden && !panel?.hidden;
  card.addEventListener("toggle", () => { if (card.open) void refresh(); });
  setTimer(() => { if (showing()) void refresh(); }, POLL_MS);
  void refresh();
  return { refresh };
}
