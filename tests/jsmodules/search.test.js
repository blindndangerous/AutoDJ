import { beforeEach, describe, expect, it, vi } from "vitest";

import { installSearch } from "../../src/autodj/static/modules/search.js";

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup() {
  document.body.innerHTML = `
    <input id="search"><button id="go">Search</button>
    <p id="count"></p><p id="announce"></p><ul id="results"></ul>
    <div id="status-toast" aria-hidden="true" hidden></div>`;
  const els = {
    searchInput: document.querySelector("#search"),
    btnSearch: document.querySelector("#go"),
    searchResults: document.querySelector("#results"),
    searchCount: document.querySelector("#count"),
    queueAnnounce: document.querySelector("#announce"),
  };
  installSearch(els);
  return els;
}

describe("search requests", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("renders only the newest search response", async () => {
    const resolvers = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolvers.push(resolve);
    })));
    const els = setup();
    els.searchInput.value = "old";
    els.btnSearch.click();
    els.searchInput.value = "new";
    els.searchInput.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
    }));

    resolvers[1](jsonResponse({ results: [{ path: "new.mp3", title: "Newest" }] }));
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Newest"));
    resolvers[0](jsonResponse({ results: [{ path: "old.mp3", title: "Stale" }] }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(els.searchResults.textContent).not.toContain("Stale");
    vi.unstubAllGlobals();
  });

  it("does not render a response after the search input is cleared", async () => {
    let resolveSearch;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveSearch = resolve;
    })));
    const els = setup();
    els.searchInput.value = "old";
    els.btnSearch.click();
    els.searchInput.value = "";
    els.searchInput.dispatchEvent(new Event("input", { bubbles: true }));
    resolveSearch(jsonResponse({
      results: [{ path: "old.mp3", title: "Stale result" }],
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(els.searchResults.textContent).toBe("");
    // A plain search input is not a combobox: aria-expanded on it makes
    // NVDA announce "collapsed" on a field with no popup.
    expect(els.searchInput.hasAttribute("aria-expanded")).toBe(false);
    vi.unstubAllGlobals();
  });

  it("announces a failed mutation and restores and refocuses its control", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        results: [{ path: "song.mp3", title: "Song" }],
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: "Queue unavailable" }, 503)));
    const els = setup();
    els.searchInput.value = "song";
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.searchResults.querySelector(".result-btn"))
      .not.toBeNull());
    const mutation = els.searchResults.querySelector(".result-btn");
    mutation.click();

    await vi.waitFor(() => expect(els.queueAnnounce.textContent)
      .toContain("Queue unavailable"));
    expect(mutation.disabled).toBe(false);
    expect(document.activeElement).toBe(mutation);
    vi.unstubAllGlobals();
  });
});

describe("search feedback is visible", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("says the result list is capped when the server returns a full page", async () => {
    const results = Array.from({ length: 100 }, (_, i) => ({
      path: `/m/${i}.mp3`, artist: "A", title: `T${i}`,
    }));
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({ results }))));
    const els = setup();
    els.searchInput.value = "love";
    els.btnSearch.click();

    await vi.waitFor(() => expect(els.searchCount.textContent).not.toBe(""));
    expect(els.searchCount.textContent).toContain("the first 100 matches");
    // The count stays on screen instead of being wiped after a dwell.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(els.searchCount.textContent).not.toBe("");
    vi.unstubAllGlobals();
  });

  it("shows a failed queue add in the sighted status line", async () => {
    const els = setup();
    els.searchResults.innerHTML = `<li>
      <span class="result-name">Gone Track</span>
      <button class="result-btn" data-path="/m/gone.mp3" data-now="false">Next</button>
    </li>`;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
      jsonResponse({ detail: "No such file" }, 404),
    )));

    els.searchResults.querySelector(".result-btn").click();
    const toast = document.querySelector("#status-toast");
    await vi.waitFor(() => expect(toast.textContent).toContain("Could not update queue"));
    expect(toast.hidden).toBe(false);
    expect(els.queueAnnounce.textContent).toBe(toast.textContent);
    vi.unstubAllGlobals();
  });
});
