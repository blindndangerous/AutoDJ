import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  installHourPreview,
  renderPoints,
  renderRows,
  rowCells,
  spokenSummary,
} from "../../src/autodj/static/modules/hour-preview.js";

const byId = (id) => document.getElementById(id);

function item(title, status, fixed, starts = ["12:09:10 PM", "12:09:10 PM"]) {
  return {
    kind: "song",
    status,
    fixed,
    status_text: fixed ? `Fixed: ${status.replace("_", " ")}` : "Predicted",
    title,
    starts_text: starts[0],
    starts_words: starts[1],
    length_text: "3:20",
    length_words: "3:20",
  };
}

function view(overrides = {}) {
  return {
    minutes: 60,
    reason: null,
    summary: "Top of the hour, 1:00 PM: lands about 0:35 early. Next hour: 2 fixed and 1 predicted, until 1:10 PM.",
    items: [
      item("Gold 0, Song 100", "on_air", true),
      item("Gold 1, Song 101", "picked", true, ["12:12:30 PM", "12:12:30 PM"]),
      item("Gold 9, Song 109", "predicted", false, ["12:15:50 PM", "12:15:50 PM"]),
    ],
    points: [{ kind: "hour", text: "Top of the hour, 1:00 PM: lands about 0:35 early." }],
    notes: ["Predicted songs come from a dry run."],
    stopped: null,
    ...overrides,
  };
}

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

function harness(answers) {
  const timers = [];
  const request = vi.fn(async () => {
    const answer = answers.shift();
    if (answer instanceof Error) throw answer;
    return answer;
  });
  const spoken = [];
  let clock = 1_000_000;
  const controls = installHourPreview({
    request,
    speak: (text) => spoken.push(text),
    setTimer: (fn, ms) => {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearTimer: (id) => {
      if (timers[id - 1]) timers[id - 1].cleared = true;
    },
    now: () => clock,
  });
  return {
    controls,
    request,
    spoken,
    timers,
    advance: (ms) => { clock += ms; },
    live: () => timers.filter((timer) => !timer.cleared),
  };
}

async function settle() {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
}

async function open() {
  const card = byId("hour-preview-card");
  card.open = true;
  card.dispatchEvent(new Event("toggle"));
  await settle();
}

describe("hour preview rendering", () => {
  it("uses the same numeric text for both time columns and assistive technology", () => {
    const cells = rowCells(item("Gold 0, Song 100", "on_air", true));
    expect(cells).toEqual([
      "12:09:10 PM",
      "Gold 0, Song 100",
      "3:20",
      "Fixed: on air",
    ]);
    const tbody = document.createElement("tbody");
    renderRows(document, tbody, [item("Gold 0, Song 100", "on_air", true)]);
    expect(tbody.children[0].children[0].textContent).toBe("12:09:10 PM");
    expect(tbody.children[0].children[2].textContent).toBe("3:20");
    expect(tbody.querySelector("[aria-hidden], .visually-hidden")).toBeNull();
    expect(tbody.children[0].className).toBe("is-fixed is-on-air");
  });

  it("changes only the cells whose text changed, keeping the rows", () => {
    const tbody = document.createElement("tbody");
    const first = view().items;
    renderRows(document, tbody, first);
    const rows = [...tbody.children];
    const titleCell = rows[1].children[1];
    const timeSpan = rows[1].children[0].firstChild;
    const changed = first.map((entry, index) => (index === 2
      ? { ...entry, title: "Gold 12, Song 112" }
      : entry));
    renderRows(document, tbody, changed);
    expect([...tbody.children]).toEqual(rows);
    expect(rows[1].children[1]).toBe(titleCell);
    expect(rows[1].children[0].firstChild).toBe(timeSpan);
    expect(rows[2].children[1].textContent).toBe("Gold 12, Song 112");
    renderRows(document, tbody, changed.slice(0, 1));
    expect(tbody.children).toHaveLength(1);
    expect(tbody.children[0]).toBe(rows[0]);
  });

  it("marks predicted rows in words as well as by style", () => {
    const tbody = document.createElement("tbody");
    renderRows(document, tbody, view().items);
    const last = tbody.children[2];
    expect(last.className).toBe("is-predicted");
    expect(last.children[3].textContent).toBe("Predicted");
  });

  it("keeps list items that did not change", () => {
    const list = document.createElement("ul");
    renderPoints(document, list, [{ text: "A" }, { text: "B" }]);
    const kept = list.children[0];
    renderPoints(document, list, [{ text: "A" }]);
    expect(list.children).toHaveLength(1);
    expect(list.children[0]).toBe(kept);
  });

  it("says the reason when there is nothing to time", () => {
    expect(spokenSummary({ reason: "Nothing is playing." })).toBe("Nothing is playing.");
    expect(spokenSummary(null)).toBe("No hour preview yet.");
  });
});

describe("hour preview card", () => {
  beforeEach(() => {
    installIndex();
    byId("panel-now").hidden = false;
  });

  it("asks for nothing until the card is opened, then fills it quietly", async () => {
    const { request, timers } = harness([view()]);
    expect(request).not.toHaveBeenCalled();
    await open();
    expect(request).toHaveBeenCalledWith("/api/hour-preview?minutes=60");
    expect(byId("hour-preview-table").hidden).toBe(false);
    expect(byId("hour-preview-tbody").children).toHaveLength(3);
    expect(byId("hour-preview-summary").textContent).toContain("lands about 0:35 early");
    expect(byId("hour-preview-backtiming").hidden).toBe(false);
    expect(byId("hour-preview-status").textContent).toBe("");
    expect(timers.at(-1).ms).toBe(30000);
  });

  it("refreshes on its timer without speaking or moving focus", async () => {
    const { request, timers } = harness([view(), view({ summary: "Changed." })]);
    await open();
    const button = byId("hour-preview-refresh");
    button.focus();
    await timers.at(-1).fn();
    await settle();
    expect(request).toHaveBeenCalledTimes(2);
    expect(byId("hour-preview-summary").textContent).toBe("Changed.");
    expect(byId("hour-preview-status").textContent).toBe("");
    expect(document.activeElement).toBe(button);
  });

  it("does not refresh while Now Playing is hidden", async () => {
    const { request, timers } = harness([view()]);
    await open();
    byId("panel-now").hidden = true;
    await timers.at(-1).fn();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("says the summary once when Refresh preview is pressed", async () => {
    vi.useFakeTimers();
    try {
      const { request } = harness([view(), view()]);
      await open();
      byId("hour-preview-refresh").click();
      await settle();
      await vi.advanceTimersByTimeAsync(1);
      expect(request).toHaveBeenLastCalledWith("/api/hour-preview?minutes=60&refresh=true");
      expect(byId("hour-preview-status").textContent).toBe(
        `Hour preview updated. ${view().summary}`,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a failed quiet refresh once, and a failed Refresh preview each time", async () => {
    vi.useFakeTimers();
    try {
      const failure = new Error("The AutoDJ server is not reachable.");
      const words = "The hour preview could not be loaded: The AutoDJ server is not reachable.";
      const { timers } = harness([failure, failure, failure, view(), failure]);
      await open();
      expect(byId("hour-preview-summary").textContent).toBe(words);
      expect(byId("hour-preview-status").textContent).toBe(words);
      byId("hour-preview-status").textContent = "";
      // The next timed refresh fails the same way: nothing is said again.
      await timers.at(-1).fn();
      await settle();
      expect(byId("hour-preview-status").textContent).toBe("");
      // Refresh preview is the user's own ask: its failure is said.
      byId("hour-preview-refresh").click();
      await settle();
      await vi.advanceTimersByTimeAsync(1);
      expect(byId("hour-preview-status").textContent).toBe(words);
      byId("hour-preview-status").textContent = "";
      // A success, then a new failure, is said again.
      await timers.at(-1).fn();
      await settle();
      await timers.at(-1).fn();
      await settle();
      expect(byId("hour-preview-status").textContent).toBe(words);
    } finally {
      vi.useRealTimers();
    }
  });

  it("asks for the chosen span and says its summary once", async () => {
    vi.useFakeTimers();
    try {
      const { request } = harness([view(), view({ minutes: 120, summary: "Two hours." })]);
      await open();
      const span = byId("hour-preview-span");
      span.value = "120";
      span.dispatchEvent(new Event("change"));
      await settle();
      await vi.advanceTimersByTimeAsync(1);
      expect(request).toHaveBeenLastCalledWith("/api/hour-preview?minutes=120");
      expect(byId("hour-preview-status").textContent).toBe("Showing the next 2 hours. Two hours.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("only the newest span is said when one overtakes another", async () => {
    vi.useFakeTimers();
    try {
      let release;
      const slow = new Promise((resolve) => { release = resolve; });
      const answers = [view(), slow, view({ summary: "Three hours." })];
      const request = vi.fn(() => answers.shift());
      installHourPreview({ request, setTimer: () => 1, clearTimer: () => {} });
      await open();
      const span = byId("hour-preview-span");
      span.value = "120";
      span.dispatchEvent(new Event("change"));
      span.value = "180";
      span.dispatchEvent(new Event("change"));
      await settle();
      release(view({ summary: "Two hours." }));
      await settle();
      await vi.advanceTimersByTimeAsync(1);
      expect(byId("hour-preview-status").textContent).toBe("Showing the next 3 hours. Three hours.");
      expect(byId("hour-preview-summary").textContent).toBe("Three hours.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says when more fixed points fall in the span than are timed", async () => {
    harness([view({ points_note: "Only the first 24 of 30 fixed points are timed." })]);
    await open();
    const note = byId("hour-preview-points-note");
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe("Only the first 24 of 30 fixed points are timed.");
    expect(note.closest("[aria-live], [role=status]")).toBeNull();
  });

  it("refreshes soon after the song changes", async () => {
    const { controls, request, timers } = harness([view(), view()]);
    await open();
    const state = (path) => ({ current_track: { path }, next_track: null, queue: [] });
    const songTimers = () => timers.filter((timer) => timer.ms === 2000);
    // The first push only records what is playing; the same song again does nothing.
    controls.noteState(state("/a.flac"));
    controls.noteState(state("/a.flac"));
    expect(songTimers()).toHaveLength(0);
    controls.noteState(state("/b.flac"));
    expect(songTimers()).toHaveLength(1);
    songTimers()[0].fn();
    await settle();
    expect(request).toHaveBeenCalledTimes(2);
    expect(byId("hour-preview-status").textContent).toBe("");
  });

  it("forces a quiet refresh after a seek", async () => {
    const { controls, request, timers } = harness([view(), view()]);
    await open();
    controls.noteSeek();
    const seekTimer = timers.filter((timer) => timer.ms === 2000).at(-1);
    seekTimer.fn();
    await settle();
    expect(request).toHaveBeenLastCalledWith("/api/hour-preview?minutes=60&refresh=true");
    expect(byId("hour-preview-status").textContent).toBe("");
  });

  it("keeps the newest seek result visible and confirms the explicit refresh", async () => {
    vi.useFakeTimers();
    try {
      let finishManual;
      let finishQuiet;
      const manual = new Promise((resolve) => { finishManual = resolve; });
      const quiet = new Promise((resolve) => { finishQuiet = resolve; });
      const { controls, request, timers } = harness([view(), manual, quiet]);
      await open();
      byId("hour-preview-refresh").click();
      await settle();
      controls.noteSeek();
      timers.filter((timer) => timer.ms === 2000).at(-1).fn();
      await settle();
      expect(request).toHaveBeenCalledTimes(3);
      finishQuiet(view({ summary: "Quiet seek result." }));
      await settle();
      finishManual(view({ summary: "Manual refresh result." }));
      await settle();
      await vi.advanceTimersByTimeAsync(1);
      expect(byId("hour-preview-summary").textContent).toBe("Quiet seek result.");
      expect(byId("hour-preview-status").textContent).toContain(
        "Hour preview updated. Quiet seek result.",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("speaks a recent preview on Shift+H, or asks for a new one", async () => {
    const { controls, request, spoken, advance } = harness([view(), view({ summary: "Later." })]);
    await open();
    await controls.speakSummary();
    expect(spoken).toEqual([view().summary]);
    expect(request).toHaveBeenCalledTimes(1);
    advance(21000);
    await controls.speakSummary();
    expect(spoken.at(-1)).toBe("Later.");
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("forgets the preview on sign-out", async () => {
    const { controls } = harness([view()]);
    await open();
    controls.clear();
    expect(byId("hour-preview-tbody").children).toHaveLength(0);
    expect(byId("hour-preview-table").hidden).toBe(true);
    expect(byId("hour-preview-summary").textContent).toBe("Open this card to see the hour ahead.");
  });
});
