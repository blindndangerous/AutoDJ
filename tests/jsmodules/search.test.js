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

function setupBrowser() {
  document.body.innerHTML = `
    <select id="folder"><option value="all-music">All music</option><option value="all-liners">All voice liners</option><option value="folder:liner:9">Voice liner: N:/voice</option></select>
    <input id="search"><button id="go">Browse</button><button id="previous" disabled>Previous</button><button id="next" disabled>Next</button>
    <p id="count"></p><p id="announce"></p><ul id="results"></ul><div id="status-toast" aria-hidden="true" hidden></div>`;
  const els = {
    searchInput: document.querySelector("#search"), btnSearch: document.querySelector("#go"),
    searchResults: document.querySelector("#results"), searchCount: document.querySelector("#count"),
    queueAnnounce: document.querySelector("#announce"), folderSelect: document.querySelector("#folder"),
    previousPage: document.querySelector("#previous"), nextPage: document.querySelector("#next"),
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

  it("offers an accessible Edit points action that opens the shared library editor", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({
      results: [{ path: "M:/music/song.flac", title: "Song" }],
    }))));
    const els = setup();
    const onEditPoints = vi.fn();
    document.addEventListener("autodj:edit-points", (event) => onEditPoints(event.detail), { once: true });
    els.searchInput.value = "song";
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.searchResults.querySelector('[data-points="true"]')).not.toBeNull());
    const button = els.searchResults.querySelector('[data-points="true"]');
    expect(button.getAttribute("aria-label")).toBe("Edit points for Song");
    button.click();
    expect(onEditPoints).toHaveBeenCalledWith({ path: "M:/music/song.flac", kind: "music" });
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

  it("announces a failed mutation, restores its control and leaves focus alone", async () => {
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
    els.searchInput.focus();
    mutation.click();

    await vi.waitFor(() => expect(els.queueAnnounce.textContent)
      .toContain("Queue unavailable"));
    expect(mutation.disabled).toBe(false);
    expect(document.activeElement).toBe(els.searchInput);
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
      jsonResponse({ detail: "That track is no longer in the library." }, 404),
    )));

    els.searchResults.querySelector(".result-btn").click();
    const toast = document.querySelector("#status-toast");
    await vi.waitFor(() => expect(toast.textContent).toBe(
      "Could not add Gone Track to the queue. That track is no longer in the library.",
    ));
    expect(toast.hidden).toBe(false);
    expect(els.queueAnnounce.textContent).toBe(toast.textContent);
    vi.unstubAllGlobals();
  });

  it("says a failed Play next did not play next", async () => {
    const els = setup();
    els.searchResults.innerHTML = `<li>
      <span class="result-name">Gone Track</span>
      <button class="result-btn" data-path="/m/gone.mp3" data-now="false"
              data-next="true">Play next</button>
    </li>`;
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
      jsonResponse({ detail: "That track is no longer in the library." }, 404),
    )));

    els.searchResults.querySelector(".result-btn").click();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe(
      "Could not play Gone Track next. That track is no longer in the library.",
    ));
    vi.unstubAllGlobals();
  });
});

describe("search result actions", () => {
  it("Play next puts the track after the current one; Add to queue appends", async () => {
    const fetchImpl = vi.fn((url) => Promise.resolve(url.startsWith("/api/search")
      ? jsonResponse({ results: [{ path: "a.mp3", title: "Alpha" }] })
      : jsonResponse({ ok: true })));
    vi.stubGlobal("fetch", fetchImpl);
    const els = setup();
    els.searchInput.value = "alpha";
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Alpha"));

    const button = (label) => [...els.searchResults.querySelectorAll("button")]
      .find((b) => b.getAttribute("aria-label") === label);
    button("Play Alpha next").click();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Alpha plays next."));
    button("Add Alpha to queue").click();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Added Alpha to queue."));

    const posts = fetchImpl.mock.calls.filter(([url]) => !url.startsWith("/api/search"))
      .map(([url, init]) => [url, JSON.parse(init.body)]);
    expect(posts).toEqual([
      ["/api/play-next", { path: "a.mp3", now: false }],
      ["/api/queue/add", { path: "a.mp3" }],
    ]);
    vi.unstubAllGlobals();
  });
});

describe("library browsing", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("browses blank queries by kind, carries liner kind to Edit points, and pages by 100", async () => {
    const fetchImpl = vi.fn((url) => {
      const parsed = new URL(url, "http://localhost");
      const offset = Number(parsed.searchParams.get("offset"));
      return Promise.resolve(jsonResponse({
        results: [{ path: offset ? "N:/voice/next.wav" : "N:/voice/liner.wav", title: offset ? "Next liner" : "Liner", kind: "liner" }],
        total: 101, offset, limit: 100,
      }));
    });
    vi.stubGlobal("fetch", fetchImpl);
    const els = setupBrowser();
    els.folderSelect.value = "folder:liner:9";
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Liner"));
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/library/tracks?kind=liner&offset=0&limit=100&source_id=9");
    expect(els.nextPage.disabled).toBe(false);
    let editDetail;
    els.searchResults.addEventListener("autodj:edit-points", (event) => { editDetail = event.detail; }, { once: true });
    const edit = els.searchResults.querySelector('[data-points="true"]');
    edit.click();
    expect(editDetail).toEqual({ path: "N:/voice/liner.wav", kind: "liner" });
    els.nextPage.focus();
    els.nextPage.click();
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Next liner"));
    expect(fetchImpl.mock.calls[1][0]).toContain("offset=100");
    expect(document.activeElement).toBe(els.previousPage);
    vi.unstubAllGlobals();
  });

  it("ignores stale browser replies and failures after a newer browse starts", async () => {
    const resolvers = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve, reject) => resolvers.push({ resolve, reject }))));
    const els = setupBrowser();
    els.btnSearch.click();
    els.folderSelect.value = "all-liners";
    els.folderSelect.dispatchEvent(new Event("change", { bubbles: true }));
    resolvers[1].resolve(jsonResponse({ results: [{ path: "new.wav", title: "Newest", kind: "liner" }], total: 1, offset: 0 }));
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Newest"));
    resolvers[0].reject(new Error("aborted old request"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(els.searchResults.textContent).toContain("Newest");
    expect(els.searchCount.textContent).not.toContain("Could not");
    vi.unstubAllGlobals();
  });

  it("does not steal focus when a page request finishes after the user moved to the query", async () => {
    let resolvePage;
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({ results: [{ path: "first.wav", title: "First" }], total: 101, offset: 0 }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolvePage = resolve; })));
    const els = setupBrowser();
    els.btnSearch.click();
    await vi.waitFor(() => expect(els.nextPage.disabled).toBe(false));
    els.nextPage.focus();
    els.nextPage.click();
    els.searchInput.focus();
    resolvePage(jsonResponse({ results: [{ path: "last.wav", title: "Last" }], total: 101, offset: 100 }));
    await vi.waitFor(() => expect(els.searchResults.textContent).toContain("Last"));
    expect(document.activeElement).toBe(els.searchInput);
    vi.unstubAllGlobals();
  });
});
