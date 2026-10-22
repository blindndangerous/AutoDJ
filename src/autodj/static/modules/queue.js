// User-managed queue: render, reorder (Top/Up/Down), Remove, Clear.
//
// Event delegation at document level so the per-row buttons share a
// single handler.  Optimistic local render + key tracking so the UI updates
// immediately without waiting for the server round trip.

import { fmtTrack, placeRows } from "./dom-helpers.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  staleActionText,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { sendWithVoiceTrackConflict, voiceTrackConflict } from "./voice-track-conflict.js";

let _lastKey = "";
let _renderGeneration = 0;
// The queue as it stood before a reorder or removal still in flight.  A
// push of exactly that queue left the server before the change did, and
// would only show the old order again for a second.
let _pendingFromKey = null;

function _queueKey(queue) {
  // A bridge song's label is part of the row, so a change to it redraws.
  return JSON.stringify(queue.map((t) => (t.bridge_to ? [t.path, t.bridge_to] : t.path)));
}

// "Bridge to Song" for a song AutoDJ queued to lead into a listener
// request (#49), said with the row and in each button's name.
function bridgeText(track) {
  const target = String(track?.bridge_to || "").trim();
  return target ? `Bridge to ${target}` : "";
}

// A websocket push draws the list again.  Remember which row (path plus
// which duplicate of it) and which action held focus, so focus can go back
// to the same control when its row is gone or the action is off.
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

// The button for *action* in *row*, or the row's first other action that
// is on once the list is drawn.
function _rowAction(row, action, usable) {
  if (!row) return null;
  const named = row.querySelector(`.queue-btn[data-action="${action}"]`);
  if (usable(named)) return named;
  return [...row.querySelectorAll('.queue-btn:not([data-action="info"])')].find(usable) || null;
}

// Where focus goes after a push: the same control on the same track, or
// the row now where it was.
function _spotTarget(spot) {
  return (rows, usable) => {
    const samePath = rows.filter((row) => row.dataset.path === spot.path);
    const row = samePath[spot.occurrence] || rows[Math.min(spot.index, rows.length - 1)];
    return _rowAction(row, spot.action, usable);
  };
}

export function applyQueueState(queue, els) {
  const key = _queueKey(queue);
  if (key === _lastKey || key === _pendingFromKey) return;
  _lastKey = key;
  const spot = els.queueList ? _focusedQueueSpot(els.queueList) : null;
  renderQueue(queue, els, { focus: spot ? _spotTarget(spot) : null });
}

export function resetQueueState(els) {
  const empty = [];
  _pendingFromKey = null;
  _lastKey = _queueKey(empty);
  renderQueue(empty, els);
}

const EMPTY_TEXT = "Queue is empty.  Search, then use Play next or Add to queue.";
// Each row's buttons: action, words on screen, icon, and the name built
// from the song.
const ROW_ACTIONS = [
  ["top", "Top", "", (name) => `Move ${name} to top of queue`],
  ["up", "Up", "▲", (name) => `Move ${name} up in queue`],
  ["down", "Down", "▼", (name) => `Move ${name} down in queue`],
  ["remove", "Remove", "✕", (name) => `Remove ${name} from queue`],
  ["info", "Track info", "", (name) => `Track info for ${name}`],
];
const ROW_LABELS = Object.fromEntries(ROW_ACTIONS.map(([action, , , label]) => [action, label]));

function _newQueueRow(doc, path) {
  const row = doc.createElement("li");
  row.dataset.path = path;
  const name = doc.createElement("span");
  name.className = "queue-name";
  row.append(name);
  for (const [action, words, icon] of ROW_ACTIONS) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "queue-btn";
    button.dataset.action = action;
    button.dataset.path = path;
    if (icon) {
      const shape = doc.createElement("span");
      shape.setAttribute("aria-hidden", "true");
      shape.textContent = icon;
      button.append(shape, ` ${words}`);
    } else {
      button.textContent = words;
    }
    row.append(button);
  }
  return row;
}

// Changes only what differs, so a row that stays says nothing new.
function _fillQueueRow(row, track, index) {
  const bridge = bridgeText(track);
  const shown = fmtTrack(track);
  // Each button names the song, and a bridge says which request it leads into.
  const name = bridge ? `${shown}, b${bridge.slice(1)}` : shown;
  if (row.dataset.queueIndex !== String(index)) row.dataset.queueIndex = String(index);
  if (bridge) {
    if (row.dataset.bridgeTo !== track.bridge_to) row.dataset.bridgeTo = track.bridge_to;
  } else if ("bridgeTo" in row.dataset) {
    delete row.dataset.bridgeTo;
  }
  const label = row.querySelector(".queue-name");
  const text = `${index + 1}. ${shown}`;
  if (label.textContent !== text) label.textContent = text;
  if (label.title !== shown) label.title = shown;
  let note = row.querySelector(".queue-bridge");
  if (bridge) {
    if (!note) {
      note = row.ownerDocument.createElement("span");
      note.className = "queue-bridge";
      label.after(note);
    }
    if (note.textContent !== bridge) note.textContent = bridge;
  } else {
    note?.remove();
  }
  for (const button of row.querySelectorAll(":scope > .queue-btn")) {
    const words = ROW_LABELS[button.dataset.action](name);
    if (button.getAttribute("aria-label") !== words) button.setAttribute("aria-label", words);
  }
}

// Draws the queue in place (#328): a row that stays keeps its node, the
// focused row is never moved, and focus never sits on a row that goes or
// a button that goes off.  *focus*, when given, picks the button that
// gets focus from the new rows (an empty queue gives it to the list).
export function renderQueue(queue, { queueList, queueCount }, { focus = null } = {}) {
  if (queueCount) queueCount.textContent = queue.length ? `(${queue.length})` : "";
  if (!queueList) return;
  _renderGeneration += 1;
  const doc = queueList.ownerDocument;
  if (queue.length === 0) {
    let empty = queueList.querySelector(":scope > li.no-results");
    if (!empty) {
      empty = doc.createElement("li");
      empty.className = "no-results";
      empty.textContent = EMPTY_TEXT;
    }
    if (focus) queueList.focus();
    placeRows(queueList, [empty]);
    return;
  }
  // The rows already drawn, by song and which duplicate of it.
  const drawn = new Map();
  const seen = new Map();
  const keyOf = (path) => {
    const occurrence = seen.get(path) || 0;
    seen.set(path, occurrence + 1);
    return JSON.stringify([path, occurrence]);
  };
  for (const row of queueList.querySelectorAll(":scope > li[data-path]")) {
    drawn.set(keyOf(row.dataset.path), row);
  }
  seen.clear();
  const off = new Map();
  const rows = queue.map((track, index) => {
    const row = drawn.get(keyOf(track.path)) || _newQueueRow(doc, track.path);
    _fillQueueRow(row, track, index);
    for (const button of row.querySelectorAll(":scope > .queue-btn")) {
      const action = button.dataset.action;
      off.set(button, action === "top" || action === "up" ? index === 0
        : action === "down" ? index === queue.length - 1 : false);
    }
    return row;
  });
  // Buttons come on first, so focus can go to them; they go off last,
  // once focus has left them.
  for (const [button, disabled] of off) if (!disabled && button.disabled) button.disabled = false;
  const target = focus ? focus(rows, (button) => Boolean(button) && off.get(button) === false) : null;
  if (target?.isConnected) target.focus();
  placeRows(queueList, rows);
  if (target && doc.activeElement !== target) target.focus();
  for (const [button, disabled] of off) if (disabled && !button.disabled) button.disabled = true;
  installActionListNavigation(queueList).refresh();
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
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    let voiceConflict = null;
    const clear = (voice = {}) => requestJson("/api/queue/reorder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: [], ...voice }),
    });
    // Cleared while the confirmation is still open, so focus comes back
    // to the button with the list already empty (see confirmAction).  The
    // result is said after the dialog has closed.
    const confirmed = await confirmAction(doc, {
      title: "Clear the queue?",
      message: `This removes all ${count} queued ${plural}.  AutoDJ keeps picking tracks on its own.`,
      confirmLabel: "Clear queue",
      onConfirm: async () => {
        try {
          await clear();
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          _lastKey = _queueKey([]);
          renderQueue([], { queueList, queueCount });
          result = [`Cleared the queue.  Removed ${count} ${plural}.`, { dwellMs: 3000, force: true }];
        } catch (errorValue) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          if (voiceTrackConflict(errorValue)) voiceConflict = errorValue;
          else result = [`Could not clear the queue: ${errorValue.message}`,
            { dwellMs: 6000, force: true, tone: "error" }];
        }
        return queueClear;
      },
    });
    queueClear.focus();
    if (confirmed && voiceConflict && isAuthenticatedRequestCurrent(epoch)) {
      try {
        const sent = await sendWithVoiceTrackConflict(doc, clear, voiceConflict);
        if (!sent) result = ["Queue clear canceled.", { dwellMs: 3000, force: true }];
        else {
          _lastKey = _queueKey([]);
          renderQueue([], { queueList, queueCount });
          result = [`Cleared the queue.  Removed ${count} ${plural}.`, { dwellMs: 3000, force: true }];
        }
      } catch (errorValue) {
        result = [`Could not clear the queue: ${errorValue.message}`,
          { dwellMs: 6000, force: true, tone: "error" }];
      }
      queueClear.focus();
    }
    if (confirmed && result && isAuthenticatedRequestCurrent(epoch)) {
      announceStatus(queueAnnounce, ...result);
    }
  });
}

export function installQueueButtons(els) {
  const { queueList, queueAnnounce } = els;
  if (!queueList) return;
  let mutationPending = false;
  // Whether a press while a change is sent has been answered yet: once per
  // change, so a held key does not repeat it.
  let stillSendingSaid = false;

  installActionListNavigation(queueList);

  // Delegated from the document, not the <ol>: a click listener on the
  // list made NVDA call the whole list "clickable".
  queueList.ownerDocument.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".queue-btn");
    if (!btn || !queueList.contains(btn) || btn.disabled) return;
    const action = btn.dataset.action;
    const path   = btn.dataset.path;
    if (action === "info") {
      queueList.ownerDocument.dispatchEvent(new CustomEvent("autodj:edit-points", {
        detail: { path, kind: "music", opener: btn },
      }));
      return;
    }
    if (mutationPending) {
      // A second press while a change is sent does nothing; it says why.
      if (!stillSendingSaid) {
        stillSendingSaid = true;
        announceStatus(queueAnnounce, "Still sending the last change…", { dwellMs: 3000, force: true });
      }
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch();

    const items = Array.from(queueList.querySelectorAll("li[data-path]"));
    const snapshot = items.map((li) => ({
      path: li.dataset.path,
      display_name: li.querySelector(".queue-name").textContent.replace(/^\d+\.\s*/, ""),
      ...(li.dataset.bridgeTo ? { bridge_to: li.dataset.bridgeTo } : {}),
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

    // Whether the pressed button had focus.  The optimistic render moves it
    // at once to where it lands (the moved track's button, the next row's
    // Remove, or the list when none is left), so it never sits on the page
    // while the change is sent (#328); the reply places it again whatever
    // the order.  The pressed button stays on: mutationPending refuses a
    // second press, and a button turned off under focus dropped focus.
    const hadFocus = queueList.contains(queueList.ownerDocument.activeElement);

    // Optimistic local render so the user sees instant feedback.
    mutationPending = true;
    stillSendingSaid = false;
    queueList.setAttribute("aria-busy", "true");
    renderQueue(newQueue, els, {
      focus: hadFocus ? (rows, usable) => _rowAction(rows[focusIndex], focusAction, usable) : null,
    });
    const optimisticGeneration = _renderGeneration;
    const newPaths = newQueue.map((item) => item.path);
    _pendingFromKey = _queueKey(snapshot);
    _lastKey = _queueKey(newQueue);
    let ownsRenderedQueue = true;

    try {
      const duplicateRemoval = action === "remove" && paths.indexOf(path) !== idx;
      const url = action === "remove" && !duplicateRemoval ? "/api/queue/remove" : "/api/queue/reorder";
      const body = action === "remove" && !duplicateRemoval
        ? { path, position: idx }
        : { paths: newPaths, from_paths: paths, order: newQueue.map((item) => snapshot.indexOf(item)) };
      const sent = await sendWithVoiceTrackConflict(queueList.ownerDocument, (voice) => requestJson(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The place in the queue as shown, so the server moves or removes
        // that very entry, not another of the same file (a show's copy, #106).
        body: JSON.stringify({ ...body, ...voice }),
      }));
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      if (!sent) {
        if (_renderGeneration === optimisticGeneration) {
          renderQueue(snapshot, els);
          _lastKey = _queueKey(snapshot);
          focusIndex = idx;
          focusAction = action;
        } else ownsRenderedQueue = false;
        announceStatus(queueAnnounce, "Queue change canceled.", { dwellMs: 3000, force: true });
      } else {
        ownsRenderedQueue = _renderGeneration === optimisticGeneration;
        // force: a second "Move up" on the same track is a second action,
        // and silence would read as the button not working.
        announceStatus(queueAnnounce, announceMsg, { dwellMs: 3000, force: true });
      }
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      // Answered after a station switch: the queue on screen is the new
      // station's, so only what happened on the old one is said (#70).
      const left = staleActionText(errorValue);
      if (left) {
        ownsRenderedQueue = false;
        announceStatus(queueAnnounce, left,
          { dwellMs: 6000, force: true, tone: errorValue.actionDone ? "info" : "error" });
      } else {
        if (_renderGeneration === optimisticGeneration) {
          renderQueue(snapshot, els);
          _lastKey = _queueKey(snapshot);
          focusIndex = idx;
          focusAction = action;
          focusQueueList = false;
        } else {
          ownsRenderedQueue = false;
        }
        announceStatus(queueAnnounce,
          `Could not update queue: ${errorValue.message}`,
          { dwellMs: 6000, force: true, tone: "error" });
      }
    } finally {
      mutationPending = false;
      _pendingFromKey = null;
      queueList.setAttribute("aria-busy", "false");
    }

    if (!isAuthenticatedRequestCurrent(epoch)) return;
    // The reply places focus only when it was in the queue at the press and
    // still is (or was lost to the page): focus the user has taken
    // elsewhere meanwhile stays there.
    if (!hadFocus) return;
    const active = queueList.ownerDocument.activeElement;
    const onPage = !active || active === queueList.ownerDocument.body;
    if (!onPage && !queueList.contains(active)) return;
    if (!ownsRenderedQueue) {
      // A push drew the list the server has.  Focus that was on the
      // pressed button goes to the same track's button there, or to the
      // row now where it was.
      if (!onPage) return;
      const rows = Array.from(queueList.querySelectorAll("li[data-path]"));
      const own = action === "remove" ? -1 : rows.findIndex((row) => row.dataset.path === path);
      focusQueueList = rows.length === 0;
      focusIndex = own >= 0 ? own : Math.max(0, Math.min(idx, rows.length - 1));
    }
    if (focusQueueList) {
      queueList.focus();
      return;
    }
    // The moved track's button, the neighbour that took a removed row's
    // place, or the pressed button again after a rollback.
    const row = queueList.querySelector(`li[data-queue-index="${focusIndex}"]`);
    const target = row?.querySelector(`.queue-btn[data-action="${focusAction}"]:not(:disabled)`)
      || row?.querySelector('.queue-btn:not([data-action="info"]):not(:disabled)');
    target?.focus();
  });
}
