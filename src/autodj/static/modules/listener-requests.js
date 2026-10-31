// Queue & Search > Listener requests (#49): the requests listeners sent
// from the public request page, for the operator to approve, play next or
// turn down.
//
// The card shows once the station answers.  Take listener requests opens
// and closes the station's public request page; while requests are off the
// summary says so and the lists stay hidden.  While the card is open (and
// the tab is showing) it refreshes every 15 seconds, silently:
// the summary and shared tables are not live regions. A refresh updates
// keyed rows in place; removing a focused request moves focus to a remaining
// row or the heading and announces that destination.
//
// After Approve, Play next or Turn down, focus moves to the next request's
// Approve cell (Options when Approve is unavailable), or to the
// card's summary when none is left.  The result ("Queued Song by Artist.")
// is said once through #requests-status a moment later, so the speech for
// the newly focused button does not cut it off.
//
// Request settings (on or off, approval, bridge songs and what happens when
// no smooth spot is found) save with their own button.  While it saves, the
// button is marked busy (aria-busy), never aria-disabled, so NVDA does not
// call the pressed button unavailable (#331).  A refresh fills the form only while
// nobody is changing it.  A refused Roughness limit gets aria-invalid, its
// error linked to the field and shown under it, and focus; the result of a
// save is said once through #requests-status.

import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, staleActionText } from "./api-client.js";
import { announceStatus, sayIfStillPending } from "./live-region.js";
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { stationEpoch } from "./stations.js";
import { clockText } from "./time-format.js";
import { sendWithVoiceTrackConflict } from "./voice-track-conflict.js";

const POLL_MS = 15000;
// Long enough for a screen reader to start on the focused button first.
export const ANNOUNCE_DELAY_MS = 600;

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

export const OFF_SUMMARY = "Listener requests are off for this station. Its request page says requests are closed.";

export function summaryText(state) {
  if (state.enabled === false) return OFF_SUMMARY;
  const waiting = state.pending.length;
  const mode = state.approval === "auto"
    ? "Requests that keep the rules join the queue at once."
    : "Each request waits for you.";
  const count = waiting === 0
    ? "No requests waiting."
    : `${waiting} request${waiting === 1 ? "" : "s"} waiting.`;
  return `${count} ${state.today} of ${state.daily_limit} taken today. ${mode}`;
}

// What a save says: whether it opened or closed the request page, or just "saved".
export function savedText(before, after) {
  if (before?.enabled !== true && after.enabled === true) {
    const mode = after.approval === "auto"
      ? "Requests that keep the rules join the queue at once."
      : "Each request waits for you.";
    return `Listener requests are on. The request page is open. ${mode}`;
  }
  if (before?.enabled === true && after.enabled !== true) {
    return "Listener requests are off. The request page now says requests are closed.";
  }
  return "Request settings saved.";
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
  isEnabled = () => true,
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
    return { refresh: async () => {}, clear: () => {}, setQueueDisabledReason: () => {} };
  }
  let lastJson = null;
  let lastState = null;
  let busy = false;
  let queueDisabledReason = "";

  function actionReason(item, action) {
    if (action === "reject") return "";
    if (queueDisabledReason) return queueDisabledReason;
    return action === "approve" && item.can_approve === false
      ? fitText(item) || "This request cannot be approved in the current queue." : "";
  }

  function say(message, tone = "info") {
    if (!isEnabled()) return;
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  const pendingTable = createCollectionTable({
    host: list, label: "Waiting requests", status, fallback: heading,
    keyOf: (item) => item.id, nameOf: songText, emptyText: "No requests waiting.",
    columns: [
      { key: "song", label: "Song", value: songText },
      { key: "requester", label: "Requester", value: (item) => item.name || "Anonymous" },
      { key: "time", label: "Time", value: (item) => formatTime(item.requested_at) },
      { key: "fit", label: "Fit", value: fitText },
    ],
    primary: (item) => ({ id: "approve", label: "Approve",
      disabledReason: actionReason(item, "approve"),
      run: (button) => decide(item, "approve", button) }),
    actions: (item) => [
      { id: "play-next", label: "Play next", disabledReason: actionReason(item, "play-next"), run: (button) => decide(item, "play-next", button) },
      { id: "reject", label: "Turn down", run: (button) => {
        const mine = settingsGeneration;
        return deleteCollectionItem(doc, { title: "Turn down this request?",
          message: `Turn down ${songText(item)}?`, confirmLabel: "Turn down",
          onConfirm: () => mine === settingsGeneration && isEnabled() ? decide(item, "reject", button) : null });
      } },
    ],
  });
  const recentTable = createCollectionTable({
    host: recent, label: "Recent decisions", status, fallback: heading,
    keyOf: (item) => item.id, nameOf: songText, emptyText: "No recent decisions.",
    columns: [
      { key: "song", label: "Song", value: songText },
      { key: "requester", label: "Requester", value: (item) => item.name || "Anonymous" },
      { key: "time", label: "Time", value: (item) => formatTime(item.requested_at) },
      { key: "decision", label: "Decision", value: (item) => STATUS_WORDS[item.status] || item.status },
      { key: "reason", label: "Reason", value: (item) => fitText({ ...item, reason: item.note || "" }) },
    ],
  });

  const settingsForm = doc.getElementById("requests-settings");
  const enabledBox = doc.getElementById("requests-enabled");
  const saveButton = doc.getElementById("requests-settings-save");
  const bridgesBox = doc.getElementById("requests-bridges");
  const maxBridges = doc.getElementById("requests-max-bridges");
  const threshold = doc.getElementById("requests-threshold");
  const thresholdError = doc.getElementById("requests-threshold-error");
  let settingsDirty = false;
  let saving = false;
  let settingsGeneration = 0;
  let settingsSaveId = 0;
  let refreshGeneration = 0;

  function clearThresholdError() {
    clearFieldError(threshold, thresholdError);
  }

  function showThresholdError(message) {
    showFieldError(threshold, message, thresholdError, status);
  }

  function noSmoothChoice() {
    return settingsForm?.querySelector('input[name="requests-no-smooth"]:checked')?.value || "best";
  }

  function approvalChoice() {
    return settingsForm?.querySelector('input[name="requests-approval"]:checked')?.value || "manual";
  }

  // The saved settings, unless the operator is changing them now.
  function fillSettings(state) {
    if (!settingsForm || settingsDirty || saving) return;
    if (settingsForm.contains(doc.activeElement)) return;
    if (enabledBox) enabledBox.checked = state.enabled === true;
    const approval = settingsForm.querySelector(
      `input[name="requests-approval"][value="${state.approval === "auto" ? "auto" : "manual"}"]`,
    );
    if (approval) approval.checked = true;
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
      if (saving || !isEnabled()) return;
      clearThresholdError();
      const limit = parseThreshold(threshold?.value);
      if (limit === null) {
        showThresholdError(THRESHOLD_ERROR);
        return;
      }
      const body = {
        enabled: Boolean(enabledBox?.checked),
        approval: approvalChoice(),
        bridges_enabled: Boolean(bridgesBox?.checked),
        max_bridges: Number(maxBridges?.value || 3),
        bridge_cost_threshold: limit,
        no_smooth_spot: noSmoothChoice(),
      };
      const before = lastState;
      const mine = settingsGeneration;
      const saveId = ++settingsSaveId;
      const epoch = captureAuthenticatedRequestEpoch();
      const station = stationEpoch();
      const currentSave = () => mine === settingsGeneration && epoch === captureAuthenticatedRequestEpoch()
        && station === stationEpoch();
      // saving holds off another press; aria-busy, not aria-disabled (#331).
      saving = true;
      saveButton?.classList.add("is-pending");
      saveButton?.setAttribute("aria-busy", "true");
      try {
        const stopPending = sayIfStillPending((message) => {
          if (currentSave() && isAuthenticatedRequestCurrent(epoch)) say(message);
        }, "Saving request settings…");
        let reply;
        try { reply = await request("/api/requests/settings", {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }); } finally { stopPending(); }
        if (!isEnabled() || !currentSave() || !isAuthenticatedRequestCurrent(epoch)) return;
        settingsDirty = false;
        saving = false;
        lastJson = null;
        const state = { ...reply, pending: reply.pending || [], recent: reply.recent || [] };
        render(state);
        say(savedText(before, state));
      } catch (error) {
        if (!isEnabled() || !currentSave() || !isAuthenticatedRequestCurrent(epoch)) return;
        // Answered after a station switch: the form is the new station's (#70).
        const left = staleActionText(error);
        if (left) say(left, error?.actionDone ? "info" : "error");
        else say(`Could not save the request settings: ${error?.message || "no answer"}`, "error");
      } finally {
        if (saveId === settingsSaveId) {
          saving = false;
          saveButton?.classList.remove("is-pending");
          saveButton?.removeAttribute("aria-busy");
        }
      }
    });
  }

  function render(state, { announceFocus = true } = {}) {
    lastState = state;
    const json = JSON.stringify(state);
    if (json === lastJson) return;
    lastJson = json;
    fillSettings(state);
    setText(summary, summaryText(state));
    // Off: nothing is waiting or decided for the operator to act on.
    const off = state.enabled === false;
    pendingTable.update(off ? [] : state.pending, { announceFocus });
    recentTable.update(off ? [] : state.recent);
    list.hidden = off;
    recent.hidden = off;
    if (recentHeading) recentHeading.hidden = off;
  }

  async function refresh() {
    if (!isEnabled()) return;
    const mine = ++refreshGeneration;
    const station = stationEpoch();
    const epoch = captureAuthenticatedRequestEpoch();
    let state;
    try {
      state = await request("/api/requests", { headers: { Accept: "application/json" } });
    } catch {
      return;
    }
    if (!isEnabled() || mine !== refreshGeneration || station !== stationEpoch() || !isAuthenticatedRequestCurrent(epoch)) return;
    if (!state || typeof state !== "object") return;
    // Shown off or on: Take listener requests is here.
    card.hidden = false;
    render({ ...state, pending: state.pending || [], recent: state.recent || [] });
  }

  async function decide(item, action, button) {
    if (busy || !isEnabled()) return null;
    const restriction = actionReason(item, action);
    if (restriction) { say(restriction); return null; }
    const mine = settingsGeneration;
    const { id } = item;
    const song = songText(item);
    const index = lastState.pending.findIndex((pending) => pending.id === id);
    if (index < 0) return null;
    // busy holds off another press.  No aria-disabled: NVDA speaks the
    // state change on the focused button, so the press was heard as
    // "unavailable" (#331).  .is-pending gives the busy look, and
    // aria-busy a state a screen reader can report.
    busy = true;
    button.classList.add("is-pending");
    button.setAttribute("aria-busy", "true");
    // A short line when the reply is slow, so the press is heard (#384).
    const stopPending = sayIfStillPending((message) => {
      if (mine === settingsGeneration) say(message);
    }, {
      approve: `Queueing ${song}…`,
      "play-next": `Queueing ${song} to play next…`,
      reject: `Turning down ${song}…`,
    }[action] || "Sending…");
    try {
      const url = `/api/requests/${encodeURIComponent(id)}/${action}`;
      let reply;
      if (action === "approve" || action === "play-next") {
        const sent = await sendWithVoiceTrackConflict(doc, (voice) => {
          const reason = actionReason(item, action);
          if (reason) throw new Error(reason);
          return request(url, {
            method: "POST",
            headers: Object.keys(voice).length
              ? { Accept: "application/json", "Content-Type": "application/json" }
              : { Accept: "application/json" },
            ...(Object.keys(voice).length ? { body: JSON.stringify(voice) } : {}),
          });
        });
        if (!sent) {
          const canceled = action === "approve" ? "Approval" : "Play next";
          if (isEnabled() && mine === settingsGeneration) say(`${canceled} canceled for ${song}.`);
          return;
        }
        reply = sent.value;
      } else {
        reply = await request(url, {
          method: "POST",
          headers: { Accept: "application/json" },
        });
      }
      if (!isEnabled() || mine !== settingsGeneration) return;
      const state = { ...reply, pending: reply.pending || [], recent: reply.recent || [] };
      refreshGeneration++;
      lastJson = null;
      render(state, { announceFocus: false });
      const next = state.pending[Math.min(index, state.pending.length - 1)];
      const target = next ? pendingTable.target(next.id, next.can_approve === false ? "options" : "approve") : heading;
      target?.focus();
      const done = action === "reject" ? `Turned down ${song}.` : `Queued ${song}.`;
      later(() => { if (mine === settingsGeneration) say(done); }, ANNOUNCE_DELAY_MS);
      return target;
    } catch (error) {
      if (!isEnabled()) return;
      // Answered after a station switch: done, or not, on the old station.
      const left = staleActionText(error);
      say(left || error?.message || "That did not work.", error?.actionDone ? "info" : "error");
    } finally {
      stopPending();
      busy = false;
      button.classList.remove("is-pending");
      button.removeAttribute("aria-busy");
    }
  }

  const showing = () => isEnabled() && card.open && !card.hidden && !doc.hidden && !panel?.hidden;
  card.addEventListener("toggle", () => { if (card.open) void refresh(); });
  setTimer(() => { if (showing()) void refresh(); }, POLL_MS);
  void refresh();
  // Switching stations (#70) or signing out: drop the other station's
  // requests at once; refresh the station only while authenticated.
  function clear() {
    settingsGeneration++;
    settingsSaveId++;
    saving = false;
    saveButton?.classList.remove("is-pending");
    saveButton?.removeAttribute("aria-busy");
    refreshGeneration++;
    lastJson = null;
    lastState = null;
    settingsDirty = false;
    clearThresholdError();
    pendingTable.clear();
    recentTable.clear();
    card.hidden = true;
    void refresh();
  }
  function setQueueDisabledReason(reason = "") {
    if (queueDisabledReason === reason) return;
    queueDisabledReason = reason;
    if (lastState) pendingTable.update(lastState.enabled === false ? [] : lastState.pending, { announceFocus: false });
  }
  return { refresh, clear, setQueueDisabledReason };
}
