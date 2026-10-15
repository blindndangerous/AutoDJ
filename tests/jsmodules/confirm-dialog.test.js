import { afterEach, describe, expect, it } from "vitest";

import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";

// The shared confirmation as a browser runs it: close() puts focus back on
// the opener while the dialog is still closing, when focus is still in the
// dialog (or always, *restoreAlways*, as an older browser may).  That
// return, made while the page was still inert, is what NVDA missed.
function mount({ restoreAlways = false } = {}) {
  document.body.innerHTML = `
    <button id="opener">Delete Legacy set</button>
    <dialog id="confirm-dialog"><form method="dialog">
      <h2 id="confirm-title"></h2><p id="confirm-message"></p>
      <button id="confirm-cancel" value="cancel">Cancel</button>
      <button id="confirm-accept" value="confirm"></button>
    </form></dialog>`;
  const dialog = document.getElementById("confirm-dialog");
  let before = null;
  dialog.showModal = () => {
    before = document.activeElement;
    dialog.setAttribute("open", "");
  };
  dialog.close = (value) => {
    if (!dialog.hasAttribute("open")) return;
    if (value !== undefined) dialog.returnValue = value;
    const inside = dialog.contains(document.activeElement);
    if ((inside || restoreAlways) && before?.isConnected) before.focus();
    dialog.removeAttribute("open");
    dialog.dispatchEvent(new Event("close"));
  };
  const opener = document.getElementById("opener");
  // Each time the opener takes focus: whether the dialog was still open.
  const focusedWhileOpen = [];
  opener.addEventListener("focus", () => focusedWhileOpen.push(dialog.hasAttribute("open")));
  return { dialog, opener, focusedWhileOpen };
}

const ask = () => confirmAction(document, {
  title: "Delete category Legacy set?", message: "It goes.", confirmLabel: "Delete",
});

afterEach(() => document.body.replaceChildren());

describe("confirmAction focus (#385)", () => {
  it("puts focus back on the opener only once the dialog has closed, after Cancel", async () => {
    const { opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    expect(document.activeElement.id).toBe("confirm-cancel");
    document.getElementById("confirm-cancel").click();
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(focusedWhileOpen).toEqual([false]);
  });

  it("does the same for Escape, and holds Escape while the action runs", async () => {
    const { dialog, opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    // Escape: the browser's cancel event, then its close.
    const escape = new Event("cancel", { cancelable: true });
    dialog.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(false);
    dialog.close("");
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(focusedWhileOpen).toEqual([false]);

    let finish;
    const running = confirmAction(document, {
      title: "Remove?", message: "Gone.", confirmLabel: "Remove",
      onConfirm: () => new Promise((resolve) => { finish = resolve; }),
    });
    document.getElementById("confirm-accept").click();
    const held = new Event("cancel", { cancelable: true });
    dialog.dispatchEvent(held);
    expect(held.defaultPrevented).toBe(true);
    finish(null);
    expect(await running).toBe(true);
    expect(dialog.hasAttribute("open")).toBe(false);
  });

  it("still reports the return when a browser put focus back as the dialog closed", async () => {
    const { opener, focusedWhileOpen } = mount({ restoreAlways: true });
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    document.getElementById("confirm-cancel").click();
    await answer;
    // The browser's return, then the page's own once the dialog is closed:
    // a focus change a screen reader reports.
    expect(focusedWhileOpen).toEqual([true, false]);
    expect(document.activeElement).toBe(opener);
  });

  it("moves focus to the action's target, not the opener, after a confirm", async () => {
    const { opener, focusedWhileOpen } = mount();
    const next = document.createElement("button");
    next.textContent = "Delete Next set";
    document.body.prepend(next);
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = confirmAction(document, {
      title: "Delete?", message: "Gone.", confirmLabel: "Delete",
      onConfirm: async () => next,
    });
    document.getElementById("confirm-accept").click();
    expect(await answer).toBe(true);
    expect(document.activeElement).toBe(next);
    expect(focusedWhileOpen).toEqual([]);
  });
});
