// Track search + play-now / queue-add buttons.
//
// Event delegation on the results <ul> so each result row's "Now" /
// "Next" buttons share a single handler instead of N per-row listeners.

import { escHtml, fmtTrack } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";

export function installSearch({
  searchInput, btnSearch, searchResults, searchCount, queueAnnounce,
}) {
  if (!searchInput || !searchResults) return;

  async function doSearch() {
    const q = searchInput.value.trim();
    if (!q) {
      searchResults.innerHTML = "";
      searchInput.setAttribute("aria-expanded", "false");
      if (searchCount) searchCount.textContent = "";
      return;
    }

    const res  = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
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
      searchResults.innerHTML = "";
      searchInput.setAttribute("aria-expanded", "false");
      if (searchCount) searchCount.textContent = "";
    }
  });

  // Play-now / queue-add buttons via event delegation.
  searchResults.addEventListener("click", async (e) => {
    const btn = e.target.closest(".result-btn");
    if (!btn) return;
    const path = btn.dataset.path;
    const now  = btn.dataset.now === "true";
    const name = btn.closest("li").querySelector(".result-name").textContent;
    btn.disabled = true;
    try {
      if (now) {
        await fetch("/api/play-next", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path, now: true }),
        });
        if (queueAnnounce) {
          queueAnnounce.textContent = `Playing ${name} now.`;
          clearLiveRegionLater(queueAnnounce);
        }
      } else {
        await fetch("/api/queue/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        if (queueAnnounce) {
          queueAnnounce.textContent = `Added ${name} to queue.`;
          clearLiveRegionLater(queueAnnounce);
        }
      }
    } finally {
      btn.disabled = false;
      btn.focus();
    }
  });
}
