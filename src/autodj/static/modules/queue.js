// The shared collection table owns rendering, menus and cell navigation.
// Queue entries keep local identities through optimistic moves so duplicate
// occurrences retain their rows and their exact server-side permutation.
import { fmtTrack } from "./dom-helpers.js";
import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { makeSay } from "./live-region.js";
import { withDisabled, failureText, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, staleActionText } from "./api-client.js";
import { currentStation } from "./stations.js";
import { confirmAction } from "./confirm-dialog.js";
import { sendWithVoiceTrackConflict, voiceTrackConflict } from "./voice-track-conflict.js";

const controllers = new WeakMap();
const EMPTY_TEXT = "Queue is empty.  Search, then use Play next or Add to queue.";
const queueKey = (queue) => JSON.stringify(queue.map((track) => [track.path, fmtTrack(track), track.bridge_to || ""]));
const bridgeText = (track) => track.bridge_to ? `Bridge to ${String(track.bridge_to).trim()}` : "";
const entryName = ({ track }) => `${fmtTrack(track)}${bridgeText(track) ? `, b${bridgeText(track).slice(1)}` : ""}`;

function controller(els) {
  const { queueList } = els;
  if (!queueList) return null;
  let state = controllers.get(queueList);
  if (state) { Object.assign(state.els, els); return state; }
  state = { els: { ...els }, items: [], nextId: 0, generation: 0, key: "", pendingFromKey: null,
    busy: false, busyController: new AbortController(), disabledReason: "", installed: false };
  controllers.set(queueList, state);
  state.say = makeSay(() => state.els.queueAnnounce, { dwellMs: 3000, errorDwellMs: 6000 });
  state.grid = createCollectionTable({
    host: queueList, label: "Queued tracks", status: els.queueAnnounce, fallback: queueList,
    emptyText: EMPTY_TEXT,
    // The position and any bridge share the track's cell, as before #707.
    columns: [{ key: "track", label: "Track", value: (entry, index) => `${index + 1}. ${entryName(entry)}` }],
    keyOf: (entry) => entry.id, nameOf: entryName,
    actions: (entry, index) => [{ id: "info", label: "Track info", run: (button) => {
      // Track info keeps its existing editor and return-to-collection event.
      button.dataset.path = entry.track.path;
      queueList.ownerDocument.dispatchEvent(new CustomEvent("autodj:edit-points", {
        detail: { path: entry.track.path, kind: "music", opener: button },
      }));
    } }, ...[["top", "Move to top"], ["up", "Move up"], ["down", "Move down"], ["remove", "Remove"]]
      .map(([id, label]) => ({ id, label, disabledReason: actionReason(state, id, index),
        run: (button) => changeQueue(state, id, entry.id, button) }))],
  });
  return state;
}

function actionReason(state, action, index) {
  if (state.disabledReason) return state.disabledReason;
  if (state.busy) return "Still sending the last change…";
  if ((action === "up" || action === "top") && index === 0) return "This track is already first in the queue.";
  if (action === "down" && index === state.items.length - 1) return "This track is already last in the queue.";
  return "";
}

function draw(state, items, announceFocus = true) {
  state.items = items;
  state.key = queueKey(items.map((entry) => entry.track));
  state.generation += 1;
  if (state.els.queueCount) state.els.queueCount.textContent = items.length ? `(${items.length})` : "";
  state.grid.update(items, { announceFocus });
  // Track info's existing return matcher also handles a replacement row.
  for (const entry of items) {
    const button = state.grid.target(entry.id)?.querySelector("button");
    if (button) button.dataset.path = entry.track.path;
  }
}

function renderQueue(queue, els) {
  const state = controller(els);
  if (!state) return;
  const drawn = new Map();
  for (const entry of state.items) {
    if (!drawn.has(entry.track.path)) drawn.set(entry.track.path, []);
    drawn.get(entry.track.path).push(entry);
  }
  draw(state, queue.map((track) => ({
    id: drawn.get(track.path)?.shift()?.id ?? ++state.nextId, track,
  })));
}

export function applyQueueState(queue, els) {
  const state = controller(els);
  if (!state) return;
  const key = queueKey(queue);
  if (key === state.key || key === state.pendingFromKey) return;
  renderQueue(queue, els);
}

export function resetQueueState(els) {
  const state = controller(els);
  if (!state) return;
  state.busyController.abort();
  state.busyController = new AbortController();
  state.busy = false;
  state.pendingFromKey = null;
  draw(state, []);
}

// The app supplies its current playback restriction. Rechecking at invocation
// also covers keyboard activation and a confirmation already on screen.
export function setQueueDisabledReason(els, reason = "") {
  const state = controller(els);
  if (!state || state.disabledReason === reason) return;
  state.disabledReason = reason;
  state.grid.update(state.items, { announceFocus: false });
}


function beginChange(state, action, id) {
  const index = state.items.findIndex((entry) => entry.id === id);
  if (index < 0) return null;
  const reason = actionReason(state, action, index);
  if (reason) { state.say(reason); return null; }
  const snapshot = state.items.slice();
  const entry = snapshot[index];
  const next = snapshot.slice();
  const destination = action === "top" ? 0 : action === "up" ? index - 1 : action === "down" ? index + 1 : index;
  next.splice(index, 1);
  if (action !== "remove") next.splice(destination, 0, entry);
  const paths = snapshot.map((item) => item.track.path);
  const duplicateRemoval = action === "remove" && paths.indexOf(entry.track.path) !== index;
  const remove = action === "remove" && !duplicateRemoval;
  const body = remove ? { path: entry.track.path, position: index }
    : { paths: next.map((item) => item.track.path), from_paths: paths, order: next.map((item) => snapshot.indexOf(item)) };
  const epoch = captureAuthenticatedRequestEpoch();
  const station = currentStation();
  const { queueList } = state.els;
  const busyController = state.busyController;
  state.busy = true;
  state.pendingFromKey = queueKey(snapshot.map((item) => item.track));
  draw(state, next, false);
  const generation = state.generation;
  const focusTarget = () => state.grid.target(state.items[Math.min(destination, state.items.length - 1)]?.id, "options") || queueList;
  const request = (voice = {}) => {
    if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return Promise.resolve(null);
    if (state.disabledReason) return Promise.reject(new Error(state.disabledReason));
    return requestJson(remove ? "/api/queue/remove" : "/api/queue/reorder", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, ...voice }),
    });
  };
  async function settle(send) {
    return withDisabled(queueList, async () => {
      let restoreRemovedFocus = false;
      const rollback = () => {
        if (state.generation !== generation) return;
        restoreRemovedFocus = action === "remove" && queueList.contains(queueList.ownerDocument.activeElement);
        draw(state, snapshot, false);
      };
      try {
        const sent = await send();
        if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return;
        if (!sent) {
          rollback();
          state.say("Queue change canceled.");
        } else {
          const name = fmtTrack(entry.track);
          state.say(action === "remove"
            ? `Removed ${name} from queue. Focus moved to ${state.items[Math.min(index, state.items.length - 1)] ? entryName(state.items[Math.min(index, state.items.length - 1)]) : "Queued tracks"}.`
            : `Moved ${name} to position ${destination + 1} of ${next.length}.`);
        }
      } catch (error) {
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        const stale = staleActionText(error);
        if (stale) state.say(stale, error.actionDone ? "info" : "error");
        else {
          rollback();
          state.say(failureText("update queue", error), "error");
        }
      } finally {
        if (state.busyController === busyController) {
          state.busy = false;
          state.pendingFromKey = null;
          state.grid.update(state.items, { announceFocus: false });
        }
      }
      if (restoreRemovedFocus) state.grid.target(entry.id, "options")?.focus();
    }, { signal: busyController.signal });
  }
  return { request, settle, focusTarget };
}

async function changeQueue(state, action, id, button) {
  if (!state.installed) return;
  if (action !== "remove") {
    const change = beginChange(state, action, id);
    if (change) await change.settle(() => sendWithVoiceTrackConflict(button.ownerDocument, change.request));
    return;
  }
  const index = state.items.findIndex((entry) => entry.id === id);
  if (index < 0) return;
  const reason = actionReason(state, action, index);
  if (reason) { state.say(reason); return; }
  const generation = state.generation;
  const epoch = captureAuthenticatedRequestEpoch();
  const station = currentStation();
  let change = null, result = null, failure = null;
  // No Undo: the queue APIs cannot restore consumed occurrences or their
  // programme/voice bindings. A path append is not a safe restoration.
  const confirmed = await deleteCollectionItem(button.ownerDocument, {
    title: "Remove queued track?", message: `Remove ${entryName(state.items[index])} from the queue?`,
    confirmLabel: "Remove", pendingText: "Removing…",
    onConfirm: async () => {
      if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return null;
      if (generation !== state.generation) { state.say("The queue changed. Choose the track again."); return button; }
      change = beginChange(state, action, id);
      if (!change) return button;
      try { result = await change.request(); } catch (error) { failure = error; }
      if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return null;
      return change.focusTarget();
    },
  });
  // A voice conflict needs the same shared confirmation dialog; the initial
  // removal confirmation must have closed before asking the voice question.
  if (confirmed && change) await change.settle(async () => {
    if (voiceTrackConflict(failure)) {
      if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return null;
      return sendWithVoiceTrackConflict(button.ownerDocument, change.request, failure);
    }
    if (failure) throw failure;
    return { value: result };
  });
}

export function installQueueButtons(els) {
  const state = controller(els);
  if (state) state.installed = true;
}

export function installQueueClear(els) {
  const state = controller(els);
  const { queueClear } = els;
  if (!state || !queueClear) return;
  const doc = queueClear.ownerDocument;
  queueClear.addEventListener("click", async () => {
    if (state.disabledReason || state.busy) { state.say(state.disabledReason || "Still sending the last change…"); return; }
    const count = state.items.length;
    if (!count) { state.say("The queue is already empty."); return; }
    const plural = count === 1 ? "track" : "tracks";
    const epoch = captureAuthenticatedRequestEpoch();
    const station = currentStation();
    let result = null, conflict = null;
    const clear = (voice = {}) => {
      if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return Promise.resolve(null);
      if (state.disabledReason) return Promise.reject(new Error(state.disabledReason));
      return requestJson("/api/queue/reorder", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: [], ...voice }) });
    };
    const done = () => {
      resetQueueState(els);
      result = `Cleared the queue.  Removed ${count} ${plural}.`;
    };
    const confirmed = await confirmAction(doc, {
      title: "Clear the queue?", message: `This removes all ${count} queued ${plural}.  AutoDJ keeps picking tracks on its own.`,
      confirmLabel: "Clear queue",
      onConfirm: async () => {
        if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return null;
        try {
          await clear();
          if (isAuthenticatedRequestCurrent(epoch)) done();
        } catch (error) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          if (voiceTrackConflict(error)) conflict = error;
          else result = failureText("clear the queue", error);
        }
        return queueClear;
      },
    });
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    queueClear.focus();
    if (confirmed && conflict) {
      try {
        const sent = await sendWithVoiceTrackConflict(doc, clear, conflict);
        if (!isAuthenticatedRequestCurrent(epoch) || currentStation() !== station) return;
        if (sent) done(); else result = "Queue clear canceled.";
      } catch (error) { result = failureText("clear the queue", error); }
      queueClear.focus();
    }
    if (confirmed && result && isAuthenticatedRequestCurrent(epoch)) state.say(result, result.startsWith("Could not") ? "error" : "info");
  });
}
