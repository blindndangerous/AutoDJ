import { beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installProgrammeReview } from "../../src/autodj/static/modules/programme-review.js";

const get = (name) => document.getElementById(`review-${name}`);
const result = {
  id: "snapshot-1", created_at: "2026-10-17T12:00:00Z", stale: true, incomplete: true,
  duration: 25, effective_range: [0, 600], omissions: ["Missing ID"],
  items: [
    { id: "a", label: "Song A", kind: "song", programme_start: 0, programme_end: 300, preview_start: 0, preview_end: 12 },
    { id: "b", label: "Song B", kind: "song", programme_start: 300, programme_end: 600, preview_start: 12, preview_end: 25 },
  ],
};
async function settle() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}
function harness(overrides = {}) {
  const spoken = [];
  const timers = [];
  const request = vi.fn(async (url) => {
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [{ id: "show-1", name: "Tomorrow" }] };
    return result;
  });
  const controls = installProgrammeReview({
    request, announce: (text) => spoken.push(text),
    setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: vi.fn(),
    ...overrides,
  });
  return { controls, request, spoken, timers };
}

beforeEach(() => installIndexMarkup());

it("loads saved snapshots without generating audio and navigates matching download content", async () => {
  const { controls, request } = harness();
  await controls.refresh();
  expect(get("show").options[0].text).toBe("Tomorrow");
  get("load").click();
  await settle();
  expect(get("summary").textContent).toContain("Out of date");
  expect(get("summary").textContent).toContain("Incomplete");
  expect(get("omissions").textContent).toContain("Missing ID");
  expect(get("mp3").getAttribute("href")).toContain("/reviews/snapshot-1/audio/mp3");
  expect(get("wav").getAttribute("href")).toContain("/reviews/snapshot-1/audio/wav");
  expect(get("audio").autoplay).toBe(false);
  expect(get("audio").hasAttribute("data-preview-output")).toBe(true);
  get("next").click();
  expect(get("audio").currentTime).toBe(12);
  expect(get("items").textContent).toContain("Programme 5:00");
  expect(request.mock.calls.every(([, options]) => !options?.method)).toBe(true);
});

it("generates explicitly, polls, and preserves the old snapshot after cancellation", async () => {
  let cancelled = false;
  const request = vi.fn(async (url, options) => {
    if (url === "/api/shows") return { shows: [{ id: "show-1", name: "Tomorrow" }] };
    if (url.endsWith("/cancel")) { cancelled = true; return { id: "job-1", status: "cancelled" }; }
    if (options?.method === "POST") return { id: "job-1", status: "running" };
    if (url.includes("/jobs/")) return { id: "job-1", status: "running" };
    if (url === "/api/reviews") return { results: [result], job: null };
    return result;
  });
  const { controls, timers } = harness({ request });
  await controls.refresh(); get("load").click(); await settle();
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  const body = JSON.parse(request.mock.calls.find(([, o]) => o?.method === "POST")[1].body);
  expect(body).toMatchObject({ source: "show", show_id: "show-1", head_seconds: 10, tail_seconds: 10, clip_threshold_seconds: 300, tone_seconds: .25 });
  expect(timers.length).toBe(1);
  await timers[0](); await settle();
  get("cancel").click(); await settle();
  expect(cancelled).toBe(true);
  expect(get("audio").getAttribute("src")).toContain("snapshot-1");
  expect(get("cancel").hidden).toBe(true);
});

it("rejects invalid timing with focus and announces server TTS failure once", async () => {
  const { controls, request, spoken } = harness();
  await controls.refresh();
  get("head").value = "0";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  expect(document.activeElement).toBe(get("head"));
  expect(get("head").getAttribute("aria-invalid")).toBe("true");
  expect(request.mock.calls.some(([, o]) => o?.method === "POST")).toBe(false);
  get("head").value = "10";
  request.mockRejectedValueOnce(new Error("Configure default TTS for missing ID"));
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  expect(spoken.filter((s) => s.includes("Configure default TTS"))).toHaveLength(1);
});

// ADR-0014-05
it("clears audio and drops delayed replies when station context changes", async () => {
  let resolve;
  const delayed = new Promise((done) => { resolve = done; });
  const { controls, request } = harness();
  await controls.refresh(); get("load").click(); await settle();
  request.mockImplementationOnce(() => delayed);
  const pending = controls.refresh();
  controls.clear();
  resolve({ results: [result], job: null });
  await pending;
  expect(get("audio").hasAttribute("src")).toBe(false);
  expect(get("result").hidden).toBe(true);
  expect(get("snapshots").options.length).toBe(0);
});

it("loads the completed frozen result and refresh marks it stale without changing its audio", async () => {
  let complete = false;
  const clean = { ...result, stale: false, incomplete: false, omissions: [] };
  const request = vi.fn(async (url, options) => {
    if (url === "/api/shows") return { shows: [] };
    if (options?.method === "POST") return { id: "job", status: "running" };
    if (url.includes("/jobs/")) { complete = true; return { id: "job", status: "complete", result_id: result.id }; }
    if (url === "/api/reviews") return { results: complete ? [clean] : [], job: null };
    return { ...clean };
  });
  const { controls, timers } = harness({ request });
  get("source").value = "forecast";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  await timers[0](); await settle();
  expect(get("result").hidden).toBe(false);
  expect(get("summary").textContent).toContain("Complete");
  const src = get("audio").src;
  clean.stale = true;
  await controls.refresh();
  expect(get("summary").textContent).toContain("Out of date");
  expect(get("audio").src).toBe(src);
  expect(request.mock.calls.every(([url]) => url.startsWith("/api/reviews") || url === "/api/shows")).toBe(true);
});

it("deletion is explicit and does not generate a replacement", async () => {
  const { controls, request } = harness();
  await controls.refresh();
  get("load").click(); await settle();
  get("delete").click(); await settle();
  expect(request.mock.calls.filter(([, options]) => options?.method === "DELETE")).toHaveLength(1);
  expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  expect(get("audio").hasAttribute("src")).toBe(false);
});

it("does not announce ready after completed-result loading fails", async () => {
  const request = vi.fn(async (url, options) => {
    if (options?.method === "POST") return { id: "job", status: "done", result_id: result.id };
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    throw new Error("Review download is unavailable");
  });
  const { spoken } = harness({ request });
  get("source").value = "forecast";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  expect(spoken).toContain("Review download is unavailable");
  expect(spoken.some((text) => text.startsWith("Review ready"))).toBe(false);
});

it("does not announce ready when station context clears during completed-result loading", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const request = vi.fn(async (url, options) => {
    if (options?.method === "POST") return { id: "job", status: "done", result_id: result.id };
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return pending;
  });
  const { controls, spoken } = harness({ request });
  get("source").value = "forecast";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  controls.clear(); resolve(result); await settle();
  expect(spoken.some((text) => text.startsWith("Review ready"))).toBe(false);
  expect(get("audio").hasAttribute("src")).toBe(false);
});
