// User-managed queue: render, reorder (Top/Up/Down), Remove, Clear.
//
// Event delegation on the <ul> so the per-row buttons share a single
// handler.  Optimistic local render + key tracking so the UI updates
// immediately without waiting for the server round trip.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";

let _lastKey = "";
let _renderGeneration = 0;

function _queueKey(queue) {
  return JSON.stringify(queue.map((t) => t.path));
}

// A websocket push rebuilds every row, which destroys the button a
// keyboard user is standing on and drops focus to the top of the page.
// Remember which row (path plus which duplicate of it) and which action
// held focus, then put focus back on the same control in the new list.
function _focusedQueueSpot(queueList) {
  const active = queueList.ownerDocument?.activeElement;
  const button = active?.closest?.(".queue-btn");
  const row = button?.closest("li[data-path]");
  if (!row || !queueList.contains(row)) return null;
  const rows = Array.from(queueList.querySelectorAll("li[data-path]"));
  const index = rows.indexOf(row);
  return {
    path: row.dataset.path,
    occurrence: rows.slice(0, index)
      .filter((other) => other.dataset.path === row.dataset.path).length,
    index,
    action: button.dataset.action,
  };
}

function _restoreQueueFocus(queueList, spot) {
  const rows = Array.from(queueList.querySelectorAll("li[data-path]"));
  if (rows.length === 0) {
    queueList.focus();
    return;
  }
  const samePath = rows.filter((row) => row.dataset.path === spot.path);
  const row = samePath[spot.occurrence]
    || rows[Math.min(spot.index, rows.length - 1)];
  const target = row.querySelector(`.queue-btn[data-action="${spot.action}"]:not(:disabled)`)
    || row.querySelector(".queue-btn:not(:disabled)");
  target?.focus();
}

export function applyQueueState(queue, els) {
  const key = _queueKey(queue);
  if (key === _lastKey) return;
  _lastKey = key;
  const spot = els.queueList ? _focusedQueueSpot(els.queueList) : null;
  renderQueue(queue, els);
  if (spot) _restoreQueueFocus(els.queueList, spot);
}

export function resetQueueState(els) {
  const empty = [];
  _lastKey = _queueKey(empty);
  renderQueue(empty, els);
}

export function renderQueue(queue, { queueList, queueCount }) {
  if (queueCount) queueCount.textContent = queue.length ? `(${queue.length})` : "";
  if (!queueList) return;
  _renderGeneration += 1;
  if (queue.length === 0) {
    queueList.innerHTML = `
      <li class="no-results">
        Queue is empty.  Search, then use Play next or Add to queue.
      </li>`;
    return;
  }
  queueList.innerHTML = queue.map((t, i) => {
    const name = escHtml(fmtTrack(t));
    const path = escHtml(t.path);
    const isFirst = i === 0;
    const isLast  = i === queue.length - 1;
    return `<li data-path="${path}" data-queue-index="${i}">
      <span class="queue-name" title="${name}">${i + 1}. ${name}</span>
      <button class="queue-btn" data-action="top"    data-path="${path}"
              aria-label="Move ${name} to top of queue" ${isFirst ? "disabled" : ""}>
        Top
      </button>
      <button class="queue-btn" data-action="up"     data-path="${path}"
              aria-label="Move ${name} up in queue"     ${isFirst ? "disabled" : ""}>
        <span aria-hidden="true">▲</span> Up
      </button>
      <button class="queue-btn" data-action="down"   data-path="${path}"
              aria-label="Move ${name} down in queue"   ${isLast  ? "disabled" : ""}>
        <span aria-hidden="true">▼</span> Down
      </button>
      <button class="queue-btn" data-action="remove" data-path="${path}"
              aria-label="Remove ${name} from queue">
        <span aria-hidden="true">✕</span> Remove
      </button>
    </li>`;
  }).join("");
}

// Up and Down said only "Moved X up.", which left the listener counting
// rows to find out where the track landed.
function movedMessage(name, index, length) {
  return `Moved ${name} to position ${index + 1} of ${length}.`;
}

// Clear queue asks first: it throws away every row at once.  Focus stays
// on the button whatever the answer, so the user is where they started.
export function installQueueClear({ queueList, queueAnnounce, queueClear, queueCount }) {
  if (!queueClear || !queueList) return;
  const doc = queueClear.ownerDocument;
  queueClear.addEventListener("click", async () => {
    const count = queueList.querySelectorAll("li[data-path]").length;
    if (count === 0) {
      announceStatus(queueAnnounce, "The queue is already empty.", { dwellMs: 3000, force: true });
      return;
    }
    const plural = count === 1 ? "track" : "tracks";
    const confirmed = await confirmAction(doc, {
      title: "Clear the queue?",
      message: `This removes all ${count} queued ${plural}.  AutoDJ keeps picking tracks on its own.`,
      confirmLabel: "Clear queue",
    });
    queueClear.focus();
    if (!confirmed) return;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson("/api/queue/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: [] }),
      });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      _lastKey = _queueKey([]);
      renderQueue([], { queueList, queueCount });
      announceStatus(queueAnnounce, `Cleared the queue.  Removed ${count} ${plural}.`,
        { dwellMs: 3000, force: true });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      announceStatus(queueAnnounce, `Could not clear the queue: ${errorValue.message}`,
        { dwellMs: 6000, force: true, tone: "error" });
    }
  });
}

export function installQueueButtons(els) {
  const { queueList, queueAnnounce } = els;
  if (!queueList) return;
  let mutationPending = false;

  // Delegated from the document, not the <ol>: a click listener on the
  // list made NVDA call the whole list "clickable".
  queueList.ownerDocument.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".queue-btn");
    if (!btn || !queueList.contains(btn) || btn.disabled || mutationPending) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const action = btn.dataset.action;
    const path   = btn.dataset.path;

    const items = Array.from(queueList.querySelectorAll("li[data-path]"));
    const snapshot = items.map((li) => ({
      path: li.dataset.path,
      display_name: li.querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, ""),
    }));
    const paths = items.map((li) => li.dataset.path);
    const idx   = items.indexOf(btn.closest("li[data-path]"));
    if (idx < 0) return;

    const newQueue = snapshot.slice();
    let focusAction = action;
    let focusIndex = idx;
    let focusQueueList = false;
    let announceMsg = "";

    const niceName = items[idx]
      ? items[idx].querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
      : path;

    if (action === "top" && idx > 0) {
      newQueue.unshift(...newQueue.splice(idx, 1));
      focusIndex = 0;
      announceMsg = movedMessage(niceName, 0, newQueue.length);
      focusAction = newQueue.length > 1 ? "down" : "remove";
    } else if (action === "up" && idx > 0) {
      [newQueue[idx - 1], newQueue[idx]] = [newQueue[idx], newQueue[idx - 1]];
      focusIndex = idx - 1;
      announceMsg = movedMessage(niceName, idx - 1, newQueue.length);
      if (idx - 1 === 0) focusAction = "down";
    } else if (action === "down" && idx < newQueue.length - 1) {
      [newQueue[idx + 1], newQueue[idx]] = [newQueue[idx], newQueue[idx + 1]];
      focusIndex = idx + 1;
      announceMsg = movedMessage(niceName, idx + 1, newQueue.length);
      if (idx + 1 === newQueue.length - 1) focusAction = "up";
    } else if (action === "remove") {
      newQueue.splice(idx, 1);
      announceMsg = `Removed ${niceName} from queue.`;
      if (newQueue.length === 0) {
        focusIndex = -1;
        focusQueueList = true;
      } else {
        focusIndex = Math.min(idx, newQueue.length - 1);
        focusAction = "remove";
      }
    } else {
      return;
    }

    // Optimistic local render so the user sees instant feedback.
    mutationPending = true;
    queueList.setAttribute("aria-busy", "true");
    btn.disabled = true;
    renderQueue(newQueue, els);
    const optimisticGeneration = _renderGeneration;
    const newPaths = newQueue.map((item) => item.path);
    _lastKey = _queueKey(newQueue);
    let ownsRenderedQueue = true;
    let successful = false;
    let rolledBack = false;

    try {
      const duplicateRemoval = action === "remove" && paths.indexOf(path) !== idx;
      await requestJson(
        action === "remove" && !duplicateRemoval ? "/api/queue/remove" : "/api/queue/reorder",
        {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "remove" && !duplicateRemoval ? { path } : { paths: newPaths }),
        },
      );
      if (!isAuthenticatedRequestCurrent(epoch)) {
        ownsRenderedQueue = false;
        focusIndex = -1;
        return;
      }
      ownsRenderedQueue = _renderGeneration === optimisticGeneration;
      if (!ownsRenderedQueue) focusIndex = -1;
      successful = ownsRenderedQueue;
      // force: a second "Move up" on the same track is a second action,
      // and silence would read as the button not working.
      announceStatus(queueAnnounce, announceMsg, { dwellMs: 3000, force: true });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) {
        ownsRenderedQueue = false;
        focusIndex = -1;
        return;
      }
      if (_renderGeneration === optimisticGeneration) {
        renderQueue(snapshot, els);
        _lastKey = _queueKey(snapshot);
        focusIndex = idx;
        focusAction = action;
        rolledBack = true;
      } else {
        ownsRenderedQueue = false;
        focusIndex = -1;
      }
      focusQueueList = false;
      announceStatus(queueAnnounce,
        `Could not update queue: ${errorValue.message}`,
        { dwellMs: 6000, force: true, tone: "error" });
    } finally {
      mutationPending = false;
      queueList.setAttribute("aria-busy", "false");
    }

    if ((!successful && !rolledBack) || !ownsRenderedQueue) return;
    if (focusQueueList) {
      queueList.focus();
      return;
    }
    if (focusIndex >= 0) {
      const target = queueList.querySelector(
        `li[data-queue-index="${focusIndex}"] .queue-btn[data-action="${focusAction}"]`
      );
      if (target) {
        target.disabled = false;
        if (!target.disabled) target.focus();
      }
    }
  });
}
