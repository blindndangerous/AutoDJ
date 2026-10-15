// Live-region clear-after-dwell behaviour.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  announceStatus,
  clearLiveRegionLater,
  installVisibleStatusBehaviour,
  liveAgain,
  showVisibleStatus,
  shownText,
  writeLive,
  writeQuietly,
} from "../../src/autodj/static/modules/live-region.js";

describe("clearLiveRegionLater", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(()  => { vi.useRealTimers(); });

  it("wipes textContent after the dwell elapses", () => {
    const el = { textContent: "Volume 90%." };
    clearLiveRegionLater(el, 1000);
    expect(el.textContent).toBe("Volume 90%.");
    vi.advanceTimersByTime(999);
    expect(el.textContent).toBe("Volume 90%.");
    vi.advanceTimersByTime(1);
    expect(el.textContent).toBe("");
  });

  it("resets the timer on a second call (most recent wins)", () => {
    const el = { textContent: "first" };
    clearLiveRegionLater(el, 1000);
    vi.advanceTimersByTime(900);
    el.textContent = "second";
    clearLiveRegionLater(el, 1000);  // reset
    vi.advanceTimersByTime(900);
    expect(el.textContent).toBe("second");
    vi.advanceTimersByTime(100);
    expect(el.textContent).toBe("");
  });

  it("no-ops on null", () => {
    expect(() => clearLiveRegionLater(null)).not.toThrow();
  });

  it("does not blank already-empty regions", () => {
    const el = { textContent: "" };
    clearLiveRegionLater(el, 100);
    vi.advanceTimersByTime(200);
    expect(el.textContent).toBe("");
  });
});

describe("announceStatus", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<p id="region" role="status" aria-live="polite" aria-atomic="true"></p>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
  });

  it("mirrors the announced text into the visible status line", () => {
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");

    announceStatus(region, "Could not update queue: request refused.");

    expect(region.textContent).toBe("Could not update queue: request refused.");
    expect(toast.textContent).toBe("Could not update queue: request refused.");
    expect(toast.hidden).toBe(false);
  });

  it("keeps the visible mirror out of the accessibility tree", () => {
    const toast = document.querySelector("#status-toast");
    announceStatus(document.querySelector("#region"), "Added a track to queue.");
    // One source of truth, one announcement: the region speaks, the
    // sighted mirror is hidden from assistive tech.
    expect(toast.getAttribute("aria-hidden")).toBe("true");
    expect(toast.hasAttribute("aria-live")).toBe(false);
    expect(toast.hasAttribute("role")).toBe(false);
  });

  it("does not rewrite a region that already carries the message", () => {
    const region = document.querySelector("#region");
    region.textContent = "Saved.";
    const records = [];
    const observer = new window.MutationObserver((r) => records.push(...r));
    observer.observe(region, { childList: true, characterData: true, subtree: true });

    expect(announceStatus(region, "Saved.")).toBe(false);
    expect(records).toHaveLength(0);
    observer.disconnect();
  });

  it("clears both channels after the dwell", () => {
    vi.useFakeTimers();
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");
    announceStatus(region, "Audio output: Speakers", { dwellMs: 1000 });
    vi.advanceTimersByTime(1001);
    expect(region.textContent).toBe("");
    vi.advanceTimersByTime(60000);
    expect(toast.hidden).toBe(true);
    expect(toast.textContent).toBe("");
    vi.useRealTimers();
  });

  it("no-ops without a region", () => {
    expect(() => announceStatus(null, "nothing")).not.toThrow();
  });
});

describe("announceStatus force", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<p id="region" role="status" aria-live="polite" aria-atomic="true"></p>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
  });

  function additions(node) {
    const records = [];
    const observer = new window.MutationObserver((r) => records.push(...r));
    observer.observe(node, { childList: true, characterData: true, subtree: true });
    return {
      count: () => records.filter((r) => r.addedNodes.length > 0).length,
      stop: () => observer.disconnect(),
    };
  }

  it("re-announces a repeated user action instead of swallowing it", async () => {
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");
    const spoken = additions(region);
    const shown = additions(toast);

    // Pressing "Move up" twice puts the same sentence in the region twice;
    // both presses must be reported.
    announceStatus(region, "Moved Kryptonite up.", { dwellMs: 3000, force: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    announceStatus(region, "Moved Kryptonite up.", { dwellMs: 3000, force: true });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(spoken.count()).toBe(2);
    expect(shown.count()).toBe(2);
    // The repeat ends in a no-break space, so the text itself changes.
    expect(region.textContent).toBe(`Moved Kryptonite up.${String.fromCharCode(160)}`);
    expect(toast.textContent).toBe("Moved Kryptonite up.");
    spoken.stop();
    shown.stop();
  });

  it("clears the region before re-setting it so the change is real", async () => {
    const region = document.querySelector("#region");
    region.textContent = "Saved.";

    expect(announceStatus(region, "Saved.", { force: true })).toBe(true);
    expect(region.textContent).toBe("");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(region.textContent).toBe(`Saved.${String.fromCharCode(160)}`);
  });

  it("still honours the dwell when forced", async () => {
    const region = document.querySelector("#region");
    announceStatus(region, "Microphone permission denied.",
      { dwellMs: 20, force: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(region.textContent).toBe("Microphone permission denied.");
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(region.textContent).toBe("");
  });

  it("leaves websocket-tick writes on change-detection", () => {
    const region = document.querySelector("#region");
    region.textContent = "index running.";
    expect(announceStatus(region, "index running.")).toBe(false);
  });
});

describe("visible status mirror behaviour", () => {
  beforeEach(() => {
    document.body.innerHTML =
      '<p id="region" role="status" aria-live="polite" aria-atomic="true"></p>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
  });

  it("marks a failure differently from a confirmation", () => {
    const region = document.querySelector("#region");
    const toast = document.querySelector("#status-toast");

    announceStatus(region, "Added Kryptonite to queue.");
    expect(toast.classList.contains("is-error")).toBe(false);

    announceStatus(region, "Could not update queue: refused.", { tone: "error" });
    expect(toast.classList.contains("is-error")).toBe(true);

    announceStatus(region, "Added Loser to queue.");
    expect(toast.classList.contains("is-error")).toBe(false);
  });

  it("dismisses on Escape without moving focus", () => {
    installVisibleStatusBehaviour(document);
    const button = document.createElement("button");
    document.body.appendChild(button);
    button.focus();
    showVisibleStatus("Could not update queue: refused.");
    const toast = document.querySelector("#status-toast");
    expect(toast.hidden).toBe(false);

    document.dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "Escape", bubbles: true,
    }));

    expect(toast.hidden).toBe(true);
    expect(toast.textContent).toBe("");
    expect(document.activeElement).toBe(button);
  });

  it("leaves Escape to an open dialog", () => {
    installVisibleStatusBehaviour(document);
    const dialog = document.createElement("dialog");
    dialog.setAttribute("open", "");
    document.body.appendChild(dialog);
    showVisibleStatus("Still visible.");

    document.dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "Escape", bubbles: true,
    }));

    expect(document.querySelector("#status-toast").hidden).toBe(false);
  });
});

describe("visible status never covers the focused control", () => {
  function setup() {
    document.body.innerHTML = '<button id="top">Top</button><button id="bottom">Bottom</button>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
    const rect = (top) => () => ({ top, bottom: top + 40, left: 0, right: 100, width: 100, height: 40 });
    document.querySelector("#top").getBoundingClientRect = rect(10);
    document.querySelector("#bottom").getBoundingClientRect = rect(window.innerHeight - 45);
    return document.querySelector("#status-toast");
  }

  it("moves to the top when focus sits where the toast would be", () => {
    const toast = setup();
    document.querySelector("#bottom").focus();
    showVisibleStatus("Saved.");
    expect(toast.classList.contains("at-top")).toBe(true);
  });

  it("stays at the foot otherwise, and follows focus while showing", () => {
    installVisibleStatusBehaviour(document);
    const toast = setup();
    document.querySelector("#top").focus();
    showVisibleStatus("Saved.");
    expect(toast.classList.contains("at-top")).toBe(false);

    document.querySelector("#bottom").focus();
    expect(toast.classList.contains("at-top")).toBe(true);
    document.querySelector("#top").focus();
    expect(toast.classList.contains("at-top")).toBe(false);
  });
});

// The page's toast: the message, aria-hidden, beside a Dismiss button in
// one frame.  Clicking the button, hovering the frame and focusing it.
describe("dismissible, hoverable status message", () => {
  const PAGE = '<button id="before">Save</button>'
    + '<main id="main-content" tabindex="-1"></main>'
    + '<p id="region" role="status" aria-live="polite" aria-atomic="true"></p>'
    + '<div id="status-toast-box" hidden>'
    + '<div id="status-toast" aria-hidden="true" hidden></div>'
    + '<button type="button" id="status-toast-dismiss">Dismiss'
    + '<span class="visually-hidden"> message</span></button></div>';
  const $ = (id) => document.getElementById(id);
  const pointer = (type, target, relatedTarget = null) => target.dispatchEvent(
    new window.MouseEvent(type, { bubbles: true, relatedTarget }),
  );

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = PAGE;
    installVisibleStatusBehaviour(document);
  });
  afterEach(() => {
    showVisibleStatus("", document);
    vi.useRealTimers();
  });

  it("speaks only the message: the button is outside the region and the mirror", () => {
    const region = $("region");
    const records = [];
    const observer = new window.MutationObserver((r) => records.push(...r));
    observer.observe(region, { childList: true, characterData: true, subtree: true });
    $("before").focus();

    announceStatus(region, "Token created.");

    records.push(...observer.takeRecords());
    observer.disconnect();
    expect(region.textContent).toBe("Token created.");
    // One write of the region, as before the button existed.
    expect(records).toHaveLength(1);
    expect($("status-toast").textContent).toBe("Token created.");
    expect($("status-toast-box").hidden).toBe(false);
    expect(region.contains($("status-toast-dismiss"))).toBe(false);
    expect($("status-toast").contains($("status-toast-dismiss"))).toBe(false);
    // Showing the toast never takes focus.
    expect(document.activeElement).toBe($("before"));
  });

  it("hides on a click of Dismiss, by mouse or keyboard alike", () => {
    showVisibleStatus("Saved.", document);
    $("status-toast-dismiss").click();
    expect($("status-toast-box").hidden).toBe(true);
    expect($("status-toast").hidden).toBe(true);
    expect($("status-toast").textContent).toBe("");
  });

  it("puts focus back where it was when its own button is used", () => {
    $("before").focus();
    showVisibleStatus("Saved.", document);
    $("status-toast-dismiss").focus();
    $("status-toast-dismiss").click();
    expect(document.activeElement).toBe($("before"));
  });

  // WebKit does not focus a clicked button: the press itself took focus
  // off the control that had it and left it on the body.  The press's
  // default is held back, so focus never moves in any browser.
  it("keeps focus where it was when Dismiss is pressed with the mouse", () => {
    $("before").focus();
    showVisibleStatus("Saved.", document);
    const press = new window.MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 0 });
    $("status-toast-dismiss").querySelector("span").dispatchEvent(press);
    expect(press.defaultPrevented).toBe(true);
    $("status-toast-dismiss").click();
    expect($("status-toast-box").hidden).toBe(true);
    expect(document.activeElement).toBe($("before"));
  });

  it("leaves presses elsewhere, and other mouse buttons, alone", () => {
    showVisibleStatus("Saved.", document);
    const elsewhere = new window.MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 0 });
    $("before").dispatchEvent(elsewhere);
    expect(elsewhere.defaultPrevented).toBe(false);
    const secondary = new window.MouseEvent("mousedown", { bubbles: true, cancelable: true, button: 2 });
    $("status-toast-dismiss").dispatchEvent(secondary);
    expect(secondary.defaultPrevented).toBe(false);
  });

  it("puts focus back after Escape from its own button", () => {
    $("before").focus();
    showVisibleStatus("Saved.", document);
    $("status-toast-dismiss").focus();
    $("status-toast-dismiss").dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "Escape", bubbles: true,
    }));
    expect($("status-toast-box").hidden).toBe(true);
    expect(document.activeElement).toBe($("before"));
  });

  it("stays while the pointer is over it, and runs out after it leaves", () => {
    showVisibleStatus("Saved.", document);
    vi.advanceTimersByTime(5000);
    pointer("pointerover", $("status-toast"));
    vi.advanceTimersByTime(60000);
    expect($("status-toast-box").hidden).toBe(false);

    // Moving between the message and the button is still over the toast.
    pointer("pointerout", $("status-toast"), $("status-toast-dismiss"));
    vi.advanceTimersByTime(60000);
    expect($("status-toast-box").hidden).toBe(false);

    pointer("pointerout", $("status-toast-dismiss"), document.body);
    vi.advanceTimersByTime(2999);
    expect($("status-toast-box").hidden).toBe(false);
    vi.advanceTimersByTime(1);
    expect($("status-toast-box").hidden).toBe(true);
  });

  it("stays while focus is inside it, and runs out after focus leaves", () => {
    $("before").focus();
    showVisibleStatus("Saved.", document);
    $("status-toast-dismiss").focus();
    vi.advanceTimersByTime(60000);
    expect($("status-toast-box").hidden).toBe(false);
    // Focus on its own button does not send it to the top.
    expect($("status-toast-box").classList.contains("at-top")).toBe(false);

    $("before").focus();
    vi.advanceTimersByTime(8000);
    expect($("status-toast-box").hidden).toBe(true);
  });

  // The frame takes no pointer (app.css), so crossing the gap between the
  // message and the button passes over the page for a moment.
  it("is still there when the pointer crosses from the message to the button", () => {
    showVisibleStatus("Saved.", document);
    pointer("pointerover", $("status-toast"));
    vi.advanceTimersByTime(60000);
    pointer("pointerout", $("status-toast"), document.body);
    vi.advanceTimersByTime(500);
    pointer("pointerover", $("status-toast-dismiss"));
    vi.advanceTimersByTime(60000);
    expect($("status-toast-box").hidden).toBe(false);
  });

  it("holds a new message while hovered, too", () => {
    showVisibleStatus("First.", document);
    pointer("pointerover", $("status-toast"));
    showVisibleStatus("Second.", document);
    vi.advanceTimersByTime(60000);
    expect($("status-toast").textContent).toBe("Second.");
    pointer("pointerout", $("status-toast"), document.body);
    vi.advanceTimersByTime(8000);
    expect($("status-toast-box").hidden).toBe(true);
  });

  it("keeps the error tone and the top placement on the frame", () => {
    showVisibleStatus("Could not save.", document, { tone: "error" });
    expect($("status-toast-box").classList.contains("is-error")).toBe(true);
    showVisibleStatus("", document);
    expect($("status-toast-box").classList.contains("is-error")).toBe(false);
  });
});

describe("writeQuietly", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("writes with the region off, then turns it back on as it was", () => {
    const polite = document.createElement("p");
    polite.setAttribute("aria-live", "polite");
    writeQuietly(polite, "Live");
    expect(polite.textContent).toBe("Live");
    expect(polite.getAttribute("aria-live")).toBe("off");
    // A second quiet write keeps the region's own setting to go back to.
    writeQuietly(polite, "Live again");
    vi.advanceTimersByTime(999);
    expect(polite.getAttribute("aria-live")).toBe("off");
    vi.advanceTimersByTime(1);
    expect(polite.getAttribute("aria-live")).toBe("polite");

    const status = document.createElement("div");
    status.setAttribute("role", "status");
    writeQuietly(status, "Idle.");
    expect(status.getAttribute("aria-live")).toBe("off");
    liveAgain(status);
    expect(status.hasAttribute("aria-live")).toBe(false);
    vi.advanceTimersByTime(2000);
    expect(status.hasAttribute("aria-live")).toBe(false);
  });

  it("turns a quiet region on before words that must be said go in", () => {
    const region = document.createElement("p");
    region.setAttribute("aria-live", "polite");
    writeLive(region, "Disconnected, retrying");
    // Not quiet: written at once.
    expect(region.textContent).toBe("Disconnected, retrying");
    writeQuietly(region, "Live");
    vi.advanceTimersByTime(300);
    writeLive(region, "Disconnected, retrying");
    expect(region.getAttribute("aria-live")).toBe("polite");
    expect(region.textContent).toBe("Live");
    expect(shownText(region)).toBe("Disconnected, retrying");
    vi.advanceTimersByTime(100);
    expect(region.textContent).toBe("Disconnected, retrying");
    // A later write replaces one still waiting; a quiet write cancels it.
    writeQuietly(region, "Live");
    writeLive(region, "One");
    writeLive(region, "Two");
    vi.advanceTimersByTime(100);
    expect(region.textContent).toBe("Two");
    writeQuietly(region, "Live");
    writeLive(region, "Lost");
    writeQuietly(region, "Live again");
    vi.advanceTimersByTime(2000);
    expect(region.textContent).toBe("Live again");
    expect(region.getAttribute("aria-live")).toBe("polite");
  });
});
