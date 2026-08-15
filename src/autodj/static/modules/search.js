// Track search + play-now / queue-add buttons.
//
// Event delegation on the results <ul> so each result row's "Now" /
// "Next" buttons share a single handler instead of N per-row listeners.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";
import { createLatestRequestOwner } from "./latest-request.js";

export function installSearch({
  searchInput, btnSearch, searchResults, searchCount, queueAnnounce,
}) {
  if (!searchInput || !searchResults) return;
  const searchRequestOwner = createLatestRequestOwner();

  function announce(message) {
    if (!queueAnnounce) return;
    queueAnnounce.textContent = message;
    clearLiveRegionLater(queueAnnounce);
  }

  async function doSearch() {
    const q = searchInput.value.trim();
    if (!q) {
      searchResults.innerHTML = "";
      searchInput.setAttribute("aria-expanded", "false");
      if (searchCount) searchCount.textContent = "";
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
      if (searchCount) searchCount.textContent = `Could not search: ${errorValue.message}`;
      searchInput.focus();
      return;
    }
    if (!searchRequestOwner.isCurrent(request)) return;
    searchRequestOwner.finish(request);
    const results = data.results || [];

    if (results.length === 0) {
      searchResults.innerHTML =
        `<li><span class="no-results">No results for "${escHtml(q)}".</span></li>`;
      searchInput.setAttribute("aria-expanded", "true");
      if (searchCount) {
        searchCount.textContent = "No results found.";
        clearLiveRegionLater(searchCount);
      }
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
    searchInput.setAttribute("aria-expanded", "true");
    if (searchCount) {
      searchCount.textContent =
        `${results.length} result${results.length === 1 ? "" : "s"} found.`;
      clearLiveRegionLater(searchCount);
    }
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
      searchInput.setAttribute("aria-expanded", "false");
      if (searchCount) searchCount.textContent = "";
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
