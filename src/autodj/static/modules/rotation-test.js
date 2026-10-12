// Settings > Song picking > Test rotation: a dry run of song picking (#125).
//
// The server runs the simulation on a low-priority thread of its own
// (rotation_api.py); this module starts it, follows its progress and shows
// the report.  Nothing on air changes.
//
// Both buttons stay enabled and in place throughout, so focus never lands
// on a control that disappears or goes dead; a press that cannot act says
// why.  The start and the end of a run are each said once, politely,
// through #rotation-status.  Progress is plain text beside a native
// progress bar, never a live region, so a long run is not read out every
// second.  The results are headings and lists (no tables), filled in
// without moving focus.
import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const POLL_MS = 1000;
// A progress check that fails is tried again later each time, up to
// MAX_POLL_FAILURES in a row; the first failure and the last are said.
const MAX_POLL_FAILURES = 6;
const MAX_POLL_DELAY_MS = 16000;
const ENDPOINT = "/api/rotation-test";

const number = (n) => Number(n || 0).toLocaleString("en-US");

// The request body for the chosen span ("hours:24", "days:7", "picks:500"),
// the optional start (a datetime-local value, read in the station time
// zone by the server) and the optional seed.
export function requestBody(span, start = "", seed = "") {
  const [unit, amount] = String(span).split(":");
  const body = { [unit]: Number(amount) };
  if (start) body.start = start;
  if (String(seed).trim() !== "") body.seed = Number(seed);
  return body;
}

// Why the seed box cannot be sent, or null when it can.
export function seedProblem(seed) {
  const text = String(seed).trim();
  if (text === "") return null;
  return /^\d{1,15}$/.test(text) ? null : "Seed must be a whole number, 0 or more, or empty.";
}

// What the status region says once a run ends.
export function endMessage(view) {
  const picks = view?.report?.picks ?? view?.progress?.picks ?? 0;
  if (view?.state === "done" && view?.report?.stopped_reason === "time limit") {
    return `Rotation test stopped at its time limit after ${number(picks)} songs. The results so far are under Rotation test results.`;
  }
  if (view?.state === "done") {
    return `Rotation test finished: ${number(picks)} songs picked. The results are under Rotation test results.`;
  }
  if (view?.state === "cancelled") {
    return `Rotation test cancelled after ${number(picks)} songs. The results so far are under Rotation test results.`;
  }
  if (view?.state === "failed") return `Rotation test failed: ${view.error || "no reason was given"}`;
  return "";
}

// Said once when the page gives up checking on a run.
export function stoppedFollowingMessage(tries, error) {
  return `Stopped checking the rotation test after ${tries} failed tries: ${error?.message || "no reason was given"} It may still be running on the server; reload the page to see its results.`;
}

// The progress line beside the bar: plain text, never announced by itself.
export function progressText(view) {
  const percent = Number(view?.progress?.percent || 0);
  return `${number(view?.progress?.picks)} songs picked, ${percent} percent done.`;
}

// The report's sections (rotation_sim.report_sections) as an <h5> and a
// list each; a line with items gets a nested list.
export function renderSections(doc, container, sections) {
  container.replaceChildren();
  for (const section of sections || []) {
    const heading = doc.createElement("h5");
    heading.textContent = section.title;
    const list = doc.createElement("ul");
    for (const line of section.lines || []) {
      const item = doc.createElement("li");
      if (typeof line === "string") {
        item.textContent = line;
      } else {
        item.append(doc.createTextNode(line.text));
        const inner = doc.createElement("ul");
        for (const text of line.items || []) {
          const entry = doc.createElement("li");
          entry.textContent = text;
          inner.append(entry);
        }
        item.append(inner);
      }
      list.append(item);
    }
    container.append(heading, list);
  }
}

export function installRotationTest({
  doc = document,
  request = requestJson,
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
} = {}) {
  const get = (id) => doc.getElementById(`rotation-${id}`);
  const runButton = get("run");
  const cancelButton = get("cancel");
  const span = get("span");
  const start = get("start");
  const seed = get("seed");
  const status = get("status");
  const errorText = get("error");
  const progressRow = get("progress-row");
  const bar = get("progress");
  const barText = get("progress-text");
  const results = get("results");
  const resultsBody = get("results-body");
  if (!runButton || !cancelButton || !span || !status || !results || !resultsBody) {
    return { refresh: async () => {}, clear() {} };
  }
  let running = false;
  let timer = null;
  // Bumped by clear(): an answer for an older session is dropped.
  let session = 0;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 8000 : 6000, tone });
  }

  function markSeed(problem) {
    if (problem) {
      seed.setAttribute("aria-invalid", "true");
      seed.setAttribute("aria-describedby", "rotation-seed-desc rotation-error");
    } else {
      seed.removeAttribute("aria-invalid");
      seed.setAttribute("aria-describedby", "rotation-seed-desc");
    }
    errorText.textContent = problem || "";
    errorText.hidden = !problem;
  }

  function stopPolling() {
    if (timer !== null) clearTimer(timer);
    timer = null;
  }

  function show(view) {
    running = view?.state === "running";
    progressRow.hidden = !running;
    if (running) {
      bar.value = Number(view.progress?.percent || 0);
      barText.textContent = progressText(view);
    }
    if (!running && view?.sections) {
      renderSections(doc, resultsBody, view.sections);
      results.hidden = false;
    }
  }

  function follow(mine, failures = 0) {
    stopPolling();
    const delay = Math.min(POLL_MS * 2 ** failures, MAX_POLL_DELAY_MS);
    timer = setTimer(async () => {
      timer = null;
      let view;
      try {
        view = await request(ENDPOINT);
      } catch (error) {
        if (mine !== session) return;
        const failed = failures + 1;
        if (failed >= MAX_POLL_FAILURES) {
          running = false;
          progressRow.hidden = true;
          say(stoppedFollowingMessage(failed, error), "error");
          return;
        }
        if (failed === 1) say(`Could not check the rotation test: ${error.message} Trying again.`, "error");
        follow(mine, failed);
        return;
      }
      if (mine !== session) return;
      show(view);
      if (view.state === "running") follow(mine);
      else say(endMessage(view), view.state === "failed" ? "error" : "info");
    }, delay);
  }

  async function run() {
    if (running) {
      say("A rotation test is already running. Cancel it first to start another.");
      return;
    }
    const problem = seedProblem(seed.value);
    markSeed(problem);
    if (problem) {
      seed.focus();
      say(problem, "error");
      return;
    }
    const mine = session;
    let view;
    try {
      view = await request(ENDPOINT, {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(requestBody(span.value, start?.value || "", seed.value)),
      });
    } catch (error) {
      if (mine === session) say(`The rotation test could not start: ${error.message}`, "error");
      return;
    }
    if (mine !== session) return;
    say("Rotation test started. It runs in the background; nothing on air changes.");
    show(view);
    if (view.state === "running") follow(mine);
    else say(endMessage(view));
  }

  async function cancel() {
    if (!running) {
      say("No rotation test is running.");
      return;
    }
    try {
      await request(ENDPOINT, { method: "DELETE" });
    } catch (error) {
      say(`The rotation test could not be cancelled: ${error.message}`, "error");
      return;
    }
    say("Cancelling the rotation test.");
  }

  runButton.addEventListener("click", () => { void run(); });
  cancelButton.addEventListener("click", () => { void cancel(); });
  seed.addEventListener("input", () => {
    if (seed.getAttribute("aria-invalid") === "true") markSeed(seedProblem(seed.value));
  });

  // After a page load or a new sign-in: follow a run already going, and
  // show the last results quietly.
  async function refresh() {
    const mine = session;
    let view;
    try {
      view = await request(ENDPOINT);
    } catch (_) {
      return;
    }
    if (mine !== session) return;
    show(view);
    if (view.state === "running") follow(mine);
  }

  function clear() {
    session += 1;
    stopPolling();
    running = false;
    progressRow.hidden = true;
    resultsBody.replaceChildren();
    results.hidden = true;
    markSeed(null);
  }

  return { refresh, clear };
}
