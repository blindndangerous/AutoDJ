// Cart wall: pages of twelve numbered carts that play a clip at once.
//
// Everything is found inside the root element, so the wall works in a
// main tab or in a panel.  The server says which page is showing; every
// open page follows it.
//
// Keys: with focus on the wall (a cart button, Stop carts or another
// button in it), 1 to 9, 0, minus and equals play carts 1 to 12, and
// Page Up and Page Down change the page.  Text fields and dropdowns keep
// their keys, and the Keyboard shortcuts dialog button turns these keys
// off with the other single-key shortcuts (modules/hotkeys.js, which
// reserves them for the wall).
//
// Start, end and stop come from the server over the websocket and are
// spoken once, through #sr-status, so a scheduled cart is heard on any
// tab.  The wall's own status region reports what the user just did.
//
// scheduled-date labels follow the station's day: a page left
// open past midnight relabels the carts in place, without a sound or a
// move of focus, when the day changes.

import { failureText, requestBinary, requestJson, withDisabled, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { createEditorDialog } from "./editor-dialog.js";
import { applyServerFieldError, clearFieldError, showFieldError, focusAfterDialog, srSpeak, stationDay } from "./dom-helpers.js";
import { shortcutsEnabled } from "./hotkeys.js";
import { createSelectionAnnouncer, makeSay } from "./live-region.js";

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="];
const SLOTS = KEYS.length;

function option(value, label) {
  const item = document.createElement("option");
  item.value = String(value);
  item.textContent = label;
  return item;
}

// 7.4 seconds is "0:07".
function clock(seconds) {
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// Whether scheduled cart events may play today (ISO dates compare as text).
function cartSkipped(assigned, today) {
  if (!assigned || !today) return "";
  if (assigned.not_after && today > assigned.not_after) return "scheduled: expired";
  if (assigned.not_before && today < assigned.not_before) return "scheduled: not playable yet";
  return "";
}

function cartLabel(number, assigned, today = "") {
  if (!assigned) return `Cart ${number}, empty`;
  const parts = [`Cart ${number}`, assigned.name];
  if (Number.isFinite(assigned.duration)) parts.push(clock(assigned.duration));
  const skipped = cartSkipped(assigned, today);
  if (skipped) parts.push(skipped);
  return parts.join(", ");
}

function cartCount(slots = []) {
  const count = slots.filter(Boolean).length;
  return count === 0 ? "No carts" : `${count} ${count === 1 ? "cart" : "carts"}`;
}

// `prepare` runs inside the click or key press, before the request: it
// returns why this page cannot play the clip (browser playback only), or
// null.  `playAudio(bytes, mode, duckDb)` plays a fetched clip through the
// page's liner path, ducking by duckDb in duck mode; `stopAudio` stops what
// it started.
export function installCartWall(root = document.getElementById("cart-wall"), {
  prepare = () => null,
  playAudio = async () => false,
  stopAudio = () => {},
  now = () => new Date(),
  interval = setInterval,
} = {}) {
  if (!root) return null;
  const el = (id) => root.querySelector(`#${id}`);
  const pageSelect = el("cart-page");
  const list = el("cart-slots");
  const status = el("cart-status");
  const pageName = el("cart-page-name");
  const slotSelect = el("cart-edit-slot");
  const editButton = el("cart-edit-open");
  const editForm = el("cart-edit-form");
  const editHeading = el("cart-editor-heading");
  const clearButton = el("cart-clear");
  const cancelButton = el("cart-cancel");
  const uploadButton = el("cart-upload");
  const renameButton = el("cart-rename");
  const stopButton = el("cart-stop");
  const renameError = el("cart-page-error");
  const name = el("cart-name");
  const source = el("cart-source");
  const clip = el("cart-clip");
  const mode = el("cart-mode");
  const file = el("cart-upload-file");
  const deleteUpload = el("cart-delete-upload");
  const notBefore = el("cart-not-before");
  const notAfter = el("cart-not-after");
  const editError = el("cart-edit-error");
  let data = null;
  let editingPage = null;
  let editingSlot = null;
  let editingPageName = "";
  let editorGeneration = 0;
  let requestEpoch = 0;
  let playEpoch = 0;
  const held = new Set();

  let editor = null;
  const rawSay = makeSay(() => editor?.region(status) || status, { dwellMs: 5000 });
  const selectionAnnouncer = createSelectionAnnouncer((...args) => {
    if (root.isConnected && !root.closest("[hidden]") && !root.closest("details:not([open])")
      && root.ownerDocument.visibilityState !== "hidden") rawSay(...args);
  });
  const say = (...args) => { selectionAnnouncer.cancel(); rawSay(...args); };
  const page = () => data?.wall.pages[data.wall.active_page - 1];
  const editing = () => Number(slotSelect.value) || 1;
  const editorCurrent = (epoch, mine) => editor.dialog.open && editorGeneration === mine
    && isAuthenticatedRequestCurrent(epoch);

  // The buttons are made once and relabelled, so focus stays on the same
  // cart number when the page changes or another page edits the wall.
  const buttons = Array.from({ length: SLOTS }, (_, i) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.slot = String(i + 1);
    // A cart plays on the station the page controls (#70).
    button.textContent = cartLabel(i + 1, null);
    button.addEventListener("click", () => {
      if (page()?.slots[i]) void play(i + 1);
      else openEditor(i + 1, button);
    });
    const item = document.createElement("li");
    item.append(button);
    return item;
  });
  list.replaceChildren(...buttons);

  function fillClips(selected = clip.value) {
    const items = source.value === "upload"
      ? (data?.uploads || []).map((upload) => option(upload, upload))
      : (data?.liners || []).map((liner) => option(liner.id, liner.name));
    if (!items.length) {
      items.push(option("", source.value === "upload" ? "No uploaded clips yet" : "No voice liners yet"));
    }
    clip.replaceChildren(...items);
    if (selected && Array.from(clip.options).some((item) => item.value === selected)) {
      clip.value = selected;
    }
    deleteUpload.disabled = source.value !== "upload" || !clip.value;
  }

  // Load slot `number` of the page shown into the editor.
  function loadEditor(number = editing()) {
    slotSelect.value = String(number);
    const assigned = page()?.slots[number - 1];
    name.value = assigned?.name || "";
    source.value = assigned?.source || "liner";
    mode.value = assigned?.mode || "duck";
    if (notBefore) notBefore.value = assigned?.not_before || "";
    if (notAfter) notAfter.value = assigned?.not_after || "";
    fillClips(assigned?.clip || "");
    clearButton.disabled = !assigned;
    clearEditError();
  }

  function unmark(field) {
    if (field.getAttribute("aria-invalid") === "true") {
      clearFieldError(field, field === pageName ? renameError : editError);
    }
  }
  function clearEditError() {
    clearFieldError([name, clip, file, notBefore, notAfter].filter(Boolean), editError);
  }

  function clearEditorContext() {
    const pageChanged = editingPage !== null && data?.wall.active_page !== editingPage;
    editorGeneration += 1;
    editingPage = null;
    editingSlot = null;
    editingPageName = "";
    clearEditError();
    return pageChanged ? pageSelect : null;
  }

  function openEditor(number, opener = editButton) {
    if (!data || editor?.dialog.open) return;
    selectionAnnouncer.cancel();
    editorGeneration += 1;
    loadEditor(number);
    editingPage = data.wall.active_page;
    editingSlot = number;
    editingPageName = page()?.name || `page ${editingPage}`;
    editHeading.textContent = `Edit Cart ${number} on ${editingPageName}`;
    editor.open(name, opener);
  }

  editor = createEditorDialog({ form: editForm, heading: editHeading, cancel: cancelButton,
    fallback: editButton, save: saveCart, onCancel: clearEditorContext, describe: false });

  // `resetEditor` is false when another page changed the wall, so a cart
  // or page name being edited here keeps what was typed.
  function render(resetEditor, { preserveDraft = false } = {}) {
    const current = page();
    if (!current) return;
    pageSelect.replaceChildren(...data.wall.pages.map((item, i) => option(i + 1, item.name)));
    pageSelect.value = String(data.wall.active_page);
    current.slots.forEach((assigned, i) => {
      buttons[i].firstChild.textContent = cartLabel(i + 1, assigned, data.today);
    });
    const number = editing();
    slotSelect.replaceChildren(...current.slots.map((assigned, i) => option(i + 1, cartLabel(i + 1, assigned, data.today))));
    slotSelect.value = String(number);
    if (resetEditor) {
      pageName.value = current.name;
      if (!preserveDraft) loadEditor(number);
      else fillClips();
    } else {
      fillClips();
    }
  }

  // The station's day changed while the page was open: update scheduled-date
  // labels on the carts it changes, in place. Only the text
  // changes, so focus stays where it is and nothing is announced.
  function followStationDay() {
    const current = page();
    const day = stationDay(data?.timezone, now());
    if (!current || !day || day === data.today) return;
    data.today = day;
    current.slots.forEach((assigned, i) => {
      const label = cartLabel(i + 1, assigned, day);
      if (buttons[i].firstChild.textContent !== label) buttons[i].firstChild.textContent = label;
      const item = slotSelect.options[i];
      if (item && item.textContent !== label) item.textContent = label;
    });
  }

  async function refresh(resetEditor = false) {
    const epoch = ++requestEpoch;
    const stationEpoch = captureAuthenticatedRequestEpoch();
    try {
      const response = await requestJson("/api/carts");
      if (epoch !== requestEpoch || !isAuthenticatedRequestCurrent(stationEpoch)) return;
      // A new page (from another open page or a profile) reloads the editor.
      const moved = data?.wall.active_page !== response.wall.active_page;
      data = response;
      const preserveDraft = Boolean(moved && editor.dialog.open);
      render(resetEditor || moved, { preserveDraft });
      if (preserveDraft) {
        say(`The active page changed to ${page().name}. This edit is still for ${editingPageName}.`);
      }
    } catch (error) {
      if (epoch === requestEpoch && isAuthenticatedRequestCurrent(stationEpoch)) {
        say(failureText("load the carts.", error), "error");
      }
    }
  }

  async function play(number) {
    selectionAnnouncer.cancel();
    const assigned = page()?.slots[number - 1];
    if (!assigned) {
      say(`Cart ${number} is empty.`);
      return;
    }
    const refusal = prepare();
    if (refusal) {
      say(refusal, "error");
      return;
    }
    const epoch = ++playEpoch;
    try {
      const response = await requestJson(
        `/api/carts/pages/${data.wall.active_page}/slots/${number}/play`, { method: "POST" });
      if (!response.url) return;
      // Browser playback: one cart at a time, as in the server mix.
      stopAudio();
      const bytes = await requestBinary(response.url);
      if (epoch !== playEpoch) return;
      if (!await playAudio(bytes, response.mode, response.duck_db)) say("This browser could not play the cart.", "error");
    } catch (error) {
      say(`Cart ${number} did not play. ${error.message}`, "error");
    }
  }

  async function showPage(number, { announce = true } = {}) {
    if (announce) selectionAnnouncer.cancel();
    if (editor.dialog.open) return;
    if (!data || number === data.wall.active_page) return;
    if (number < 1 || number > data.wall.pages.length) return;
    data.wall.active_page = number;
    render(true);
    if (announce) say(cartCount(page().slots));
    try {
      await requestJson(`/api/carts/active-page/${number}`, { method: "PUT" });
    } catch (error) {
      say(failureText("change the cart page.", error), "error");
      void refresh(true);
    }
  }

  function invalid(field, message) {
    if (field === pageName) showFieldError(field, message, renameError, status);
    else showFieldError(field, message, editError, editor.dialog.open ? editor.status : status);
  }

  async function saveCart(_event, active) {
    selectionAnnouncer.cancel();
    clearEditError();
    if (!name.value.trim()) { invalid(name, "Enter a cart name."); return; }
    if (!clip.value) { invalid(clip, "Choose a clip, or upload one first."); return; }
    const from = notBefore?.value || null;
    const until = notAfter?.value || null;
    if (from && until && until < from) {
      invalid(notAfter, "Do not play after must be on or after the Not before date.");
      return;
    }
    const number = editingSlot ?? editing();
    const pageNumber = editingPage ?? data.wall.active_page;
    const pageLabel = editingPageName || data.wall.pages[pageNumber - 1]?.name || `page ${pageNumber}`;
    let saved;
    try {
      saved = await requestJson(`/api/carts/pages/${pageNumber}/slots/${number}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.value, source: source.value, clip: clip.value, mode: mode.value,
          ...(from ? { not_before: from } : {}), ...(until ? { not_after: until } : {}),
        }),
      });
    } catch (error) {
      if (!active()) return;
      // A refusal that names its field is shown and linked there, as the
      // page's own checks are.
      const message = failureText(`save cart ${number}.`, error);
      if (!applyServerFieldError({ field: error.field, message },
        { name, clip, not_before: notBefore, not_after: notAfter }, editError, editor.status)) say(message, "error");
      return;
    }
    if (!active()) return;
    await refresh(false);
    if (!active()) return;
    const currentPage = data?.wall.active_page;
    const assigned = saved?.slot ?? data?.wall.pages[pageNumber - 1]?.slots[number - 1];
    editor.close();
    clearEditorContext();
    focusAfterDialog(currentPage === pageNumber ? buttons[number - 1].firstElementChild : pageSelect);
    say(currentPage === pageNumber
      ? `Saved. ${cartLabel(number, assigned, data?.today)}.`
      : `Saved Cart ${number} on ${pageLabel}. The wall is showing ${page()?.name}.`);
  }

  async function clearSlot() {
    selectionAnnouncer.cancel();
    const number = editingSlot ?? editing();
    const pageNumber = editingPage ?? data.wall.active_page;
    const pageLabel = editingPageName || data.wall.pages[pageNumber - 1]?.name || `page ${pageNumber}`;
    const stationEpoch = captureAuthenticatedRequestEpoch();
    const mine = editorGeneration;
    const current = () => editorCurrent(stationEpoch, mine);
    let result = null;
    const confirmed = await confirmAction(root.ownerDocument, {
      title: `Clear Cart ${number}?`,
      message: "This removes the clip from the cart. Unsaved changes in the editor will be discarded.",
      confirmLabel: "Clear cart",
      onConfirm: async () => {
        try {
          await requestJson(`/api/carts/pages/${pageNumber}/slots/${number}`, { method: "DELETE" });
          if (!current()) return null;
          result = ["cleared", "info"];
          return clearButton;
        } catch (error) {
          if (!current()) return null;
          result = [failureText(`clear Cart ${number}`, error), "error"];
          return clearButton;
        }
      },
    });
    if (!current()) return;
    if (!confirmed) {
      clearButton.focus();
      return;
    }
    if (result?.[0] !== "cleared") {
      if (result) say(...result);
      return;
    }
    await refresh(false);
    if (!current()) return;
    editor.close();
    clearEditorContext();
    focusAfterDialog(data?.wall.active_page === pageNumber ? buttons[number - 1].firstElementChild : pageSelect);
    say(data?.wall.active_page === pageNumber ? `Cart ${number} cleared.`
      : `Cart ${number} cleared on ${pageLabel}. The wall is showing ${page()?.name}.`);
  }

  async function rename(button) {
    selectionAnnouncer.cancel();
    clearFieldError(pageName, renameError);
    if (!pageName.value.trim()) { invalid(pageName, "Enter a page name."); return; }
    await withDisabled(button, async () => {
      try {
        const body = await requestJson(`/api/carts/pages/${data.wall.active_page}/name`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: pageName.value }),
        });
        await refresh(false);
        say(`Page renamed to ${body.name}.`);
      } catch (error) {
        say(failureText("rename the page.", error), "error");
      }
    });
  }

  async function upload(button) {
    selectionAnnouncer.cancel();
    const chosen = file.files?.[0];
    if (!chosen) { invalid(file, "Choose an audio file first."); return; }
    const stationEpoch = captureAuthenticatedRequestEpoch();
    const mine = editorGeneration;
    const current = () => editorCurrent(stationEpoch, mine);
    const form = new FormData();
    form.append("file", chosen);
    await withDisabled(button, async () => {
      try {
        const body = await requestJson("/api/carts/upload", { method: "POST", body: form });
        if (!current()) return;
        file.value = "";
        clearFieldError(file, editError);
        await refresh(false);
        if (!current()) return;
        source.value = "upload";
        fillClips(body.name);
        say(`Uploaded ${body.name}.`);
      } catch (error) {
        if (!current()) return;
        say(failureText("upload the clip.", error), "error");
      }
    });
  }

  async function removeUpload() {
    selectionAnnouncer.cancel();
    const chosen = clip.value;
    if (source.value !== "upload" || !chosen) return;
    const stationEpoch = captureAuthenticatedRequestEpoch();
    const mine = editorGeneration;
    const current = () => editorCurrent(stationEpoch, mine);
    let result = null;
    const confirmed = await confirmAction(root.ownerDocument, {
      title: `Delete uploaded clip ${chosen}?`,
      message: "The file is removed from the server.",
      confirmLabel: "Delete clip",
      onConfirm: async () => {
        try {
          await requestJson(`/api/carts/uploads/${encodeURIComponent(chosen)}`, { method: "DELETE" });
          if (!current()) return null;
          await refresh(false);
          if (!current()) return null;
          result = [`Deleted ${chosen}.`, "info"];
          return clip;
        } catch (error) {
          if (!current()) return null;
          result = [failureText(`delete ${chosen}.`, error), "error"];
          return deleteUpload;
        }
      },
    });
    if (!confirmed && current()) deleteUpload.focus();
    else if (!current()) return;
    else if (result) say(...result);
  }

  async function stop() {
    selectionAnnouncer.cancel();
    playEpoch += 1;
    stopAudio();
    try {
      const body = await requestJson("/api/carts/stop", { method: "POST" });
      if (!body.stopped) say("No cart is playing.");
    } catch (error) {
      say(failureText("stop the carts.", error), "error");
    }
  }

  root.addEventListener("keydown", (event) => {
    if (!data || event.defaultPrevented) return;
    if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
    if (event.target.closest("input, select, textarea, [contenteditable]")) return;
    if (!shortcutsEnabled()) return;
    const number = KEYS.indexOf(event.key) + 1;
    const step = { PageUp: -1, PageDown: 1 }[event.key];
    if (!number && !step) return;
    event.preventDefault();
    // NVDA can pass a held key on without event.repeat, so a key acts
    // again only after it is released.
    if (event.repeat || held.has(event.key)) return;
    held.add(event.key);
    if (number) void play(number);
    else void showPage(data.wall.active_page + step);
  });
  root.addEventListener("keyup", () => held.clear());
  root.addEventListener("focusout", () => held.clear());
  pageSelect.addEventListener("change", () => {
    if (editor.dialog.open) return;
    const finish = selectionAnnouncer.start();
    const number = Number(pageSelect.value);
    if (!data || number === data.wall.active_page || number < 1 || number > data.wall.pages.length) return;
    finish(cartCount(data.wall.pages[number - 1].slots));
    void showPage(number, { announce: false });
  });
  editButton.addEventListener("click", () => openEditor(editing(), editButton));
  source.addEventListener("change", () => fillClips(""));
  clip.addEventListener("change", () => {
    unmark(clip);
    deleteUpload.disabled = source.value !== "upload" || !clip.value;
  });
  for (const field of [name, pageName, file, notBefore, notAfter].filter(Boolean)) {
    field.addEventListener("input", () => unmark(field));
  }
  clearButton.addEventListener("click", () => void clearSlot());
  renameButton.addEventListener("click", (event) => void rename(event.currentTarget));
  uploadButton.addEventListener("click", (event) => void upload(event.currentTarget));
  deleteUpload.addEventListener("click", () => void removeUpload());
  stopButton.addEventListener("click", () => void stop());
  interval(followStationDay, 60_000);

  return {
    refresh: () => refresh(false),
    clear() {
      selectionAnnouncer.cancel();
      requestEpoch += 1;
      playEpoch += 1;
      editor.close();
      clearEditorContext();
      data = null;
      buttons.forEach((item, i) => { item.firstChild.textContent = cartLabel(i + 1, null); });
      for (const select of [pageSelect, slotSelect, clip]) select.replaceChildren();
      for (const field of [pageName, name]) field.value = "";
      clearFieldError(pageName, renameError);
      status.textContent = "";
      stopAudio();
    },
    event(message) {
      if (message.type === "cart_changed") {
        void refresh(false);
        return;
      }
      if (message.type !== "cart") return;
      if (message.event === "stop") {
        playEpoch += 1;
        stopAudio();
      }
      const word = { start: "playing", end: "ended", stop: "stopped" }[message.event];
      if (word && message.label) srSpeak(`${message.label}, ${word}.`);
    },
  };
}
