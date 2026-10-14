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
//
// Smooth transitions (bridge songs and what happens when no smooth spot is
// found) save with their own button.  A refresh fills the form only while
// nobody is changing it.  A refused Roughness limit gets aria-invalid, its
// error linked to the field and shown under it, and focus; the result of a
// save is said once through #requests-status.

import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";
import { clockText } from "./time-format.js";

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

// Where a waiting request would go and why, with any bridge songs.
export function fitText(request) {
  const reason = String(request.reason || request.fit || "").trim();
  const bridges = Array.isArray(request.bridges) ? request.bridges.filter(Boolean) : [];
  if (!bridges.length || /^Bridge to /.test(reason)) return reason;
  return `${reason} Bridge songs: ${bridges.join(", ")}.`.trim();
}

// The Roughness limit as typed: a number from 0 to 10, or null.
export function parseThreshold(text) {
  const value = String(text ?? "").trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$|^\.\d+$/.test(value)) return null;
  const number = Number(value);
  return number >= 0 && number <= 10 ? number : null;
}

export const THRESHOLD_ERROR = "Enter a roughness limit from 0 to 10, such as 2 or 1.5.";

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
  return clockText(new Date(Number(seconds) * 1000));
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
  if (!card || !summary || !list || !recent || !status) {
    return { refresh: async () => {}, clear: () => {} };
  }
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
    button.textContent = label;
    button.dataset.song = songText(item);
    button.setAttribute("aria-label", `${label} ${button.dataset.song}`);
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
    setText(li.querySelector(".request-fit"), fitText(item));
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
    return `${word}: ${songText(item)}. ${fitText({ ...item, reason: item.note || "" })}`.trim();
  }

  const settingsForm = doc.getElementById("requests-settings");
  const bridgesBox = doc.getElementById("requests-bridges");
  const maxBridges = doc.getElementById("requests-max-bridges");
  const threshold = doc.getElementById("requests-threshold");
  const thresholdError = doc.getElementById("requests-threshold-error");
  let settingsDirty = false;
  let saving = false;

  function linkThresholdError(linked) {
    if (!threshold || !thresholdError) return;
    const ids = (threshold.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== thresholdError.id);
    if (linked) ids.push(thresholdError.id);
    threshold.setAttribute("aria-describedby", ids.join(" "));
  }

  function clearThresholdError() {
    threshold?.removeAttribute("aria-invalid");
    linkThresholdError(false);
    if (thresholdError) { thresholdError.textContent = ""; thresholdError.hidden = true; }
  }

  function showThresholdError(message) {
    threshold.setAttribute("aria-invalid", "true");
    linkThresholdError(true);
    thresholdError.textContent = message;
    thresholdError.hidden = false;
    threshold.focus();
    say(message, "error");
  }

  function noSmoothChoice() {
    return settingsForm?.querySelector('input[name="requests-no-smooth"]:checked')?.value || "best";
  }

  // The saved settings, unless the operator is changing them now.
  function fillSettings(state) {
    if (!settingsForm || settingsDirty || saving) return;
    if (settingsForm.contains(doc.activeElement)) return;
    if (bridgesBox) bridgesBox.checked = state.bridges_enabled === true;
    if (maxBridges && state.max_bridges != null) maxBridges.value = String(state.max_bridges);
    if (threshold && state.bridge_cost_threshold != null) {
      threshold.value = String(state.bridge_cost_threshold);
    }
    const choice = settingsForm.querySelector(
      `input[name="requests-no-smooth"][value="${state.no_smooth_spot === "wait" ? "wait" : "best"}"]`,
    );
    if (choice) choice.checked = true;
  }

  if (settingsForm) {
    settingsForm.addEventListener("input", () => { settingsDirty = true; });
    settingsForm.addEventListener("change", () => { settingsDirty = true; });
    settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (saving) return;
      clearThresholdError();
      const limit = parseThreshold(threshold?.value);
      if (limit === null) {
        showThresholdError(THRESHOLD_ERROR);
        return;
      }
      const body = {
        bridges_enabled: Boolean(bridgesBox?.checked),
        max_bridges: Number(maxBridges?.value || 3),
        bridge_cost_threshold: limit,
        no_smooth_spot: noSmoothChoice(),
      };
      saving = true;
      try {
        const reply = await request("/api/requests/settings", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        settingsDirty = false;
        saving = false;
        lastJson = null;
        render({ ...reply, pending: reply.pending || [], recent: reply.recent || [] });
        say("Request settings saved.");
      } catch (error) {
        say(error?.message || "Could not save the request settings.", "error");
      } finally {
        saving = false;
      }
    });
  }

  function render(state) {
    lastState = state;
    const json = JSON.stringify(state);
    if (json === lastJson) return;
    lastJson = json;
    fillSettings(state);
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
    const song = button.dataset.song || "the song";
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
  // Switching stations (#70) or signing out: drop the other station's
  // requests at once, then ask the station now controlled.
  function clear() {
    lastJson = null;
    lastState = null;
    settingsDirty = false;
    clearThresholdError();
    list.replaceChildren();
    recent.replaceChildren();
    card.hidden = true;
    void refresh();
  }
  return { refresh, clear };
}
