import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeStep, installClockwheelPanel, parseResyncMinutes } from "../../src/autodj/static/modules/clockwheel-panel.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => byId("clock-status").textContent;
const rows = () => [...byId("clock-slots").querySelectorAll("li[data-index]")];
const rowText = (index) => rows()[index].querySelector("span").textContent;
const rowButton = (index, action) => rows()[index].querySelector(`button[data-action="${action}"]`);

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
function fakeServer({ notice = null } = {}) {
  const server = { clocks: { Morning: copy(MORNING) }, active: "Morning", calls: [], fail: null };
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.fail && method !== "GET") throw server.fail;
    const names = () => Object.keys(server.clocks).sort();
    const snapshot = () => ({
      names: names(), active: server.active, sources: SOURCES, presets: ["night", "wakeup"],
      types: ["liner", "music", "settings", "track"], cursor: 1, last_status: "Ready", notice,
    });
    if (url === "/api/library/clock" && method === "GET") return snapshot();
    if (url === "/api/library/clock" && method === "POST") { server.clocks[body.name] = body; return body; }
    if (url === "/api/library/clock/active") { server.active = body.name; return snapshot(); }
    if (url === "/api/library/clock/import") {
      server.clocks[body.name] = { version: 1, name: body.name, steps: [MUSIC], resync_minutes: body.resync_minutes };
      return server.clocks[body.name];
    }
    const [, name, verb] = /^\/api\/library\/clock\/([^/]+)(?:\/(\w+))?$/.exec(url);
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
    expect(byId("clock-slots").tagName).toBe("OL");
    expect(byId("clock-slots").getAttribute("aria-labelledby")).toBe("clock-steps-heading");
    expect(byId("clock-list").getAttribute("aria-labelledby")).toBe("clock-list-heading");
    expect(byId("clock-status").getAttribute("role")).toBe("status");
    expect(byId("clock-name").maxLength).toBe(64);
    expect(byId("clock-step-legend").closest("fieldset").id).toBe("clock-step-form");
    expect(section.querySelector("[aria-controls]")).toBeNull();
    expect(byId("clock-steps-help").textContent).toContain("Alt+Up and Alt+Down");
    expect(byId("clock-steps-help").textContent).toContain("Save clockwheel");
    expect(document.querySelectorAll('[id^="clock-"]').length).toBe(new Set([...document.querySelectorAll('[id^="clock-"]')].map((node) => node.id)).size);
  });
});

describe("clockwheel editor", () => {
  it("validates restart minutes and describes steps in words", () => {
    expect(parseResyncMinutes("30, 0, 30")).toEqual([0, 30]);
    expect(parseResyncMinutes(" ")).toEqual([]);
    expect(() => parseResyncMinutes("61")).toThrow("0 to 59");
    expect(() => parseResyncMinutes("a")).toThrow("whole minutes");
    expect(describeStep(MUSIC, SOURCES)).toBe("music from Rock");
    expect(describeStep(MORNING.steps[1], SOURCES)).toBe("liner from Top, at random, ignoring repeat rules");
    expect(describeStep({ ...MUSIC, recursive: true, source: "gone" }, SOURCES)).toBe("music from a folder that is no longer registered, with subfolders");
    expect(describeStep({ type: "settings", preset: "night" })).toBe("switch to preset night");
    expect(describeStep({ type: "time_check" })).toBe("time check step");
  });

  it("opens the active clockwheel quietly and lists each step with its position", async () => {
    await open(fakeServer());
    expect(rowText(0)).toBe("Step 1 of 3, music from Rock");
    expect(rowText(2)).toBe("Step 3 of 3, track Album/Song.flac");
    expect(byId("clock-open-name").textContent).toBe("Morning");
    expect(byId("clock-next").textContent).toBe("Plays next: Step 2 of 3, liner from Top, at random, ignoring repeat rules.");
    expect(byId("clock-list").querySelector("button").textContent).toBe("Morning, active");
    expect(byId("clock-list").querySelector("button").getAttribute("aria-current")).toBe("true");
    const button = rowButton(1, "move-up");
    expect(button.textContent).toBe("Move up step 2");
    expect(button.querySelector(".visually-hidden").textContent).toBe(" step 2");
    expect(rowButton(0, "move-up").disabled).toBe(true);
    expect(rowButton(2, "move-down").disabled).toBe(true);
    expect(status()).toBe("");
  });

  it("moves a step with Alt+Down, keeps focus on it and says the new position", async () => {
    const server = await open(fakeServer());
    rowButton(0, "edit").focus();
    altKey(rowButton(0, "edit"), "ArrowDown");
    expect(document.activeElement).toBe(rowButton(1, "edit"));
    expect(rowText(1)).toBe("Step 2 of 3, music from Rock");
    await vi.waitFor(() => expect(status()).toBe("Moved. Step 2 of 3, music from Rock. Not saved yet."));
    expect(byId("clock-next").textContent).toBe("Save to play these changes.");
    altKey(document.activeElement, "ArrowUp");
    expect(document.activeElement).toBe(rowButton(0, "edit"));
    altKey(document.activeElement, "ArrowUp");
    await vi.waitFor(() => expect(status()).toBe("This is already the first step."));
    altKey(document.activeElement, "ArrowDown");
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Morning, 3 steps."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/library/clock/Morning");
    expect(put.body.steps.map((step) => step.type)).toEqual(["liner", "music", "track"]);
    expect(put.body.resync_minutes).toEqual([0]);
  });

  it("moves with the buttons and moves focus to the other move button at an end", async () => {
    await open(fakeServer());
    rowButton(1, "move-down").click();
    expect(rowText(2)).toBe("Step 3 of 3, liner from Top, at random, ignoring repeat rules");
    expect(document.activeElement).toBe(rowButton(2, "move-up"));
    rowButton(1, "move-up").click();
    expect(document.activeElement).toBe(rowButton(0, "move-down"));
    expect(rowText(0)).toBe("Step 1 of 3, track Album/Song.flac");
  });

  it("duplicates and deletes steps, keeping focus in the list", async () => {
    await open(fakeServer());
    rowButton(0, "duplicate").click();
    expect(rows()).toHaveLength(4);
    expect(document.activeElement).toBe(rowButton(1, "duplicate"));
    await vi.waitFor(() => expect(status()).toBe("Duplicated as Step 2 of 4, music from Rock. Not saved yet."));
    rowButton(3, "delete").click();
    expect(rows()).toHaveLength(3);
    expect(document.activeElement).toBe(rowButton(2, "edit"));
    await vi.waitFor(() => expect(status()).toBe("Deleted step 4, track Album/Song.flac. 3 steps remain. Not saved yet."));
    for (let left = 3; left > 0; left -= 1) rowButton(0, "delete").click();
    expect(rows()).toHaveLength(0);
    expect(byId("clock-slots").textContent).toBe("No steps yet. Add one with the form below.");
    expect(document.activeElement).toBe(byId("clock-step-type"));
  });

  it("edits a step in the form and returns focus to it", async () => {
    await open(fakeServer());
    rowButton(1, "edit").click();
    expect(document.activeElement).toBe(byId("clock-step-type"));
    expect(byId("clock-step-legend").textContent).toBe("Edit step 2");
    expect(byId("clock-add-step").textContent).toBe("Update step 2");
    expect(byId("clock-cancel-edit").hidden).toBe(false);
    expect(byId("clock-step-source").value).toBe("ids");
    expect(byId("clock-step-folder").value).toBe("Top");
    expect(byId("clock-step-enforce-rules").checked).toBe(false);
    byId("clock-step-folder").value = "Top/Hour";
    byId("clock-step-selection").value = "least_recent";
    byId("clock-add-step").click();
    expect(rowText(1)).toBe("Step 2 of 3, liner from Top/Hour, ignoring repeat rules");
    expect(document.activeElement).toBe(rowButton(1, "edit"));
    expect(byId("clock-step-legend").textContent).toBe("Add a step");
    rowButton(2, "edit").click();
    byId("clock-cancel-edit").click();
    expect(document.activeElement).toBe(rowButton(2, "edit"));
    expect(byId("clock-add-step").textContent).toBe("Add step");
  });

  it("shows only the fields of the chosen step type and refuses paths that leave the folder", async () => {
    await open(fakeServer());
    const type = byId("clock-step-type");
    const shown = () => [...document.querySelectorAll(".clock-step-field")].filter((field) => !field.hidden)
      .map((field) => field.querySelector("input, select").id.replace("clock-step-", ""));
    expect(shown()).toEqual(["source", "folder", "recursive", "selection", "enforce-rules"]);
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
    expect(rowText(3)).toBe("Step 4 of 4, track Album/Other.flac");
    type.value = "liner";
    type.dispatchEvent(new Event("change"));
    expect([...byId("clock-step-source").options].map((option) => option.value)).toEqual(["ids"]);
    type.value = "settings";
    type.dispatchEvent(new Event("change"));
    expect(shown()).toEqual(["preset"]);
    byId("clock-step-preset").value = "night";
    byId("clock-add-step").click();
    await vi.waitFor(() => expect(status()).toBe("Added Step 5 of 5, switch to preset night. Not saved yet."));
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
    byId("clock-step-source").value = "jazz";
    byId("clock-step-folder").value = "Late/Slow";
    byId("clock-step-recursive").checked = true;
    byId("clock-add-step").click();
    byId("clock-resync-minutes").value = "30, 0";
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Night, 1 step."));
    const post = server.calls.find((call) => call.method === "POST" && call.url === "/api/library/clock");
    expect(post.body).toEqual({ version: 1, name: "Night", resync_minutes: [0, 30], steps: [
      { type: "music", source: "jazz", folder: "Late/Slow", recursive: true, selection: "least_recent", enforce_rules: true },
    ] });
    expect(byId("clock-open-name").textContent).toBe("Night");
  });

  it("focuses the step the server refused and says why", async () => {
    const server = await open(fakeServer());
    rowButton(0, "duplicate").click();
    server.fail = Object.assign(new Error("Step 3: its music folder is not registered"), { status: 400 });
    byId("clock-save").focus();
    byId("clock-save").click();
    await vi.waitFor(() => expect(status()).toBe("Step 3: its music folder is not registered"));
    expect(document.activeElement).toBe(rowButton(2, "edit"));
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
