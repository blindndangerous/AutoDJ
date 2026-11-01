
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import { describeStep, installClockwheelPanel, parseResyncMinutes } from "../../src/autodj/static/modules/clockwheel-panel.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => (byId("clock-name-form-dialog")?.open ? byId("clock-name-form-status") : byId("clock-step-dialog")?.open ? byId("clock-step-form-status") : byId("clock-status")).textContent;
const rows = () => [...byId("clock-slots").querySelectorAll("tbody tr[data-index]")];
const rowText = (index) => rows()[index].children[0].textContent;
const rowButton = (index, action) => {
  const row = rows()[index];
  if (action !== "edit") {
    const menu = row.querySelector('[aria-haspopup="menu"]');
    if (menu.getAttribute("aria-expanded") !== "true") menu.click();
  }
  return row.querySelector(`button[data-action="${action}"]`);
};
const savedAction = (name, action) => {
  const row = [...byId("clock-list").querySelectorAll("tbody tr[data-row-key]")].find((item) => item.dataset.rowKey === name);
  if (action !== "open") row.querySelector('[aria-haspopup="menu"]').click();
  return row.querySelector(`button[data-action="${action}"]`);
};
const submitName = (name) => {
  byId("clock-editor-name").value = name;
  byId("clock-name-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
};
// Every step action, Edit included, returns to the row's Actions cell.
const rowCell = (index) => rows()[index].querySelector('[role="gridcell"][data-action="options"]');

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
  installIndexMarkup();
  byId("panel-library").hidden = false;
  byId("clockwheel-heading").closest("details").open = true;
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
  ads = {},
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
      ...ads,
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
    const section = byId("clockwheel-heading").closest("details");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    // The job card keeps its single live region; this card has its own.
    expect(section.parentElement.closest("details")).toBeNull();
    expect(byId("clockwheel-heading").tagName).toBe("H2");
    expect(byId("clockwheel-heading").textContent).toBe("Clockwheels");
    expect([...section.querySelectorAll("h3")].map((heading) => heading.id)).toEqual(["clock-list-heading", "clock-steps-heading"]);
    expect(section.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')).toHaveLength(1);
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
    }
    expect(byId("clock-step-require-following").nextElementSibling.textContent).toBe("Only play if the following track fits before the next reset");
    expect(byId("clock-slots").tagName).toBe("DIV");
    expect(byId("clock-slots").hasAttribute("aria-labelledby")).toBe(false);
    expect(byId("clock-list").hasAttribute("aria-labelledby")).toBe(false);
    expect(byId("clock-status").getAttribute("role")).toBe("status");
    expect(byId("clock-name").maxLength).toBe(64);
    expect(byId("clock-step-legend").closest("fieldset").id).toBe("clock-step-form");
    expect(byId("clock-step-match-count").getAttribute("aria-live")).toBe("off");
    expect(byId("clock-step-match-count").getAttribute("aria-hidden")).toBeNull();
    expect(section.querySelector("[aria-controls]")).toBeNull();
    expect(document.querySelectorAll('[id^="clock-"]').length).toBe(new Set([...document.querySelectorAll('[id^="clock-"]')].map((node) => node.id)).size);
  });
});

describe("clockwheel editor", () => {
  it.each(["clock-resync-minutes", "clock-station-name"])("updates saved action availability immediately after editing %s", async (id) => {
    const server = await open(fakeServer());
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    savedAction("Night", "open").click();
    await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("Night"));
    byId(id).value = id === "clock-resync-minutes" ? "0, 30" : "Example FM";
    byId(id).dispatchEvent(new Event("input", { bubbles: true }));
    const row = [...byId("clock-list").querySelectorAll("tr[data-row-key]")].find((item) => item.dataset.rowKey === "Night");
    for (const action of ["rename", "activate"]) {
      expect(row.querySelector(`button[data-action="${action}"]`).getAttribute("aria-disabled")).toBe("true");
    }
    expect(row.querySelector('button[data-action="duplicate"]').getAttribute("aria-disabled")).toBe("false");
    expect(server.calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("reads no saved wheel but the open one to list saved clockwheels", async () => {
    const server = fakeServer();
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await open(server);
    await controls.refresh();
    expect(server.calls.filter((call) => call.url === "/api/library/clock/Night")).toHaveLength(0);
    const night = [...byId("clock-list").querySelectorAll("tr[data-row-key]")].find((row) => row.dataset.rowKey === "Night");
    expect([...night.cells].map((cell) => cell.textContent)).toEqual(["Night", "No", expect.stringContaining("Options")]);
  });

  it("says comma-containing names, recursive folders and conditional hours in each step's sentence", async () => {
    const server = fakeServer({ categories: [{ id: "c1", name: "Rock, gold" }] });
    server.clocks.Morning.steps = [
      { type: "liner", source: "ids", folder: "News, weather", recursive: true, selection: "random", enforce_rules: false,
        require_following: true, days: [4], hours_from: "08:00", hours_until: "09:00" },
      { type: "category", category: "c1", selection: "least_recent" },
    ];
    await open(server);
    expect(rowText(0)).toBe("1. liner from News, weather, with subfolders, at random, ignoring repeat rules, only if the following track fits before the next reset, only on Friday from 08:00 until 09:00");
    expect(rowText(1)).toBe("2. music from category Rock, gold, least recently played");
  });

  // ADR-0021-09 ADR-0021-38
  it("uses shared collection columns and row menus without row headers", async () => {
    controls = installClockwheelPanel({ document, request: fakeServer().request });
    await vi.waitFor(() => expect(byId("clock-list").querySelector("table.collection-table")).not.toBeNull());
    expect([...byId("clock-list").querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Name", "Active", "Actions"]);
    expect([...byId("clock-slots").querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Step", "Actions"]);
    expect(byId("clock-slots").querySelector('[role="rowheader"]')).toBeNull();
    await vi.waitFor(() => expect(byId("clock-slots").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(3));
    const first = byId("clock-slots").querySelector("tbody tr[data-row-key]");
    expect([...first.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent))
      .toEqual(["Edit", "Move up", "Move down", "Duplicate", "Delete"]);
    expect(first.querySelector('[aria-haspopup="menu"]').getAttribute("aria-label")).toMatch(/^Options for Step 1/);
    const saved = byId("clock-list").querySelector('tbody tr[data-row-key="Morning"]');
    expect([...saved.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent))
      .toEqual(["Open", "Duplicate", "Rename", "Turn off", "Delete"]);
  });

  // ADR-0021-38
  it("shows each step as one numbered sentence with its target, selection and hours", async () => {
    const server = fakeServer({ categories: [{ id: "c1", name: "Rock gold" }] });
    server.clocks.Morning.steps = [
      MUSIC,
      MORNING.steps[1],
      { type: "category", category: "c1", selection: "least_recent" },
      { ...MORNING.steps[2], days: [0], hours_from: "06:00", hours_until: "10:00" },
      { type: "settings", preset: "Evening" },
      { type: "ad_break", max_spots: 1, max_seconds: 60 },
      { type: "ad_break", max_spots: 3, max_seconds: 0 },
    ];
    await open(server);
    expect(rows().map((row) => row.cells.length)).toEqual([2, 2, 2, 2, 2, 2, 2]);
    expect(rows().map((_, index) => rowText(index))).toEqual([
      "1. music from Rock, least recently played",
      "2. liner from Top, at random, ignoring repeat rules",
      "3. music from category Rock gold, least recently played",
      "4. track Album/Song.flac, only on Monday from 06:00 until 10:00",
      "5. switch to preset Evening",
      "6. ad break, up to 1 spot, up to 60 seconds",
      "7. ad break, up to 3 spots",
    ]);
    expect(byId("clock-slots").textContent).not.toMatch(/Not specified|Not applicable|Least recently played/);
  });

  // ADR-0021-20
  it("labels the shared new name and uses it to prefill Create", async () => {
    await open(fakeServer());
    expect(document.querySelector('label[for="clock-name"]').textContent).toBe("New clockwheel name");
    const help = byId(byId("clock-name").getAttribute("aria-describedby"));
    expect(help.textContent).toContain("Create and Import .PAL use this name.");
    byId("clock-name").value = "Evening";
    byId("clock-create").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    expect(byId("clock-editor-name").value).toBe("Evening");
  });

  // ADR-0021-22
  it("preserves an empty or operator-entered new name when opening saved wheels", async () => {
    const server = await open(fakeServer());
    expect(byId("clock-name").value).toBe("");
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    for (const name of ["", "  Future wheel  "]) {
      byId("clock-name").value = name;
      savedAction("Night", "open").click();
      await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("Night"));
      expect(byId("clock-name").value).toBe(name);
      savedAction("Morning", "open").click();
      await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("Morning"));
      expect(byId("clock-name").value).toBe(name);
    }
  });

  it("shows a debounced matching track count for the current step", async () => {
    const server = await open(fakeServer());
    const row = byId("clock-step-conditions").firstElementChild;
    row.querySelector("select").value = "genre";
    row.querySelector("select").dispatchEvent(new Event("change", { bubbles: true }));
    row.querySelectorAll("select")[3].value = "Rock";
    row.querySelectorAll("select")[3].dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(byId("clock-step-match-count").textContent).toBe("12 tracks"));
    expect(status()).toContain("12 tracks");
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
    byId("clock-new-step").click();
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
    expect(rowText(3)).toBe("4. music from category 2000s or category Rock gold, least recently played");
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

  it("changes a step's words in place when its category is deleted, keeping focus", async () => {
    const categories = [{ id: "c1", name: "Rock gold" }];
    const server = fakeServer({ categories });
    server.clocks.Morning.steps[0] = {
      type: "music", match: "all", conditions: [{ field: "category", category: "c1" }],
      selection: "least_recent", enforce_rules: true,
    };
    await open(server);
    expect(rowText(0)).toBe("1. music from category Rock gold, least recently played");
    const cell = rowCell(0, "edit");
    const button = rowButton(0, "edit");
    cell.focus();
    categories.splice(0);
    await controls.refresh();
    expect(rowText(0)).toBe("1. music from category that no longer exists, least recently played");
    // The same row and cell, so the grid's place and focus stay.
    expect(rowButton(0, "edit")).toBe(button);
    expect(document.activeElement).toBe(cell);
    expect(status()).toBe("");
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
    expect(describeStep({ type: "ad_break", max_spots: 1, max_seconds: 0 })).toBe("ad break, up to 1 spot");
    expect(describeStep({ type: "ad_break", max_spots: 3, max_seconds: 60, advertiser: "Example Motors", category: "Car dealers", hours_from: "06:00", hours_until: "10:00" }))
      .toBe("ad break, up to 3 spots, up to 60 seconds, from Example Motors, category Car dealers, only every day from 06:00 until 10:00");
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
    byId("clock-new-step").click();
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
    byId("clock-new-step").click();
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
    byId("clock-step-conditions").children[0].querySelector("button[data-remove-condition]").click();
    expect(byId("clock-step-conditions").children).toHaveLength(1);
    expect(byId("clock-step-conditions").firstElementChild.querySelector("legend").textContent).toBe("Condition 1");
    expect(document.activeElement).toBe(byId("clock-step-conditions").firstElementChild.querySelector("select"));
    byId("clock-step-conditions").firstElementChild.querySelector("button[data-remove-condition]").click();
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
    expect(byId("clock-slots").querySelectorAll('[role="rowheader"]')).toHaveLength(0);
    expect(byId("clock-slots").querySelectorAll('tbody [role="gridcell"]')).toHaveLength(6);
    expect(rowText(0)).toBe("1. music from Rock, least recently played");
    expect(rowText(2)).toBe("3. track Album/Song.flac");
    expect(byId("clock-open-name").textContent).toBe("Morning");
    expect(byId("clock-next").textContent).toBe("Plays next: Step 2 of 3, liner from Top, at random, ignoring repeat rules.");
    expect(savedAction("Morning", "open").textContent).toBe("Open");
    expect(savedAction("Morning", "open").closest("tr").querySelector("td[data-name]").getAttribute("aria-current")).toBe("true");
    expect(byId("clock-list").querySelectorAll('[aria-current="true"]')).toHaveLength(1);
    expect([...byId("clock-slots").querySelectorAll('[role="gridcell"]')].filter((item) => item.tabIndex === 0)).toHaveLength(1);
    expect(rowButton(0, "move-up").getAttribute("aria-disabled")).toBe("true");
    expect(rowButton(0, "move-up").disabled).toBe(false);
    rowButton(0, "move-up").click();
    await vi.waitFor(() => expect(status()).toBe("This is already the first step."));
  });

  it("reuses saved clockwheel cells and supports one-stop row navigation", async () => {
    const server = await open(fakeServer());
    const list = byId("clock-list");
    const morning = savedAction("Morning", "open").closest("td");
    morning.focus();
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    expect(savedAction("Morning", "open").closest("td")).toBe(morning);
    expect(list.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    morning.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe("Night");
    const before = [...list.querySelectorAll("tbody tr")];
    await controls.refresh();
    expect([...list.querySelectorAll("tbody tr")]).toEqual(before);
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe("Night");
  });

  it("navigates cells with arrows, edits with Enter and deletes with Undo", async () => {
    await open(fakeServer());
    const key = (name, extra = {}) => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...extra }));
    rows()[1].children[0].focus();
    key("ArrowDown");
    expect(document.activeElement).toBe(rows()[2].children[0]);
    key("ArrowRight");
    expect(document.activeElement).toBe(rowCell(2));
    key("Home");
    expect(document.activeElement).toBe(rows()[2].children[0]);
    key("Home", { ctrlKey: true });
    expect(document.activeElement).toBe(rows()[0].children[0]);
    key("End", { ctrlKey: true });
    expect(document.activeElement).toBe(rows()[2].children[0]);
    rowCell(2).focus();
    key("Enter");
    await vi.waitFor(() => expect(document.activeElement.getAttribute("role")).toBe("menuitem"));
    expect(document.activeElement.textContent).toBe("Edit");
    key("Enter");
    expect(byId("clock-step-legend").textContent).toBe("Edit step 3");
    byId("clock-cancel-edit").click();
    expect(document.activeElement).toBe(rowCell(2, "edit"));
    key("Delete");
    expect(byId("confirm-dialog").open).toBe(false);
    expect(rows()).toHaveLength(2);
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    key("Delete", { repeat: true });
    expect(rows()).toHaveLength(2);
  });

  it("opens the step menu from its cell and returns focus after Escape", async () => {
    await open(fakeServer());
    rowCell(0, "options").focus();
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(document.activeElement.getAttribute("role")).toBe("menuitem"));
    expect(document.activeElement.textContent).toBe("Edit");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement.textContent).toBe("Move up");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement.textContent).toBe("Move down");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(rowText(1)).toBe("2. music from Rock, least recently played");
    expect(document.activeElement).toBe(rowCell(1, "options"));
    rowButton(1, "duplicate");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(rowCell(1, "options"));
  });

  it("undoes either Delete path and restores the removed step's focus without saving", async () => {
    const server = await open(fakeServer());
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
    expect(byId("clock-undo").disabled).toBe(false);
    byId("clock-undo").focus();
    byId("clock-undo").click();
    await vi.waitFor(() => expect(status()).toBe("No step changes to undo."));
    expect(document.activeElement).toBe(byId("clock-undo"));
    rowButton(0, "delete").click();
    expect(rows()).toHaveLength(2);
    expect(byId("confirm-dialog").open).toBe(false);
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("false");
    byId("clock-undo").click();
    expect(rowText(0)).toBe("1. music from Rock, least recently played");
    expect(document.activeElement).toBe(rowCell(0, "edit"));
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
    rowCell(0, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(3);
    expect(document.activeElement).toBe(rowCell(0, "edit"));
    await vi.waitFor(() => expect(status()).toBe("Undid the last step change. Not saved yet."));
    expect(server.calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("preserves retained step row identities and focus through undo", async () => {
    await open(fakeServer());
    const originalRows = rows();
    const firstEdit = rowCell(0, "edit");
    firstEdit.focus();
    altKey(firstEdit, "ArrowDown");
    expect(rows()[1]).toBe(originalRows[0]);
    expect(document.activeElement).toBe(firstEdit);
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    rows().forEach((row, index) => expect(row).toBe(originalRows[index]));
    expect(document.activeElement).toBe(firstEdit);
    expect(rowText(0)).toBe("1. music from Rock, least recently played");
  });

  it("refuses Delete and Undo while a step edit is open and keeps the form values", async () => {
    await open(fakeServer());
    rowButton(0, "delete").click();
    rowButton(1, "edit").click();
    byId("clock-step-path").value = "Unsaved/Path";
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
    expect(rowButton(0, "delete").getAttribute("aria-disabled")).toBe("true");

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
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("clock-step-dialog").open).toBe(false));
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("false");
    byId("clock-undo").click();
    expect(rows()).toHaveLength(3);
  });

  it("keeps an unsaved edit on its own step when an earlier step is duplicated or moved", async () => {
    await open(fakeServer());
    rowButton(2, "edit").click();
    byId("clock-step-path").value = "Unsaved/Path.flac";
    // The open step dialog keeps the page out of reach (#631); the steps
    // change under it here as a script would change them, past its guard.
    const dialog = byId("clock-step-dialog");
    dialog.removeAttribute("open");
    rowButton(0, "duplicate").click();
    dialog.setAttribute("open", "");
    expect(byId("clock-step-legend").textContent).toBe("Edit step 4");
    expect(byId("clock-add-step").textContent).toBe("Update step 4");
    rowButton(3, "move-up").click();
    expect(byId("clock-step-legend").textContent).toBe("Edit step 3");
    expect(byId("clock-step-path").value).toBe("Unsaved/Path.flac");
    byId("clock-step-type").dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(4);
    byId("clock-add-step").click();
    expect(rowText(2)).toBe("3. track Unsaved/Path.flac");
    expect(rowText(3)).toContain("liner from Top");
  });

  it("restores the last deleted step with Ctrl+Z from New step", async () => {
    const server = fakeServer();
    server.clocks.Morning.steps = [MUSIC];
    await open(server);
    rowCell(0, "edit").dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(0);
    expect(document.activeElement).toBe(byId("clock-new-step"));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(rows()).toHaveLength(1);
    expect(document.activeElement).toBe(rowCell(0, "edit"));
  });

  it("undoes move, duplicate, add and update in order, without intercepting text undo", async () => {
    await open(fakeServer());
    rowButton(0, "move-down").click();
    rowButton(1, "duplicate").click();
    byId("clock-new-step").click();
    byId("clock-step-type").value = "settings";
    byId("clock-step-type").dispatchEvent(new Event("change"));
    byId("clock-step-preset").value = "night";
    byId("clock-add-step").click();
    rowButton(4, "edit").click();
    byId("clock-step-preset").value = "wakeup";
    byId("clock-add-step").click();
    expect(rowText(4)).toBe("5. switch to preset wakeup");
    const textUndo = new KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true, cancelable: true });
    byId("clock-name").dispatchEvent(textUndo);
    expect(textUndo.defaultPrevented).toBe(false);
    expect(rows()).toHaveLength(5);
    byId("clock-undo").click();
    expect(rowText(4)).toBe("5. switch to preset night");
    byId("clock-undo").click();
    expect(rows()).toHaveLength(4);
    byId("clock-undo").click();
    expect(rows()).toHaveLength(3);
    byId("clock-undo").click();
    expect(rowText(0)).toBe("1. music from Rock, least recently played");
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
  });

  it("keeps undo across refresh and failed saves, and clears it on save or sign-out", async () => {
    const server = await open(fakeServer());
    rowButton(0, "delete").click();
    await controls.refresh();
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("false");
    server.fail = new Error("Could not save");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Could not save"));
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("false");
    server.fail = null;
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 2 steps."));
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
    rowButton(0, "delete").click();
    controls.clearProtectedData();
    expect(byId("clock-undo").getAttribute("aria-disabled")).toBe("true");
  });

  it("moves a step with Alt+Down, keeps focus on it and says the new position", async () => {
    const server = await open(fakeServer());
    rowCell(0, "edit").focus();
    altKey(rowCell(0, "edit"), "ArrowDown");
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    expect(rowText(1)).toBe("2. music from Rock, least recently played");
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

  // ADR-0009-08
  it("saves the station name while active and puts a refusal on the field", async () => {
    const server = await open(fakeServer());
    const field = byId("clock-station-name");
    const error = byId("clock-station-name-error");
    expect(document.querySelector('label[for="clock-station-name"]').textContent).toBe("Station name while active");
    expect(field.maxLength).toBe(100);
    expect(field.getAttribute("aria-describedby")).toBe("clock-station-name-help");
    expect(byId("clock-station-name-help").textContent).toContain("Leave empty for the station's normal name.");
    expect(field.value).toBe("");
    field.value = "Night\tShift";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    expect(byId("clock-next").textContent).toBe("Save to play these changes.");
    byId("clock-save").click();
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby").split(" ")).toEqual(["clock-station-name-error", "clock-station-name-help"]);
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Station name while active cannot contain tabs, line breaks or other control characters.");
    expect(document.activeElement).toBe(field);
    expect(server.calls.some((call) => call.method === "PUT")).toBe(false);
    field.value = "  Example FM Night Shift ";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    expect(field.hasAttribute("aria-invalid")).toBe(false);
    expect(field.getAttribute("aria-describedby")).toBe("clock-station-name-help");
    expect(error.hidden).toBe(true);
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 3 steps. The station name applies now."));
    expect(server.calls.find((call) => call.method === "PUT").body.station_name).toBe("Example FM Night Shift");
    expect(field.value).toBe("Example FM Night Shift");
    // The server's refusal of the name goes to the field as well.
    byId("clock-save").focus();
    server.fail = new Error("Station name while active can be at most 100 characters");
    byId("clock-save").click();
    await vi.waitFor(() => expect(field.getAttribute("aria-invalid")).toBe("true"));
    expect(error.textContent).toBe("Station name while active can be at most 100 characters");
    expect(document.activeElement).toBe(field);
    // A blank name is sent as blank: the station's normal name.
    server.fail = null;
    field.value = "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    byId("clock-save").click();
    // Saved, refused, then saved blank.
    await vi.waitFor(() => expect(server.calls.filter((call) => call.method === "PUT")).toHaveLength(3));
    expect(server.calls.filter((call) => call.method === "PUT")[2].body.station_name).toBe("");
  });

  // ADR-0009-08
  it("says the restart note only when the steps or restart minutes changed", async () => {
    await open(fakeServer());
    const field = byId("clock-station-name");
    const type = (input, value) => { input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); };
    // Nothing that waits for a restart changed: no restart note.
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 3 steps."));
    type(field, "Example FM Night Shift");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 3 steps. The station name applies now."));
    // Restart minutes wait for the restart, with or without a new name.
    type(field, "Example FM After Dark");
    type(byId("clock-resync-minutes"), "0, 30");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 3 steps. Changes apply at the next clock restart."));
    // So do steps.
    rowButton(0, "delete").click();
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 2 steps. Changes apply at the next clock restart."));
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
    // A step change, so there is something to apply.
    rowButton(0, "delete").click();
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
    expect(rowText(2)).toBe("3. liner from Top, at random, ignoring repeat rules");
    expect(document.activeElement).toBe(rowCell(2, "move-up"));
    rowButton(1, "move-up").click();
    expect(document.activeElement).toBe(rowCell(0, "move-down"));
    expect(rowText(0)).toBe("1. track Album/Song.flac");
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
    expect(byId("clock-slots").querySelector(".no-results").textContent).toBe("No steps. Open or create a clockwheel, then press New step.");
    expect(document.activeElement).toBe(byId("clock-new-step"));
  });

  it("opens New step in a dialog and Cancel returns to New step", async () => {
    await open(fakeServer());
    byId("clock-new-step").click();
    expect(byId("clock-step-dialog").open).toBe(true);
    expect(document.activeElement).toBe(byId("clock-step-type"));
    expect(byId("clock-step-legend").textContent).toBe("Add a step");
    byId("clock-cancel-edit").click();
    expect(byId("clock-step-dialog").open).toBe(false);
    expect(document.activeElement).toBe(byId("clock-new-step"));
  });

  it("edits a step in the form and returns focus to it", async () => {
    const server = await open(fakeServer());
    rowButton(1, "edit").click();
    expect(document.activeElement).toBe(byId("clock-step-type"));
    expect(byId("clock-step-legend").textContent).toBe("Edit step 2");
    expect(byId("clock-add-step").textContent).toBe("Update step 2");
    expect(byId("clock-cancel-edit").hidden).toBe(false);
    // Escape cancels the edit and returns to the step.
    byId("clock-step-dialog").dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(byId("clock-step-legend").textContent).toBe("Add a step");
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    rowButton(1, "edit").click();
    const legacyCondition = byId("clock-step-conditions").firstElementChild;
    expect(legacyCondition.querySelectorAll("select")[1].value).toBe("ids");
    expect(legacyCondition.querySelectorAll("input[type=text]")[0].value).toBe("Top");
    expect(byId("clock-step-enforce-rules").checked).toBe(false);
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-step-conditions").firstElementChild.querySelectorAll("input[type=text]")[0].value = "Top/Hour";
    byId("clock-step-selection").value = "least_recent";
    byId("clock-step-require-following").checked = true;
    byId("clock-add-step").click();
    expect(rowText(1)).toBe("2. liner from Top/Hour, least recently played, ignoring repeat rules, only if the following track fits before the next reset");
    expect(document.activeElement).toBe(rowCell(1, "edit"));
    expect(byId("clock-step-legend").textContent).toBe("Add a step");
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 3 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.body.steps[1].require_following).toBe(true);
    expect(put.body.steps[0]).not.toHaveProperty("require_following");
    byId("clock-new-step").click();
    byId("clock-step-require-following").checked = true;
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Added Step 4 of 4, liner from Liners, least recently played, ignoring repeat rules, only if the following track fits before the next reset. Not saved yet."));
    expect(byId("clock-step-require-following").checked).toBe(false);
    byId("clock-new-step").click();
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

  it("gives a step days and hours, says them in its sentence and fills them in on Edit", async () => {
    const server = await open(fakeServer());
    rowButton(2, "edit").click();
    expect(byId("clock-step-day-4").checked).toBe(false);
    byId("clock-step-day-4").checked = true;
    byId("clock-step-day-5").checked = true;
    byId("clock-step-hours-from").value = "22:00";
    byId("clock-step-hours-until").value = "02:00";
    byId("clock-add-step").click();
    expect(rowText(2)).toBe("3. track Album/Song.flac, only on Friday and Saturday from 22:00 until 02:00");
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
    byId("clock-cancel-edit").click();
    rowButton(0, "edit").click();
    expect(byId("clock-step-day-5").checked).toBe(false);
  });

  it("names a half-filled time window, links the error and keeps the step out", async () => {
    await open(fakeServer());
    byId("clock-new-step").click();
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
    byId("clock-new-step").click();
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
    expect(byId("clock-step-form-error").textContent).toContain('with no ".."');
    expect(byId("clock-step-path").getAttribute("aria-describedby")).toContain("clock-step-form-error");
    byId("clock-step-path").value = "Album\\Other.flac";
    byId("clock-add-step").click();
    expect(rowText(3)).toBe("4. track Album/Other.flac");
    byId("clock-new-step").click();
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
    byId("clock-name").value = "Future wheel";
    byId("clock-create").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    submitName("Morning");
    await vi.waitFor(() => expect(status()).toBe("A clockwheel named Morning already exists. Type another name."));
    submitName("Bad/Name");
    expect(byId("clock-editor-name").getAttribute("aria-invalid")).toBe("true");
    await vi.waitFor(() => expect(byId("clock-name-form").hasAttribute("aria-busy")).toBe(false));
    submitName("Night");
    await vi.waitFor(() => expect(status()).toBe("New clockwheel Night. Add its steps, then press Save clockwheel."));
    expect(document.activeElement).toBe(byId("clock-new-step"));
    expect(byId("clock-open-name").textContent).toBe("Night, not saved yet");
    expect(byId("clock-name").value).toBe("Future wheel");
    byId("clock-new-step").click();
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
      station_name: "",
      steps: [{
        type: "music",
        match: "all",
        conditions: [{ field: "folder", source: "jazz", folder: "Late/Slow", recursive: true }],
        selection: "least_recent",
        enforce_rules: true,
      }],
    });
    expect(byId("clock-open-name").textContent).toBe("Night");
    expect(byId("clock-name").value).toBe("Future wheel");
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

  it("copies, renames, activates and turns off the named saved row", async () => {
    const server = await open(fakeServer());
    byId("clock-name").value = "Future wheel";
    savedAction("Morning", "duplicate").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    expect(byId("clock-editor-name").value).toBe("");
    submitName("Weekend");
    await vi.waitFor(() => expect(status()).toBe("Copied Morning as Weekend, and opened the copy."));
    expect(byId("clock-open-name").textContent).toBe("Weekend");
    expect(byId("clock-name").value).toBe("Future wheel");
    savedAction("Weekend", "rename").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    expect(byId("clock-editor-name").value).toBe("Weekend");
    submitName("Saturday");
    await vi.waitFor(() => expect(status()).toBe("Renamed Weekend to Saturday."));
    expect(Object.keys(server.clocks).sort()).toEqual(["Morning", "Saturday"]);
    expect(byId("clock-name").value).toBe("Future wheel");
    savedAction("Saturday", "activate").click();
    await vi.waitFor(() => expect(status()).toBe("Saturday is the active clockwheel. It plays when Pick mode is set to Clockwheel."));
    expect(server.active).toBe("Saturday");
    savedAction("Saturday", "activate").click();
    await vi.waitFor(() => expect(status()).toBe("No clockwheel is active now."));
    expect(server.active).toBeNull();
    rowButton(0, "move-down").click();
    savedAction("Morning", "activate").click();
    await vi.waitFor(() => expect(server.active).toBe("Morning"));
    expect(byId("clock-open-name").textContent).toBe("Saturday");
    expect(controls.hasUnsavedWork()).toBe(true);
  });

  it("deletes after the confirm dialog and says so once it closes", async () => {
    const server = await open(fakeServer());
    savedAction("Morning", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Delete clockwheel Morning?");
    expect(byId("confirm-message").textContent).toContain("active clockwheel");
    expect(byId("confirm-message").textContent).toContain("references are removed from saved profiles and station defaults");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Deleted Morning."));
    expect(server.clocks).toEqual({});
    expect(byId("clock-list").querySelector(".no-results").textContent).toBe("No saved clockwheels.");
    expect(document.activeElement).toBe(byId("clock-create"));
    expect(byId("clock-open-name").textContent).toBe("no clockwheel open");
  });

  it("warns that deleting the open dirty wheel discards unsaved changes", async () => {
    await open(fakeServer());
    rowButton(0, "move-down").click();
    savedAction("Morning", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-message").textContent).toContain("Unsaved changes to Morning will also be discarded.");
    byId("confirm-cancel").click();
    expect(controls.hasUnsavedWork()).toBe(true);
    expect(rowText(1)).toBe("2. music from Rock, least recently played");
  });

  it("shows an unsaved wheel with a confirmed local Delete action", async () => {
    const server = await open(fakeServer());
    byId("clock-create").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    submitName("Draft");
    await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("Draft, not saved yet"));
    const draftRow = [...byId("clock-list").querySelectorAll("tr[data-row-key]")].find((row) => row.dataset.rowKey === "Draft");
    expect(draftRow).toBeDefined();
    expect(draftRow.textContent).toContain("Not saved");
    savedAction("Draft", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-message").textContent).toContain("has not been saved");
    byId("confirm-cancel").click();
    expect(byId("clock-open-name").textContent).toBe("Draft, not saved yet");
    savedAction("Draft", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("no clockwheel open"));
    expect([...byId("clock-list").querySelectorAll("tr[data-row-key]")].some((row) => row.dataset.rowKey === "Draft")).toBe(false);
    expect(server.calls.some((call) => call.method === "DELETE")).toBe(false);
    expect(controls.hasUnsavedWork()).toBe(false);
  });

  it.each(["Morning", "Night"])("allows duplicating %s after a prompt naming the dirty open wheel", async (source) => {
    const server = await open(fakeServer());
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    rowButton(0, "move-down").click();
    const duplicate = savedAction(source, "duplicate");
    expect(duplicate.getAttribute("aria-disabled")).toBe("false");
    duplicate.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Discard changes to Morning?");
    byId("confirm-cancel").click();
    expect(controls.hasUnsavedWork()).toBe(true);
    savedAction(source, "duplicate").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("clock-name-form-dialog").open).toBe(true));
    submitName("Copy");
    await vi.waitFor(() => expect(byId("clock-open-name").textContent).toBe("Copy"));
    expect(server.calls.some((call) => call.url === `/api/library/clock/${source}/duplicate` && call.method === "POST")).toBe(true);
    expect(controls.hasUnsavedWork()).toBe(false);
  });

  it("returns saved deletion focus to the adjacent row or the preceding last row", async () => {
    const server = fakeServer();
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    server.clocks.Weekend = { ...copy(MORNING), name: "Weekend" };
    await open(server);
    savedAction("Night", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(document.activeElement).toBe(savedAction("Weekend", "open").closest("td"));
    expect(byId("clock-open-name").textContent).toBe("Morning");
    savedAction("Weekend", "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(document.activeElement).toBe(savedAction("Morning", "open").closest("td"));
  });

  it("asks before dropping unsaved changes when another clockwheel opens", async () => {
    const server = await open(fakeServer());
    server.clocks.Night = { ...copy(MORNING), name: "Night" };
    await controls.refresh();
    rowButton(0, "move-down").click();
    savedAction("Night", "open").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-cancel").click();
    expect(byId("clock-open-name").textContent).toBe("Morning");
    savedAction("Night", "open").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Opened Night, 3 steps."));
  });

  it("imports a .PAL file under the typed name", async () => {
    const server = await open(fakeServer());
    byId("clock-import").click();
    await vi.waitFor(() => expect(status()).toBe("Choose a .PAL file first."));
    Object.defineProperty(byId("clock-pal-file"), "files", { value: [{ name: "throwback.pal", size: 10, text: async () => "Dir['D:\\Music\\Rock'].QueueBottom(smLRP, EnforceRules);" }] });
    byId("clock-name").value = "  Throwback  ";
    byId("clock-resync-minutes").value = "0, 30";
    byId("clock-import").click();
    await vi.waitFor(() => expect(status()).toBe("Imported Throwback from throwback.pal, 1 step. Choose Options for Throwback, then Activate."));
    const call = server.calls.find((item) => item.url === "/api/library/clock/import");
    expect(call.body).toEqual({ text: "Dir['D:\\Music\\Rock'].QueueBottom(smLRP, EnforceRules);", name: "Throwback", resync_minutes: [0, 30] });
    expect(byId("clock-open-name").textContent).toBe("Throwback");
    expect(byId("clock-name").value).toBe("  Throwback  ");
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
    expect(byId("clock-list").querySelectorAll("tr[data-row-key]")).toHaveLength(0);
    expect(byId("clock-name").value).toBe("");
  });
});

describe("unsaved work, for a station switch", () => {
  it("reports changes not saved, until they are saved or the station's data is cleared", async () => {
    const server = await open(fakeServer());
    expect(controls.hasUnsavedWork()).toBe(false);
    rowButton(0, "move-down").click();
    expect(controls.hasUnsavedWork()).toBe(true);
    byId("clock-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    await vi.waitFor(() => expect(controls.hasUnsavedWork()).toBe(false));
    rowButton(0, "move-down").click();
    expect(controls.hasUnsavedWork()).toBe(true);
    controls.clearProtectedData();
    expect(controls.hasUnsavedWork()).toBe(false);
  });
});

describe("clockwheel step conditions, for screen readers", () => {
  const conditions = () => [...byId("clock-step-conditions").children];
  const startMusicStep = () => {
    byId("clock-new-step").click();
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

  it("keeps general help visible without repeating it on controls", async () => {
    await open(fakeServer({ beets_fields: [], beets_error: "Beets database unavailable." }));
    startMusicStep();
    const host = byId("clock-step-conditions");
    expect(host.getAttribute("role")).toBe("group");
    expect(host.hasAttribute("aria-label")).toBe(false);
    expect(byId(host.getAttribute("aria-labelledby")).textContent).toBe("Conditions");
    const help = byId("clock-step-conditions-help");
    expect(help.textContent).toContain("Add category");
    expect(help.textContent).toContain("MM:SS");
    expect(help.textContent).toContain("*");
    const [row] = conditions();
    const [, value, upper] = row.querySelectorAll("input[type=text]");
    const beetsField = row.querySelectorAll("select")[4];
    expect(value.hasAttribute("aria-describedby")).toBe(false);
    expect(upper.hasAttribute("aria-describedby")).toBe(false);
    // The Beets notice is linked only while it shows, since a hidden one would still be read.
    expect(beetsField.hasAttribute("aria-describedby")).toBe(false);
    row.querySelector('option[value="beets"]').disabled = false;
    setField(row, "beets");
    expect(byId("clock-step-beets-notice").hidden).toBe(false);
    expect(beetsField.getAttribute("aria-describedby")).toBe("clock-step-beets-notice");
    setField(row, "year");
    expect(byId("clock-step-beets-notice").hidden).toBe(true);
    expect(beetsField.hasAttribute("aria-describedby")).toBe(false);
  });

  it("says each condition added or removed, numbers Remove buttons and keeps focus in place", async () => {
    await open(fakeServer());
    startMusicStep();
    byId("clock-add-condition").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 added."));
    byId("clock-add-condition").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 3 added."));
    expect(document.activeElement).toBe(conditions()[2].querySelector("select"));
    const removeButtons = () => conditions().map((row) => row.querySelector("button[data-remove-condition]").textContent);
    expect(removeButtons()).toEqual(["Remove condition 1", "Remove condition 2", "Remove condition 3"]);
    const third = conditions()[2];
    const oldLegend = third.querySelector("legend");
    let legendAtFocus = null;
    third.querySelector("select").addEventListener("focus", () => {
      legendAtFocus = third.querySelector("legend").textContent;
    }, { once: true });
    conditions()[1].querySelector("button[data-remove-condition]").focus();
    conditions()[1].querySelector("button[data-remove-condition]").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 removed, 2 conditions left."));
    // The condition that took its place has focus, renumbered before focus
    // arrived, with a new legend so its group's name is worked out again (#326).
    expect(document.activeElement).toBe(third.querySelector("select"));
    expect(legendAtFocus).toBe("Condition 2");
    expect(third.querySelector("legend")).not.toBe(oldLegend);
    expect(third.querySelector("legend").textContent).toBe("Condition 2");
    expect(removeButtons()).toEqual(["Remove condition 1", "Remove condition 2"]);
    const first = conditions()[0];
    third.querySelector("button[data-remove-condition]").click();
    await vi.waitFor(() => expect(status()).toBe("Condition 2 removed, 1 condition left."));
    // The last one gone, the one before it has focus.
    expect(document.activeElement).toBe(first.querySelector("select"));
    first.querySelector("button[data-remove-condition]").click();
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
    expect(byId("clock-step-form-error").textContent).toBe("Enter a value for artist.");
    expect(byId("clock-step-form-error").hidden).toBe(false);
    expect(document.activeElement).toBe(first.querySelectorAll("input[type=text]")[1]);
    expect(document.activeElement.getAttribute("aria-describedby")).toContain("clock-step-form-error");
    expect(first.querySelectorAll("input[type=text]")[1].getAttribute("aria-invalid")).toBe("true");
    first.querySelectorAll("input[type=text]")[1].value = "Björk";
    byId("clock-add-step").click();
    expect(byId("clock-step-form-error").textContent).toBe("Enter the duration as seconds or minutes:seconds, for example 90 or 1:30.");
    expect(high.getAttribute("aria-describedby")).toContain("clock-step-form-error");
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
    expect(byId("clock-step-form-error").textContent).toBe("Duration must be non-negative.");
    expect(byId("clock-step-form-error").hidden).toBe(false);
    expect(document.activeElement).toBe(value);
    expect(value.getAttribute("aria-describedby")).toContain("clock-step-form-error");
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
    expect(byId("clock-step-form-error").textContent).toBe("Enter a value for artist.");
    expect(byId("clock-step-form-error").hidden).toBe(false);
    expect(document.activeElement).toBe(first.querySelectorAll("input[type=text]")[1]);
    expect(document.activeElement.getAttribute("aria-describedby")).toContain("clock-step-form-error");
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

  it("adds and edits an ad break step with its length, spots and filters", async () => {
    const server = await open(fakeServer({
      ads: { ad_advertisers: ["Example Motors", "Sample Bakery"], ad_categories: ["Car dealers"], ads_reason: "Ad breaks are off for this station." },
    }));
    byId("clock-new-step").click();
    const type = byId("clock-step-type");
    const shown = () => [...document.querySelectorAll(".clock-step-field")].filter((field) => !field.hidden)
      .map((field) => field.querySelector("input, select").id.replace("clock-step-", ""));
    type.value = "ad_break";
    type.dispatchEvent(new Event("change"));
    expect(shown()).toEqual(["ad-spots"]);
    expect(byId("clock-step-ads-notice").hidden).toBe(false);
    expect(byId("clock-step-ads-notice").textContent).toBe("Ad breaks are off for this station.");
    expect([...byId("clock-step-ad-advertiser").options].map((option) => option.textContent))
      .toEqual(["Any advertiser", "Example Motors", "Sample Bakery"]);
    expect([...byId("clock-step-ad-category").options].map((option) => option.textContent)).toEqual(["Any category", "Car dealers"]);
    expect(byId("clock-step-ad-spots").value).toBe("3");
    expect(byId("clock-step-ad-seconds").value).toBe("0");
    const seconds = byId("clock-step-ad-seconds");
    seconds.value = "5";
    byId("clock-add-step").click();
    expect(document.activeElement).toBe(seconds);
    expect(seconds.getAttribute("aria-invalid")).toBe("true");
    expect(seconds.getAttribute("aria-describedby")).toBe("clock-step-ad-error");
    expect(byId("clock-step-ad-error").hidden).toBe(false);
    expect(byId("clock-step-ad-error").textContent).toBe("Longest break must be 0 for no limit, or from 10 to 900 seconds.");
    expect(status()).not.toBe("Longest break must be 0 for no limit, or from 10 to 900 seconds.");
    expect(rows()).toHaveLength(3);
    seconds.value = "60";
    seconds.dispatchEvent(new Event("input", { bubbles: true }));
    expect(seconds.hasAttribute("aria-invalid")).toBe(false);
    byId("clock-step-ad-spots").value = "11";
    byId("clock-add-step").click();
    expect(document.activeElement).toBe(byId("clock-step-ad-spots"));
    expect(byId("clock-step-ad-error").textContent).toBe("Spots in the break must be from 1 to 10.");
    byId("clock-step-ad-spots").value = "2";
    byId("clock-step-ad-advertiser").value = "Example Motors";
    byId("clock-add-step").click();
    expect(byId("clock-step-ad-error").hidden).toBe(true);
    expect(byId("clock-step-ad-spots").hasAttribute("aria-invalid")).toBe(false);
    await vi.waitFor(() => expect(status()).toBe("Added Step 4 of 4, ad break, up to 2 spots, up to 60 seconds, from Example Motors. Not saved yet."));
    expect(byId("clock-step-ad-spots").value).toBe("3");
    expect(byId("clock-step-ad-advertiser").value).toBe("");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toContain("Saved Morning, 4 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.body.steps[3]).toEqual({ type: "ad_break", max_spots: 2, max_seconds: 60, advertiser: "Example Motors" });

    // A saved advertiser with no spot now stays chosen, said as such.
    server.clocks.Morning.steps[3] = { type: "ad_break", max_spots: 1, max_seconds: 0, advertiser: "Gone Shop", category: "Car dealers" };
    controls.destroy();
    await open(server);
    rowButton(3, "edit").click();
    expect(type.value).toBe("ad_break");
    expect(document.activeElement).toBe(type);
    expect(byId("clock-step-ad-spots").value).toBe("1");
    expect(byId("clock-step-ad-advertiser").value).toBe("Gone Shop");
    expect(byId("clock-step-ad-advertiser").selectedOptions[0].textContent).toBe("Gone Shop (no spot now)");
    expect(byId("clock-step-ad-category").value).toBe("Car dealers");
    byId("clock-step-ad-category").value = "";
    byId("clock-add-step").click();
    expect(rowText(3)).toBe("4. ad break, up to 1 spot, from Gone Shop");
  });
});
