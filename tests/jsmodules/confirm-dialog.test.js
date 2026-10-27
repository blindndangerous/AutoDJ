import { afterEach, describe, expect, it } from "vitest";

import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";

// The shared confirmation as a browser runs it: close() puts focus back on
// the opener while the dialog is still closing, when focus is still in the
// dialog (or always, *restoreAlways*, as an older browser may).  That
// return, made while the page was still inert, is what NVDA missed.
function mount({ restoreAlways = false, lateClose = false } = {}) {
  document.body.innerHTML = `
    <button id="opener">Delete Legacy set</button>
    <button id="elsewhere">Restart AutoDJ</button>
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
    // A browser fires close a task later; *lateClose* keeps that gap.
    if (lateClose) setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
    else dialog.dispatchEvent(new Event("close"));
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
  // ADR-0011-03
  it("leaves focus on a control chosen before the late close event", async () => {
    const { dialog, opener } = mount({ lateClose: true });
    opener.focus();
    const answer = ask();
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    dialog.close("");
    // The next control is chosen before the browser's close event comes.
    document.getElementById("elsewhere").focus();
    expect(await answer).toBe(false);
    expect(document.activeElement.id).toBe("elsewhere");
  });

  it("still returns focus after the late close event when nothing else was chosen", async () => {
    const { dialog, opener } = mount({ lateClose: true });
    opener.focus();
    const answer = ask();
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    dialog.close("");
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
  });

  // ADR-0011-03
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

describe("confirmAction steady focus (#385)", () => {
  it("goes to the card's summary when the opener is gone, and the main content when there was none", async () => {
    const { dialog } = mount();
    document.body.insertAdjacentHTML("afterbegin", `
      <main id="main-content" tabindex="-1">
        <details class="card" open><summary><h2>Categories</h2></summary>
          <ul><li><button id="gone">Delete Legacy set</button></li></ul>
        </details>
      </main>`);
    const gone = document.getElementById("gone");
    gone.focus();
    const answer = confirmAction(document, {
      title: "Delete?", message: "Gone.", confirmLabel: "Delete",
      onConfirm: async () => { gone.closest("li").remove(); return null; },
    });
    document.getElementById("confirm-accept").click();
    await answer;
    expect(document.activeElement.tagName).toBe("SUMMARY");

    // Opened with focus on the page itself: the main content, not the body.
    document.activeElement.blur();
    const second = confirmAction(document, { title: "Sure?", message: "Yes.", confirmLabel: "OK" });
    document.getElementById("confirm-cancel").click();
    await second;
    expect(document.activeElement).toBe(document.getElementById("main-content"));
    expect(dialog.hasAttribute("open")).toBe(false);
  });

  it("goes to the card's heading when the opener was turned off meanwhile", async () => {
    mount();
    document.body.insertAdjacentHTML("afterbegin", `
      <section data-view="queue"><h2>Queue</h2><button id="turned-off">Clear queue</button></section>`);
    const button = document.getElementById("turned-off");
    button.focus();
    const answer = confirmAction(document, {
      title: "Clear?", message: "All of it.", confirmLabel: "Clear",
      onConfirm: async () => { button.disabled = true; return null; },
    });
    document.getElementById("confirm-accept").click();
    await answer;
    const heading = document.querySelector("section h2");
    expect(document.activeElement).toBe(heading);
    expect(heading.getAttribute("tabindex")).toBe("-1");
  });
});
