import { afterEach, describe, expect, it, vi } from "vitest";

import { createEditorDialog, showEditorError } from "../../src/autodj/static/modules/editor-dialog.js";
import { installIndexMarkup } from "./helpers.js";

const byId = (id) => document.getElementById(id);

function modal(dialog) {
  dialog.show = vi.fn(() => { dialog.open = true; });
  dialog.showModal = vi.fn(() => { dialog.open = true; });
  dialog.close = vi.fn(() => { dialog.open = false; dialog.dispatchEvent(new Event("close")); });
}

function confirmMarkup() {
  document.body.insertAdjacentHTML("beforeend", `
    <dialog id="confirm-dialog" aria-labelledby="confirm-title">
      <h2 id="confirm-title"></h2><p id="confirm-message"></p>
      <button id="confirm-accept" type="button"></button><button id="confirm-cancel" type="button">Keep editing</button>
    </dialog>`);
  modal(byId("confirm-dialog"));
}

function makeEditor(id, { fallback, parent = document.body, save = vi.fn(), onCancel = vi.fn() } = {}) {
  const form = document.createElement("form");
  form.id = id;
  form.innerHTML = `<h2 id="${id}-heading">${id}</h2><label>Value <input id="${id}-value" value="original"></label>
    <button id="${id}-save" type="submit">Save</button><button id="${id}-cancel" type="button">Cancel</button>`;
  parent.append(form);
  const editor = createEditorDialog({
    form,
    heading: form.querySelector("h2"),
    cancel: byId(`${id}-cancel`),
    fallback: fallback || document.querySelector("#fallback"),
    save,
    onCancel,
  });
  modal(editor.dialog);
  return { ...editor, form, value: byId(`${id}-value`), submit: byId(`${id}-save`), cancel: byId(`${id}-cancel`), onCancel };
}

afterEach(() => document.body.replaceChildren());

describe("the dialog's name", () => {
  // ADR-0021-21
  it("describes editors with short help instead of their form contents", () => {
    const editor = makeEditor("edit");
    editor.open(editor.value);
    const description = byId(editor.dialog.getAttribute("aria-describedby"));
    expect(description?.textContent).toBe("Use Tab to move between fields and actions.");
    expect(description?.contains(editor.form)).toBe(false);
    expect(description?.querySelector("input, select, textarea, button")).toBeNull();
    expect(editor.value.labels[0].textContent.trim()).toBe("Value");
    expect(document.activeElement).toBe(editor.value);
    editor.close();
  });

  it("preserves the clockwheel step editor's explicit short description", () => {
    installIndexMarkup();
    const dialog = byId("clock-step-dialog");
    const editor = createEditorDialog({ dialog, form: byId("clock-step-form"), heading: byId("clock-step-legend"),
      cancel: byId("clock-cancel-edit"), save: vi.fn() });
    expect(editor.dialog.getAttribute("aria-describedby")).toBe("clock-step-desc");
    expect(byId("clock-step-desc").textContent).toBe("Choose what the step plays, how it chooses and when it plays.");
  });

  it("is not given again to a group around the form, as Add category's legend was", () => {
    // NVDA said "Add category dialog", then "Add category grouping".
    const form = document.createElement("form");
    form.id = "cat";
    form.innerHTML = `<fieldset><legend><h1 id="cat-legend">Add category</h1></legend>
      <label for="cat-name">Name</label><input id="cat-name">
      <fieldset id="cat-source"><legend>Songs come from</legend><input type="radio" aria-label="Query"></fieldset>
      <button id="cat-cancel" type="button">Cancel</button></fieldset>`;
    document.body.append(form);
    const editor = createEditorDialog({ form, heading: byId("cat-legend"), cancel: byId("cat-cancel"), fallback: null, save: vi.fn() });
    expect(editor.dialog.getAttribute("aria-labelledby")).toBe("cat-legend");
    expect(form.querySelector("fieldset").getAttribute("role")).toBe("none");
    // A group of its own inside the form keeps its role.
    expect(byId("cat-source").hasAttribute("role")).toBe(false);
  });
});

describe("editor dialog controller", () => {
  it("opens from a raw table action directly on the first field without visiting its cell", () => {
    document.body.innerHTML = '<table><tbody><tr><td id="cell" role="gridcell" tabindex="0"><button id="opener">Edit</button></td></tr></tbody></table>';
    const editor = makeEditor("edit");
    byId("opener").focus();
    const events = [];
    const record = (event) => events.push(event.target.id);
    document.addEventListener("focusin", record);
    try {
      editor.open(editor.value);
      expect(events).toEqual(["edit-value"]);
    } finally {
      document.removeEventListener("focusin", record);
      editor.close();
    }
  });

  // ADR-0021-25
  it("lets native opening focus the requested field once without an intermediate field", () => {
    const editor = makeEditor("edit");
    const later = document.createElement("input");
    later.id = "later";
    editor.form.append(later);
    // Model the browser's dialog focusing steps, absent from happy-dom.
    editor.dialog.show = () => {
      editor.dialog.open = true;
      (editor.dialog.querySelector("[autofocus]") || editor.value).focus();
    };
    const focus = vi.spyOn(later, "focus");
    const events = [];
    editor.form.addEventListener("focusin", (event) => events.push(event.target.id));
    editor.open(later);
    expect(events).toEqual(["later"]);
    expect(focus).toHaveBeenCalledOnce();
    expect(later.hasAttribute("autofocus")).toBe(false);
    editor.close();
  });

  it("does not refocus the first field after the browser already focused it", () => {
    const editor = makeEditor("edit");
    editor.value.setAttribute("autofocus", "");
    editor.dialog.show = () => { editor.dialog.open = true; editor.value.focus(); };
    const focus = vi.spyOn(editor.value, "focus");
    editor.open(editor.value);
    expect(focus).toHaveBeenCalledOnce();
    expect(editor.value.hasAttribute("autofocus")).toBe(true);
    editor.close();
  });

  it("restores the owning table cell when opened from its action button", () => {
    document.body.innerHTML = '<table><tbody><tr><td id="cell" role="gridcell" tabindex="0"><button id="opener">Edit</button></td></tr></tbody></table>';
    const editor = makeEditor("edit");
    byId("cell").focus();
    editor.open(editor.value, byId("opener"));
    editor.cancel.click();
    expect(document.activeElement).toBe(byId("cell"));
  });

  it("closes child editors and invalidates their pending work with the parent", async () => {
    const parent = makeEditor("parent");
    parent.open(parent.value);
    let current;
    let finish;
    const child = makeEditor("child", { save: (_event, isCurrent) => {
      current = isCurrent;
      return new Promise((resolve) => { finish = resolve; });
    } });
    child.open(child.value, parent.value);
    child.form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(current()).toBe(true);
    parent.close();
    expect(child.dialog.open).toBe(false);
    expect(parent.dialog.open).toBe(false);
    expect(current()).toBe(false);
    finish();
    await Promise.resolve();
  });

  it("reuses an existing dialog and finishes a synchronous step save immediately", () => {
    document.body.innerHTML = '<dialog id="step"><fieldset id="step-form"><h1 id="title">Step</h1><input id="value"><button id="save" type="button">Add step</button><button id="cancel">Cancel</button></fieldset></dialog>';
    const save = vi.fn();
    const editor = createEditorDialog({ form: byId("step-form"), heading: byId("title"),
      dialog: byId("step"), submit: byId("save"), cancel: byId("cancel"), save });
    modal(editor.dialog);
    editor.open(byId("value"));
    byId("save").click();
    byId("save").click();
    expect(save).toHaveBeenCalledTimes(2);
    expect(editor.region(null)).toBe(editor.status);
    expect(document.querySelectorAll("dialog")).toHaveLength(1);
    expect(byId("save").disabled).toBe(false);
  });
  it("keeps each production editor title as a level-one heading when its text changes", () => {
    installIndexMarkup();
    for (const [formId, titleId, cancelId] of [
      ["categories-form", "categories-form-legend", "categories-cancel"],
      ["intro-rules-form", "intro-rules-form-legend", "intro-rules-cancel"],
      ["schedule-form", "schedule-form-legend", "schedule-cancel"],
      ["ads-spot-form", "ads-spot-legend", "ads-spot-cancel"],
      ["ads-break-form", "ads-break-legend", "ads-break-cancel"],
    ]) {
      const heading = byId(titleId);
      const editor = createEditorDialog({ form: byId(formId), heading, cancel: byId(cancelId), save: vi.fn() });
      heading.textContent = "Edit example";
      expect(heading.tagName, formId).toBe("H1");
      expect(editor.dialog.getAttribute("aria-labelledby"), formId).toBe(titleId);
      expect(editor.dialog.querySelector("h1, h2, h3, h4, h5, h6"), formId).toBe(heading);
    }
  });

  it("moves forms out of their tab panel into a document-level modal", () => {
    const panel = document.createElement("section");
    panel.setAttribute("role", "tabpanel");
    document.body.append(panel);
    const editor = makeEditor("panel-editor", { parent: panel });
    expect(editor.dialog.parentElement).toBe(document.body);
    expect(editor.dialog.closest('[role="tabpanel"]')).toBeNull();
    expect(editor.form.closest('[role="tabpanel"]')).toBeNull();
    expect(editor.dialog.contains(editor.form)).toBe(true);
  });

  it("keeps a checkbox beside its label when showing its field error", () => {
    document.body.innerHTML = '<input id="day" type="checkbox"><label for="day">Monday</label><p id="error" hidden></p><p id="status"></p>';
    showEditorError(byId("day"), "Choose a day", byId("error"), byId("status"));
    expect(byId("day").nextElementSibling.htmlFor).toBe("day");
    expect(byId("error").previousElementSibling.htmlFor).toBe("day");
    expect(byId("day").getAttribute("aria-describedby")).toBe("error");
  });

  // #631: show() and aria-modal, not showModal(), so NVDA keeps its place.
  it("opens a named dialog with show() and aria-modal, the requested initial focus, and returns to its opener", () => {
    document.body.innerHTML = '<main id="main-content" tabindex="-1"><button id="opener">Edit</button><button id="fallback">Add</button></main>';
    const opener = byId("opener");
    opener.focus();
    const editor = makeEditor("edit");
    editor.open(editor.value, opener);
    expect(editor.dialog.show).toHaveBeenCalledOnce();
    expect(editor.dialog.showModal).not.toHaveBeenCalled();
    expect(editor.dialog.getAttribute("aria-modal")).toBe("true");
    expect(editor.dialog.getAttribute("aria-labelledby")).toBe("edit-heading");
    expect(editor.dialog.open).toBe(true);
    expect(document.activeElement).toBe(editor.value);
    editor.cancel.click();
    expect(editor.dialog.open).toBe(false);
    expect(editor.onCancel).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(opener);
  });

  it("returns focus to fallback when the opener is gone", () => {
    document.body.innerHTML = '<main id="main-content" tabindex="-1"><button id="opener">Edit</button><button id="fallback">Add</button></main>';
    const opener = byId("opener");
    opener.focus();
    const editor = makeEditor("edit");
    editor.open(editor.value, opener);
    opener.remove();
    editor.cancel.click();
    expect(document.activeElement).toBe(byId("fallback"));
  });

  it("keeps dirty edits after declining discard, then discards on Escape confirmation", async () => {
    confirmMarkup();
    document.body.innerHTML += '<main id="main-content" tabindex="-1"><button id="opener">Edit</button><button id="fallback">Add</button></main>';
    const opener = byId("opener");
    opener.focus();
    const editor = makeEditor("edit");
    editor.open(editor.value, opener);
    editor.value.value = "changed";
    editor.cancel.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Discard changes?");
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(editor.dialog.open).toBe(true);
    expect(editor.value.value).toBe("changed");

    editor.dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(editor.dialog.open).toBe(false));
    expect(editor.onCancel).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(opener);
  });

  it("blocks duplicate submissions and cancellation while save is pending", async () => {
    let finish;
    const save = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const editor = makeEditor("edit", { save });
    editor.open(editor.value);
    const submit = () => editor.form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    submit();
    submit();
    expect(save).toHaveBeenCalledOnce();
    expect(editor.form.getAttribute("aria-busy")).toBe("true");
    expect(editor.submit.disabled).toBe(true);
    editor.cancel.click();
    expect(editor.dialog.open).toBe(true);
    finish();
    await vi.waitFor(() => expect(editor.form.hasAttribute("aria-busy")).toBe(false));
    expect(editor.submit.disabled).toBe(false);
  });

  it("invalidates a pending save callback when the editor closes", async () => {
    let finish;
    let current;
    const save = vi.fn((_event, isCurrent) => {
      current = isCurrent;
      return new Promise((resolve) => { finish = resolve; });
    });
    const editor = makeEditor("edit", { save });
    editor.open(editor.value);
    editor.form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(current()).toBe(true);
    editor.close();
    expect(current()).toBe(false);
    finish();
    await Promise.resolve();
    expect(editor.dialog.open).toBe(false);
    expect(editor.form.hasAttribute("aria-busy")).toBe(false);
  });

  it("closes the discard confirmation when an external station change closes the editor", async () => {
    confirmMarkup();
    document.body.innerHTML += '<main id="main-content" tabindex="-1"><button id="opener">Edit</button><button id="fallback">Add</button></main>';
    const editor = makeEditor("edit", { onCancel: vi.fn() });
    editor.open(editor.value, byId("opener"));
    editor.value.value = "changed";
    editor.cancel.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    editor.close();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(editor.dialog.open).toBe(false);
    expect(editor.onCancel).not.toHaveBeenCalled();
  });

  // #631: editors open with show(), so the guard keeps the keys in the top
  // one; Escape closes it with focus straight back on its opener.
  it("keeps Tab in the top editor and closes it on Escape, focus straight back on its opener", () => {
    document.body.innerHTML = '<main id="main-content" tabindex="-1"><button id="opener">Edit parent</button><button id="fallback">Add</button></main>';
    const key = (name, init = {}) => {
      const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...init });
      document.activeElement.dispatchEvent(event);
      return event;
    };
    const opener = byId("opener");
    opener.focus();
    const parent = makeEditor("parent");
    parent.open(parent.value, opener);
    const nestedOpener = document.createElement("button");
    nestedOpener.textContent = "Open nested editor";
    parent.dialog.append(nestedOpener);
    nestedOpener.focus();
    const child = makeEditor("child");
    child.open(child.value, nestedOpener);
    expect(child.dialog.getAttribute("aria-modal")).toBe("true");
    child.cancel.focus();
    expect(key("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(child.value);
    // The parent under it is page to the child.
    parent.value.focus();
    expect(document.activeElement).toBe(child.value);
    expect(key("Escape").defaultPrevented).toBe(true);
    expect(child.dialog.open).toBe(false);
    expect(parent.dialog.open).toBe(true);
    expect(document.activeElement).toBe(nestedOpener);
    // The parent guards again.
    parent.value.focus();
    expect(key("Tab", { shiftKey: true }).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(nestedOpener);
    expect(key("Escape").defaultPrevented).toBe(true);
    expect(parent.dialog.open).toBe(false);
    expect(document.activeElement).toBe(opener);
  });

  it("keeps parent fields and focus when a nested editor closes on Escape", () => {
    document.body.innerHTML = '<main id="main-content" tabindex="-1"><button id="opener">Edit parent</button><button id="fallback">Add</button></main>';
    const opener = byId("opener");
    opener.focus();
    const parent = makeEditor("parent");
    parent.open(parent.value, opener);
    parent.value.value = "parent draft";
    const nestedOpener = document.createElement("button");
    nestedOpener.textContent = "Open nested editor";
    parent.dialog.append(nestedOpener);
    nestedOpener.focus();
    const child = makeEditor("child");
    child.open(child.value, nestedOpener);
    expect(parent.region(null)).toBe(child.status);
    child.dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(child.dialog.open).toBe(false);
    expect(parent.dialog.open).toBe(true);
    expect(child.region(null)).toBe(parent.status);
    expect(parent.value.value).toBe("parent draft");
    expect(document.activeElement).toBe(nestedOpener);
    parent.value.value = "original";
    parent.dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(parent.dialog.open).toBe(false);
    expect(document.activeElement).toBe(opener);
  });
});
