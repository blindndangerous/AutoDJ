// User-managed queue: render, reorder (Up/Down), Remove.
//
// Event delegation on the <ul> so the per-row buttons share a single
// handler.  Optimistic local render + key tracking so the UI updates
// immediately without waiting for the server round trip.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";

let _lastKey = "";
let _renderGeneration = 0;

function _queueKey(queue) {
  return JSON.stringify(queue.map((t) => t.path));
}

export function applyQueueState(queue, els) {
  const key = _queueKey(queue);
  if (key === _lastKey) return;
  _lastKey = key;
  renderQueue(queue, els);
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
      <li class="no-results"
          style="color:var(--text-dim);font-style:italic;list-style:none;padding-left:0">
        Queue is empty.  Search and use "Next" to add a track.
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

export function installQueueButtons(els) {
  const { queueList, queueAnnounce } = els;
  if (!queueList) return;
  let mutationPending = false;

  queueList.addEventListener("click", async (e) => {
    const btn = e.target.closest(".queue-btn");
    if (!btn || btn.disabled || mutationPending) return;
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
    let announceMsg = "";

    const niceName = items[idx]
      ? items[idx].querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
      : path;

    if (action === "up" && idx > 0) {
      [newQueue[idx - 1], newQueue[idx]] = [newQueue[idx], newQueue[idx - 1]];
      focusIndex = idx - 1;
      announceMsg = `Moved ${niceName} up.`;
      if (idx - 1 === 0) focusAction = "down";
    } else if (action === "down" && idx < newQueue.length - 1) {
      [newQueue[idx + 1], newQueue[idx]] = [newQueue[idx], newQueue[idx + 1]];
      focusIndex = idx + 1;
      announceMsg = `Moved ${niceName} down.`;
      if (idx + 1 === newQueue.length - 1) focusAction = "up";
    } else if (action === "remove") {
      newQueue.splice(idx, 1);
      announceMsg = `Removed ${niceName} from queue.`;
      if (newQueue.length === 0) {
        focusIndex = -1;
      } else {
        focusIndex = Math.min(idx, newQueue.length - 1);
        focusAction = "remove";
      }
    } else {
      return;
    }

    // Optimistic local render so the user sees instant feedback.
    mutationPending = true;
    btn.disabled = true;
    queueList.setAttribute("aria-busy", "true");
    renderQueue(newQueue, els);
    const optimisticGeneration = _renderGeneration;
    const newPaths = newQueue.map((item) => item.path);
    _lastKey = _queueKey(newQueue);
    let ownsRenderedQueue = true;

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
      if (queueAnnounce) {
        queueAnnounce.textContent = announceMsg;
        clearLiveRegionLater(queueAnnounce);
      }
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
      } else {
        ownsRenderedQueue = false;
        focusIndex = -1;
      }
      if (queueAnnounce) {
        queueAnnounce.textContent = `Could not update queue: ${errorValue.message}`;
        clearLiveRegionLater(queueAnnounce);
      }
    } finally {
      mutationPending = false;
      queueList.setAttribute("aria-busy", "false");
    }

    if (ownsRenderedQueue && focusIndex >= 0) {
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
