import { afterEach, describe, expect, it } from "vitest";

import { chooseAction, confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";

// The shared confirmation as a browser runs it.  show() and showModal()
// note the focused control; close() drops the open attribute, then puts
// focus back on it when focus was in the dialog or the dialog was modal.
// An inert, disabled or removed control cannot take it.  The close event
// comes a task later.  The buttons start in the old order, Cancel first.
function mount() {
  document.body.innerHTML = `
    <button id="opener">Delete Legacy set</button>
    <dialog id="confirm-dialog"><form method="dialog">
      <h2 id="confirm-title"></h2><p id="confirm-message"></p>
      <div class="dialog-actions">
        <button id="confirm-cancel" value="cancel">Cancel</button>
        <button id="confirm-accept" value="confirm"></button>
      </div>
    </form></dialog>`;
  const dialog = document.getElementById("confirm-dialog");
  let before = null;
  let modal = false;
  const opened = [];
  const open = (asModal) => {
    before = document.activeElement;
    modal = asModal;
    opened.push(asModal ? "showModal" : "show");
    dialog.setAttribute("open", "");
  };
  dialog.show = () => open(false);
  dialog.showModal = () => open(true);
  dialog.close = (value) => {
    if (!dialog.hasAttribute("open")) return;
    if (value !== undefined) dialog.returnValue = value;
    const inside = dialog.contains(document.activeElement);
    dialog.removeAttribute("open");
    const focusable = before?.isConnected && !before.disabled && !before.closest("[inert]");
    if ((inside || modal) && focusable) before.focus();
    setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
  };
  const opener = document.getElementById("opener");
  // Each time the opener takes focus: whether the dialog was still open.
  const focusedWhileOpen = [];
  opener.addEventListener("focus", () => focusedWhileOpen.push(dialog.hasAttribute("open")));
  return { dialog, opener, focusedWhileOpen, opened };
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
    expect(press("Escape").defaultPrevented).toBe(true);
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
    press("Escape");
    expect(dialog.hasAttribute("open")).toBe(true);
    finish(null);
    expect(await running).toBe(true);
    expect(dialog.hasAttribute("open")).toBe(false);
  });

  it("leaves focus the browser put back on the opener there, without letting go of it", async () => {
    const { dialog, opener, focusedWhileOpen } = mount();
    opener.focus();
    focusedWhileOpen.length = 0;
    const answer = ask();
    const trail = focusTrail();
    // Closed by the browser, as the form's dialog method does.
    dialog.close("cancel");
    await answer;
    // The close event, a task later, finds focus on the opener already;
    // letting go of it to take it again put it on the page body (#602).
    expect(focusedWhileOpen).toEqual([false]);
    expect(trail).toEqual(["out:confirm-cancel>opener", "in:opener"]);
    expect(document.activeElement).toBe(opener);
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

// #602, #627: opened with show(), so the page keeps its accessibility tree
// and NVDA returns to where it was; Cancel last, so NVDA's reading of the
// dialog leaves its cursor on Cancel.
describe("confirmAction without showModal (#602, #627)", () => {
  // ADR-0011-06
  it("opens with show(), aria-modal, the form out of the tree, Cancel last and the backdrop shown", async () => {
    const { dialog, opened } = mount();
    const answer = ask();
    expect(opened).toEqual(["show"]);
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.querySelector("form").getAttribute("role")).toBe("none");
    expect([...dialog.querySelectorAll("button")].map((button) => button.id))
      .toEqual(["confirm-accept", "confirm-cancel"]);
    expect(document.activeElement.id).toBe("confirm-cancel");
    const backdrop = document.getElementById("confirm-backdrop");
    expect(backdrop.hidden).toBe(false);
    document.getElementById("confirm-cancel").click();
    await answer;
    expect(backdrop.hidden).toBe(true);
  });

  // ADR-0011-06
  it("keeps Tab, Shift+Tab and focus in the dialog, and the backdrop does nothing", async () => {
    const { dialog, opener } = mount();
    opener.focus();
    const answer = ask();
    const cancelButton = document.getElementById("confirm-cancel");
    const acceptButton = document.getElementById("confirm-accept");
    expect(press("Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(acceptButton);
    expect(press("Tab", { shiftKey: true }).defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(cancelButton);
    // From the page body, where a click on the dialog's text leaves focus.
    cancelButton.blur();
    press("Tab");
    expect(document.activeElement).toBe(acceptButton);
    // Focus sent to the page comes back to Cancel.
    opener.focus();
    expect(document.activeElement).toBe(cancelButton);
    const backdrop = document.getElementById("confirm-backdrop");
    for (const type of ["mousedown", "click"]) {
      const event = new Event(type, { bubbles: true, cancelable: true });
      backdrop.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    }
    expect(dialog.hasAttribute("open")).toBe(true);
    expect(document.activeElement).toBe(cancelButton);
    press("Escape");
    expect(await answer).toBe(false);
    // Closed, the page has Tab again.
    expect(press("Tab").defaultPrevented).toBe(false);
  });

  it("opens with showModal() over another modal dialog, whose page is inert", async () => {
    const { dialog, opened } = mount();
    const editor = document.createElement("dialog");
    editor.setAttribute("open", "");
    const matches = editor.matches.bind(editor);
    editor.matches = (selector) => selector === ":modal" || matches(selector);
    document.body.append(editor);
    const answer = ask();
    expect(opened).toEqual(["showModal"]);
    expect(document.getElementById("confirm-backdrop")).toBeNull();
    // Escape is the browser's cancel event here, held while the action runs.
    const escape = new Event("cancel", { cancelable: true });
    dialog.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(false);
    dialog.close("");
    expect(await answer).toBe(false);
  });

  it("leaves the keys to a modal dialog opened over it", async () => {
    const { dialog } = mount();
    const answer = ask();
    const signIn = document.createElement("dialog");
    signIn.innerHTML = "<input id=\"sign-in-code\">";
    signIn.setAttribute("open", "");
    const matches = signIn.matches.bind(signIn);
    signIn.matches = (selector) => selector === ":modal" || matches(selector);
    document.body.append(signIn);
    const code = document.getElementById("sign-in-code");
    code.focus();
    expect(document.activeElement).toBe(code);
    expect(press("Tab").defaultPrevented).toBe(false);
    expect(press("Escape").defaultPrevented).toBe(false);
    expect(dialog.hasAttribute("open")).toBe(true);
    signIn.remove();
    document.getElementById("confirm-cancel").click();
    expect(await answer).toBe(false);
  });

  it("keeps Cancel last in a three-way choice, and after it", async () => {
    const { dialog } = mount();
    const answer = chooseAction(document, {
      title: "Changed on the server", message: "Keep yours?", choices: ["Keep mine", "Use theirs", "Cancel"],
    });
    await Promise.resolve();
    expect([...dialog.querySelectorAll("button")].map((button) => button.textContent))
      .toEqual(["Keep mine", "Use theirs", "Cancel"]);
    press("Escape");
    expect(await answer).toBeNull();
    expect([...dialog.querySelectorAll("button")].map((button) => button.id))
      .toEqual(["confirm-accept", "confirm-cancel"]);
  });
});
