// Track search + Play now / Play next / Add to queue buttons.
//
// Event delegation at document level so each result row's buttons
// share a single handler instead of N per-row listeners.
// Not on the results <ul>: a click listener there made NVDA call the
// whole list "clickable".

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

// Mirrors the `limit` default of GET /api/search (server.py).
const SEARCH_RESULT_LIMIT = 100;

// playNow(request) runs the Play now request and starts this page's
// audio in the same click; it resolves false when it has already said
// why nothing plays.
export function installSearch({
  searchInput, btnSearch, searchResults, searchCount, queueAnnounce,
  folderSelect, previousPage, nextPage,
  playNow = async (request) => { await request(); return true; },
}) {
  if (!searchInput || !searchResults) return;
  const browsing = Boolean(folderSelect && previousPage && nextPage);
  // The search in flight: a newer one, or emptying the field, aborts it.
  let searchRequest = null;
  let offset = 0;
  let total = 0;

  // Announced once and shown once: announceStatus writes the region and
  // copies the same string into the visible #status-toast, so a failed
  // "Next" is no longer silent for a sighted user.
  function announce(message, tone = "info") {
    announceStatus(queueAnnounce, message, { dwellMs: 3000, force: true, tone });
  }

  // #search-count is visible, so it keeps its text instead of being
  // wiped after a dwell; announceStatus still guarantees one write per
  // real change.
  // Searching again and getting the same number of hits is still a
  // completed search, so a repeated count has to be announced again --
  // except when the field is simply being emptied.
  function setCount(message, { mirror = false, tone = "info" } = {}) {
    announceStatus(searchCount, message,
      { mirror, tone, force: Boolean(message) });
  }

  function clearBrowse() {
    searchRequest?.abort();
    searchResults.replaceChildren();
    setCount("");
    if (previousPage) previousPage.disabled = true;
    if (nextPage) nextPage.disabled = true;
  }

  async function doSearch(pageOffset = 0) {
    const q = searchInput.value.trim();
    if (!q && !browsing) {
      clearBrowse();
      return;
    }
    searchRequest?.abort();
    const request = searchRequest = new AbortController();
    let data;
    try {
      let url;
      if (browsing) {
        const params = new URLSearchParams({ kind: folderSelect.value.startsWith("folder:liner:") || folderSelect.value === "all-liners" ? "liner" : "music", offset: String(pageOffset), limit: "100" });
        if (q) params.set("q", q);
        if (folderSelect.value.startsWith("folder:")) params.set("source_id", folderSelect.value.slice(folderSelect.value.lastIndexOf(":") + 1));
        url = `/api/library/tracks?${params}`;
      } else url = `/api/search?q=${encodeURIComponent(q)}`;
      data = await withDisabled(btnSearch, () => requestJson(url, { signal: request.signal }));
    } catch (errorValue) {
      if (request.signal.aborted) return;
      if (browsing && searchRequest !== request) return;
      setCount(`Could not search: ${errorValue.message}`,
        { mirror: true, tone: "error" });
      if (!browsing) searchInput.focus();
      return;
    }
    if (request.signal.aborted || searchRequest !== request) return;
    const results = data.results || [];

    if (results.length === 0) {
      searchResults.innerHTML =
        `<li><span class="no-results">${browsing ? "No tracks found." : `No results for "${escHtml(q)}".`}</span></li>`;
      total = browsing ? Number(data.total || 0) : 0;
      offset = browsing ? Number(data.offset ?? pageOffset) : 0;
      if (browsing) {
        setCount("No tracks found.");
        updatePages();
      } else setCount(`No results for "${q}".`);
      return;
    }

    searchResults.innerHTML = results.map((t) => {
      const name = escHtml(fmtTrack(t));
      const path = escHtml(t.path);
      return `<li>
        <span class="result-name" title="${name}">${name}</span>
        <button class="result-btn"
                aria-label="Play ${name} now"
                data-path="${path}"
                data-now="true"><span aria-hidden="true">&#9654;</span> Play now</button>
        <button class="result-btn"
                aria-label="Play ${name} next"
                data-path="${path}"
                data-now="false"
                data-next="true"><span aria-hidden="true">&#9197;</span> Play next</button>
        <button class="result-btn"
                aria-label="Add ${name} to queue"
                data-path="${path}"
                data-now="false">Add to queue</button>
        <button class="result-btn"
                aria-label="Edit points for ${name}"
                data-path="${path}"
                data-kind="${escHtml(t.kind || "music") }"
                data-points="true">Edit points</button>
      </li>`;
    }).join("");
    if (browsing) {
      total = Number(data.total ?? results.length);
      offset = Number(data.offset ?? pageOffset);
      setCount(`${total} track${total === 1 ? "" : "s"} found; showing ${offset + 1}–${offset + results.length}.`);
      updatePages();
      return;
    }
    // The server caps at SEARCH_RESULT_LIMIT; say so, otherwise a full
    // page of results silently pretends to be the whole library.
    const plural = results.length === 1 ? "" : "s";
    setCount(results.length >= SEARCH_RESULT_LIMIT
      ? `${results.length} result${plural} shown — the first `
        + `${SEARCH_RESULT_LIMIT} matches.  Refine the search to narrow it.`
      : `${results.length} result${plural} found.`);
  }

  function updatePages() {
    if (!browsing) return;
    const focused = searchResults.ownerDocument.activeElement;
    previousPage.disabled = offset <= 0;
    nextPage.disabled = offset + 100 >= total;
    if ((focused === previousPage || focused === nextPage) && focused.disabled) {
      const target = !previousPage.disabled ? previousPage : !nextPage.disabled ? nextPage : btnSearch;
      target?.focus();
    }
  }

  function changePage(delta) {
    const nextOffset = Math.max(0, offset + delta * 100);
    void doSearch(nextOffset);
  }

  if (btnSearch) btnSearch.addEventListener("click", () => doSearch());
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  // Collapse results when input is cleared.
  searchInput.addEventListener("input", () => {
    if (browsing) {
      offset = 0;
      clearBrowse();
    } else if (!searchInput.value.trim()) clearBrowse();
  });
  if (browsing) {
    folderSelect.addEventListener("change", () => { offset = 0; void doSearch(); });
    previousPage.addEventListener("click", () => changePage(-1));
    nextPage.addEventListener("click", () => changePage(1));
  }

  // Play-now / queue-add buttons via event delegation.
  searchResults.ownerDocument.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".result-btn");
    if (!btn || !searchResults.contains(btn)) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const path = btn.dataset.path;
    if (btn.dataset.points === "true") {
      searchResults.dispatchEvent(new CustomEvent("autodj:edit-points", {
        bubbles: true,
        detail: { path, kind: btn.dataset.kind || "music" },
      }));
      return;
    }
    const now  = btn.dataset.now === "true";
    const name = btn.closest("li").querySelector(".result-name").textContent;
    try {
      await withDisabled(btn, async () => {
      if (now) {
        // Called synchronously in the click: a page that never pressed
        // Play unlocks its audio inside this gesture.
        const started = await playNow(() => requestJson("/api/play-next", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, now: true }),
        }));
        if (!isAuthenticatedRequestCurrent(epoch) || !started) return;
        announce(`Playing ${name} now.`);
      } else if (btn.dataset.next === "true") {
        // Plays straight after the current track, ahead of the queue.
        await requestJson("/api/play-next", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, now: false }),
        });
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        announce(`${name} plays next.`);
      } else {
        await requestJson("/api/queue/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        announce(`Added ${name} to queue.`);
      }
      });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      const failed = now ? `Could not play ${name} now.`
        : btn.dataset.next === "true" ? `Could not play ${name} next.`
          : `Could not add ${name} to the queue.`;
      announce(`${failed} ${errorValue.message}`, "error");
    }
  });
}
