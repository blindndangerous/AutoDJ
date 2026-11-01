// Browse buttons for fields that hold a path on the machine running AutoDJ.
//
// A native file input cannot help: it uploads a copy of a file from the
// browser's own computer and never reveals a server path, and AutoDJ often
// runs in a container or on another machine.  The dialog asks the server
// what a folder holds instead (autodj/path_browse.py):
//
//   - serverFolderSource: any folder on the server, folders only, for a new
//     library folder;
//   - libraryFolderSource: folders, and optionally audio files, inside one
//     registered library folder, by relative path.
//
// One <dialog> serves every button, opened with show() and aria-modal, not
// showModal(), so NVDA keeps its place on the page (#631, see
// dialog-guard.js); it stacks over an editor it is opened from.  Cancel is
// its last button, so Enter on the last button NVDA read cannot choose the
// folder.  The folder list is one Tab stop; the arrow keys move through it
// (action-list-navigation.js).  Enter on a folder opens it, and focus lands
// on its first entry; Up one folder goes back and lands on the folder just
// left.  Select this folder, or Enter on a file, fills the field, which
// hears input and change like typing.  Cancel and Escape change nothing.
// Either way focus goes back to the Browse button.  What happens inside is
// said in the dialog's own status line, where a screen reader reading the
// dialog finds it.
import { requestJson, withDisabled } from "./api-client.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { closeDialog, openDialog } from "./dialog-guard.js";
import { clearFieldError, clearFormErrors, dialogStatusLine, showFieldError } from "./dom-helpers.js";
import { clearAnnouncedStatus, makeSay } from "./live-region.js";

export function serverFolderSource({ request = requestJson } = {}) {
  return {
    mode: "folder",
    list: (path) => request(`/api/library/browse?path=${encodeURIComponent(path)}`),
  };
}

// *sourceId* is read when the dialog opens: the library folder chosen then.
export function libraryFolderSource(sourceId, { files = false, request = requestJson } = {}) {
  return {
    mode: files ? "file" : "folder",
    relative: true,
    list: (path) => {
      const id = sourceId();
      if (!id) return Promise.reject(new Error("Choose a library folder first."));
      return request(`/api/library/folders/${encodeURIComponent(id)}/browse?path=${encodeURIComponent(path)}${files ? "&files=true" : ""}`);
    },
  };
}

const pickers = new WeakMap();

function element(doc, tag, props = {}) {
  const node = doc.createElement(tag);
  Object.assign(node, props);
  return node;
}

function buildPicker(doc) {
  const dialog = element(doc, "dialog", { id: "path-picker-dialog", className: "editor-dialog path-picker" });
  dialog.setAttribute("aria-labelledby", "path-picker-title");
  const form = element(doc, "form", { method: "dialog", noValidate: true });
  const title = element(doc, "h1", { id: "path-picker-title" });
  const location = element(doc, "p", { id: "path-picker-location" });
  const up = element(doc, "button", { type: "button", id: "path-picker-up", textContent: "Up one folder" });
  const list = element(doc, "ul", { id: "path-picker-list", className: "path-picker-list" });
  list.setAttribute("aria-labelledby", "path-picker-location");
  const empty = element(doc, "p", { id: "path-picker-empty", className: "no-results", hidden: true });
  const error = element(doc, "p", { id: "path-picker-error", className: "field-error", hidden: true });
  const cancel = element(doc, "button", { type: "button", id: "path-picker-cancel", textContent: "Cancel" });
  const select = element(doc, "button", { type: "button", id: "path-picker-select", textContent: "Select this folder" });
  const actions = element(doc, "div", { className: "dialog-actions" });
  actions.append(select, cancel);
  form.append(title, location, up, list, empty, error, actions);
  dialog.append(form);
  doc.body.append(dialog);
  const status = dialogStatusLine(dialog, "path-picker-status");
  const navigation = installActionListNavigation(list);

  let state = null;
  let loadController = null;
  const say = makeSay(status, {});
  const where = (path) => path || (state.relative ? "Library folder" : "Drives");
  const setDisabled = (button, value) => {
    if (value) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");
  };

  function showError(message) {
    if (message) showFieldError(null, message, error, status);
    else clearFormErrors(form, error);
  }

  function entry(item, kind) {
    const li = doc.createElement("li");
    const button = element(doc, "button", { type: "button" });
    button.dataset.path = item.path;
    button.dataset.kind = kind;
    button.append(item.name);
    if (kind === "folder") {
      const suffix = element(doc, "span", { className: "path-picker-kind", textContent: ", folder" });
      button.append(suffix);
    }
    li.append(button);
    return li;
  }

  function render(listing, focusPath) {
    state.listing = listing;
    location.textContent = `Folder: ${where(listing.path)}`;
    const rows = [
      ...(listing.folders || []).map((item) => entry(item, "folder")),
      ...(state.mode === "file" ? (listing.files || []).map((item) => entry(item, "file")) : []),
    ];
    list.replaceChildren(...rows);
    navigation.refresh();
    const folders = listing.folders?.length || 0;
    const files = state.mode === "file" ? listing.files?.length || 0 : 0;
    empty.textContent = state.mode === "file" ? "No folders or audio files here." : "No folders here.";
    empty.hidden = rows.length > 0;
    setDisabled(up, listing.parent == null);
    // At the list of drives there is no folder to select yet.
    setDisabled(select, !state.relative && !listing.path);
    const counts = [`${folders} folder${folders === 1 ? "" : "s"}`];
    if (state.mode === "file") counts.push(`${files} audio file${files === 1 ? "" : "s"}`);
    const more = listing.truncated ? " Only the first entries are listed; type the rest of the path." : "";
    const buttons = [...list.querySelectorAll("button")];
    const target = buttons.find((button) => focusPath != null && button.dataset.path === focusPath)
      || buttons[0] || up;
    target.focus();
    say(`${where(listing.path)}: ${counts.join(", ")}.${more}`);
  }

  async function open(path, focusPath = null, fallback = false) {
    const mine = ++state.loads;
    loadController?.abort();
    const busy = new AbortController();
    loadController = busy;
    showError("");
    try {
      const listing = await withDisabled(form, () => state.source.list(path), { signal: busy.signal });
      if (!dialog.open || mine !== state.loads) return;
      render(listing, focusPath);
    } catch (problem) {
      if (!dialog.open || mine !== state.loads) return;
      // A typed path that is not a folder starts the dialog at the top.
      if (fallback && path) { await open("", null, false); return; }
      const message = problem?.message || "That folder could not be listed.";
      showError(message);
    }
  }

  function close(value) {
    const done = state;
    loadController?.abort();
    state.loads += 1;
    // Focus goes straight back to the Browse button, or to the field when
    // the button is gone.
    closeDialog(dialog, { opener: done.opener?.isConnected ? done.opener : done.input });
    clearAnnouncedStatus(status);
    list.replaceChildren();
    state = null;
    if (value != null) {
      done.input.value = value;
      clearFieldError(done.input, null);
      done.input.dispatchEvent(new Event("input", { bubbles: true }));
      done.input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-path]");
    if (!button || !state) return;
    if (button.dataset.kind === "folder") void open(button.dataset.path);
    else close(button.dataset.path);
  });
  up.addEventListener("click", () => {
    if (!state?.listing) return;
    if (up.getAttribute("aria-disabled") === "true") { say("This is the top folder."); return; }
    void open(state.listing.parent, state.listing.path);
  });
  select.addEventListener("click", () => {
    if (!state?.listing) return;
    if (select.getAttribute("aria-disabled") === "true") { say("Open a drive first, then select a folder."); return; }
    close(state.listing.path);
  });
  cancel.addEventListener("click", () => { if (state) close(null); });
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); if (state) close(null); });

  return {
    dialog,
    show({ input, opener, source, label }) {
      if (state) close(null);
      state = { input, opener, source, mode: source.mode, relative: Boolean(source.relative), loads: 0, listing: null };
      title.textContent = `Choose ${label}`;
      select.hidden = source.mode !== "folder";
      location.textContent = "";
      list.replaceChildren();
      empty.hidden = true;
      showError("");
      clearAnnouncedStatus(status);
      openDialog(dialog, { home: cancel });
      cancel.focus();
      let start = input.value.trim();
      // A track's own folder, so a changed track starts beside the old one.
      if (source.mode === "file" && start) start = start.includes("/") ? start.slice(0, start.lastIndexOf("/")) : "";
      void open(start, source.mode === "file" ? input.value.trim() : null, true);
    },
  };
}

function pathPicker(doc = document) {
  // Built again if the page's markup was replaced under it.
  if (!pickers.get(doc)?.dialog.isConnected) pickers.set(doc, buildPicker(doc));
  return pickers.get(doc);
}

// Put a Browse… button after *input*.  *label* names what is chosen
// ("folder path", "track").  Returns the button, so callers can hide it
// with its field.
export function addBrowseButton(input, { source, label }) {
  const doc = input.ownerDocument;
  const button = element(doc, "button", { type: "button", className: "browse-button", textContent: "Browse…" });
  button.setAttribute("aria-label", `Browse for ${label}`);
  input.insertAdjacentElement("afterend", button);
  button.addEventListener("click", () => {
    pathPicker(doc).show({ input, opener: button, source, label });
  });
  return button;
}
