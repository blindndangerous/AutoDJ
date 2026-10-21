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

it("requests a complete pending edition target and exposes explicit approval readiness", async () => {
  const ready = vi.fn();
  document.addEventListener("autodj:edition-review-ready", ready, { once: true });
  const request = vi.fn(async (url, options) => {
    if (options?.method === "POST") return { id: "job", status: "done", result_id: "review" };
    if (url === "/api/shows") return { shows: [] };
    if (url === "/api/reviews") return { results: [], job: null };
    return { ...result, id: "review", source: "edition", edition_id: "edition", target_take_id: "take",
      edition_revision: 3, incomplete: false, approval_review_complete: true };
  });
  harness({ request });
  document.dispatchEvent(new CustomEvent("autodj:review-edition", { detail: {
    edition_id: "edition", take_id: "take", include_pending: true,
  } }));
  await settle();
  const submitted = JSON.parse(request.mock.calls.find(([, options]) => options?.method === "POST")[1].body);
  expect(submitted).toMatchObject({ source: "edition", edition_id: "edition", target_take_id: "take",
    include_pending: true, selection_start: null, selection_end: null });
  expect(ready).toHaveBeenCalledOnce();
  expect(ready.mock.calls[0][0].detail).toMatchObject({ edition_id: "edition", take_id: "take", review_id: "review", revision: 3 });
  expect(request.mock.calls.some(([url]) => url.includes("/approve"))).toBe(false);
});

it("does not expose approval readiness for an incomplete target review", async () => {
  const ready = vi.fn();
  document.addEventListener("autodj:edition-review-ready", ready, { once: true });
  const request = vi.fn(async (url) => url === "/api/reviews" ? { results: [result], job: null }
    : url === "/api/shows" ? { shows: [] }
      : { ...result, source: "edition", target_take_id: "take", approval_review_complete: false });
  const { controls } = harness({ request });
  await controls.refresh(); get("load").click(); await settle();
  expect(ready).not.toHaveBeenCalled();
  document.removeEventListener("autodj:edition-review-ready", ready);
});

it.each(["load", "delete"])("announces when %s has no saved review selected", async (name) => {
  const { controls, spoken } = harness();
  await controls.refresh();
  get("snapshots").selectedIndex = -1;
  get(name).click();
  await settle();
  expect(get("status").textContent).toBe("Choose a saved review first.");
  expect(spoken).toContain("Choose a saved review first.");
});

it("formats saved dates for display", async () => {
  const created_at = "2024-10-17T12:00:00Z";
  const dated = { ...result, created_at };
  const request = vi.fn(async (url) => url === "/api/reviews" ? { results: [dated], job: null }
    : url === "/api/shows" ? { shows: [] } : dated);
  const { controls } = harness({ request });
  const expected = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" })
    .format(new Date("2024-10-17T12:00:00Z"));
  await controls.refresh();
  expect(get("snapshots").options[0].textContent).toContain(expected);
  get("load").click(); await settle();
  expect(get("summary").textContent).toContain(expected);
  get("delete").click(); await settle();
  expect(document.getElementById("confirm-title").textContent).toContain(expected);
});

it("distinguishes reviews created in the same minute", async () => {
  const request = vi.fn(async (url) => url === "/api/reviews" ? { results: [
    { ...result, created_at: "2026-10-17T12:00:01Z" },
    { ...result, id: "snapshot-2", created_at: "2026-10-17T12:00:45Z" },
  ], job: null } : { shows: [] });
  const { controls } = harness({ request });
  await controls.refresh();
  const labels = [...get("snapshots").options].map((option) => option.textContent);
  expect(labels[0]).not.toBe(labels[1]);
});

it("announces a late field error without stealing focus outside programme review", async () => {
  const request = vi.fn(async (url, options) => options?.method === "POST"
    ? { id: "job", status: "running" }
    : { id: "job", status: "failed", error: "Correct the intended start", field: "start" });
  const { timers, spoken } = harness({ request });
  get("source").value = "forecast";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  const outside = document.createElement("button");
  outside.textContent = "Other control"; document.body.append(outside); outside.focus();
  await timers[0](); await settle();
  expect(document.activeElement).toBe(outside);
  expect(get("start").getAttribute("aria-invalid")).toBe("true");
  expect(spoken).toContain("Correct the intended start");
});

it.each(["start", "show_id"])("focuses the %s field after a worker validation error", async (field) => {
  const request = vi.fn(async (url, options) => options?.method === "POST"
    ? { id: "job", status: "failed", error: "Correct the input", field }
    : url === "/api/shows" ? { shows: [{ id: "show-1", name: "Tomorrow" }] } : { results: [], job: null });
  const { controls } = harness({ request });
  await controls.refresh();
  get("source").value = field === "show_id" ? "show" : "forecast";
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  const input = get(field === "show_id" ? "show" : "start");
  expect(input.getAttribute("aria-invalid")).toBe("true");
  expect(document.activeElement).toBe(input);
});

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
  expect(document.activeElement).toBe(get("heading"));
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
  expect(get("generate").disabled).toBe(true);
  await timers[0](); await settle();
  get("cancel").click(); await settle();
  expect(cancelled).toBe(true);
  expect(get("audio").getAttribute("src")).toContain("snapshot-1");
  expect(get("cancel").hidden).toBe(true);
  expect(get("generate").disabled).toBe(false);
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

it("confirms the dated deletion and leaves no replacement selected", async () => {
  let deleted = false;
  const request = vi.fn(async (url, options) => {
    if (options?.method === "DELETE") { deleted = true; return {}; }
    if (url === "/api/reviews") return { results: deleted ? [{ ...result, id: "other" }] : [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return result;
  });
  const { controls } = harness({ request });
  await controls.refresh();
  get("load").click(); await settle();
  get("delete").click(); await settle();
  expect(request.mock.calls.filter(([, options]) => options?.method === "DELETE")).toHaveLength(0);
  expect(document.getElementById("confirm-title").textContent).toContain(
    new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(result.created_at)),
  );
  expect(document.getElementById("confirm-message").textContent).toContain("backups");
  document.getElementById("confirm-cancel").click(); await settle();
  expect(deleted).toBe(false);
  get("delete").click(); await settle();
  document.getElementById("confirm-accept").click(); await settle();
  expect(request.mock.calls.filter(([, options]) => options?.method === "DELETE")).toHaveLength(1);
  expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  expect(get("audio").hasAttribute("src")).toBe(false);
  expect(get("snapshots").selectedIndex).toBe(-1);
  await controls.refresh();
  expect(get("snapshots").selectedIndex).toBe(-1);
});

it("associates start and range format hints with their inputs", () => {
  for (const name of ["start", "selection-start", "selection-end"]) {
    const ids = get(name).getAttribute("aria-describedby");
    expect(ids).toBeTruthy();
    for (const id of ids.split(/\s+/)) expect(document.getElementById(id)?.textContent.trim()).toBeTruthy();
  }
});

it("does not scan reviews when a station refreshes with its card closed", async () => {
  const { controls, request } = harness();
  await controls.refresh({ ifOpen: true });
  expect(request).not.toHaveBeenCalled();
});

it("discards a pending load when its review is deleted", async () => {
  let finishLoad, deleted = false;
  const pending = new Promise((resolve) => { finishLoad = resolve; });
  const request = vi.fn(async (url, options) => {
    if (options?.method === "DELETE") { deleted = true; return {}; }
    if (url === "/api/reviews") return { results: deleted ? [] : [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return pending;
  });
  const { controls } = harness({ request });
  await controls.refresh();
  get("load").click(); await settle();
  get("delete").click(); await settle();
  document.getElementById("confirm-accept").click(); await settle();
  finishLoad(result); await settle();
  expect(get("result").hidden).toBe(true);
  expect(get("audio").hasAttribute("src")).toBe(false);
  expect(get("snapshots").selectedIndex).toBe(-1);
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

it.each(["done", "cancelled", "failed"])("moves keyboard focus to Cancel and back after %s", async (status) => {
  const request = vi.fn(async (url, options) => {
    if (options?.method === "POST") return { id: "job", status: "running" };
    if (url.includes("/jobs/")) return { id: "job", status, result_id: result.id, error: "Review timed out" };
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return result;
  });
  const { timers } = harness({ request });
  get("source").value = "forecast";
  get("generate").focus();
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  expect(document.activeElement).toBe(get("cancel"));
  expect(get("generate").disabled).toBe(true);
  await timers[0](); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(document.activeElement).toBe(get("generate"));
});

it.each([false, true])("leaves focus elsewhere untouched during generation, moved after start: %s", async (moveAfterStart) => {
  const request = vi.fn(async (url, options) => {
    if (options?.method === "POST") return { id: "job", status: "running" };
    if (url.includes("/jobs/")) return { id: "job", status: "done", result_id: result.id };
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return result;
  });
  const { timers } = harness({ request });
  get("source").value = "forecast";
  (moveAfterStart ? get("generate") : get("head")).focus();
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  if (moveAfterStart) get("head").focus();
  expect(document.activeElement).toBe(get("head"));
  await timers[0](); await settle();
  expect(document.activeElement).toBe(get("head"));
});

// The server answers Cancel with the job still running when its renderer is
// between cancellation checks.  Generate stayed unavailable and Cancel kept
// offering itself, with nothing said, until the worker stopped.
function cancelHarness(cancelReply) {
  const calls = [];
  let poll = { id: "job", status: "running" };
  const request = vi.fn(async (url, options) => {
    calls.push([url, options?.method]);
    if (url.endsWith("/cancel")) return cancelReply();
    if (options?.method === "POST") return { id: "job", status: "running" };
    if (url.includes("/jobs/")) return poll;
    if (url === "/api/reviews") return { results: [result], job: null };
    if (url === "/api/shows") return { shows: [] };
    return result;
  });
  const setPoll = (value) => { poll = value; };
  return { calls, request, setPoll };
}

async function startReview(request) {
  const harnessed = harness({ request });
  get("source").value = "forecast";
  get("generate").focus();
  get("form").dispatchEvent(new Event("submit", { cancelable: true })); await settle();
  expect(document.activeElement).toBe(get("cancel"));
  return harnessed;
}

it("says Cancel was accepted and resets the buttons once the worker stops", async () => {
  const { request, setPoll, calls } = cancelHarness(() => ({ id: "job", status: "running", cancel_requested: true }));
  const { timers, spoken } = await startReview(request);
  get("cancel").click(); await settle();
  expect(get("cancel").getAttribute("aria-disabled")).toBe("true");
  expect(get("generate").disabled).toBe(true);
  expect(spoken.at(-1)).toBe("Cancelling review generation…");
  get("cancel").click(); await settle();
  expect(calls.filter(([url]) => url.endsWith("/cancel"))).toHaveLength(1);
  expect(spoken.at(-1)).toBe("Cancellation already requested. Waiting for the review to stop.");
  setPoll({ id: "job", status: "cancelled", error: "Review cancelled" });
  await timers.at(-1)(); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(get("cancel").hidden).toBe(true);
  expect(get("cancel").hasAttribute("aria-disabled")).toBe(false);
  expect(document.activeElement).toBe(get("generate"));
  expect(spoken.at(-1)).toBe("Review cancelled. Previous results are retained.");
});

it("resets at once when the server answers Cancel with the stopped job", async () => {
  const { request } = cancelHarness(() => ({ id: "job", status: "cancelled", error: "Review cancelled" }));
  const { spoken } = await startReview(request);
  get("cancel").click(); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(get("cancel").hidden).toBe(true);
  expect(document.activeElement).toBe(get("generate"));
  expect(spoken.at(-1)).toBe("Review cancelled. Previous results are retained.");
});

it("keeps Cancel available and polling after a failed cancel request", async () => {
  const { request, setPoll } = cancelHarness(() => { throw new Error("Review worker is busy or shutting down"); });
  const { timers, spoken } = await startReview(request);
  const before = timers.length;
  get("cancel").click(); await settle();
  expect(spoken.at(-1)).toBe("Could not cancel the review: Review worker is busy or shutting down");
  expect(get("cancel").hidden).toBe(false);
  expect(get("cancel").hasAttribute("aria-disabled")).toBe(false);
  expect(get("generate").disabled).toBe(true);
  expect(timers.length).toBe(before);
  setPoll({ id: "job", status: "done", result_id: result.id });
  await timers.at(-1)(); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(get("cancel").hidden).toBe(true);
});

it("resets when Cancel finds the job already gone", async () => {
  const { request } = cancelHarness(() => {
    throw Object.assign(new Error("Review not found"), { status: 404 });
  });
  const { spoken } = await startReview(request);
  get("cancel").click(); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(get("cancel").hidden).toBe(true);
  expect(spoken.at(-1)).toBe("The review had already stopped. Previous results are retained.");
});

it("reports a review that finished before Cancel reached it", async () => {
  const { request } = cancelHarness(() => ({ id: "job", status: "done", result_id: result.id }));
  const { spoken } = await startReview(request);
  get("cancel").click(); await settle();
  expect(get("generate").disabled).toBe(false);
  expect(get("cancel").hidden).toBe(true);
  expect(spoken.at(-1)).toMatch(/^Review ready/);
});
