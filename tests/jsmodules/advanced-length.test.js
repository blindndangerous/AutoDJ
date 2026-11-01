import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { advancedLengthProblem, installAdvancedLength } from "../../src/autodj/static/modules/advanced-length.js";
import { importSummary } from "../../src/autodj/static/modules/library-folders.js";
import { applySettingsDrafts, resetSettingsDrafts } from "../../src/autodj/static/modules/settings-drafts.js";

const api = vi.hoisted(() => ({ request: vi.fn(), station: "default", epoch: 1 }));
vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => ({
  ...await importOriginal(),
  requestJson: api.request,
  captureAuthenticatedRequestEpoch: () => api.epoch,
  isAuthenticatedRequestCurrent: (epoch) => epoch === api.epoch,
}));
vi.mock("../../src/autodj/static/modules/stations.js", () => ({
  currentStation: () => api.station,
  stationName: () => api.station,
}));
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({ chooseAction: vi.fn() }));

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");

function mount() {
  const page = document.createElement("template");
  page.innerHTML = html;
  document.body.replaceChildren(page.content.querySelector("#spanel-server").cloneNode(true));
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const field = (id) => document.getElementById(id);
const saveButton = () => document.querySelector("#spanel-server [data-settings-save]");
const saveStatus = () => document.querySelector("#spanel-server .settings-save-status");
function setup(options = {}) {
  const controls = installAdvancedLength({ document, ...options });
  applySettingsDrafts({ revision: "one", profile: "", values: {} });
  return controls;
}
function edit(value) {
  field("advanced-length").value = String(value);
  field("advanced-length").dispatchEvent(new Event("input", { bubbles: true }));
}

describe("similarity analysis length (#391)", () => {
  beforeEach(() => {
    api.request.mockReset();
    api.station = "default";
    api.epoch = 1;
    resetSettingsDrafts(document);
    mount();
  });
  afterEach(() => vi.useRealTimers());

  it("is a labelled number field in a named group, its 0 hint linked", () => {
    const group = field("advanced-length-group");
    expect(group.querySelector("legend").textContent).toBe("Similarity analysis");
    const page = document.createElement("template");
    page.innerHTML = html;
    expect(page.content.querySelector("#advanced-length-group").closest("#spanel-server")).not.toBeNull();
    const input = field("advanced-length");
    expect(document.querySelector('label[for="advanced-length"]').textContent).toBe("Similarity analysis maximum song length (minutes)");
    expect([input.type, input.min, input.max, input.step, input.value]).toEqual(["number", "0", "600", "1", "15"]);
    expect(input.getAttribute("aria-describedby")).toBe("advanced-length-help");
    expect(field("advanced-length-help").textContent).toBe("0 means no limit. Longer tracks still get audio analysis when selected.");
    expect(field("advanced-length-error").hidden).toBe(true);
    setup({ request: vi.fn() });
    expect(document.querySelectorAll("#spanel-server [data-settings-save]")).toHaveLength(1);
    expect(document.querySelectorAll("#spanel-server [data-settings-discard]")).toHaveLength(1);
    expect(field("advanced-length-save")).toBeNull();
  });

  it("refuses values the server would refuse", () => {
    for (const ok of ["0", "15", " 90 ", "600"]) expect(advancedLengthProblem(ok)).toBeNull();
    for (const bad of ["", "-1", "1.5", "601", "ten"]) expect(advancedLengthProblem(bad)).toMatch(/whole number of minutes from 0 to 600/);
  });

  it("loads the saved value and says what a save means", async () => {
    const request = vi.fn(async (url, options) => (options ? JSON.parse(options.body) : { minutes: 30 }));
    const controls = setup({ request });
    await controls.load();
    expect(field("advanced-length").value).toBe("30");

    edit(0);
    saveButton().click();
    await vi.waitFor(() => expect(saveStatus().textContent)
      .toContain("Similarity analysis has no length limit."));
    expect(request).toHaveBeenLastCalledWith("/api/library/advanced-length", expect.objectContaining({ method: "PUT", body: JSON.stringify({ minutes: 0 }) }));

    edit(45);
    saveButton().click();
    await vi.waitFor(() => expect(saveStatus().textContent)
      .toContain("Songs longer than 45 minutes get audio analysis only."));
    expect(saveStatus().textContent).toContain("Settings saved for this server.");
  });

  it("puts a refused value's message under the field, linked and focused", () => {
    const request = vi.fn();
    setup({ request });
    const input = field("advanced-length");
    edit(700);
    saveButton().click();
    const error = field("advanced-length-error");
    expect(request).not.toHaveBeenCalled();
    expect(error.hidden).toBe(false);
    expect(error.textContent.trim()).not.toBe("");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toContain("advanced-length-error");
    expect(input.getAttribute("aria-describedby")).toContain("advanced-length-help");
    expect(document.activeElement).toBe(input);
    edit(60);
    expect(error.hidden).toBe(true);
    expect(input.hasAttribute("aria-invalid")).toBe(false);
    expect(input.getAttribute("aria-describedby")).toBe("advanced-length-help");
  });

  it("marks Save busy, not disabled, and says Saving only when the reply is slow", async () => {
    vi.useFakeTimers();
    let answer;
    const request = vi.fn(() => new Promise((resolve) => { answer = resolve; }));
    setup({ request });
    const save = saveButton();
    edit(16);
    save.focus();
    save.click();
    expect(save.getAttribute("aria-busy")).toBe("true");
    expect(save.getAttribute("aria-disabled")).toBe("false");
    expect(save.disabled).toBe(false);
    save.click();
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(saveStatus().textContent).toContain("Saving changes");
    answer({ minutes: 16 });
    await vi.advanceTimersByTimeAsync(0);
    expect(save.hasAttribute("aria-busy")).toBe(false);
    expect(saveStatus().textContent).toContain("Songs longer than 16 minutes get audio analysis only.");
  });

  it("ignores pending and late save messages after the session is cleared", async () => {
    vi.useFakeTimers();
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const controls = setup({ request });
    edit(16);
    saveButton().click();
    controls.clear();
    api.epoch += 1;
    resetSettingsDrafts(document);
    await vi.advanceTimersByTimeAsync(2100);
    expect(saveStatus().textContent).toBe("");
    finish({ minutes: 45 });
    await vi.advanceTimersByTimeAsync(0);
    expect(saveStatus().textContent).toBe("");
    expect(field("advanced-length").value).toBe("15");
  });

  it("reports a failed save in the status line", async () => {
    const request = vi.fn(async () => { throw Object.assign(new Error("Server unavailable"), { status: 503 }); });
    setup({ request });
    edit(16);
    saveButton().click();
    await flush();
    await vi.waitFor(() => expect(saveStatus().textContent)
      .toContain("Could not save all settings: Server unavailable"));
    expect(document.querySelector("#spanel-server .settings-save-bar > p:nth-of-type(2)").textContent)
      .toContain("Unsaved changes");
  });
});

describe("import summary (#391)", () => {
  it("gives counts, never the joined paths", () => {
    const errors = ["/music/a.flac: could not decode", "/music/b.mp3: could not decode"];
    expect(importSummary({ import: { running: false, errors }, tracks_ready: 1204, tracks_indexed: 1100 }))
      .toBe("Import finished with 2 errors; the list follows. 1,204 tracks ready; 1,100 with similarity analysis.");
    expect(importSummary({ import: { running: true, phase: "reading tracks", done: 3, total: 9, current: "/music/c.flac", errors: [errors[0]] } }))
      .toBe("Importing tracks (reading tracks): 3 of 9; /music/c.flac; 1 error so far");
    expect(importSummary({ import: { errors: [] }, tracks_ready: 1, tracks_indexed: 0 })).toBe("1 track ready; 0 with similarity analysis.");
  });

  it("says how many long songs have audio analysis only, without calling them errors", () => {
    expect(importSummary({ import: { errors: [] }, tracks_ready: 60, tracks_indexed: 1, too_long_for_analysis: 59, advanced_max_minutes: 15 }))
      .toBe("60 tracks ready; 1 with similarity analysis. 59 songs longer than 15 minutes have audio analysis only.");
    expect(importSummary({ import: { errors: [] }, tracks_ready: 2, tracks_indexed: 1, too_long_for_analysis: 1, advanced_max_minutes: 20 }))
      .toBe("2 tracks ready; 1 with similarity analysis. 1 song longer than 20 minutes has audio analysis only.");
  });
});
