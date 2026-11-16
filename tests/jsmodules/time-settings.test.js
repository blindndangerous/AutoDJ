import { beforeEach, describe, expect, it, vi } from "vitest";
import { clockText, setTimeFormat } from "../../src/autodj/static/modules/time-format.js";
import { installTimeSettings } from "../../src/autodj/static/modules/time-settings.js";
import { selectStation } from "../../src/autodj/static/modules/stations.js";

const { groups, choose, confirm } = vi.hoisted(() => ({ groups: [], choose: vi.fn(), confirm: vi.fn() }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    groups.push(spec);
    return { accept: (body) => spec.write(body), dirty: () => false };
  },
}));
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({
  chooseAction: choose,
  confirmAction: confirm,
}));

const when = new Date(2026, 9, 14, 15, 4, 30);

describe("time format", () => {
  beforeEach(() => { groups.length = 0; setTimeFormat("24"); });

  it("writes 24-hour times unless set to 12 hour", () => {
    expect(clockText(when, { locale: "en-US" })).toBe("15:04");
    setTimeFormat("12");
    expect(clockText(when, { locale: "en-US", seconds: true })).toBe("3:04:30 PM");
  });

  it("loads time settings and exposes the station time draft", async () => {
    document.body.innerHTML = `
      <div id="spanel-stations" role="tabpanel"><div id="stations-status"></div>
      <select id="time-zone"></select></div>
      <div id="spanel-server" role="tabpanel">
      <select id="time-format"><option value="24">24</option><option value="12">12</option></select></div>`;
    const view = (timezone, format) => ({
      timezone, running_timezone: "UTC", time_format: format, timezones: ["Pacific/Auckland", "UTC"],
    });
    const request = vi.fn(async () => view("UTC", "24"));
    const settings = installTimeSettings({ request });
    await settings.load();
    expect(document.getElementById("time-zone").options[0].textContent).toBe("Pacific/Auckland");
    document.getElementById("time-zone").value = "Pacific/Auckland";
    document.getElementById("time-format").value = "12";
    const drafts = groups.filter((group) => group.group === "time");
    expect(drafts.find((group) => group.panel.id === "spanel-stations").read())
      .toEqual({ timezone: "Pacific/Auckland" });
    expect(drafts.find((group) => group.panel.id === "spanel-server").read())
      .toEqual({ time_format: "12" });
    expect(request).toHaveBeenCalledTimes(2);
    drafts.find((group) => group.panel.id === "spanel-server").write({ time_format: "12" });
    expect(clockText(when, { locale: "en-US" })).toBe("3:04 PM");
  });
});


it("exposes station output and cap as a draft, clearing incompatible monitoring", async () => {
  groups.length = 0;
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    </div>`;
  const options = { output: "stream", server_audio_too: true, liner_upload_max_mib: 80 };
  const request = vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    return options;
  });
  await installTimeSettings({ request }).load();
  const output = document.getElementById("station-output");
  const monitor = document.getElementById("station-monitor");
  expect(monitor.checked).toBe(true);
  output.value = "browser";
  output.dispatchEvent(new Event("change"));
  expect(monitor.checked).toBe(false);
  expect(monitor.disabled).toBe(true);
  const draft = groups.find((group) => group.group === "station");
  expect(draft.read()).toEqual({ output: "browser", server_audio_too: false, liner_upload_max_mib: 80 });
  expect(request).toHaveBeenCalledTimes(2);
});

it("edits the log label with the station options and names the station ID it replaces", async () => {
  groups.length = 0;
  selectStation("default");
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    <label for="station-log-label">Log label</label>
    <input id="station-log-label" type="text" maxlength="16" aria-describedby="station-log-label-help">
    <p id="station-log-label-help">Shown at the start of this station's log lines: up to 16 letters, digits, hyphens, underscores or dots, different from other stations' labels and IDs. Leave empty to use the station ID, <span id="station-log-label-id">default</span>.</p>
    </div>`;
  const options = { output: "stream", server_audio_too: false, liner_upload_max_mib: 50, log_label: "nor" };
  const request = vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    return options;
  });
  await installTimeSettings({ request }).load();
  const input = document.getElementById("station-log-label");
  const id = document.getElementById("station-log-label-id");
  expect(input.value).toBe("nor");
  expect(id.textContent).toBe("default");
  const draft = groups.find((group) => group.group === "station");
  // The error for a refused label is shown on this field: its key's place in read().
  const keys = Object.keys(draft.read());
  expect(draft.controls[keys.indexOf("log_label")]).toBe(input);
  input.value = "  nor-2 ";
  expect(draft.read()).toEqual({
    output: "stream", server_audio_too: false, liner_upload_max_mib: 50, log_label: "nor-2",
  });
  input.value = "";
  expect(draft.read().log_label).toBe("");
  // A station without a label shows an empty field, not "null".
  draft.write({ ...options, log_label: null });
  expect(input.value).toBe("");
  selectStation("nightowl");
  expect(id.textContent).toBe("nightowl");
  selectStation("default");
});

it("stages a station library choice and restarts only after a successful save", async () => {
  groups.length = 0;
  selectStation("default");
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    <select id="station-library"><option value="main">Main</option><option value="oldies">Oldies</option></select>
    </div>`;
  const request = vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    if (url.includes("/check?")) return { running: false, warnings: [] };
    return {};
  });
  await installTimeSettings({ request }).load();
  const library = document.getElementById("station-library");
  const draft = groups.find((group) => group.group === "station" && group.controls?.includes(library));
  expect(draft.read()).toEqual({ library_id: "main" });
  library.value = "oldies";
  expect(draft.read()).toEqual({ library_id: "oldies" });
  expect(await draft.beforeSave(draft.read(), { library_id: "main" }, {
    station: "default", current: () => true,
  })).toBe("next");
  expect(request).toHaveBeenCalledWith("/api/libraries/oldies/check?station=default");

  await draft.afterSave(draft.read(), {
    station: "default", current: () => true, decision: "restart",
  });
  expect(request.mock.calls.map(([url, options]) => [url, options?.method])).toEqual(expect.arrayContaining([
    ["/api/stations/default/stop", "POST"],
    ["/api/stations/default/start", "POST"],
  ]));
});

async function stationLibraryDraft({ check = { running: true, warnings: [] }, requestOverride } = {}) {
  groups.length = 0;
  selectStation("default");
  document.body.innerHTML = `
    <div id="spanel-stations"><div id="stations-status"></div>
    <select id="time-zone"></select><select id="time-format"></select>
    <select id="station-output"><option value="browser">Browser</option><option value="stream">Stream</option></select>
    <input id="station-monitor" type="checkbox"><input id="station-upload-cap" type="number">
    <select id="station-library"><option value="main">Main</option><option value="oldies">Oldies</option></select>
    </div>`;
  const request = requestOverride || vi.fn(async (url) => {
    if (url === "/api/time-settings") return { timezone: "UTC", timezones: ["UTC"], time_format: "24" };
    if (url.includes("/check?")) return check;
    return {};
  });
  await installTimeSettings({ request }).load();
  const library = document.getElementById("station-library");
  const draft = groups.find((group) => group.group === "station" && group.controls?.includes(library));
  library.value = "oldies";
  return { draft, library, request };
}

// ADR-0023-06
it("offers all three running station choices and their warning text", async () => {
  for (const [answer, result] of [["accept", "restart"], ["alternate", "next"], ["cancel", false]]) {
    choose.mockReset().mockResolvedValue(answer);
    confirm.mockReset();
    const { draft, request } = await stationLibraryDraft({ check: {
      running: true, warnings: ["Clockwheel: Morning uses a missing category."],
    } });
    expect(await draft.beforeSave(draft.read(), { library_id: "main" }, {
      station: "default", current: () => true,
    })).toBe(result);
    expect(request).toHaveBeenCalledWith("/api/libraries/oldies/check?station=default");
    expect(choose).toHaveBeenCalledWith(document, expect.objectContaining({
      choices: ["Save and restart now", "Save for next start", "Cancel"],
    }));
    expect(choose.mock.calls[0][1].message).toContain("Clockwheel: Morning uses a missing category.");
    expect(confirm).not.toHaveBeenCalled();
  }
});

// ADR-0023-06
it("asks before saving a stopped station's library when programming warnings exist", async () => {
  choose.mockReset();
  confirm.mockReset().mockResolvedValue(false);
  const { draft } = await stationLibraryDraft({ check: {
    running: false, warnings: ["Schedule: Thursday show is unavailable."],
  } });
  expect(await draft.beforeSave(draft.read(), { library_id: "main" }, {
    station: "default", current: () => true,
  })).toBe(false);
  expect(confirm).toHaveBeenCalledWith(document, expect.objectContaining({
    title: "Save music library",
    message: expect.stringContaining("Schedule: Thursday show is unavailable."),
  }));
  confirm.mockResolvedValue(true);
  expect(await draft.beforeSave(draft.read(), { library_id: "main" }, {
    station: "default", current: () => true,
  })).toBe("next");
});

// ADR-0023-06
it("drops a library preflight reply after the station context changes", async () => {
  let finishCheck;
  choose.mockReset();
  confirm.mockReset();
  const request = vi.fn((url) => url.includes("/check?")
    ? new Promise((resolve) => { finishCheck = resolve; })
    : Promise.resolve({ timezone: "UTC", timezones: ["UTC"], time_format: "24" }));
  const { draft } = await stationLibraryDraft({ requestOverride: request });
  const current = vi.fn(() => true);
  const pending = draft.beforeSave(draft.read(), { library_id: "main" }, { station: "default", current });
  current.mockReturnValue(false);
  finishCheck({ running: true, warnings: [] });
  expect(await pending).toBe(false);
  expect(choose).not.toHaveBeenCalled();
});
