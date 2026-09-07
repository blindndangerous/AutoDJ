import { describe, expect, it, vi } from "vitest";

import { applyLibraryJobState, installLibraryJobs } from
  "../../src/autodj/static/modules/library-jobs.js";

function makeEls() {
  document.body.innerHTML =
    '<p id="status">Idle.</p><p id="elapsed" aria-live="off"></p><pre id="log"></pre>';
  return {
    jobStatus: document.querySelector("#status"),
    jobElapsed: document.querySelector("#elapsed"),
    libLog: document.querySelector("#log"),
  };
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
    announcements: () => records.filter((r) => r.addedNodes.length > 0).length,
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
    expect(els.jobElapsed.textContent).toBe("120s elapsed");
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
    expect(els.jobStatus.textContent).toBe("index finished cleanly in 61 seconds.");
    expect(seen.announcements()).toBe(2);

    for (let i = 0; i < 10; i++) applyLibraryJobState(finished, els);
    await settle();
    expect(seen.announcements()).toBe(2);
    expect(els.jobElapsed.textContent).toBe("");
    seen.stop();
  });

  it("announces a non-zero exit once, in whole seconds", async () => {
    const els = makeEls();
    const seen = watch(els.jobStatus);

    applyLibraryJobState({
      library_job: {
        name: "prune", running: false, elapsed_seconds: 4.7, exit_code: 2, lines: [],
      },
    }, els);
    await settle();

    expect(els.jobStatus.textContent)
      .toBe("prune exited with code 2 after 5 seconds.");
    expect(seen.announcements()).toBe(1);
    seen.stop();
  });

  it("does not re-announce the start click on the next websocket tick", async () => {
    document.body.innerHTML = `
      <button id="run-index">Index</button>
      <p id="status">Idle.</p><p id="elapsed" aria-live="off"></p><pre id="log"></pre>`;
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
    expect(els.libLog.textContent).toBe("one");
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
    expect(els.libLog.textContent).toBe("one\ntwo");
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

    expect(els.libLog.textContent).toBe("b\nc\nd");
    const removed = seen.records.reduce((n, r) => n + r.removedNodes.length, 0);
    const added = seen.records.reduce((n, r) => n + r.addedNodes.length, 0);
    expect(removed).toBe(1);
    expect(added).toBe(1);
    seen.stop();
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
});
