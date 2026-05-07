// Pure-function tests for ./modules/dom-helpers.js.

import { describe, it, expect } from "vitest";
import { fmtTime, fmtTrack, escHtml, isTypingTarget } from
  "../../src/autodj/static/modules/dom-helpers.js";

describe("fmtTime", () => {
  it("formats seconds into mm:ss", () => {
    expect(fmtTime(0)).toBe("0:00");
    expect(fmtTime(5)).toBe("0:05");
    expect(fmtTime(65)).toBe("1:05");
    expect(fmtTime(3661)).toBe("61:01");
  });
  it("returns 0:00 for falsy / NaN", () => {
    expect(fmtTime(null)).toBe("0:00");
    expect(fmtTime(undefined)).toBe("0:00");
    expect(fmtTime(NaN)).toBe("0:00");
  });
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
