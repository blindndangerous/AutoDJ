// Track search + play-now / queue-add buttons.
//
// Event delegation on the results <ul> so each result row's "Now" /
// "Next" buttons share a single handler instead of N per-row listeners.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";
import { createLatestRequestOwner } from "./latest-request.js";

// Mirrors the `limit` default of GET /api/search (server.py).
const SEARCH_RESULT_LIMIT = 100;

export function installSearch({
  searchInput, btnSearch, searchResults, searchCount, queueAnnounce,
}) {
  if (!searchInput || !searchResults) return;
  const searchRequestOwner = createLatestRequestOwner();

  // Announced once and shown once: announceStatus writes the region and
  // copies the same string into the visible #status-toast, so a failed
  // "Next" is no longer silent for a sighted user.
  function announce(message) {
    announceStatus(queueAnnounce, message, { dwellMs: 3000, force: true });
  }

  // #search-count is visible, so it keeps its text instead of being
  // wiped after a dwell; announceStatus still guarantees one write per
  // real change.
  // Searching again and getting the same number of hits is still a
  // completed search, so a repeated count has to be announced again --
  // except when the field is simply being emptied.
  function setCount(message, { mirror = false } = {}) {
    announceStatus(searchCount, message, { mirror, force: Boolean(message) });
  }

  async function doSearch() {
    const q = searchInput.value.trim();
    if (!q) {
      searchResults.innerHTML = "";
      setCount("");
      searchRequestOwner.cancel();
      return;
    }
    const request = searchRequestOwner.begin();
    let data;
    try {
      data = await withDisabled(btnSearch, () => requestJson(
        `/api/search?q=${encodeURIComponent(q)}`,
        { signal: request.signal },
      ));
    } catch (errorValue) {
      if (!searchRequestOwner.isCurrent(request)) return;
      setCount(`Could not search: ${errorValue.message}`, { mirror: true });
      searchInput.focus();
      return;
    }
    if (!searchRequestOwner.isCurrent(request)) return;
    searchRequestOwner.finish(request);
    const results = data.results || [];

    if (results.length === 0) {
      searchResults.innerHTML =
        `<li><span class="no-results">No results for "${escHtml(q)}".</span></li>`;
      setCount(`No results for "${q}".`);
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
                data-now="true"><span aria-hidden="true">&#9654;</span> Now</button>
        <button class="result-btn"
                aria-label="Queue ${name} as next track"
                data-path="${path}"
                data-now="false"><span aria-hidden="true">&#9197;</span> Next</button>
      </li>`;
    }).join("");
    // The server caps at SEARCH_RESULT_LIMIT; say so, otherwise a full
    // page of results silently pretends to be the whole library.
    const plural = results.length === 1 ? "" : "s";
    setCount(results.length >= SEARCH_RESULT_LIMIT
      ? `${results.length} result${plural} shown — the first `
        + `${SEARCH_RESULT_LIMIT} matches.  Refine the search to narrow it.`
      : `${results.length} result${plural} found.`);
  }

  if (btnSearch) btnSearch.addEventListener("click", doSearch);
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  // Collapse results when input is cleared.
  searchInput.addEventListener("input", () => {
    if (!searchInput.value.trim()) {
      searchRequestOwner.cancel();
      searchResults.innerHTML = "";
      setCount("");
    }
  });

  // Play-now / queue-add buttons via event delegation.
  searchResults.addEventListener("click", async (e) => {
    const btn = e.target.closest(".result-btn");
    if (!btn) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const path = btn.dataset.path;
    const now  = btn.dataset.now === "true";
    const name = btn.closest("li").querySelector(".result-name").textContent;
    try {
      await withDisabled(btn, async () => {
      if (now) {
        await requestJson("/api/play-next", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, now: true }),
        });
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        announce(`Playing ${name} now.`);
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
      announce(`Could not update queue: ${errorValue.message}`);
    } finally {
      if (isAuthenticatedRequestCurrent(epoch)) btn.focus();
    }
  });
}
