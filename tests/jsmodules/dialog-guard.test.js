import { afterEach, describe, expect, it } from "vitest";

import { closeDialog, openDialog } from "../../src/autodj/static/modules/dialog-guard.js";

// A dialog as a browser runs it.  show() and showModal() note the focused
// control; close() drops the open attribute, then puts focus back on it
// when focus was in the dialog.  An inert, disabled or removed control
// cannot take it.  The close event comes a task later.
function browserDialog(dialog) {
  let before = null;
  const open = () => {
    before = document.activeElement;
    dialog.setAttribute("open", "");
  };
  dialog.show = open;
  dialog.showModal = open;
  dialog.close = (value) => {
    if (!dialog.hasAttribute("open")) return;
    if (value !== undefined) dialog.returnValue = value;
    const inside = dialog.contains(document.activeElement);
    dialog.removeAttribute("open");
    const focusable = before?.isConnected && !before.disabled && !before.closest("[inert]");
    if (inside && focusable) before.focus();
    setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
  };
}

function mount() {
  document.body.innerHTML = `
    <main id="main-content" tabindex="-1">
      <button id="opener">Edit</button>
      <button id="elsewhere">Saved row</button>
      <a id="page-link" href="#top">Top</a>
    </main>
    <dialog id="editor" aria-labelledby="editor-title">
      <h1 id="editor-title">Edit</h1>
      <input id="name">
      <button id="off" disabled>Off</button>
      <p hidden><button id="hidden-button">Hidden</button></p>
      <details><summary id="more">More</summary><button id="inside-details">Inside</button></details>
      <button id="browse">Browse…</button>
      <button id="save">Save</button>
      <button id="cancel">Cancel</button>
    </dialog>
    <dialog id="picker" aria-labelledby="picker-title">
      <h1 id="picker-title">Choose</h1>
      <button id="pick">Select this folder</button>
      <button id="picker-cancel">Cancel</button>
    </dialog>`;
  const byId = (id) => document.getElementById(id);
  browserDialog(byId("editor"));
  browserDialog(byId("picker"));
  return { byId, editor: byId("editor"), picker: byId("picker") };
}

const press = (key, init = {}) => {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  (document.activeElement || document.body).dispatchEvent(event);
  return event;
};

const stopTrails = [];

// Every focus change from now on: "out:<from>><to>" as focus leaves one
// control for another ("body" when it goes nowhere), and "in:<to>".
function focusTrail() {
  const name = (element) => (!element || element === document.body ? "body" : element.id || element.tagName);
  const trail = [];
  const out = (event) => trail.push(`out:${name(event.target)}>${name(event.relatedTarget)}`);
  const into = (event) => trail.push(`in:${name(event.target)}`);
  document.addEventListener("focusout", out, true);
  document.addEventListener("focusin", into, true);
  stopTrails.push(() => {
    document.removeEventListener("focusout", out, true);
    document.removeEventListener("focusin", into, true);
  });
  return trail;
}

afterEach(() => {
  for (const stop of stopTrails.splice(0)) stop();
  for (const dialog of document.querySelectorAll("dialog[open]")) closeDialog(dialog);
  document.body.replaceChildren();
});

// #631: every dialog the page opens itself acts modal without showModal(),
// as the shared confirmation does since #602 and #627.
describe("openDialog and closeDialog (#631)", () => {
  it("preserves focus already handed outside when closing without an explicit opener", () => {
    const { byId, editor } = mount();
    byId("opener").focus();
    const guard = openDialog(editor, { home: byId("name") });
    byId("name").focus();
    guard.release();
    byId("elsewhere").focus();
    closeDialog(editor);
    expect(document.activeElement).toBe(byId("elsewhere"));
  });

  it("rejects a disabled replacement button before mapping it to a gridcell", () => {
    const { byId, editor } = mount();
    document.querySelector("main").insertAdjacentHTML("beforeend", '<table><tbody><tr><td id="disabled-cell" role="gridcell" tabindex="-1"></td></tr></tbody></table>');
    const replacement = byId("elsewhere");
    byId("disabled-cell").append(replacement);
    replacement.disabled = true;
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("name").focus();
    const trail = focusTrail();
    closeDialog(editor, { opener: byId("opener"), target: replacement });
    expect(document.activeElement).toBe(byId("opener"));
    expect(trail).toEqual(["out:name>opener", "in:opener"]);
  });

  it.each(["cancel", "same", "replacement"])("normalizes native grid restoration before %s closes the dialog", (destination) => {
    const { byId, editor } = mount();
    document.querySelector("main").insertAdjacentHTML("beforeend", '<table><tbody><tr><td id="first-cell" role="gridcell" tabindex="0"></td><td id="second-cell" role="gridcell" tabindex="-1"></td></tr></tbody></table>');
    byId("first-cell").append(byId("opener"));
    byId("second-cell").append(byId("elsewhere"));
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("name").focus();
    const trail = focusTrail();
    const target = destination === "cancel" ? null : byId(destination === "same" ? "opener" : "elsewhere");
    closeDialog(editor, { opener: byId("opener"), target });
    const expected = destination === "replacement" ? "second-cell" : "first-cell";
    expect(document.activeElement).toBe(byId(expected));
    expect(trail).toEqual([`out:name>${expected}`, `in:${expected}`]);
  });

  // ADR-0011-08
  it("opens with show() and aria-modal, with the backdrop shown until it closes", () => {
    const { byId, editor } = mount();
    const shown = [];
    editor.show = ((show) => () => { shown.push("show"); show(); })(editor.show);
    editor.showModal = () => shown.push("showModal");
    const { modal } = openDialog(editor, { home: byId("name") });
    expect(modal).toBe(false);
    expect(shown).toEqual(["show"]);
    expect(editor.getAttribute("aria-modal")).toBe("true");
    const backdrop = byId("dialog-backdrop");
    expect(backdrop.hidden).toBe(false);
    closeDialog(editor);
    expect(editor.hasAttribute("open")).toBe(false);
    expect(backdrop.hidden).toBe(true);
  });

  // ADR-0011-08
  it("keeps Tab, Shift+Tab and focus in the dialog, past controls Tab cannot reach", () => {
    const { byId, editor } = mount();
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("cancel").focus();
    expect(press("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(byId("name"));
    expect(press("Tab", { shiftKey: true }).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(byId("cancel"));
    // Inside the dialog Tab is the browser's own.
    byId("more").focus();
    expect(press("Tab").defaultPrevented).toBe(false);
    // From the page body, where a click on the dialog's text leaves focus.
    byId("more").blur();
    press("Tab", { shiftKey: true });
    expect(document.activeElement).toBe(byId("cancel"));
    // Focus sent to the page comes back to the home control.
    byId("elsewhere").focus();
    expect(document.activeElement).toBe(byId("name"));
    const backdrop = byId("dialog-backdrop");
    for (const type of ["mousedown", "click"]) {
      const event = new Event(type, { bubbles: true, cancelable: true });
      backdrop.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    }
    expect(editor.hasAttribute("open")).toBe(true);
    closeDialog(editor, { opener: byId("opener") });
    // Closed, the page has Tab again.
    expect(press("Tab").defaultPrevented).toBe(false);
  });

  // ADR-0011-08
  it("stops a page control activated while it is open, as a screen reader may", () => {
    const { byId, editor } = mount();
    openDialog(editor, { home: byId("cancel") });
    const pressed = [];
    for (const id of ["elsewhere", "page-link"]) byId(id).addEventListener("click", () => pressed.push(id));
    byId("elsewhere").click();
    byId("page-link").click();
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    byId("elsewhere").dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
    expect(pressed).toEqual([]);
    expect(document.activeElement).toBe(byId("cancel"));
    // The dialog's own controls still work.
    let saved = false;
    byId("save").addEventListener("click", () => { saved = true; });
    byId("save").click();
    expect(saved).toBe(true);
  });

  // ADR-0011-08
  it("fires cancel on Escape and closes with focus straight back on the opener", () => {
    const { byId, editor } = mount();
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("name").focus();
    const trail = focusTrail();
    let cancels = 0;
    editor.addEventListener("cancel", () => { cancels += 1; });
    expect(press("Escape").defaultPrevented).toBe(true);
    expect(cancels).toBe(1);
    expect(editor.hasAttribute("open")).toBe(false);
    expect(document.activeElement).toBe(byId("opener"));
    expect(trail).toEqual(["out:name>opener", "in:opener"]);
  });

  it("leaves Escape to a cancel listener that holds the dialog open", () => {
    const { byId, editor } = mount();
    openDialog(editor, { home: byId("name") });
    editor.addEventListener("cancel", (event) => event.preventDefault());
    expect(press("Escape").defaultPrevented).toBe(true);
    expect(editor.hasAttribute("open")).toBe(true);
  });

  it("calls onEscape instead of firing cancel when it is given", () => {
    const { byId, editor } = mount();
    let escapes = 0;
    let cancels = 0;
    openDialog(editor, { home: byId("name"), onEscape: () => { escapes += 1; } });
    editor.addEventListener("cancel", () => { cancels += 1; });
    press("Escape");
    expect([escapes, cancels]).toEqual([1, 0]);
    expect(editor.hasAttribute("open")).toBe(true);
  });

  // ADR-0011-08
  it("moves focus straight to a target, the opener inert while the dialog closes", () => {
    const { byId, editor } = mount();
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("save").focus();
    const trail = focusTrail();
    closeDialog(editor, { opener: byId("opener"), target: byId("elsewhere") });
    expect(document.activeElement).toBe(byId("elsewhere"));
    expect(byId("opener").hasAttribute("inert")).toBe(false);
    expect(trail.filter((step) => step.startsWith("in:"))).toEqual(["in:elsewhere"]);
  });

  it("goes somewhere steady, never the page body, when the opener is gone", () => {
    const { byId, editor } = mount();
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("save").focus();
    byId("opener").remove();
    closeDialog(editor, { opener: byId("opener") });
    expect(document.activeElement).toBe(byId("main-content"));
  });

  // ADR-0011-08
  it("stacks a dialog opened from another: only the top one guards", () => {
    const { byId, editor, picker } = mount();
    byId("opener").focus();
    openDialog(editor, { home: byId("name") });
    byId("browse").focus();
    openDialog(picker, { home: byId("picker-cancel") });
    byId("picker-cancel").focus();
    const backdrop = byId("dialog-backdrop");
    expect(Number(picker.style.zIndex)).toBeGreaterThan(Number(backdrop.style.zIndex));
    expect(Number(backdrop.style.zIndex)).toBeGreaterThan(Number(editor.style.zIndex));
    // Tab goes round the picker; the editor under it is page to it.
    expect(press("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(byId("pick"));
    byId("save").focus();
    expect(document.activeElement).toBe(byId("picker-cancel"));
    // Escape closes the picker only, back onto Browse… in the editor.
    press("Escape");
    expect(picker.hasAttribute("open")).toBe(false);
    expect(editor.hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(byId("browse"));
    expect(backdrop.hidden).toBe(false);
    // The editor guards again.
    byId("cancel").focus();
    expect(press("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(byId("name"));
  });

  it("opens with showModal() over a modal dialog, whose page is inert", () => {
    const { byId, editor } = mount();
    const modalDialog = document.createElement("dialog");
    modalDialog.setAttribute("open", "");
    const matches = modalDialog.matches.bind(modalDialog);
    modalDialog.matches = (selector) => selector === ":modal" || matches(selector);
    document.body.append(modalDialog);
    const shown = [];
    editor.showModal = () => { shown.push("showModal"); editor.setAttribute("open", ""); };
    editor.show = () => shown.push("show");
    const { modal } = openDialog(editor, { home: byId("name") });
    expect(modal).toBe(true);
    expect(shown).toEqual(["showModal"]);
    expect(byId("dialog-backdrop")).toBeNull();
    // No guard: the modal dialog's page is inert already.
    expect(press("Tab").defaultPrevented).toBe(false);
  });
});
