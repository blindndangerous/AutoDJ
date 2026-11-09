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

it("collapses only the current tab's cards, retaining contents, focus and preferences", () => {
  document.body.innerHTML = `
    <button id="collapse-all-cards" type="button">Collapse all</button>
    <section data-view="settings">
      <section role="tabpanel">
        <details class="card" id="outer" open><summary>Outer</summary>
          <input value="unsaved draft">
          <details class="card" id="nested" open><summary>Nested</summary></details>
        </details>
        <details class="card" id="closed"><summary>Closed</summary></details>
        <details class="card" id="unavailable" open hidden></details>
        <details id="other-disclosure" open><summary>Other disclosure</summary></details>
      </section>
      <section role="tabpanel" hidden>
        <details class="card" id="other-subtab" open></details>
      </section>
    </section>
    <section data-view="queue" hidden><details class="card" id="other-tab" open></details></section>`;
  installCardState();
  const button = document.getElementById("collapse-all-cards");
  const input = document.querySelector("input");
  input.value = "edited draft";
  button.focus();
  button.click();
  button.click();
  for (const id of ["outer", "nested", "closed", "unavailable"]) {
    const card = document.getElementById(id);
    expect(card.open).toBe(false);
    card.dispatchEvent(new Event("toggle"));
    expect(localStorage.getItem(`autodj.card.${id}`)).toBe("closed");
  }
  for (const id of ["other-subtab", "other-tab", "other-disclosure"]) {
    expect(document.getElementById(id).open).toBe(true);
  }
  expect(document.querySelector("input")).toBe(input);
  expect(input.value).toBe("edited draft");
  expect(document.activeElement).toBe(button);
  document.querySelector('[data-view="settings"]').hidden = true;
  document.querySelector('[data-view="queue"]').hidden = false;
  button.click();
  expect(document.getElementById("other-tab").open).toBe(false);
  expect(document.getElementById("other-subtab").open).toBe(true);
});

it("places a named native Collapse all button before the card panels", () => {
  const template = document.createElement("template");
  template.innerHTML = readFileSync("src/autodj/static/index.html", "utf8");
  const button = template.content.getElementById("collapse-all-cards");
  expect(button.tagName).toBe("BUTTON");
  expect(button.type).toBe("button");
  expect(button.getAttribute("aria-label")).toBe("Collapse all cards in the current tab");
  expect(button.closest("details")).toBeNull();
  expect(button.previousElementSibling.getAttribute("role")).toBe("tablist");
  expect(button.parentElement.id).toBe("view-nav");
});

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
