import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeStep, installClockwheelPanel, parseResyncMinutes } from "../../src/autodj/static/modules/clockwheel-panel.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => byId("clock-status").textContent;
const rows = () => [...byId("clock-slots").querySelectorAll("li[data-index]")];
const rowText = (index) => rows()[index].querySelector('button[data-action="edit"]').textContent;
const rowButton = (index, action) => rows()[index].querySelector(`button[data-action="${action}"]`);
// Keyboard focus rests on the grid cell around each button.
const rowCell = (index, action) => rows()[index].querySelector(`[role="gridcell"][data-action="${action}"]`);

const SOURCES = [
  { id: "rock", kind: "music", path: "D:\\Music\\Rock", recursive: true },
  { id: "jazz", kind: "music", path: "D:\\Music\\Jazz", recursive: true },
  { id: "ids", kind: "liner", path: "D:\\Liners", recursive: true },
];
const MUSIC = { type: "music", source: "rock", folder: "", recursive: false, selection: "least_recent", enforce_rules: true };
const MORNING = {
  version: 1, name: "Morning", resync_minutes: [0],
  steps: [
    MUSIC,
    { type: "liner", source: "ids", folder: "Top", recursive: false, selection: "random", enforce_rules: false },
    { type: "track", source: "jazz", path: "Album/Song.flac" },
  ],
};

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  byId("panel-library").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

// A stand-in for the clockwheel routes, holding files the way the server does.
function fakeServer({
  notice = null,
  categories = [],
  genres = ["Rock", "Jazz"],
  beets_fields = ["mood"],
  beets_error = null,
  pending_update = false,
  active = "Morning",
} = {}) {
  const server = { clocks: { Morning: copy(MORNING) }, active, pending_update, calls: [], fail: null };
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.fail && method !== "GET") throw server.fail;
    const names = () => Object.keys(server.clocks).sort();
    const snapshot = () => ({
      names: names(), active: server.active, sources: SOURCES, presets: ["night", "wakeup"], categories,
      genres, beets_fields, beets_error,
      types: ["liner", "music", "settings", "track"], cursor: 1, last_status: "Ready", notice,
      pending_update: server.pending_update,
    });
    if (url === "/api/library/clock" && method === "GET") return snapshot();
    if (url === "/api/library/clock/check") return { tracks: 12, error: null };
    if (url === "/api/library/clock" && method === "POST") { server.clocks[body.name] = body; return body; }
    if (url === "/api/library/clock/active") { server.active = body.name; return snapshot(); }
    if (url === "/api/library/clock/import") {
      server.clocks[body.name] = { version: 1, name: body.name, steps: [MUSIC], resync_minutes: body.resync_minutes };
      return server.clocks[body.name];
    }
    const path = url.split("?", 1)[0];
    const [, name, verb] = /^\/api\/library\/clock\/([^/]+)(?:\/(\w+))?$/.exec(path);
    const clockName = decodeURIComponent(name);
    if (method === "GET") return copy(server.clocks[clockName]);
    if (method === "PUT") { server.clocks[clockName] = body; return body; }
    if (method === "DELETE") { delete server.clocks[clockName]; return snapshot(); }
    if (verb === "duplicate" || verb === "rename") {
      server.clocks[body.name] = { ...server.clocks[clockName], name: body.name };
      if (verb === "rename") delete server.clocks[clockName];
      return server.clocks[body.name];
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

let controls = null;

async function open(server) {
  controls = installClockwheelPanel({ document, request: server.request });
  await vi.waitFor(() => expect(rows()).toHaveLength(server.clocks.Morning?.steps.length ?? 0));
  return server;
}

function altKey(target, key) {
  target.dispatchEvent(new KeyboardEvent("keydown", { key, altKey: true, bubbles: true, cancelable: true }));
}

beforeEach(installIndex);
afterEach(() => { controls?.destroy(); controls = null; });

describe("clockwheel markup", () => {
  it("is its own card in the Library tools tab, with labelled controls", () => {
    const section = byId("clockwheel-heading").closest("section");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    // The job card keeps its single live region; this card has its own.
    expect(section.closest("details")).toBeNull();
    expect(byId("clockwheel-heading").tagName).toBe("H2");
    expect(byId("clockwheel-heading").textContent).toBe("Clockwheels");
    expect([...section.querySelectorAll("h3")].map((heading) => heading.id)).toEqual(["clock-list-heading", "clock-steps-heading"]);
    expect(section.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')).toHaveLength(1);
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
    }
    expect(byId("clock-step-require-following").nextElementSibling.textContent).toBe("Only play if the following track fits before the next reset");
    expect(byId("clock-slots").tagName).toBe("OL");
    expect(byId("clock-slots").getAttribute("role")).toBe("grid");
    expect(byId("clock-slots").querySelector('[role="row"] [role="gridcell"]')).not.toBeNull();
    expect(byId("clock-slots").getAttribute("aria-labelledby")).toBe("clock-steps-heading");
    expect(byId("clock-list").getAttribute("aria-labelledby")).toBe("clock-list-heading");
    expect(byId("clock-status").getAttribute("role")).toBe("status");
    expect(byId("clock-name").maxLength).toBe(64);
    expect(byId("clock-step-legend").closest("fieldset").id).toBe("clock-step-form");
    expect(byId("clock-step-match-count").getAttribute("aria-hidden")).toBe("true");
    expect(section.querySelector("[aria-controls]")).toBeNull();
    expect(byId("clock-steps-help").textContent).toContain("Alt+Up and Alt+Down");
    expect(byId("clock-steps-help").textContent).toContain("Enter on a step name edits it");
    expect(byId("clock-steps-help").textContent).toContain("Save clockwheel");
    expect(document.querySelectorAll('[id^="clock-"]').length).toBe(new Set([...document.querySelectorAll('[id^="clock-"]')].map((node) => node.id)).size);
  });
});

describe("clockwheel editor", () => {
  it("shows a debounced matching track count for the current step", async () => {
    const server = await open(fakeServer());
    const row = byId("clock-step-conditions").firstElementChild;
    row.querySelector("select").value = "genre";
    row.querySelector("select").dispatchEvent(new Event("change", { bubbles: true }));
    row.querySelectorAll("select")[3].value = "Rock";
    row.querySelectorAll("select")[3].dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(byId("clock-step-match-count").textContent).toContain("12 tracks match"));
    expect(status()).toContain("12 tracks match this selection before repeat protection");
    const call = server.calls.find((item) => item.url === "/api/library/clock/check");
    expect(call.body.step.conditions).toEqual([{ field: "genre", operator: "is", value: "Rock" }]);
  });

  it("offers exact negative text matching for library and Beets fields", async () => {
    await open(fakeServer());
    const row = byId("clock-step-conditions").firstElementChild;
    const field = row.querySelector("select");
    const operator = row.querySelectorAll("select")[2];
    field.value = "artist";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    expect([...operator.options].map((option) => option.value)).toContain("is_not");
    field.value = "beets";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    expect([...operator.options].map((option) => option.value)).toContain("is_not");
  });

  it("recounts Match any changes and never steals focus for an incomplete count", async () => {
    const server = await open(fakeServer());
    const first = byId("clock-step-conditions").firstElementChild;
    first.querySelector("select").value = "genre";
    first.querySelector("select").dispatchEvent(new Event("change", { bubbles: true }));
    first.querySelectorAll("select")[3].value = "Rock";
    first.querySelectorAll("select")[3].dispatchEvent(new Event("change", { bubbles: true }));
    const match = byId("clock-step-match");
    match.value = "any";
    match.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(server.calls.filter((item) => item.url === "/api/library/clock/check")).toHaveLength(1));
    expect(server.calls.at(-1).body.step.match).toBe("any");

    const row = byId("clock-step-conditions").firstElementChild;
    const field = row.querySelector("select");
    field.value = "artist";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    const value = row.querySelectorAll('input[type="text"]')[1];
    value.value = "";
    value.dispatchEvent(new Event("input", { bubbles: true }));
    match.focus();
    await vi.waitFor(() => expect(byId("clock-step-match-count").textContent).toContain("Complete the step conditions"));
    expect(document.activeElement).toBe(match);
  });

  it("names category steps and selects music categories as conditions", async () => {
    expect(describeStep({ type: "category", category: "gone", selection: "least_recent" }, [], []))
      .toBe("music from a category that no longer exists, least recently played");
    expect(describeStep({ type: "music", match: "all", conditions: [{ field: "category", category: "c1" }], selection: "least_recent" }, [], [{ id: "c1", name: "Rock gold" }]))
      .toBe("music from category Rock gold, least recently played");
    const server = await open(fakeServer({ categories: [{ id: "c1", name: "Rock gold" }, { id: "c2", name: "2000s" }] }));
    const type = byId("clock-step-type");
    expect([...type.options].map((option) => option.value)).not.toContain("category");
    type.value = "music";
    type.dispatchEvent(new Event("change"));
    const first = byId("clock-step-conditions").firstElementChild;
    const field = first.querySelector("select");
    field.value = "category";
    field.dispatchEvent(new Event("change"));
    const category = first.querySelectorAll("select")[5];
    expect(category.previousElementSibling.textContent).toBe("Music category");
    expect([...category.options].map((option) => option.textContent)).toEqual(["Rock gold", "2000s"]);
    category.value = "c2";
    byId("clock-add-condition").click();
    const second = byId("clock-step-conditions").lastElementChild;
    second.querySelector("select").value = "category";
    second.querySelector("select").dispatchEvent(new Event("change"));
    second.querySelectorAll("select")[5].value = "c1";
    byId("clock-step-match").value = "any";
    byId("clock-add-step").click();
    expect(rowText(3)).toBe("music from category 2000s or category Rock gold, least recently played");
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    expect(server.calls.find((call) => call.method === "PUT").body.steps[3]).toEqual({
      type: "music", match: "any", conditions: [{ field: "category", category: "c2" }, { field: "category", category: "c1" }],
      selection: "least_recent", enforce_rules: true,
    });
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 4 steps."));
    rowButton(3, "edit").click();
    expect(byId("clock-add-step").textContent).toBe("Update step 4");
    type.value = "liner";
    type.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(status()).toBe("Music category conditions changed to empty folder conditions for liner steps."));
    expect([...byId("clock-step-conditions").firstElementChild.querySelector("select").options]
      .map((option) => option.value)).not.toContain("category");
    expect(byId("clock-step-conditions").firstElementChild.querySelector("select").value).toBe("folder");
    const linerFolder = byId("clock-step-conditions").firstElementChild.querySelectorAll("select")[1];
    expect(linerFolder.hidden).toBe(false);
    expect(byId("clock-step-conditions").firstElementChild.querySelectorAll("select")[5].hidden).toBe(true);
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toContain("Updated Step 4 of 4, liner from Liners or Liners"));
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 4 steps."));
    expect(server.calls.filter((call) => call.method === "PUT").at(-1).body.steps[3]).toMatchObject({
      type: "liner", conditions: [
        { field: "folder", source: "ids", folder: "" }, { field: "folder", source: "ids", folder: "" },
      ],
    });
  });

  it("opens and saves a legacy category step as a music condition with its settings", async () => {
    const server = fakeServer();
    server.clocks.Morning.steps[0] = { type: "category", category: "old", selection: "random", enforce_rules: false };
    await open(server);
    rowButton(0, "edit").click();
    expect(byId("clock-step-type").value).toBe("music");
    expect(byId("clock-step-match").value).toBe("all");
    expect(byId("clock-step-conditions").firstElementChild.querySelector("select").value).toBe("category");
    const category = byId("clock-step-conditions").firstElementChild.querySelectorAll("select")[5];
    expect(category.value).toBe("old");
    expect(category.selectedOptions[0].textContent).toBe("old (unavailable)");
    expect(byId("clock-step-selection").value).toBe("random");
    expect(byId("clock-step-enforce-rules").checked).toBe(false);
    byId("clock-add-step").click();
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    expect(server.calls.find((call) => call.method === "PUT").body.steps[0]).toEqual({
      type: "music", match: "all", conditions: [{ field: "category", category: "old" }],
      selection: "random", enforce_rules: false,
    });
  });

  it("says a step's days and hours in words", async () => {
    const { stepHoursText } = await import("../../src/autodj/static/modules/step-hours.js");
    expect(stepHoursText({})).toBe("");
    expect(stepHoursText({ days: [0, 1, 2, 3, 4] })).toBe("only on weekdays");
    expect(stepHoursText({ days: [6, 5] })).toBe("only at weekends");
    expect(stepHoursText({ days: [0, 1, 2, 3, 4, 5, 6] })).toBe("");
    expect(stepHoursText({ hours_from: "06:00", hours_until: "10:00" })).toBe("only every day from 06:00 until 10:00");
    expect(stepHoursText({ days: [0, 2], hours_from: "06:00:00", hours_until: "10:00:00" }))
      .toBe("only on Monday and Wednesday from 06:00 until 10:00");
  });

  it("validates restart minutes and describes steps in words", () => {
    expect(parseResyncMinutes("30, 0, 30")).toEqual([0, 30]);
    expect(parseResyncMinutes(" ")).toEqual([]);
    expect(() => parseResyncMinutes("61")).toThrow("0 to 59");
    expect(() => parseResyncMinutes("a")).toThrow("whole minutes");
    expect(describeStep(MUSIC, SOURCES)).toBe("music from Rock, least recently played");
    expect(describeStep(MORNING.steps[1], SOURCES)).toBe("liner from Top, at random, ignoring repeat rules");
    expect(describeStep({ ...MORNING.steps[1], require_following: true }, SOURCES)).toBe("liner from Top, at random, ignoring repeat rules, only if the following track fits before the next reset");
    expect(describeStep({ ...MUSIC, recursive: true, source: "gone" }, SOURCES)).toBe("music from a folder that is no longer registered, with subfolders, least recently played");
    expect(describeStep({ type: "settings", preset: "night" })).toBe("switch to preset night");
    expect(describeStep({ type: "time_check" })).toBe("time check step");
  });

  it("shares step hours in descriptions used by the list and Why this track", async () => {
    const { describeStep: sharedDescribeStep } = await import("../../src/autodj/static/modules/clockwheel-step.js");
    expect(describeStep).toBe(sharedDescribeStep);
    expect(sharedDescribeStep({ ...MUSIC, days: [0, 1, 2, 3, 4], hours_from: "06:00", hours_until: "10:00" }, SOURCES))
      .toBe("music from Rock, least recently played, only on weekdays from 06:00 until 10:00");
    expect(sharedDescribeStep({ type: "settings", preset: "night", days: [5, 6] }))
      .toBe("switch to preset night, only at weekends");
  });

  it("says how every clockwheel music step chooses among its matches", () => {
    expect(describeStep({ ...MUSIC, selection: "least_recent" }, SOURCES))
      .toBe("music from Rock, least recently played");
    expect(describeStep({ ...MUSIC, selection: "random" }, SOURCES)).toBe("music from Rock, at random");
    expect(describeStep({ ...MORNING.steps[1], selection: "least_recent" }, SOURCES))
      .toBe("liner from Top, least recently played, ignoring repeat rules");
    expect(describeStep({ type: "category", category: "c1", selection: "least_recent" }, [], [{ id: "c1", name: "Rock gold" }]))
      .toBe("music from category Rock gold, least recently played");
    expect(describeStep({ type: "track", path: "Song.flac" })).toBe("track Song.flac");
    expect(describeStep({ type: "settings", preset: "night" })).toBe("switch to preset night");
  });

  it("saves any-match folder rows and numeric ranges, with summaries for every condition", async () => {
    const server = await open(fakeServer());
    const type = byId("clock-step-type");
    type.value = "music";
    type.dispatchEvent(new Event("change"));
    const host = byId("clock-step-conditions");
    const first = host.firstElementChild;
    first.querySelectorAll("select")[1].value = "rock";
    first.querySelectorAll("input[type=text]")[0].value = "Live";
    byId("clock-add-condition").click();
    const second = host.lastElementChild;
    const secondField = second.querySelectorAll("select")[0];
    secondField.value = "year";
    secondField.dispatchEvent(new Event("change"));
    const secondOperator = second.querySelectorAll("select")[2];
    secondOperator.value = "between";
    secondOperator.dispatchEvent(new Event("change"));
    const numeric = second.querySelectorAll("input[type=text]");
    numeric[1].value = "1990";
    numeric[2].value = "2000";
    byId("clock-add-condition").click();
    const duration = host.lastElementChild;
    const durationField = duration.querySelector("select");
    durationField.value = "duration";
    durationField.dispatchEvent(new Event("change"));
    const durationOperator = duration.querySelectorAll("select")[2];
    durationOperator.value = "between";
    durationOperator.dispatchEvent(new Event("change"));
    duration.querySelectorAll("input[type=text]")[1].value = "1:05";
    duration.querySelectorAll("input[type=text]")[2].value = "90";
    byId("clock-step-match").value = "any";
    byId("clock-add-step").click();
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    const step = server.calls.find((call) => call.method === "PUT").body.steps.at(-1);
    expect(step).toEqual({
      type: "music",
      match: "any",
      conditions: [
        { field: "folder", source: "rock", folder: "Live", recursive: false },
        { field: "year", operator: "between", value: 1990, upper: 2000 },
        { field: "duration", operator: "between", value: 65, upper: 90 },
      ],
      selection: "least_recent",
      enforce_rules: true,
    });
    expect(rowText(3)).toContain("Live or Year between 1990 and 2000 or Duration (seconds) between 65 and 90");
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 4 steps."));
    rowButton(3, "edit").click();
    const reopenedRange = byId("clock-step-conditions").children[1];
    expect(reopenedRange.querySelectorAll("select")[2].value).toBe("between");
    expect(reopenedRange.querySelectorAll("input[type=text]")[2].value).toBe("2000");
  });

  it("allows metadata-only steps, preserves rows when edited, and focuses after removal", async () => {
    const server = await open(fakeServer());
    const type = byId("clock-step-type");
    type.value = "music";
    type.dispatchEvent(new Event("change"));
    const row = byId("clock-step-conditions").firstElementChild;
    const source = row.querySelectorAll("select")[1];
    expect(source.value).toBe("rock");
    type.value = "liner";
    type.dispatchEvent(new Event("change"));
    expect(source.value).toBe("ids");
    type.value = "music";
    type.dispatchEvent(new Event("change"));
    expect(source.value).toBe("rock");
    const field = row.querySelector("select");
    field.value = "artist";
    field.dispatchEvent(new Event("change"));
    row.querySelectorAll("select")[2].value = "not_contains";
    row.querySelectorAll("input[type=text]")[1].value = "Björk*";
    await controls.refresh();
    expect(row.querySelectorAll("input[type=text]")[1].value).toBe("Björk*");
    byId("clock-add-condition").click();
    const second = byId("clock-step-conditions").lastElementChild;
    const secondField = second.querySelector("select");
    secondField.value = "title";
    secondField.dispatchEvent(new Event("change"));
    second.querySelectorAll("select")[2].value = "contains";
    second.querySelectorAll("input[type=text]")[1].value = "Live?";
    byId("clock-add-step").click();
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    expect(server.calls.find((call) => call.method === "PUT").body.steps.at(-1).conditions).toEqual([
      { field: "artist", operator: "not_contains", value: "Björk*" },
      { field: "title", operator: "contains", value: "Live?" },
    ]);
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 4 steps."));
    rowButton(3, "edit").click();
    expect(byId("clock-step-conditions").children).toHaveLength(2);
    byId("clock-step-conditions").children[0].querySelector("button").click();
    expect(byId("clock-step-conditions").children).toHaveLength(1);
    expect(byId("clock-step-conditions").firstElementChild.querySelector("legend").textContent).toBe("Condition 1");
    expect(document.activeElement).toBe(byId("clock-step-conditions").firstElementChild.querySelector("select"));
    byId("clock-step-conditions").firstElementChild.querySelector("button").click();
    expect(byId("clock-step-conditions").children).toHaveLength(0);
    expect(document.activeElement).toBe(byId("clock-add-condition"));
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Add at least one condition."));
  });

  it("loads genres, exposes Beets and preserves an unavailable saved Beets field", async () => {
    const old = {
      ...copy(MUSIC),
      conditions: [{ field: "beets", name: "mood", operator: "contains", value: "calm" }],
    };
    const fixture = fakeServer({
      genres: ["Ambient", "Rock"],
      beets_fields: [],
      beets_error: "Beets database unavailable.",
    });
    fixture.clocks.Morning.steps[0] = old;
    await open(fixture);
    // Open again so this fixture exercises a saved condition that is no longer supplied by the library.
    byId("clock-list").querySelector("button").click();
    await vi.waitFor(() => expect(rowText(0)).toContain("Beets mood contains calm"));
    rowButton(0, "edit").click();
    const row = byId("clock-step-conditions").firstElementChild;
    expect(row.querySelector("select").value).toBe("beets");
    expect(byId("clock-step-beets-notice").hidden).toBe(false);
    expect(byId("clock-step-beets-notice").textContent).toBe("Beets database unavailable.");
    expect(row.querySelectorAll("select")[4].value).toBe("mood");
    expect([...row.querySelectorAll("select")[3].options].map((option) => option.value)).toEqual(["Ambient", "Rock"]);
  });

  it("opens the active clockwheel quietly and lists each step with its position", async () => {
    await open(fakeServer());
    expect(byId("clock-slots").querySelectorAll('[role="row"] [role="rowheader"]')).toHaveLength(3);
    expect(byId("clock-slots").querySelectorAll('[role="row"] [role="gridcell"]')).toHaveLength(15);
    expect(rowText(0)).toBe("music from Rock, least recently played");
    expect(rowText(2)).toBe("track Album/Song.flac");
    expect(byId("clock-open-name").textContent).toBe("Morning");
    expect(byId("clock-next").textContent).toBe("Plays next: Step 2 of 3, liner from Top, at random, ignoring repeat rules.");
    expect(byId("clock-list").querySelector("button").textContent).toBe("Morning, active");
    expect(byId("clock-list").querySelector("button").getAttribute("aria-current")).toBe("true");
    const button = rowButton(1, "move-up");
    expect(button.textContent).toBe("Move up step 2");
    expect(rowCell(0, "edit").tabIndex).toBe(0);
    expect(rowCell(1, "edit").tabIndex).toBe(-1);
    expect([...byId("clock-slots").querySelectorAll('[role="gridcell"]')].filter((item) => item.tabIndex === 0)).toHaveLength(1);
    expect([...byId("clock-slots").querySelectorAll("button[data-action]")].every((item) => item.tabIndex === -1)).toBe(true);
    expect(rowCell(0, "move-up").getAttribute("aria-disabled")).toBe("true");
    expect(rowCell(1, "move-up").hasAttribute("aria-disabled")).toBe(false);
    expect(button.querySelector(".visually-hidden").textContent).toBe(" step 2");
    expect(rowButton(0, "move-up").disabled).toBe(true);
    expect(rowButton(2, "move-down").disabled).toBe(true);
    expect(status()).toBe("");
  });

  it("navigates step names with arrows, edits with Enter and deletes like the button", async () => {
    await open(fakeServer());
    rowCell(1, "edit").focus();
    rowCell(1, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowCell(2, "edit"));
    expect(document.activeElement.getAttribute("role")).toBe("gridcell");
    expect(document.activeElement.textContent).toBe("track Album/Song.flac");
    const statusKey = new KeyboardEvent("keydown", { key: "T", bubbles: true, cancelable: true });
    rowCell(2, "edit").dispatchEvent(statusKey);
    expect(statusKey.defaultPrevented).toBe(false);
    const playbackKey = new KeyboardEvent("keydown", { key: "k", bubbles: true, cancelable: true });
    rowCell(2, "edit").dispatchEvent(playbackKey);
    expect(playbackKey.defaultPrevented).toBe(false);
    const modifiedArrow = new KeyboardEvent("keydown", { key: "ArrowUp", shiftKey: true, bubbles: true, cancelable: true });
    rowCell(2, "edit").dispatchEvent(modifiedArrow);
    expect(modifiedArrow.defaultPrevented).toBe(false);
    rowCell(1, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowCell(1, "move-up"));
    rowCell(1, "move-up").dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowCell(1, "delete"));
    rowCell(1, "delete").dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    rowCell(2, "edit").focus();
    rowCell(2, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(byId("clock-step-legend").textContent).toBe("Edit step 3");
    byId("clock-cancel-edit").click();
    rowCell(2, "edit").focus();
    rowCell(2, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    expect(byId("confirm-dialog").open).toBe(false);
    await vi.waitFor(() => expect(status()).toBe("Deleted step 3, track Album/Song.flac. 2 steps remain. Not saved yet."));
    expect(byId("confirm-dialog").open).toBe(false);
    expect(rows()).toHaveLength(2);
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    rowCell(1, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", repeat: true, bubbles: true, cancelable: true }));
    expect(byId("confirm-dialog").open).toBe(false);
    expect(rows()).toHaveLength(2);
  });

  it("focuses grid cells, not their buttons, and presses a cell's button with Enter or Space", async () => {
    await open(fakeServer());
    const key = (target, name, extra = {}) => {
      const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...extra });
      target.dispatchEvent(event);
      return event;
    };
    // Row 1's Move up is unavailable, so Right skips from the name to Move down.
    rowCell(0, "edit").focus();
    key(rowCell(0, "edit"), "ArrowRight");
    expect(document.activeElement).toBe(rowCell(0, "move-down"));
    expect(rowCell(0, "move-down").tabIndex).toBe(0);
    expect(rowCell(0, "edit").tabIndex).toBe(-1);
    expect(key(rowCell(0, "move-down"), " ").defaultPrevented).toBe(true);
    expect(rowText(1)).toBe("music from Rock, least recently played");
    expect(document.activeElement).toBe(rowCell(1, "move-down"));
    await vi.waitFor(() => expect(status()).toBe("Moved. Step 2 of 3, music from Rock, least recently played. Not saved yet."));
    key(rowCell(1, "move-down"), "Enter", { repeat: true });
    expect(rowText(1)).toBe("music from Rock, least recently played");
    key(rowCell(1, "move-down"), "Home");
    key(document.activeElement, "Enter");
    expect(byId("clock-step-legend").textContent).toBe("Edit step 2");
    // A disabled action's cell does nothing.
    rowCell(2, "move-down").focus();
    key(rowCell(2, "move-down"), "Enter");
    expect(rows().map((_, index) => rowText(index))).toEqual([
      "liner from Top, at random, ignoring repeat rules", "music from Rock, least recently played", "track Album/Song.flac",
    ]);
    // A button that has focus, as after a click, handles Enter itself.
    const native = key(rowButton(0, "duplicate"), "Enter");
    expect(native.defaultPrevented).toBe(false);
    expect(rows()).toHaveLength(3);
    rowButton(0, "duplicate").click();
    expect(rows()).toHaveLength(4);
    expect(document.activeElement).toBe(rowCell(1, "duplicate"));
    expect([...byId("clock-slots").querySelectorAll('[role="gridcell"]')].filter((item) => item.tabIndex === 0)).toEqual([rowCell(1, "duplicate")]);
  });

  it("undoes either Delete path and restores the removed step's focus without saving", async () => {
    const server = await open(fakeServer());
    expect(byId("clock-undo").disabled).toBe(true);
    rowButton(0, "delete").click();
    expect(rows()).toHaveLength(2);
    expect(byId("confirm-dialog").open).toBe(false);
    expect(byId("clock-undo").disabled).toBe(false);
    byId("clock-undo").click();
    expect(rowText(0)).toBe("music from Rock, least recently played");
    expect(document.activeElement).toBe(rowCell(0, "edit"));
    expect(byId("clock-undo").disabled).toBe(true);
    rowCell(0, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(3);
    expect(document.activeElement).toBe(rowCell(0, "edit"));
    await vi.waitFor(() => expect(status()).toBe("Undid the last step change. Not saved yet."));
    expect(server.calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("refuses Delete and Undo while a step edit is open and keeps the form values", async () => {
    await open(fakeServer());
    rowButton(0, "delete").click();
    rowButton(1, "edit").click();
    byId("clock-step-path").value = "Unsaved/Path";
    expect(byId("clock-undo").disabled).toBe(true);
    expect(rowButton(0, "delete").disabled).toBe(true);

    rowButton(0, "delete").click();
    rowCell(0, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    byId("clock-undo").click();
    const undo = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true });
    byId("clock-step-path").dispatchEvent(undo);

    expect(undo.defaultPrevented).toBe(false);
    expect(rows()).toHaveLength(2);
    expect(byId("clock-step-path").value).toBe("Unsaved/Path");
    expect(byId("clock-step-legend").textContent).toBe("Edit step 2");
    byId("clock-cancel-edit").click();
    expect(byId("clock-undo").disabled).toBe(false);
    byId("clock-undo").click();
    expect(rows()).toHaveLength(3);
  });

  it("keeps an unsaved edit on its own step when an earlier step is duplicated or moved", async () => {
    await open(fakeServer());
    rowButton(2, "edit").click();
    byId("clock-step-path").value = "Unsaved/Path.flac";
    rowButton(0, "duplicate").click();
    expect(byId("clock-step-legend").textContent).toBe("Edit step 4");
    expect(byId("clock-add-step").textContent).toBe("Update step 4");
    rowButton(3, "move-up").click();
    expect(byId("clock-step-legend").textContent).toBe("Edit step 3");
    expect(byId("clock-step-path").value).toBe("Unsaved/Path.flac");
    byId("clock-step-type").dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(4);
    byId("clock-add-step").click();
    expect(rowText(2)).toBe("track Unsaved/Path.flac");
    expect(rowText(3)).toContain("liner from Top");
  });

  it("restores the last deleted step with Ctrl+Z from Step type", async () => {
    const server = fakeServer();
    server.clocks.Morning.steps = [MUSIC];
    await open(server);
    rowCell(0, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(0);
    expect(document.activeElement).toBe(byId("clock-step-type"));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(1);
    expect(document.activeElement).toBe(rowCell(0, "edit"));
  });

  it("undoes move, duplicate, add and update in order, without intercepting text undo", async () => {
    await open(fakeServer());
    rowButton(0, "move-down").click();
    rowButton(1, "duplicate").click();
    byId("clock-step-type").value = "settings";
    byId("clock-step-type").dispatchEvent(new Event("change"));
    byId("clock-step-preset").value = "night";
    byId("clock-add-step").click();
    rowButton(4, "edit").click();
    byId("clock-step-preset").value = "wakeup";
    byId("clock-add-step").click();
    expect(rowText(4)).toBe("switch to preset wakeup");
    const textUndo = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true });
    byId("clock-name").dispatchEvent(textUndo);
    expect(textUndo.defaultPrevented).toBe(false);
    expect(rows()).toHaveLength(5);
    byId("clock-undo").click();
    expect(rowText(4)).toBe("switch to preset night");
    byId("clock-undo").click();
    expect(rows()).toHaveLength(4);
    byId("clock-undo").click();
    expect(rows()).toHaveLength(3);
    byId("clock-undo").click();
    expect(rowText(0)).toBe("music from Rock, least recently played");
    expect(byId("clock-undo").disabled).toBe(true);
  });

  it("keeps undo across refresh and failed saves, and clears it on save or sign-out", async () => {
    const server = await open(fakeServer());
    rowButton(0, "delete").click();
    await controls.refresh();
    expect(byId("clock-undo").disabled).toBe(false);
    server.fail = new Error("Could not save");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Could not save"));
    expect(byId("clock-undo").disabled).toBe(false);
    server.fail = null;
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 2 steps."));
    expect(byId("clock-undo").disabled).toBe(true);
    rowButton(0, "delete").click();
    controls.clearProtectedData();
    expect(byId("clock-undo").disabled).toBe(true);
  });

  it("moves a step with Alt+Down, keeps focus on it and says the new position", async () => {
    const server = await open(fakeServer());
    rowCell(0, "edit").focus();
    altKey(rowCell(0, "edit"), "ArrowDown");
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    expect(rowText(1)).toBe("music from Rock, least recently played");
    await vi.waitFor(() => expect(status()).toBe("Moved. Step 2 of 3, music from Rock, least recently played. Not saved yet."));
    expect(byId("clock-next").textContent).toBe("Save to play these changes.");
    altKey(document.activeElement, "ArrowUp");
    expect(document.activeElement).toBe(rowCell(0, "edit"));
    altKey(document.activeElement, "ArrowUp");
    await vi.waitFor(() => expect(status()).toBe("This is already the first step."));
    altKey(document.activeElement, "ArrowDown");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 3 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/library/clock/Morning?apply_timing=next_restart");
    expect(put.body.steps.map((step) => step.type)).toEqual(["liner", "music", "track"]);
    expect(put.body.resync_minutes).toEqual([0]);
  });

  it("sends immediate apply timing when selected", async () => {
    const server = await open(fakeServer());
    byId("clock-resync-minutes").value = "15";
    byId("clock-resync-minutes").dispatchEvent(new Event("input", { bubbles: true }));
    byId("clock-apply-timing").value = "immediate";
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Changes apply immediately"));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/library/clock/Morning?apply_timing=immediate");
  });

  it("sends continue apply timing when selected", async () => {
    const server = await open(fakeServer());
    byId("clock-apply-timing").value = "continue";
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("continuing at the next step"));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/library/clock/Morning?apply_timing=continue");
  });

  it("shows when saved active-wheel changes are waiting for restart", async () => {
    await open(fakeServer({ pending_update: true }));
    expect(byId("clock-next").textContent).toBe("Saved changes are waiting for the next clock restart.");
    // A forced announcement clears the status, then writes it on the next tick.
    await vi.waitFor(() => expect(status()).toBe("Saved changes are waiting for the next clock restart."));
  });

  it("hides Apply timing for an inactive wheel", async () => {
    const server = await open(fakeServer());
    expect(byId("clock-apply-timing").hidden).toBe(false);
    server.active = "Night";
    await controls.refresh();
    expect(byId("clock-apply-timing").hidden).toBe(true);
  });

  it("moves with the buttons and moves focus to the other move button at an end", async () => {
    await open(fakeServer());
    rowButton(1, "move-down").click();
    expect(rowText(2)).toBe("liner from Top, at random, ignoring repeat rules");
    expect(document.activeElement).toBe(rowCell(2, "move-up"));
    rowButton(1, "move-up").click();
    expect(document.activeElement).toBe(rowCell(0, "move-down"));
    expect(rowText(0)).toBe("track Album/Song.flac");
  });

  it("duplicates and deletes steps, keeping focus in the list", async () => {
    await open(fakeServer());
    rowButton(0, "duplicate").click();
    expect(rows()).toHaveLength(4);
    expect(document.activeElement).toBe(rowCell(1, "duplicate"));
    await vi.waitFor(() => expect(status()).toBe("Duplicated as Step 2 of 4, music from Rock, least recently played. Not saved yet."));
    rowButton(3, "delete").click();
    expect(rows()).toHaveLength(3);
    expect(document.activeElement).toBe(rowCell(2, "edit"));
    await vi.waitFor(() => expect(status()).toBe("Deleted step 4, track Album/Song.flac. 3 steps remain. Not saved yet."));
    for (let left = 3; left > 0; left -= 1) rowButton(0, "delete").click();
    expect(rows()).toHaveLength(0);
    expect(byId("clock-slots").textContent).toBe("No steps yet. Add one with the form below.");
    expect(document.activeElement).toBe(byId("clock-step-type"));
  });

  it("edits a step in the form and returns focus to it", async () => {
    const server = await open(fakeServer());
    rowButton(1, "edit").click();
    expect(document.activeElement).toBe(byId("clock-step-type"));
    expect(byId("clock-step-legend").textContent).toBe("Edit step 2");
    expect(byId("clock-add-step").textContent).toBe("Update step 2");
    expect(byId("clock-cancel-edit").hidden).toBe(false);
    const legacyCondition = byId("clock-step-conditions").firstElementChild;
    expect(legacyCondition.querySelectorAll("select")[1].value).toBe("ids");
    expect(legacyCondition.querySelectorAll("input[type=text]")[0].value).toBe("Top");
    expect(byId("clock-step-enforce-rules").checked).toBe(false);
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-step-conditions").firstElementChild.querySelectorAll("input[type=text]")[0].value = "Top/Hour";
    byId("clock-step-selection").value = "least_recent";
    byId("clock-step-require-following").checked = true;
    byId("clock-add-step").click();
    expect(rowText(1)).toBe("liner from Top/Hour, least recently played, ignoring repeat rules, only if the following track fits before the next reset");
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    expect(byId("clock-step-legend").textContent).toBe("Add a step");
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 3 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.body.steps[1].require_following).toBe(true);
    expect(put.body.steps[0]).not.toHaveProperty("require_following");
    byId("clock-step-require-following").checked = true;
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Added Step 4 of 4, liner from Liners, least recently played, ignoring repeat rules, only if the following track fits before the next reset. Not saved yet."));
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Added Step 5 of 5, liner from Liners, least recently played, ignoring repeat rules. Not saved yet."));
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 5 steps."));
    const savedAgain = server.calls.filter((call) => call.method === "PUT").at(-1);
    expect(savedAgain.body.steps[3].require_following).toBe(true);
    expect(savedAgain.body.steps[4].require_following).toBe(false);
    rowButton(2, "edit").click();
    byId("clock-cancel-edit").click();
    expect(document.activeElement).toBe(rowCell(2, "edit"));
    expect(byId("clock-add-step").textContent).toBe("Add step");
  });

  it("gives a step days and hours, says them in its name and fills them in on Edit", async () => {
    const server = await open(fakeServer());
    rowButton(2, "edit").click();
    expect(byId("clock-step-day-4").checked).toBe(false);
    byId("clock-step-day-4").checked = true;
    byId("clock-step-day-5").checked = true;
    byId("clock-step-hours-from").value = "22:00";
    byId("clock-step-hours-until").value = "02:00";
    byId("clock-add-step").click();
    expect(rowText(2)).toBe("track Album/Song.flac, only on Friday and Saturday from 22:00 until 02:00");
    expect(byId("clock-step-day-4").checked).toBe(false); // the form is cleared for the next step
    expect(byId("clock-step-hours-from").value).toBe("");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 3 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.body.steps[2]).toEqual({
      type: "track", source: "jazz", path: "Album/Song.flac",
      days: [4, 5], hours_from: "22:00", hours_until: "02:00",
    });
    expect(put.body.steps[0]).not.toHaveProperty("days");
    rowButton(2, "edit").click();
    expect(byId("clock-step-day-5").checked).toBe(true);
    expect(byId("clock-step-hours-until").value).toBe("02:00");
    rowButton(0, "edit").click();
    expect(byId("clock-step-day-5").checked).toBe(false);
  });

  it("names a half-filled time window, links the error and keeps the step out", async () => {
    await open(fakeServer());
    const from = byId("clock-step-hours-from");
    from.value = "22:00";
    byId("clock-add-step").click();
    const until = byId("clock-step-hours-until");
    const message = "Enter both Allowed from and Allowed until, or leave both empty.";
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("clock-step-hours-error");
    expect(byId("clock-step-hours-error").textContent).toBe(message);
    expect(byId("clock-step-hours-error").hidden).toBe(false);
    await vi.waitFor(() => expect(status()).toBe(message));
    expect(rows()).toHaveLength(3);
    until.value = "22:00";
    until.dispatchEvent(new Event("input"));
    expect(until.hasAttribute("aria-invalid")).toBe(false);
    byId("clock-add-step").click();
    expect(byId("clock-step-hours-error").textContent).toBe("Allowed until must differ from Allowed from.");
    until.value = "02:00";
    byId("clock-add-step").click();
    expect(byId("clock-step-hours-error").hidden).toBe(true);
    expect(rows()).toHaveLength(4);
  });

  it("shows only the fields of the chosen step type and refuses paths that leave the folder", async () => {
    const server = await open(fakeServer());
    const type = byId("clock-step-type");
    const shown = () => [...document.querySelectorAll(".clock-step-field")].filter((field) => !field.hidden)
      .map((field) => field.querySelector("input, select").id.replace("clock-step-", ""));
    expect(shown()).toEqual(["match", "selection", "enforce-rules"]);
    type.value = "track";
    type.dispatchEvent(new Event("change"));
    expect(shown()).toEqual(["source", "path"]);
    expect([...byId("clock-step-source").options].map((option) => option.value)).toEqual(["rock", "jazz"]);
    byId("clock-step-path").value = "../secret.flac";
    byId("clock-add-step").click();
    expect(document.activeElement).toBe(byId("clock-step-path"));
    expect(byId("clock-step-path").getAttribute("aria-invalid")).toBe("true");
    await vi.waitFor(() => expect(status()).toContain('with no ".."'));
    byId("clock-step-path").value = "Album\\Other.flac";
    byId("clock-add-step").click();
    expect(rowText(3)).toBe("track Album/Other.flac");
    type.value = "liner";
    type.dispatchEvent(new Event("change"));
    expect(shown()).toEqual(["match", "selection", "enforce-rules", "require-following"]);
    expect([...byId("clock-step-source").options].map((option) => option.value)).toEqual(["ids"]);
    expect(byId("clock-step-require-following").checked).toBe(false);
    type.value = "settings";
    type.dispatchEvent(new Event("change"));
    expect(shown()).toEqual(["preset"]);
    byId("clock-step-preset").value = "night";
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Added Step 5 of 5, switch to preset night. Not saved yet."));
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 5 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.body.steps[3]).not.toHaveProperty("require_following");
    expect(put.body.steps[4]).not.toHaveProperty("require_following");
  });

  it("creates a clockwheel in the page and saves it with POST", async () => {
    const server = await open(fakeServer());
    byId("clock-name").value = "Morning";
    byId("clock-create").click();
    await vi.waitFor(() => expect(status()).toBe("A clockwheel named Morning already exists. Type another name."));
    byId("clock-name").value = "Bad/Name";
    byId("clock-create").click();
    expect(byId("clock-name").getAttribute("aria-invalid")).toBe("true");
    byId("clock-name").value = "Night";
    byId("clock-create").click();
    await vi.waitFor(() => expect(status()).toBe("New clockwheel Night. Add its steps, then press Save clockwheel."));
    expect(document.activeElement).toBe(byId("clock-step-type"));
    expect(byId("clock-open-name").textContent).toBe("Night, not saved yet");
    const condition = byId("clock-step-conditions").firstElementChild;
    condition.querySelectorAll("select")[1].value = "jazz";
    condition.querySelectorAll("input[type=text]")[0].value = "Late/Slow";
    condition.querySelector("input[type=checkbox]").checked = true;
    byId("clock-add-step").click();
    byId("clock-resync-minutes").value = "30, 0";
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Night, 1 step."));
    const post = server.calls.find((call) => call.method === "POST" && call.url === "/api/library/clock");
    expect(post.body).toEqual({
      version: 1,
      name: "Night",
      resync_minutes: [0, 30],
      steps: [{
        type: "music",
        match: "all",
        conditions: [{ field: "folder", source: "jazz", folder: "Late/Slow", recursive: true }],
        selection: "least_recent",
        enforce_rules: true,
      }],
    });
    expect(byId("clock-open-name").textContent).toBe("Night");
  });

  it("focuses the step the server refused and says why", async () => {
    const server = await open(fakeServer());
    rowButton(0, "duplicate").click();
    server.fail = Object.assign(new Error("Step 3: its music folder is not registered"), { status: 400 });
    byId("clock-save").focus();
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Step 3: its music folder is not registered"));
    expect(document.activeElement).toBe(rowCell(2, "edit"));
  });

  it("copies, renames, activates and turns off clockwheels", async () => {
    const server = await open(fakeServer());
    byId("clock-duplicate").click();
    await vi.waitFor(() => expect(status()).toBe("Type a new name in Name to copy it."));
    byId("clock-name").value = "Weekend";
    byId("clock-duplicate").click();
    await vi.waitFor(() => expect(status()).toBe("Copied Morning as Weekend, and opened the copy."));
    expect(byId("clock-open-name").textContent).toBe("Weekend");
    byId("clock-name").value = "Saturday";
    byId("clock-rename").click();
    await vi.waitFor(() => expect(status()).toBe("Renamed Weekend to Saturday."));
    expect(Object.keys(server.clocks).sort()).toEqual(["Morning", "Saturday"]);
    byId("clock-activate").click();
    await vi.waitFor(() => expect(status()).toBe("Saturday is the active clockwheel. It plays when Pick mode is set to Clockwheel."));
    await vi.waitFor(() => expect(byId("clock-list").textContent).toContain("Saturday, active"));
    byId("clock-deactivate").click();
    await vi.waitFor(() => expect(status()).toBe("No clockwheel is active now."));
    expect(server.active).toBeNull();
    rowButton(0, "move-down").click();
    byId("clock-activate").click();
    await vi.waitFor(() => expect(status()).toContain("Save the changes to Saturday"));
  });

  it("deletes after the confirm dialog and says so once it closes", async () => {
    const server = await open(fakeServer());
    byId("clock-delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Delete clockwheel Morning?");
    expect(byId("confirm-message").textContent).toContain("active clockwheel");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Deleted Morning."));
    expect(server.clocks).toEqual({});
    expect(byId("clock-list").textContent).toBe("No saved clockwheels.");
    expect(document.activeElement).toBe(byId("clock-create"));
    expect(byId("clock-open-name").textContent).toBe("no clockwheel open");
  });

  it("asks before dropping unsaved changes when another clockwheel opens", async () => {
    const server = await open(fakeServer());
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    rowButton(0, "move-down").click();
    [...byId("clock-list").querySelectorAll("button")].find((button) => button.dataset.name === "Night").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-cancel").click();
    expect(byId("clock-open-name").textContent).toBe("Morning");
    [...byId("clock-list").querySelectorAll("button")].find((button) => button.dataset.name === "Night").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Opened Night, 3 steps."));
  });

  it("imports a .PAL file under the typed name", async () => {
    const server = await open(fakeServer());
    byId("clock-import").click();
    await vi.waitFor(() => expect(status()).toBe("Choose a .PAL file first."));
    Object.defineProperty(byId("clock-pal-file"), "files", { value: [{ name: "plague.pal", size: 10, text: async () => "Dir['D:\\Music\\Rock'].QueueBottom(smLRP, EnforceRules);" }] });
    byId("clock-name").value = "Plague";
    byId("clock-resync-minutes").value = "0, 30";
    byId("clock-import").click();
    await vi.waitFor(() => expect(status()).toBe("Imported Plague from plague.pal, 1 step. Press Activate to use it."));
    const call = server.calls.find((item) => item.url === "/api/library/clock/import");
    expect(call.body).toEqual({ text: "Dir['D:\\Music\\Rock'].QueueBottom(smLRP, EnforceRules);", name: "Plague", resync_minutes: [0, 30] });
    expect(byId("clock-open-name").textContent).toBe("Plague");
  });

  it("shows and says the notice about old single-clock data", async () => {
    await open(fakeServer({ notice: "The clockwheel saved by an older AutoDJ can no longer be read." }));
    expect(byId("clock-notice").hidden).toBe(false);
    expect(byId("clock-notice").textContent).toContain("older AutoDJ");
    await vi.waitFor(() => expect(status()).toContain("older AutoDJ"));
  });

  it("ignores an import read after the session ends", async () => {
    let finish;
    Object.defineProperty(byId("clock-pal-file"), "files", { value: [{ name: "x.pal", size: 10, text: () => new Promise((resolve) => { finish = resolve; }) }] });
    const server = await open(fakeServer());
    byId("clock-name").value = "Night";
    byId("clock-import").click();
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    controls.clearProtectedData();
    finish("Dir['Rock'].QueueBottom(smLRP, EnforceRules);");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(server.calls.some((call) => call.url.endsWith("/import"))).toBe(false);
    expect(byId("clock-list").children).toHaveLength(0);
    expect(byId("clock-name").value).toBe("");
  });
});

describe("clockwheel step conditions, for screen readers", () => {
  const conditions = () => [...byId("clock-step-conditions").children];
  const startMusicStep = () => {
    const type = byId("clock-step-type");
    type.value = "music";
    type.dispatchEvent(new Event("change"));
  };
  const setField = (row, value) => {
    const field = row.querySelector("select");
    field.value = value;
    field.dispatchEvent(new Event("change"));
  };
  const setOperator = (row, value) => {
    const operator = row.querySelectorAll("select")[2];
    operator.value = value;
    operator.dispatchEvent(new Event("change"));
  };

  it("names the conditions as a group and links the help to every value", async () => {
    await open(fakeServer({ beets_fields: [], beets_error: "Beets database unavailable." }));
    startMusicStep();
    const host = byId("clock-step-conditions");
    expect(host.getAttribute("role")).toBe("group");
    expect(host.hasAttribute("aria-label")).toBe(false);
    expect(byId(host.getAttribute("aria-labelledby")).textContent).toBe("Conditions");
    const help = byId("clock-step-conditions-help");
    expect(help.textContent).toContain("Library tools, Categories");
    expect(help.textContent).toContain("MM:SS");
    expect(help.textContent).toContain("*");
    const [row] = conditions();
    const [, value, upper] = row.querySelectorAll("input[type=text]");
    const beetsField = row.querySelectorAll("select")[4];
    expect(value.getAttribute("aria-describedby")).toBe("clock-step-conditions-help");
    expect(upper.getAttribute("aria-describedby")).toBe("clock-step-conditions-help");
    // The Beets notice is linked only while it shows, since a hidden one would still be read.
    expect(beetsField.getAttribute("aria-describedby")).toBe("clock-step-conditions-help");
    row.querySelector('option[value="beets"]').disabled = false;
    setField(row, "beets");
    expect(byId("clock-step-beets-notice").hidden).toBe(false);
    expect(beetsField.getAttribute("aria-describedby")).toBe("clock-step-conditions-help clock-step-beets-notice");
    setField(row, "year");
    expect(byId("clock-step-beets-notice").hidden).toBe(true);
    expect(beetsField.getAttribute("aria-describedby")).toBe("clock-step-conditions-help");
  });

  it("says each condition added or removed, numbers Remove buttons and keeps focus in place", async () => {
    await open(fakeServer());
    startMusicStep();
    byId("clock-add-condition").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 added."));
    byId("clock-add-condition").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 3 added."));
    expect(document.activeElement).toBe(conditions()[2].querySelector("select"));
    const removeButtons = () => conditions().map((row) => row.querySelector("button").textContent);
    expect(removeButtons()).toEqual(["Remove condition 1", "Remove condition 2", "Remove condition 3"]);
    const third = conditions()[2];
    conditions()[1].querySelector("button").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 removed, 2 conditions left."));
    // The condition that took its place has focus, renumbered.
    expect(document.activeElement).toBe(third.querySelector("select"));
    expect(third.querySelector("legend").textContent).toBe("Condition 2");
    expect(removeButtons()).toEqual(["Remove condition 1", "Remove condition 2"]);
    const first = conditions()[0];
    third.querySelector("button").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 removed, 1 condition left."));
    // The last one gone, the one before it has focus.
    expect(document.activeElement).toBe(first.querySelector("select"));
    first.querySelector("button").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 1 removed, 0 conditions left."));
    expect(document.activeElement).toBe(byId("clock-add-condition"));
  });

  it("marks and focuses a duration that is not seconds or MM:SS, and clears every mark once fixed", async () => {
    const server = await open(fakeServer());
    startMusicStep();
    const [first] = conditions();
    setField(first, "artist");
    first.querySelectorAll("input[type=text]")[1].value = "";
    byId("clock-add-condition").click();
    const second = conditions()[1];
    setField(second, "duration");
    setOperator(second, "between");
    const [, low, high] = second.querySelectorAll("input[type=text]");
    low.value = "1:05";
    high.value = "ninety";
    // The empty artist is marked first.
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Enter a value for artist."));
    expect(first.querySelectorAll("input[type=text]")[1].getAttribute("aria-invalid")).toBe("true");
    first.querySelectorAll("input[type=text]")[1].value = "Björk";
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Enter the duration as seconds or minutes:seconds, for example 90 or 1:30."));
    expect(high.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(high);
    expect(first.querySelector("[aria-invalid]")).toBeNull();
    high.value = "1:30";
    byId("clock-add-step").click();
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    expect(server.calls.find((call) => call.method === "PUT").body.steps.at(-1).conditions).toEqual([
      { field: "artist", operator: "is", value: "Björk" },
      { field: "duration", operator: "between", value: 65, upper: 90 },
    ]);
  });

  it("rejects negative clockwheel durations now that signed cue times are accepted", async () => {
    await open(fakeServer());
    startMusicStep();
    const [row] = conditions();
    setField(row, "duration");
    setOperator(row, "gte");
    const value = row.querySelectorAll("input[type=text]")[1];
    value.value = "-3";
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Duration must be non-negative."));
    expect(value.getAttribute("aria-invalid")).toBe("true");
  });

  it("clears marks on rows after the one that fails", async () => {
    await open(fakeServer());
    startMusicStep();
    const [first] = conditions();
    setField(first, "artist");
    first.querySelectorAll("input[type=text]")[1].value = "";
    byId("clock-add-condition").click();
    const second = conditions()[1];
    setField(second, "title");
    const secondValue = second.querySelectorAll("input[type=text]")[1];
    secondValue.value = "Live";
    secondValue.setAttribute("aria-invalid", "true");
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Enter a value for artist."));
    expect(secondValue.hasAttribute("aria-invalid")).toBe(false);
  });

  it("reads step summaries with lowercase and/or and readable Beets names", () => {
    const step = (match, conditions) => ({ type: "music", match, conditions, selection: "least_recent", enforce_rules: true });
    expect(describeStep(step("all", [
      { field: "genre", operator: "is", value: "Rock" },
      { field: "beets", name: "original_year", operator: "lt", value: 1990 },
      { field: "bpm", operator: "gt", value: 120 },
    ]))).toBe("music from Genre is Rock and Beets first release year less than 1990 and BPM greater than 120, least recently played");
    expect(describeStep(step("any", [
      { field: "beets", name: "rg_track_gain", operator: "lt", value: -3 },
      { field: "artist", operator: "is", value: "Björk" },
    ]))).toBe("music from Beets rg track gain less than -3 or Artist is Björk, least recently played");
  });

  it("describes exact negative text, folder and category conditions naturally", () => {
    expect(describeStep({ ...MUSIC, conditions: [{ field: "artist", operator: "is_not", value: "Björk" }] }, SOURCES))
      .toBe("music from Artist is not Björk, least recently played");
    expect(describeStep({ ...MUSIC, conditions: [{ field: "folder", source: "rock", folder: "Live", operator: "is_not" }] }, SOURCES))
      .toBe("music from folder is not Live, least recently played");
    expect(describeStep({ ...MUSIC, conditions: [{ field: "category", category: "c1", operator: "is_not" }] }, [], [{ id: "c1", name: "Rock gold" }]))
      .toBe("music from category is not Rock gold, least recently played");
  });

  it("leaves a focused genre list alone until focus leaves it", async () => {
    const genres = ["Rock", "Jazz"];
    await open(fakeServer({ genres }));
    startMusicStep();
    const [row] = conditions();
    setField(row, "genre");
    const genre = row.querySelectorAll("select")[3];
    genre.value = "Jazz";
    genre.focus();
    const before = [...genre.options];
    genres.push("Ambient");
    await controls.refresh();
    expect([...genre.options]).toEqual(before);
    expect(document.activeElement).toBe(genre);
    genre.blur();
    await controls.refresh();
    expect([...genre.options].map((option) => option.value)).toEqual(["Rock", "Jazz", "Ambient"]);
    expect(genre.value).toBe("Jazz");
  });

  it("leaves a focused category list alone until focus leaves it", async () => {
    const categories = [{ id: "c1", name: "Rock" }, { id: "c2", name: "Jazz" }];
    await open(fakeServer({ categories }));
    startMusicStep();
    const [row] = conditions();
    setField(row, "category");
    const category = row.querySelectorAll("select")[5];
    category.value = "c2";
    category.focus();
    const before = [...category.options];
    categories.push({ id: "c3", name: "Ambient" });
    await controls.refresh();
    expect([...category.options]).toEqual(before);
    expect(document.activeElement).toBe(category);
    category.blur();
    await controls.refresh();
    expect([...category.options].map((option) => option.value)).toEqual(["c1", "c2", "c3"]);
    expect(category.value).toBe("c2");
  });
});
