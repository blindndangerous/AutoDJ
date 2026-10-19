// Pure-function tests for ./modules/dom-helpers.js.

import { describe, it, expect } from "vitest";
import { clearFormErrors, fmtTime, fmtTrack, escHtml, focusAfterDialog, focusRevealed, isTypingTarget, placeRows } from
  "../../src/autodj/static/modules/dom-helpers.js";

describe("fmtTime", () => {
  it("formats seconds into m:ss or h:mm:ss without wrapping hours", () => {
    expect(fmtTime(0)).toBe("0:00");
    expect(fmtTime(5)).toBe("0:05");
    expect(fmtTime(65)).toBe("1:05");
    expect(fmtTime(59.9)).toBe("0:59");
    expect(fmtTime(3599)).toBe("59:59");
    expect(fmtTime(3600)).toBe("1:00:00");
    expect(fmtTime(3661)).toBe("1:01:01");
    expect(fmtTime(90061)).toBe("25:01:01");
  });
  it("returns 0:00 for falsy / NaN", () => {
    expect(fmtTime(null)).toBe("0:00");
    expect(fmtTime(undefined)).toBe("0:00");
    expect(fmtTime(NaN)).toBe("0:00");
    expect(fmtTime(Infinity)).toBe("0:00");
    expect(fmtTime(-1)).toBe("0:00");
  });
});

it("clears stale error descriptions even after the invalid marker was removed", () => {
  document.body.innerHTML = '<form><input aria-describedby="help problem"><p id="help">Hint</p>'
    + '<p id="problem" class="field-error">Refused</p></form>';
  const form = document.querySelector("form");
  const input = form.querySelector("input");
  clearFormErrors(form);
  expect(input.getAttribute("aria-describedby")).toBe("help");
  expect(form.querySelector("#problem").hidden).toBe(true);
  expect(form.querySelector("#problem").textContent).toBe("");
});

describe("fmtTrack", () => {
  it("uses artist + title when both present", () => {
    expect(fmtTrack({ artist: "A", title: "B" })).toBe("A — B");
  });
  it("falls back to display_name then title", () => {
    expect(fmtTrack({ display_name: "X" })).toBe("X");
    expect(fmtTrack({ title: "T" })).toBe("T");
  });
  it("returns dash for null/undefined track", () => {
    expect(fmtTrack(null)).toBe("—");
    expect(fmtTrack(undefined)).toBe("—");
  });
  it("returns Unknown for empty object", () => {
    expect(fmtTrack({})).toBe("Unknown");
  });
});

describe("escHtml", () => {
  it("escapes &, <, >, and double quotes", () => {
    expect(escHtml('<a href="x">b&c</a>'))
      .toBe('&lt;a href=&quot;x&quot;&gt;b&amp;c&lt;/a&gt;');
  });
  it("coerces non-strings", () => {
    expect(escHtml(42)).toBe("42");
    expect(escHtml(null)).toBe("null");
  });
});

describe("isTypingTarget", () => {
  it("reports false for null / undefined", () => {
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(undefined)).toBe(false);
  });
  it("reports true for contenteditable", () => {
    const el = { isContentEditable: true, tagName: "DIV" };
    expect(isTypingTarget(el)).toBe(true);
  });
  it("reports true for TEXTAREA", () => {
    expect(isTypingTarget({ tagName: "TEXTAREA" })).toBe(true);
  });
  it("reports true for text-ish INPUT types", () => {
    for (const t of ["text", "search", "email", "url", "password", "number",
                     "tel", "date", "datetime-local", "month", "time", "week"]) {
      expect(isTypingTarget({ tagName: "INPUT", type: t })).toBe(true);
    }
  });
  it("reports false for non-text INPUT types", () => {
    for (const t of ["range", "checkbox", "radio", "button", "submit"]) {
      expect(isTypingTarget({ tagName: "INPUT", type: t })).toBe(false);
    }
  });
  it("defaults missing input.type to text", () => {
    expect(isTypingTarget({ tagName: "INPUT" })).toBe(true);
  });
});

describe("focusRevealed", () => {
  it("opens every closed card around a field, then focuses it", () => {
    document.body.innerHTML = `
      <details class="card" id="outer"><summary><h2>Outer</h2></summary>
        <details class="card" id="inner"><summary><h3>Inner</h3></summary>
          <input id="field">
        </details>
      </details>`;
    const field = document.getElementById("field");
    focusRevealed(field);
    expect(document.getElementById("outer").open).toBe(true);
    expect(document.getElementById("inner").open).toBe(true);
    expect(document.activeElement).toBe(field);
  });
  it("leaves a card closed when focusing its own summary", () => {
    document.body.innerHTML = `
      <details class="card" id="card"><summary id="summary"><h3>Card</h3></summary>
        <input id="field">
      </details>`;
    const summary = document.getElementById("summary");
    focusRevealed(summary);
    expect(document.getElementById("card").open).toBe(false);
    expect(document.activeElement).toBe(summary);
  });
  it("does nothing without a control", () => {
    expect(() => focusRevealed(null)).not.toThrow();
  });
});

describe("placeRows (#328)", () => {
  function list(names) {
    document.body.innerHTML = "<ul></ul>";
    const ul = document.querySelector("ul");
    for (const name of names) {
      const li = document.createElement("li");
      li.innerHTML = `<button>${name}</button>`;
      li.dataset.name = name;
      ul.append(li, " ");
    }
    return ul;
  }
  const names = (ul) => [...ul.childNodes].map((node) => node.dataset?.name ?? "#text");

  it("orders the rows in place, removes the rest and never moves the focused row", () => {
    const ul = list(["a", "b", "c", "d"]);
    const [a, b, c, d] = ul.querySelectorAll("li");
    c.querySelector("button").focus();
    const moved = [];
    const original = ul.insertBefore.bind(ul);
    ul.insertBefore = (node, ref) => { moved.push(node); return original(node, ref); };
    placeRows(ul, [c, a, d]);
    expect(names(ul)).toEqual(["c", "a", "d"]);
    expect(moved).not.toContain(c);
    expect(b.isConnected).toBe(false);
    expect(document.activeElement).toBe(c.querySelector("button"));
    placeRows(ul, [d, a, c]);
    expect(names(ul)).toEqual(["d", "a", "c"]);
    expect(document.activeElement).toBe(c.querySelector("button"));
  });

  it("places new rows when focus is elsewhere", () => {
    const ul = list(["a"]);
    const fresh = document.createElement("li");
    fresh.dataset.name = "z";
    placeRows(ul, [fresh, ul.querySelector("li")]);
    expect(names(ul)).toEqual(["z", "a"]);
  });
});

describe("focusAfterDialog", () => {
  it("does not count the page body as a place focus returned to", () => {
    expect(focusAfterDialog(document.body)).toBe(false);
  });

  it("focuses a connected control and reports it", () => {
    const button = document.createElement("button");
    document.body.append(button);
    expect(focusAfterDialog(button)).toBe(true);
    expect(document.activeElement).toBe(button);
    button.remove();
  });
});
