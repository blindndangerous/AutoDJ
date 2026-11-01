import { readFileSync } from "node:fs";
import { afterEach, expect, it, vi } from "vitest";
import { installCardState } from "../../src/autodj/static/modules/card-state.js";
import * as cardState from "../../src/autodj/static/modules/card-state.js";

// ADR-0011-13
it("refreshes only its own open toggle and disposes the close listener", () => {
  expect(typeof cardState.onCardOpen).toBe("function");
  document.body.innerHTML = '<section id="outer"><details id="inner"></details></section>';
  const outer = document.getElementById("outer"), inner = document.getElementById("inner");
  let refreshes = 0, closes = 0;
  const dispose = cardState.onCardOpen(outer, () => { refreshes += 1; }, { onClose: () => { closes += 1; } });
  expect(refreshes).toBe(0);
  outer.open = true;
  outer.dispatchEvent(new Event("toggle"));
  inner.dispatchEvent(new Event("toggle", { bubbles: true }));
  expect(refreshes).toBe(1);
  outer.open = false;
  outer.dispatchEvent(new Event("toggle"));
  expect(closes).toBe(1);
  dispose();
  outer.open = true;
  outer.dispatchEvent(new Event("toggle"));
  expect(refreshes).toBe(1);
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  document.body.replaceChildren();
});

function cards() {
  document.body.innerHTML = '<details class="card" id="first" open></details><details class="card" id="second"></details>';
  installCardState();
  return [...document.querySelectorAll("details")];
}

it("keeps defaults on first visit and restores both states after a reload", () => {
  localStorage.clear();
  const [first, second] = cards();
  expect(first.open).toBe(true);
  expect(second.open).toBe(false);
  first.open = false;
  second.open = true;
  for (const card of [first, second]) card.dispatchEvent(new Event("toggle"));
  const restored = cards();
  expect(restored.map((card) => card.open)).toEqual([false, true]);
});

it("ignores invalid preferences and tolerates failed storage reads and writes", () => {
  localStorage.setItem("autodj.card.first", "invalid");
  expect(cards()[0].open).toBe(true);
  vi.spyOn(globalThis.Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(globalThis.Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
  const [first] = cards();
  expect(first.open).toBe(true);
  first.open = false;
  expect(() => first.dispatchEvent(new Event("toggle"))).not.toThrow();
});

it("tolerates browsers that block access to localStorage itself", () => {
  vi.spyOn(globalThis, "localStorage", "get").mockImplementation(() => { throw new Error("blocked"); });
  expect(() => cards()).not.toThrow();
});

it("gives every collapsible card a unique stable ID and puts Requests below Browse", () => {
  const template = document.createElement("template");
  template.innerHTML = readFileSync("src/autodj/static/index.html", "utf8");
  const root = template.content;
  const ids = [...root.querySelectorAll("details.card")].map((card) => card.id);
  expect(ids.every(Boolean)).toBe(true);
  expect(new Set(ids).size).toBe(ids.length);
  const browse = root.querySelector("#search-library-heading").closest(".card");
  expect(browse.nextElementSibling.id).toBe("requests-card");
  expect(browse.nextElementSibling.tagName).toBe("DETAILS");
  for (const id of ["history-export-card", "cart-editor-card", "cart-page-card"]) {
    const card = root.getElementById(id);
    expect(card.tagName).toBe("DETAILS");
    expect(card.open).toBe(false);
    expect(card.querySelector(":scope > summary > h3")).not.toBeNull();
  }
  expect(browse.tagName).toBe("DETAILS");
  expect(browse.open).toBe(true);
  expect(browse.querySelector(":scope > summary > h2")).not.toBeNull();
});
