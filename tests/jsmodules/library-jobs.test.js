import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { applyLibraryJobState, installLibraryJobs, libraryJobStatusText } from
  "../../src/autodj/static/modules/library-jobs.js";

function makeEls() {
  document.body.innerHTML =
    '<p id="status">Idle.</p><p id="elapsed" aria-live="off"></p><div id="log"></div>';
  return {
    jobStatus: document.querySelector("#status"),
    jobElapsed: document.querySelector("#elapsed"),
    libLog: document.querySelector("#log"),
  };
}

// One element per line: NVDA's browse mode reads one per Down Arrow.
// Text nodes joined by newlines in a <pre> are not a line each for it.
function logLines(log) {
  return [...log.childNodes].map((node) => {
    expect(node.tagName).toBe("DIV");
    return node.textContent;
  });
}

// One live-region announcement == one batch of text landing in the node.
// Replacing textContent removes the old text node and adds a new one, so
// count the additions: that is exactly the number of times a screen
// reader would speak.
function watch(node) {
  const records = [];
  const observer = new window.MutationObserver((r) => records.push(...r));
  observer.observe(node, { childList: true, characterData: true, subtree: true });
  return {
    records,
    // Count text landing in the node whichever way it got there: replacing
    // textContent swaps the child node, but a firstChild.data write would
    // report as characterData and must not slip past as "silent".
    announcements: () => records.filter(
      (r) => r.addedNodes.length > 0 || r.type === "characterData",
    ).length,
    stop: () => observer.disconnect(),
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("library job live region", () => {
  it("announces a running job exactly once no matter how long it runs", async () => {
    const els = makeEls();
    const seen = watch(els.jobStatus);

    for (let t = 1; t <= 120; t++) {
      applyLibraryJobState(
        { library_job: { name: "index", running: true, elapsed_seconds: t, lines: [] } },
        els,
      );
    }
    await settle();

    expect(els.jobStatus.textContent).toBe("index running.");
    expect(seen.announcements()).toBe(1);
    expect(els.jobElapsed.textContent).toBe("2:00 elapsed");
    seen.stop();
  });

  it("keeps the ticking clock out of the live region", async () => {
    const els = makeEls();
    const seen = watch(els.jobElapsed);

    for (const t of [1, 2, 3]) {
      applyLibraryJobState(
        { library_job: { name: "index", running: true, elapsed_seconds: t, lines: [] } },
        els,
      );
    }
    await settle();

    // The clock node mutates freely; it is aria-live="off" so nothing speaks.
    expect(seen.records.length).toBeGreaterThan(1);
    expect(els.jobElapsed.getAttribute("aria-live")).toBe("off");
    expect(els.jobStatus.textContent).toBe("index running.");
    seen.stop();
  });

  it("announces each phase change exactly once and repeats nothing", async () => {
    const els = makeEls();
    const seen = watch(els.jobStatus);

    applyLibraryJobState(
      { library_job: { name: "index", running: true, elapsed_seconds: 1, lines: [] } },
      els,
    );
    await settle();
    expect(seen.announcements()).toBe(1);

    const finished = {
      library_job: {
        name: "index", running: false, elapsed_seconds: 61.4, exit_code: 0, lines: [],
      },
    };
    applyLibraryJobState(finished, els);
    await settle();
    expect(els.jobStatus.textContent).toBe("index finished cleanly in 1 minute 1 second.");
    expect(seen.announcements()).toBe(2);

    for (let i = 0; i < 10; i++) applyLibraryJobState(finished, els);
    await settle();
    expect(seen.announcements()).toBe(2);
    expect(els.jobElapsed.textContent).toBe("");
    seen.stop();
  });

  it("announces a non-zero exit once, in whole seconds, with its reason", async () => {
    const els = makeEls();
    const seen = watch(els.jobStatus);

    applyLibraryJobState({
      library_job: {
        name: "prune", running: false, elapsed_seconds: 4.7, exit_code: 2,
        lines: ["Scanning index", "Refusing to prune: most files are missing.", ""],
      },
    }, els);
    await settle();

    expect(els.jobStatus.textContent).toBe(
      "prune exited with code 2 after 5 seconds. Refusing to prune: most files are missing.",
    );
    expect(seen.announcements()).toBe(1);
    seen.stop();
  });

  it("gives a failure's own last line as the reason, not the runner's trailer", () => {
    const els = makeEls();
    applyLibraryJobState({
      library_job: {
        name: "enrich", running: false, elapsed_seconds: 1, exit_code: 1,
        lines: [
          "[autodj-jobs] $ autodj enrich",
          "No beets_db in config — enrich requires beets.",
          "[autodj-jobs] exit 1 (elapsed 1.0s)",
        ],
      },
    }, els);

    expect(els.jobStatus.textContent).toBe(
      "enrich exited with code 1 after 1 second. No beets_db in config — enrich requires beets.",
    );
  });

  it("speaks a running job's percentage on Shift+J instead of its progress bar", () => {
    expect(libraryJobStatusText({
      name: "prune", running: true, elapsed_seconds: 27.2,
      lines: ["Pruning:  43%|████▎     | 32919/76728 [00:22<00:29, 1477.44file/s]"],
    })).toBe("prune running, 27 seconds elapsed, 43 percent done.");
    expect(libraryJobStatusText({
      name: "stats", running: true, elapsed_seconds: 65,
      lines: ["[autodj-jobs] $ autodj stats", "Loading faiss."],
    })).toBe("stats running, 1 minute 5 seconds elapsed.");
  });

  it("reports a job the user stopped as stopped, not as an exit code", () => {
    const els = makeEls();
    applyLibraryJobState({
      library_job: {
        name: "enrich", running: false, elapsed_seconds: 417, exit_code: 1,
        stopped: true, lines: [],
      },
    }, els);

    expect(els.jobStatus.textContent).toBe("enrich stopped after 6 minutes 57 seconds.");
  });

  it("does not re-announce the start click on the next websocket tick", async () => {
    document.body.innerHTML = `
      <button id="run-index">Index</button>
      <p id="status">Idle.</p><p id="elapsed" aria-live="off"></p><div id="log"></div>`;
    const els = {
      runIndex: document.querySelector("#run-index"),
      jobStatus: document.querySelector("#status"),
      jobElapsed: document.querySelector("#elapsed"),
      libLog: document.querySelector("#log"),
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
      new globalThis.Response("{}", { headers: { "Content-Type": "application/json" } }),
    )));
    installLibraryJobs(els);
    const seen = watch(els.jobStatus);

    els.runIndex.click();
    await vi.waitFor(() => expect(els.jobStatus.textContent).toBe("index started."));
    const afterClick = seen.announcements();
    expect(afterClick).toBe(1);

    applyLibraryJobState(
      { library_job: { name: "index", running: true, elapsed_seconds: 0.6, lines: [] } },
      els,
    );
    await settle();
    expect(els.jobStatus.textContent).toBe("index started.");
    expect(seen.announcements()).toBe(afterClick);
    seen.stop();
    vi.unstubAllGlobals();
  });
});

describe("library job log", () => {
  it("appends new lines instead of rewriting the whole log", async () => {
    const els = makeEls();
    const seen = watch(els.libLog);

    applyLibraryJobState({
      library_job: { name: "index", running: true, elapsed_seconds: 1, lines: ["one"] },
    }, els);
    await settle();
    expect(logLines(els.libLog)).toEqual(["one"]);
    const afterFirst = seen.records.length;

    // Identical payload -> no DOM work at all.
    for (let i = 0; i < 5; i++) {
      applyLibraryJobState({
        library_job: { name: "index", running: true, elapsed_seconds: 2, lines: ["one"] },
      }, els);
    }
    await settle();
    expect(seen.records).toHaveLength(afterFirst);

    // One new line -> exactly one appended node, nothing removed.
    applyLibraryJobState({
      library_job: {
        name: "index", running: true, elapsed_seconds: 3, lines: ["one", "two"],
      },
    }, els);
    await settle();
    expect(logLines(els.libLog)).toEqual(["one", "two"]);
    const appended = seen.records.slice(afterFirst);
    expect(appended).toHaveLength(1);
    expect(appended[0].addedNodes).toHaveLength(1);
    expect(appended[0].removedNodes).toHaveLength(0);
    seen.stop();
  });

  it("drops only the trimmed head when the server window slides", async () => {
    const els = makeEls();
    applyLibraryJobState({
      library_job: {
        name: "index", running: true, elapsed_seconds: 1, lines: ["a", "b", "c"],
      },
    }, els);
    await settle();
    const seen = watch(els.libLog);

    // Server keeps only the last 25 lines; here the window slides by one.
    applyLibraryJobState({
      library_job: {
        name: "index", running: true, elapsed_seconds: 2, lines: ["b", "c", "d"],
      },
    }, els);
    await settle();

    expect(logLines(els.libLog)).toEqual(["b", "c", "d"]);
    const removed = seen.records.reduce((n, r) => n + r.removedNodes.length, 0);
    const added = seen.records.reduce((n, r) => n + r.addedNodes.length, 0);
    expect(removed).toBe(1);
    expect(added).toBe(1);
    seen.stop();
  });

  it("names the log with its line count instead of taking focus", async () => {
    // A focusable log was read whole, in one utterance, when it got focus.
    // The log takes no focus; its heading, which names the region, says
    // how many lines it holds.
    const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const template = document.createElement("template");
    template.innerHTML = html;
    const log = template.content.querySelector("#library-log");
    expect(log.hasAttribute("tabindex")).toBe(false);
    const heading = template.content.querySelector(`#${log.getAttribute("aria-labelledby")}`);
    expect(heading.querySelector("#library-log-count")).not.toBeNull();

    const els = makeEls();
    document.body.insertAdjacentHTML("afterbegin",
      '<h3 id="heading">Output<span id="library-log-count"></span></h3>');
    const name = () => document.querySelector("#heading").textContent;
    applyLibraryJobState({ library_job: { name: "", running: false, lines: [] } }, els);
    expect(name()).toBe("Output");
    applyLibraryJobState({
      library_job: { name: "stats", running: true, elapsed_seconds: 1, lines: ["one"] },
    }, els);
    expect(name()).toBe("Output, 1 line");
    applyLibraryJobState({
      library_job: { name: "stats", running: true, elapsed_seconds: 2, lines: ["one", "two"] },
    }, els);
    expect(name()).toBe("Output, 2 lines");
  });

  it("shows the empty note when no job has run", async () => {
    const els = makeEls();
    applyLibraryJobState({
      library_job: { name: "", running: false, lines: [] },
    }, els);
    await settle();
    expect(els.libLog.textContent).toContain("No job has run yet");
    expect(els.jobStatus.textContent).toBe("Idle.");
  });
});

describe("library job controls", () => {
  it("restores the clicked control and announces checked request failures", async () => {
    document.body.innerHTML = '<button id="stop">Stop</button><p id="status"></p>';
    let resolve;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((done) => { resolve = done; })));
    const runStop = document.querySelector("#stop");
    const jobStatus = document.querySelector("#status");
    installLibraryJobs({ runStop, jobStatus });

    runStop.click();
    expect(runStop.disabled).toBe(true);
    resolve(new globalThis.Response(JSON.stringify({ detail: "Nothing is running" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(jobStatus.textContent)
      .toContain("Nothing is running"));
    expect(runStop.disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("does not render stats completed after authenticated expiry", async () => {
    document.body.innerHTML = `
      <button id="stats">Stats</button>
      <p id="status"></p>
      <span id="count"></span>
      <span id="average"></span>
      <span id="key"></span>
      <span id="genre"></span>
      <span id="energy"></span>
    `;
    let resolveStats;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveStats = resolve;
    })));
    const apiClient = await import("../../src/autodj/static/modules/api-client.js");
    const statCount = document.querySelector("#count");
    installLibraryJobs({
      jobStatus: document.querySelector("#status"),
      statCount,
      statAvgBpm: document.querySelector("#average"),
      statWithKey: document.querySelector("#key"),
      statWithGenre: document.querySelector("#genre"),
      statWithEnergy: document.querySelector("#energy"),
    });

    await vi.waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    apiClient.invalidateAuthenticatedRequestEpoch?.();
    resolveStats(new globalThis.Response(JSON.stringify({
      average_bpm: 120,
      track_count: 99,
      tracks_with_bpm: 80,
      tracks_with_energy: 60,
      tracks_with_genre: 70,
      tracks_with_key: 50,
    }), {
      headers: { "Content-Type": "application/json" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(statCount.textContent).toBe("");
    vi.unstubAllGlobals();
  });

  it("confirms every Refresh stats press, even when the numbers are unchanged", async () => {
    document.body.innerHTML = `
      <button id="refresh">Refresh stats</button>
      <p id="status"></p><div id="sr-status"></div>
      <span id="count"></span><span id="average"></span><span id="key"></span>
      <span id="genre"></span><span id="energy"></span>`;
    const stats = {
      average_bpm: 120, track_count: 99, tracks_with_bpm: 80,
      tracks_with_energy: 60, tracks_with_genre: 70, tracks_with_key: 50,
    };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new globalThis.Response(
      JSON.stringify(stats), { headers: { "Content-Type": "application/json" } },
    ))));
    const region = document.querySelector("#sr-status");
    installLibraryJobs({
      jobStatus: document.querySelector("#status"),
      statsRefresh: document.querySelector("#refresh"),
      statCount: document.querySelector("#count"),
      statAvgBpm: document.querySelector("#average"),
      statWithKey: document.querySelector("#key"),
      statWithGenre: document.querySelector("#genre"),
      statWithEnergy: document.querySelector("#energy"),
    });
    // The automatic load on install is not announced.
    await vi.waitFor(() => expect(document.querySelector("#count").textContent).toBe("99"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(region.textContent).toBe("");

    const landed = [];
    new window.MutationObserver((records) => {
      for (const record of records) if (region.textContent) landed.push(record);
    }).observe(region, { childList: true, characterData: true, subtree: true });
    for (const expected of [1, 2]) {
      document.querySelector("#refresh").click();
      await vi.waitFor(() => expect(landed).toHaveLength(expected));
    }
    expect(region.textContent).toBe("Stats refreshed.");
    expect(document.querySelector("#status").textContent).toBe("");
    vi.unstubAllGlobals();
  });
});

describe("library job follow-up", () => {
  const json = (body, status = 200) => Promise.resolve(new globalThis.Response(
    JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } },
  ));
  const stats = {
    track_count: 7, average_bpm: 120, tracks_with_bpm: 7,
    tracks_with_key: 1, tracks_with_genre: 2, tracks_with_energy: 3,
  };

  function setup(onJob) {
    document.body.innerHTML = `
      <p id="status">Idle.</p><p id="elapsed"></p><div id="log"></div>
      <span id="count"></span><span id="avg"></span><span id="key"></span>
      <span id="genre"></span><span id="energy"></span>
      <button id="run-stats">Stats</button><p id="sr-status"></p>
      <div id="status-toast" hidden></div>`;
    const fetchImpl = vi.fn((url) => (url === "/api/library/job" ? onJob() : json(stats)));
    vi.stubGlobal("fetch", fetchImpl);
    const $ = (id) => document.getElementById(id);
    const els = {
      jobStatus: $("status"), jobElapsed: $("elapsed"), libLog: $("log"),
      statCount: $("count"), statAvgBpm: $("avg"), statWithKey: $("key"),
      statWithGenre: $("genre"), statWithEnergy: $("energy"), runStats: $("run-stats"),
    };
    installLibraryJobs(els);
    return { els, fetchImpl };
  }

  const allLines = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`);
  const finished = (lines) => ({ library_job: {
    name: "stats", running: false, exit_code: 0, started_at: 100,
    elapsed_seconds: 3, lines,
  } });

  it("fetches the full log once the job finishes and keeps the shown lines", async () => {
    const { els, fetchImpl } = setup(() => json({ ...finished(allLines).library_job }));
    applyLibraryJobState(finished(allLines.slice(-25)), els);
    const firstShown = els.libLog.firstChild;

    await vi.waitFor(() => expect(logLines(els.libLog)).toEqual(allLines));
    // The 25 lines already on screen are the same nodes: a reader inside
    // the log keeps their place.
    expect(els.libLog.childNodes[15]).toBe(firstShown);
    // The next push still carries only 25 lines; the full log stays.
    applyLibraryJobState(finished(allLines.slice(-25)), els);
    expect(logLines(els.libLog)).toEqual(allLines);
    expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/library/job")).toHaveLength(1);
    await vi.waitFor(() => expect(els.statCount.textContent).toBe("7"));
    vi.unstubAllGlobals();
  });

  it("says which job is running instead of starting another", async () => {
    const { els, fetchImpl } = setup(() => json({}));
    applyLibraryJobState({ library_job: {
      name: "index", running: true, started_at: 5, elapsed_seconds: 1, lines: [],
    } }, els);
    const calls = fetchImpl.mock.calls.length;

    els.runStats.click();

    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toContain("A job is already running: index."));
    expect(fetchImpl.mock.calls.length).toBe(calls);
    expect(els.runStats.disabled).toBe(false);
    vi.unstubAllGlobals();
  });
});
