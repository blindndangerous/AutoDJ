import { describe, expect, it, vi } from "vitest";

import { applyLibraryJobState, installLibraryJobs } from
  "../../src/autodj/static/modules/library-jobs.js";

describe("library job controls", () => {
  it("throttles running live-region updates but announces terminal state immediately", async () => {
    document.body.innerHTML = '<p id="status">Idle.</p><pre id="log"></pre>';
    const jobStatus = document.querySelector("#status");
    const els = { jobStatus, libLog: document.querySelector("#log") };
    const mutations = [];
    const observer = new window.MutationObserver((records) => mutations.push(...records));
    observer.observe(jobStatus, { childList: true, characterData: true, subtree: true });

    applyLibraryJobState({
      library_job: { name: "index", running: true, elapsed_seconds: 1, lines: [] },
    }, els);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(jobStatus.textContent).toBe("index running for 1s…");
    expect(mutations.length).toBeGreaterThan(0);
    const initialMutationCount = mutations.length;

    applyLibraryJobState({
      library_job: { name: "index", running: true, elapsed_seconds: 2, lines: [] },
    }, els);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(jobStatus.textContent).toBe("index running for 1s…");
    expect(mutations).toHaveLength(initialMutationCount);

    applyLibraryJobState({
      library_job: { name: "index", running: true, elapsed_seconds: 10, lines: [] },
    }, els);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(jobStatus.textContent).toBe("index running for 10s…");
    expect(mutations.length).toBeGreaterThan(initialMutationCount);
    const meaningfulMutationCount = mutations.length;

    applyLibraryJobState({
      library_job: {
        name: "index", running: false, elapsed_seconds: 11, exit_code: 0, lines: [],
      },
    }, els);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(jobStatus.textContent).toBe("index finished cleanly in 11s.");
    expect(mutations.length).toBeGreaterThan(meaningfulMutationCount);
    observer.disconnect();
  });

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
