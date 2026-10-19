import { afterEach, describe, expect, it, vi } from "vitest";

import { createEditorDialog, showEditorError } from "../../src/autodj/static/modules/editor-dialog.js";
import { installIndexMarkup } from "./helpers.js";

const byId = (id) => document.getElementById(id);

function modal(dialog) {
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

describe("editor dialog controller", () => {
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

  it("opens a named native modal with the requested initial focus and returns to its opener", () => {
    document.body.innerHTML = '<main id="main-content" tabindex="-1"><button id="opener">Edit</button><button id="fallback">Add</button></main>';
    const opener = byId("opener");
    opener.focus();
    const editor = makeEditor("edit");
    editor.open(editor.value, opener);
    expect(editor.dialog.showModal).toHaveBeenCalledOnce();
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
