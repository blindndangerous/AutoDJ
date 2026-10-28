import { afterEach, describe, expect, it } from "vitest";

import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";

// The shared confirmation as a browser runs it.  close() drops the open
// attribute, then puts focus back on the opener: always for a modal dialog,
// as current browsers do, or only while focus is still in the dialog
// (*restoreInside*), as an older browser may.  An inert, disabled or
// removed opener cannot take it.  The close event comes a task later.
function mount({ restoreInside = false } = {}) {
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
    dialog.removeAttribute("open");
    const focusable = before?.isConnected && !before.disabled && !before.closest("[inert]");
    if ((inside || !restoreInside) && focusable) before.focus();
    setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
  };
  const opener = document.getElementById("opener");
  // Each time the opener takes focus: whether the dialog was still open.
  const focusedWhileOpen = [];
  opener.addEventListener("focus", () => focusedWhileOpen.push(dialog.hasAttribute("open")));
  return { dialog, opener, focusedWhileOpen };
}

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

const ask = () => confirmAction(document, {
  title: "Delete category Legacy set?", message: "It goes.", confirmLabel: "Delete",
});

afterEach(() => {
  for (const stop of stopTrails.splice(0)) stop();
  document.body.replaceChildren();
});

describe("confirmAction focus (#385)", () => {
  // ADR-0011-03
  it("puts focus back on the opener only once the dialog has closed, after Cancel", async () => {
    const { opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    expect(document.activeElement.id).toBe("confirm-cancel");
    const trail = focusTrail();
    document.getElementById("confirm-cancel").click();
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(focusedWhileOpen).toEqual([false]);
    // Straight from Cancel to the opener, never through the page body (#602).
    expect(trail).toEqual(["out:confirm-cancel>opener", "in:opener"]);
  });

  // ADR-0011-03
  it("does the same for Escape, and holds Escape while the action runs", async () => {
    const { dialog, opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    const trail = focusTrail();
    // Escape: the browser's cancel event, then its close.
    const escape = new Event("cancel", { cancelable: true });
    dialog.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(false);
    dialog.close("");
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(focusedWhileOpen).toEqual([false]);
    expect(trail).toEqual(["out:confirm-cancel>opener", "in:opener"]);

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

  it("leaves focus the browser put back on the opener there, without letting go of it", async () => {
    for (const restoreInside of [false, true]) {
      const { opener, focusedWhileOpen } = mount({ restoreInside });
      opener.focus();
      focusedWhileOpen.length = 0;
      const answer = ask();
      const trail = focusTrail();
      document.getElementById("confirm-cancel").click();
      await answer;
      // The close event, a task later, finds focus on the opener already;
      // letting go of it to take it again put it on the page body (#602).
      expect(focusedWhileOpen).toEqual([false]);
      expect(trail).toEqual(["out:confirm-cancel>opener", "in:opener"]);
      expect(document.activeElement).toBe(opener);
    }
  });

  it("puts focus back on the opener when the close event finds it still in the dialog", async () => {
    const { dialog, opener } = mount();
    opener.focus();
    const answer = ask();
    // A browser that closed the dialog without putting focus back.
    dialog.close = (value) => {
      dialog.returnValue = value ?? "";
      dialog.removeAttribute("open");
      setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
    };
    const trail = focusTrail();
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    dialog.close("");
    expect(document.activeElement.id).toBe("confirm-cancel");
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(trail).toEqual(["out:confirm-cancel>opener", "in:opener"]);
  });

  it("returns focus straight to the opener when the action refuses", async () => {
    const { opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = confirmAction(document, {
      title: "Restart?", message: "Off the air.", confirmLabel: "Restart",
      onConfirm: async () => { throw new Error("refused"); },
    });
    const confirmButton = document.getElementById("confirm-accept");
    confirmButton.focus();
    const trail = focusTrail();
    confirmButton.click();
    expect(await answer).toBe(true);
    expect(document.activeElement).toBe(opener);
    expect(focusedWhileOpen).toEqual([false]);
    expect(trail).toEqual(["out:confirm-accept>opener", "in:opener"]);
  });

  // ADR-0011-05
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
    const confirmButton = document.getElementById("confirm-accept");
    confirmButton.focus();
    const trail = focusTrail();
    confirmButton.click();
    expect(await answer).toBe(true);
    expect(document.activeElement).toBe(next);
    // The browser's return to the opener is held off, so focus goes
    // straight on: the opener is not read first (D14), nor the page body.
    expect(focusedWhileOpen).toEqual([]);
    expect(opener.hasAttribute("inert")).toBe(false);
    expect(trail).toEqual(["out:confirm-accept>BUTTON", "in:BUTTON"]);
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
