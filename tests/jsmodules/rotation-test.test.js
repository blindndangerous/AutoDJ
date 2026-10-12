import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  endMessage,
  installRotationTest,
  progressText,
  renderSections,
  requestBody,
  seedProblem,
} from "../../src/autodj/static/modules/rotation-test.js";

const byId = (id) => document.getElementById(id);
const status = () => byId("rotation-status").textContent;

const SECTIONS = [
  { title: "Summary", lines: ["Simulated 24 hours.", "Seed 7. Use the same seed to repeat this run exactly."] },
  {
    title: "Categories",
    lines: [{ text: "Power: six songs, 54 plays.", items: ["Plays per song: fewest 8, most ten, average 9."] }],
  },
];

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  byId("settings-tabs").closest("section").hidden = false;
  byId("spanel-picking").hidden = false;
}

function running(picks, percent) {
  return { state: "running", progress: { picks, percent }, report: null, sections: null, error: null };
}

function finished(state = "done") {
  return {
    state,
    progress: { picks: 1200, percent: 100 },
    report: { picks: 1200, station: "default", seed: 7, stopped_early: state === "cancelled" },
    sections: SECTIONS,
    text: "Rotation test for station default\n",
    error: null,
  };
}

function harness(answers) {
  const timers = [];
  const calls = [];
  const request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push({ method, url, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = answers[method].shift();
    if (answer instanceof Error) throw answer;
    return answer;
  });
  const controls = installRotationTest({
    doc: document,
    request,
    setTimer: (fn) => { timers.push(fn); return timers.length; },
    clearTimer: () => {},
  });
  const tick = async () => {
    const next = timers.shift();
    await next();
    await vi.advanceTimersByTimeAsync(10);
  };
  return { controls, calls, timers, tick };
}

async function press(id) {
  byId(id).click();
  await vi.advanceTimersByTimeAsync(10);
}

describe("rotation test helpers", () => {
  it("builds the request from the span, start and seed", () => {
    expect(requestBody("hours:24")).toEqual({ hours: 24 });
    expect(requestBody("days:7", "2026-10-12T06:00", " 42 ")).toEqual({
      days: 7, start: "2026-10-12T06:00", seed: 42,
    });
    expect(requestBody("picks:1000", "", "")).toEqual({ picks: 1000 });
  });

  it("says in words when a seed cannot be sent", () => {
    expect(seedProblem("")).toBeNull();
    expect(seedProblem("0")).toBeNull();
    expect(seedProblem("12345")).toBeNull();
    for (const bad of ["-1", "1.5", "seven", "1234567890123456"]) {
      expect(seedProblem(bad)).toBe("Seed must be a whole number, 0 or more, or empty.");
    }
  });

  it("words the end of a run and its progress", () => {
    expect(endMessage(finished())).toBe(
      "Rotation test finished: 1,200 songs picked. The results are under Rotation test results.",
    );
    expect(endMessage(finished("cancelled"))).toMatch(/^Rotation test cancelled after 1,200 songs\./);
    expect(endMessage({ state: "failed", error: "The library has no songs to pick" })).toBe(
      "Rotation test failed: The library has no songs to pick",
    );
    expect(progressText(running(2914, 40))).toBe("2,914 songs picked, 40 percent done.");
  });

  it("renders sections as headings and lists, with nested lists for items", () => {
    const box = document.createElement("div");
    renderSections(document, box, SECTIONS);
    expect([...box.querySelectorAll("h5")].map((node) => node.textContent)).toEqual(["Summary", "Categories"]);
    expect(box.querySelectorAll(":scope > ul").length).toBe(2);
    const nested = box.querySelector("ul ul li");
    expect(nested.textContent).toBe("Plays per song: fewest 8, most ten, average 9.");
    expect(box.querySelector("table")).toBeNull();
  });
});

describe("the Test rotation control", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    installIndex();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts a run, shows progress, then the results without moving focus", async () => {
    const { calls, tick } = harness({
      POST: [running(0, 0)],
      GET: [running(500, 20), finished()],
    });
    byId("rotation-span").value = "days:7";
    byId("rotation-seed").value = "7";
    const button = byId("rotation-run");
    button.focus();
    await press("rotation-run");
    expect(calls[0]).toEqual({ method: "POST", url: "/api/rotation-test", body: { days: 7, seed: 7 } });
    expect(status()).toBe("Rotation test started. It runs in the background; nothing on air changes.");
    expect(byId("rotation-progress-row").hidden).toBe(false);
    await tick();
    expect(byId("rotation-progress").value).toBe(20);
    expect(byId("rotation-progress-text").textContent).toBe("500 songs picked, 20 percent done.");
    // The progress line is not a live region: only the status speaks.
    expect(byId("rotation-progress-text").closest("[aria-live]")).toBeNull();
    expect(byId("rotation-results").hidden).toBe(true);
    await tick();
    expect(byId("rotation-progress-row").hidden).toBe(true);
    expect(byId("rotation-results").hidden).toBe(false);
    expect([...byId("rotation-results-body").querySelectorAll("h5")].map((node) => node.textContent))
      .toEqual(["Summary", "Categories"]);
    expect(status()).toBe(
      "Rotation test finished: 1,200 songs picked. The results are under Rotation test results.",
    );
    expect(document.activeElement).toBe(button);
  });

  it("refuses a bad seed in words, on the field, without asking the server", async () => {
    const { calls } = harness({ POST: [], GET: [] });
    const seed = byId("rotation-seed");
    seed.value = "lucky";
    await press("rotation-run");
    expect(calls).toEqual([]);
    expect(seed.getAttribute("aria-invalid")).toBe("true");
    expect(seed.getAttribute("aria-describedby")).toBe("rotation-seed-desc rotation-error");
    expect(byId("rotation-error").hidden).toBe(false);
    expect(byId("rotation-error").textContent).toBe("Seed must be a whole number, 0 or more, or empty.");
    expect(document.activeElement).toBe(seed);
    seed.value = "12";
    seed.dispatchEvent(new Event("input"));
    expect(seed.hasAttribute("aria-invalid")).toBe(false);
    expect(byId("rotation-error").hidden).toBe(true);
  });

  it("says why when a press cannot act, and cancels a running test", async () => {
    const { calls } = harness({ POST: [running(0, 0)], GET: [], DELETE: [running(10, 1)] });
    await press("rotation-cancel");
    expect(status()).toBe("No rotation test is running.");
    await press("rotation-run");
    await press("rotation-run");
    expect(status()).toBe("A rotation test is already running. Cancel it first to start another.");
    await press("rotation-cancel");
    expect(calls.at(-1)).toEqual({ method: "DELETE", url: "/api/rotation-test", body: undefined });
    expect(status()).toBe("Cancelling the rotation test.");
    // Both buttons stay enabled and visible throughout.
    for (const id of ["rotation-run", "rotation-cancel"]) {
      expect(byId(id).disabled).toBe(false);
      expect(byId(id).hidden).toBe(false);
    }
  });

  it("reports a refused start with the server's words", async () => {
    harness({ POST: [new Error("A rotation test is already running for station nightowl")], GET: [] });
    await press("rotation-run");
    expect(status()).toBe(
      "The rotation test could not start: A rotation test is already running for station nightowl",
    );
  });

  it("follows a run already going after a reload, and forgets everything on sign-out", async () => {
    const { controls, timers } = harness({ POST: [], GET: [running(40, 5)] });
    await controls.refresh();
    expect(byId("rotation-progress-row").hidden).toBe(false);
    expect(timers.length).toBe(1);
    controls.clear();
    expect(byId("rotation-progress-row").hidden).toBe(true);
    expect(byId("rotation-results").hidden).toBe(true);
    expect(byId("rotation-results-body").childElementCount).toBe(0);
  });
});
