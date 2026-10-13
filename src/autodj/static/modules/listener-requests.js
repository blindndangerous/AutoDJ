// Queue & Search > Listener requests (#49): the requests listeners sent
// from the public request page, for the operator to approve or turn down.
//
// The card shows only when [requests] is on for the station.  While it is
// open (and the tab is showing) it refreshes every 15 seconds, silently:
// the list and the summary line are plain text, and a refresh that finds
// nothing new touches nothing, so focus and the reading position stay put.
// Each button's result (Approved, Turned down, or why not) is said once
// through #requests-status; focus then moves to the next request's
// Approve button, or the card's heading when none is left.

import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const POLL_MS = 15000;
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

export function installListenerRequests({
  doc = document,
  request = requestJson,
  setTimer = (callback, ms) => setInterval(callback, ms),
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
  let busy = false;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  function pendingItem(item) {
    const li = doc.createElement("li");
    li.dataset.id = item.id;
    const song = doc.createElement("p");
    const strong = doc.createElement("strong");
    strong.textContent = songText(item);
    song.append(strong, ` ${requestedText(item, formatTime)}`);
    const fit = doc.createElement("p");
    fit.textContent = item.fit || "";
    const buttons = doc.createElement("p");
    buttons.className = "request-actions";
    for (const [action, label] of ACTIONS) {
      if (action === "approve" && item.can_approve === false) continue;
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
      buttons.append(button, " ");
    }
    li.append(song, fit, buttons);
    return li;
  }

  function recentItem(item) {
    const li = doc.createElement("li");
    const word = STATUS_WORDS[item.status] || item.status;
    li.textContent = `${word}: ${songText(item)}. ${item.note || ""}`.trim();
    return li;
  }

  function render(state) {
    const json = JSON.stringify(state);
    if (json === lastJson) return;
    lastJson = json;
    const active = doc.activeElement;
    const focused = active && list.contains(active)
      ? { id: active.dataset.id, action: active.dataset.action }
      : null;
    if (summary.textContent !== summaryText(state)) summary.textContent = summaryText(state);
    list.replaceChildren(...state.pending.map(pendingItem));
    list.hidden = state.pending.length === 0;
    recent.replaceChildren(...state.recent.map(recentItem));
    recent.hidden = state.recent.length === 0;
    if (recentHeading) recentHeading.hidden = recent.hidden;
    if (focused) {
      const again = list.querySelector(
        `button[data-id="${CSS.escape(focused.id || "")}"][data-action="${CSS.escape(focused.action || "")}"]`,
      );
      again?.focus();
    }
  }

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

  function nextFocus(id) {
    const items = [...list.children];
    const index = items.findIndex((li) => li.dataset.id === id);
    return { after: items[index + 1]?.dataset.id, before: items[index - 1]?.dataset.id };
  }

  list.addEventListener("click", async (event) => {
    const button = event.target.closest?.("button[data-action]");
    if (!button || busy) return;
    const { id, action } = button.dataset;
    const song = button.querySelector(".visually-hidden")?.textContent.trim() || "the song";
    const neighbours = nextFocus(id);
    busy = true;
    button.setAttribute("aria-disabled", "true");
    try {
      const state = await request(`/api/requests/${encodeURIComponent(id)}/${action}`, {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      const done = action === "reject" ? `Turned down ${song}.` : `Queued ${song}.`;
      lastJson = null;
      render({ ...state, pending: state.pending || [], recent: state.recent || [] });
      say(done);
      const target = list.querySelector(`li[data-id="${CSS.escape(neighbours.after || "")}"] button`)
        || list.querySelector(`li[data-id="${CSS.escape(neighbours.before || "")}"] button`)
        || heading;
      target?.focus();
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
