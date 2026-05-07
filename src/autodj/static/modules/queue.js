// User-managed queue: render, reorder (Up/Down), Remove.
//
// Event delegation on the <ul> so the per-row buttons share a single
// handler.  Optimistic local render + key tracking so the UI updates
// immediately without waiting for the server round trip.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";

let _lastKey = "";

function _queueKey(queue) {
  return queue.map((t) => t.path).join("|");
}

export function applyQueueState(queue, els) {
  const key = _queueKey(queue);
  if (key === _lastKey) return;
  _lastKey = key;
  renderQueue(queue, els);
}

export function renderQueue(queue, { queueList, queueCount }) {
  if (queueCount) queueCount.textContent = queue.length ? `(${queue.length})` : "";
  if (!queueList) return;
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
    return `<li data-path="${path}">
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

  queueList.addEventListener("click", async (e) => {
    const btn = e.target.closest(".queue-btn");
    if (!btn || btn.disabled) return;
    const action = btn.dataset.action;
    const path   = btn.dataset.path;

    const items = Array.from(queueList.querySelectorAll("li[data-path]"));
    const paths = items.map((li) => li.dataset.path);
    const idx   = paths.indexOf(path);
    if (idx < 0) return;

    const newPaths = paths.slice();
    let focusAction = action;
    let focusPath = path;
    let announceMsg = "";

    const niceName = items[idx]
      ? items[idx].querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
      : path;

    if (action === "up" && idx > 0) {
      [newPaths[idx - 1], newPaths[idx]] = [newPaths[idx], newPaths[idx - 1]];
      announceMsg = `Moved ${niceName} up.`;
      if (idx - 1 === 0) focusAction = "down";
    } else if (action === "down" && idx < newPaths.length - 1) {
      [newPaths[idx + 1], newPaths[idx]] = [newPaths[idx], newPaths[idx + 1]];
      announceMsg = `Moved ${niceName} down.`;
      if (idx + 1 === newPaths.length - 1) focusAction = "up";
    } else if (action === "remove") {
      newPaths.splice(idx, 1);
      announceMsg = `Removed ${niceName} from queue.`;
      if (newPaths.length === 0) {
        focusPath = null;
      } else {
        focusPath = newPaths[Math.min(idx, newPaths.length - 1)];
        focusAction = "remove";
      }
    } else {
      return;
    }

    // Optimistic local render so the user sees instant feedback.
    renderQueue(
      newPaths.map((p) => {
        const li = items.find((i) => i.dataset.path === p);
        return {
          path: p,
          display_name: li
            ? li.querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, "")
            : p,
        };
      }),
      els,
    );
    _lastKey = _queueKey(newPaths.map((p) => ({ path: p })));

    if (action === "remove") {
      await fetch("/api/queue/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
    } else {
      await fetch("/api/queue/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: newPaths }),
      });
    }

    if (queueAnnounce) {
      queueAnnounce.textContent = announceMsg;
      clearLiveRegionLater(queueAnnounce);
    }

    if (focusPath) {
      const target = queueList.querySelector(
        `li[data-path="${CSS.escape(focusPath)}"] .queue-btn[data-action="${focusAction}"]`
      );
      if (target && !target.disabled) target.focus();
    }
  });
}
