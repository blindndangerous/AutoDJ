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
  it("returns focus to the opener when the close event comes a task later", async () => {
    // The mounted dialog always fires close a task later, as a browser does.
    const { dialog, opener } = mount();
    opener.focus();
    const answer = ask();
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
    // Escape is the browser's cancel event here; the confirmation answers
    // Cancel and closes the dialog itself.
    const escape = new Event("cancel", { cancelable: true });
    dialog.dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(true);
    expect(dialog.hasAttribute("open")).toBe(false);
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

  // ADR-0011-06
  it("stops a page control activated while it is open, as a screen reader may", async () => {
    mount();
    document.body.insertAdjacentHTML("afterbegin", `
      <button id="page-action">Skip</button>
      <a id="page-link" href="#elsewhere">Elsewhere</a>
      <form id="page-form"><input id="page-field"></form>`);
    const pressed = [];
    for (const id of ["page-action", "page-link"]) {
      const control = document.getElementById(id);
      for (const type of ["click", "keydown", "pointerdown"]) {
        control.addEventListener(type, () => pressed.push(`${id} ${type}`));
      }
    }
    const typed = [];
    document.getElementById("page-field").addEventListener("input", () => typed.push("input"));
    const answer = ask();
    // TalkBack or Narrator activating a page control: a click sent to it
    // directly, which the backdrop does not take.
    document.getElementById("page-action").click();
    const link = document.getElementById("page-link");
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    link.dispatchEvent(enter);
    const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
    link.dispatchEvent(space);
    const down = new Event("pointerdown", { bubbles: true, cancelable: true });
    link.dispatchEvent(down);
    const submit = new Event("submit", { bubbles: true, cancelable: true });
    document.getElementById("page-form").dispatchEvent(submit);
    expect(pressed).toEqual([]);
    expect([enter.defaultPrevented, space.defaultPrevented, down.defaultPrevented, submit.defaultPrevented])
      .toEqual([true, true, true, true]);
    expect(document.activeElement.id).toBe("confirm-cancel");
    // An input event the page's own script sends still reaches it.
    document.getElementById("page-field").dispatchEvent(new Event("input", { bubbles: true }));
    expect(typed).toEqual(["input"]);
    document.getElementById("confirm-cancel").click();
    expect(await answer).toBe(false);
    // Closed, the page works again.
    document.getElementById("page-action").click();
    expect(pressed).toEqual(["page-action click"]);
  });

  // ADR-0011-03
  it("lets go of focus and takes it back once closed, over an editor dialog (#385)", async () => {
    for (const how of ["Escape", "Cancel"]) {
      const { dialog, opened } = mount();
      const editor = document.createElement("dialog");
      editor.innerHTML = '<button id="editor-cancel">Cancel</button>';
      editor.setAttribute("open", "");
      const matches = editor.matches.bind(editor);
      editor.matches = (selector) => selector === ":modal" || matches(selector);
      document.body.append(editor);
      const editorCancel = document.getElementById("editor-cancel");
      editorCancel.focus();
      // Each time the editor's Cancel takes focus: whether the confirmation
      // was still open, and whether its close event had come.
      let closeCame = false;
      dialog.addEventListener("close", () => { closeCame = true; });
      const taken = [];
      editorCancel.addEventListener("focus", () => taken.push({ open: dialog.hasAttribute("open"), closeCame }));
      const answer = confirmAction(document, {
        title: "Discard changes?", message: "Your changes to this event are lost.", confirmLabel: "Discard",
      });
      expect(opened).toEqual(["showModal"]);
      const trail = focusTrail();
      if (how === "Escape") {
        // The confirmation answers Escape itself: it lets go of focus, then
        // closes the dialog.
        const escape = new Event("cancel", { cancelable: true });
        dialog.dispatchEvent(escape);
        expect(escape.defaultPrevented).toBe(true);
      } else {
        document.getElementById("confirm-cancel").click();
      }
      // Answered at once; the close event comes a task later.
      expect(await answer).toBe(false);
      await new Promise((resolve) => { setTimeout(resolve, 0); });
      expect(document.activeElement).toBe(editorCancel);
      expect(trail[0]).toBe("out:confirm-cancel>body");
      // Focus was never put back while the confirmation was open, and was
      // taken again after its close event, a change NVDA reports.
      expect(taken.every((step) => !step.open)).toBe(true);
      expect(taken.at(-1)).toEqual({ open: false, closeCame: true });
      for (const stop of stopTrails.splice(0)) stop();
    }
  });

  // ADR-0011-03
  it("ignores the last confirmation's close event when another opens before it comes", async () => {
    for (const second of ["confirmAction", "chooseAction"]) {
      const { dialog, opener } = mount();
      opener.focus();
      const first = confirmAction(document, {
        title: "Over the DMCA limit", message: "Queue it anyway?", confirmLabel: "Queue anyway",
        onConfirm: async () => opener,
      });
      document.getElementById("confirm-accept").click();
      expect(await first).toBe(true);
      // The next question opens at once, before the first dialog's close
      // event, which comes a task later.
      let answered = false;
      const next = second === "confirmAction"
        ? confirmAction(document, { title: "Sure?", message: "Yes.", confirmLabel: "OK" })
        : chooseAction(document, {
          title: "Voice track", message: "Keep it?", choices: ["Keep voice track", "Discard voice track", "Cancel change"],
        });
      next.then(() => { answered = true; });
      await new Promise((resolve) => { setTimeout(resolve, 10); });
      expect(answered).toBe(false);
      expect(dialog.hasAttribute("open")).toBe(true);
      expect(document.getElementById("confirm-backdrop").hidden).toBe(false);
      expect(document.activeElement.id).toBe("confirm-cancel");
      document.getElementById("confirm-cancel").click();
      expect(await next).toBe(second === "confirmAction" ? false : null);
      expect(document.activeElement).toBe(opener);
    }
  });

  // ADR-0011-03
  it("answers each opening at its own action, so a late close event leaves no listener behind", async () => {
    const { dialog, opener } = mount();
    opener.focus();
    const ran = [];
    const action = (name) => async () => { ran.push(name); return opener; };
    const a = confirmAction(document, { title: "A?", message: "A.", confirmLabel: "A", onConfirm: action("A") });
    document.getElementById("confirm-cancel").click();
    // B opens in the same task, before A's close event.
    const b = confirmAction(document, { title: "B?", message: "B.", confirmLabel: "B", onConfirm: action("B") });
    expect(await a).toBe(false);
    // A's close event comes while B is open, and does nothing to B.
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect(dialog.hasAttribute("open")).toBe(true);
    expect(document.activeElement.id).toBe("confirm-cancel");
    document.getElementById("confirm-cancel").click();
    expect(await b).toBe(false);
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    const c = confirmAction(document, { title: "C?", message: "C.", confirmLabel: "C", onConfirm: action("C") });
    document.getElementById("confirm-accept").click();
    expect(await c).toBe(true);
    // Only C's action ran.
    expect(ran).toEqual(["C"]);
  });

  it("keeps four quick confirmations apart, with mixed answers", async () => {
    const { dialog, opener } = mount();
    const page = document.createElement("button");
    let pagePresses = 0;
    page.addEventListener("click", () => { pagePresses += 1; });
    document.body.append(page);
    opener.focus();
    const ran = [];
    const answers = [];
    // Each opens as soon as the one before is answered, before its close
    // event comes.
    const first = confirmAction(document, { title: "1?", message: "1.", confirmLabel: "1" });
    document.getElementById("confirm-accept").click();
    answers.push(await first);
    const second = confirmAction(document, {
      title: "2?", message: "2.", confirmLabel: "2", onConfirm: async () => { ran.push(2); return opener; },
    });
    document.getElementById("confirm-cancel").click();
    answers.push(await second);
    const third = chooseAction(document, { title: "3?", message: "3.", choices: ["Keep", "Discard", "Cancel"] });
    await Promise.resolve();
    [...dialog.querySelectorAll("button")].find((button) => button.textContent === "Discard").click();
    answers.push(await third);
    const fourth = confirmAction(document, {
      title: "4?", message: "4.", confirmLabel: "4", onConfirm: async () => { ran.push(4); return opener; },
    });
    document.getElementById("confirm-accept").click();
    answers.push(await fourth);
    await new Promise((resolve) => { setTimeout(resolve, 10); });
    expect(answers).toEqual([true, false, "alternate", true]);
    expect(ran).toEqual([4]);
    expect(dialog.hasAttribute("open")).toBe(false);
    expect(document.getElementById("confirm-backdrop").hidden).toBe(true);
    expect(document.activeElement).toBe(opener);
    // Nothing any of them added is left: the page works, and the hidden
    // dialog's buttons answer no one.
    page.click();
    expect(pagePresses).toBe(1);
    document.getElementById("confirm-accept").click();
    expect(ran).toEqual([4]);
  });

  it("answers No for an opening the page closed when the next one opens before its close event", async () => {
    const { dialog, opener } = mount();
    opener.focus();
    const first = ask();
    // The page closes the dialog; before its close event, another opens.
    dialog.close();
    const second = confirmAction(document, { title: "Sure?", message: "Yes.", confirmLabel: "OK" });
    expect(await first).toBe(false);
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect(dialog.hasAttribute("open")).toBe(true);
    document.getElementById("confirm-accept").click();
    expect(await second).toBe(true);
  });

  // ADR-0011-03
  it("answers a running action with its own result when the browser closes the dialog under it", async () => {
    // A second Escape over another modal dialog: the browser closes the
    // confirmation itself while its action runs.  Whether the next
    // confirmation opens after that close event, before it, or not at all,
    // the running one answers with its action's result and later ones
    // still open, close and answer.
    for (const when of ["after its close event", "before its close event", "no other opens"]) {
      const { dialog } = mount();
      const editor = document.createElement("dialog");
      editor.innerHTML = '<button id="editor-a">Revoke</button><button id="editor-b">B</button>';
      editor.setAttribute("open", "");
      const matches = editor.matches.bind(editor);
      editor.matches = (selector) => selector === ":modal" || matches(selector);
      document.body.append(editor);
      document.getElementById("editor-a").focus();
      const ran = [];
      let release;
      const slow = confirmAction(document, {
        title: "Slow", message: "s", confirmLabel: "Go",
        onConfirm: () => new Promise((resolve) => { release = () => { ran.push("slow"); resolve(null); }; }),
      });
      let slowAnswer = "unanswered";
      slow.then((value) => { slowAnswer = value; });
      document.getElementById("confirm-accept").click();
      const held = new Event("cancel", { cancelable: true });
      dialog.dispatchEvent(held);
      expect(held.defaultPrevented).toBe(true);
      expect(dialog.hasAttribute("open")).toBe(true);
      // The browser closes it anyway.
      dialog.close();
      let second = null;
      if (when === "after its close event") await new Promise((resolve) => { setTimeout(resolve, 0); });
      if (when !== "no other opens") {
        second = confirmAction(document, { title: "Two", message: "2", confirmLabel: "Go2" });
        expect(dialog.hasAttribute("open")).toBe(true);
      }
      await new Promise((resolve) => { setTimeout(resolve, 0); });
      expect(slowAnswer).toBe("unanswered");
      release();
      expect(await slow).toBe(true);
      expect(ran).toEqual(["slow"]);
      if (second) {
        // The second still has the dialog, and answers for itself.
        expect(dialog.hasAttribute("open")).toBe(true);
        document.getElementById("confirm-cancel").click();
        expect(await second).toBe(false);
      }
      await new Promise((resolve) => { setTimeout(resolve, 0); });
      // A confirmation the page closes is still answered, and the next one
      // opens and answers.
      document.getElementById("editor-b").focus();
      const third = confirmAction(document, { title: "Three", message: "3", confirmLabel: "Go3" });
      dialog.close();
      expect(await third).toBe(false);
      const fourth = confirmAction(document, { title: "Four", message: "4", confirmLabel: "Go4" });
      expect(dialog.hasAttribute("open")).toBe(true);
      document.getElementById("confirm-accept").click();
      expect(await fourth).toBe(true);
      expect(ran).toEqual(["slow"]);
      editor.remove();
    }
  });

  it("shows the next confirmation idle when a running one is replaced after saying it is busy", async () => {
    for (const ending of ["resolves", "throws"]) {
      const { dialog } = mount();
      const editor = document.createElement("dialog");
      editor.innerHTML = '<button id="editor-a">Revoke</button>';
      editor.setAttribute("open", "");
      const matches = editor.matches.bind(editor);
      editor.matches = (selector) => selector === ":modal" || matches(selector);
      document.body.append(editor);
      document.getElementById("editor-a").focus();
      let release;
      const slow = confirmAction(document, {
        title: "Slow", message: "s", confirmLabel: "Revoke", pendingText: "Removing…",
        onConfirm: () => new Promise((resolve, reject) => {
          release = () => (ending === "throws" ? reject(new Error("refused")) : resolve(null));
        }),
      });
      const confirmButton = document.getElementById("confirm-accept");
      confirmButton.click();
      // Past the moment the dialog says the action is still running.
      await new Promise((resolve) => { setTimeout(resolve, 350); });
      const status = document.getElementById("confirm-status");
      expect(status.textContent).toBe("Removing…");
      expect(confirmButton.getAttribute("aria-busy")).toBe("true");
      // The browser closes it on a second Escape; another confirmation opens.
      dialog.close();
      await new Promise((resolve) => { setTimeout(resolve, 0); });
      const next = confirmAction(document, { title: "Two", message: "2", confirmLabel: "Go" });
      const idle = () => ({ busy: confirmButton.hasAttribute("aria-busy"), status: status.textContent });
      expect(idle()).toEqual({ busy: false, status: "" });
      release();
      expect(await slow).toBe(true);
      expect(idle()).toEqual({ busy: false, status: "" });
      expect(document.getElementById("confirm-title").textContent).toBe("Two");
      document.getElementById("confirm-cancel").click();
      expect(await next).toBe(false);
      editor.remove();
    }
  });

  it("marks Cancel for the browser's own focus as the dialog opens", async () => {
    const { dialog } = mount();
    const answer = ask();
    const cancelButton = document.getElementById("confirm-cancel");
    expect(cancelButton.hasAttribute("autofocus")).toBe(true);
    expect(dialog.querySelector("[autofocus]")).toBe(cancelButton);
    cancelButton.click();
    await answer;
  });

  it("leaves focus where the page put it when an editor closes its Discard question", async () => {
    const { dialog } = mount();
    const editor = document.createElement("dialog");
    editor.innerHTML = '<button id="editor-cancel">Cancel</button>';
    editor.setAttribute("open", "");
    const matches = editor.matches.bind(editor);
    editor.matches = (selector) => (selector === ":modal" ? editor.hasAttribute("open") : matches(selector));
    document.body.append(editor);
    const add = document.createElement("button");
    add.id = "add-category";
    document.body.append(add);
    document.getElementById("editor-cancel").focus();
    const answer = confirmAction(document, {
      title: "Discard changes?", message: "Your changes have not been saved.", confirmLabel: "Discard changes",
    });
    // The panel closes its form: the editor closes the question with it,
    // then the panel moves focus to its own target.
    dialog.close();
    editor.removeAttribute("open");
    add.focus();
    expect(await answer).toBe(false);
    expect(document.activeElement).toBe(add);
  });

  it("answers No and takes down its backdrop when the dialog is taken out of the page", async () => {
    const { dialog } = mount();
    const page = document.createElement("button");
    let pressed = 0;
    page.addEventListener("click", () => { pressed += 1; });
    document.body.append(page);
    const answer = ask();
    dialog.remove();
    page.click();
    expect(await answer).toBe(false);
    expect(document.getElementById("confirm-backdrop").hidden).toBe(true);
    expect(pressed).toBe(1);
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
