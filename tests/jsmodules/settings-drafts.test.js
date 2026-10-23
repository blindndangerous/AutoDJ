import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ request: vi.fn(), choose: vi.fn(), station: "default", epoch: 1 }));
vi.mock("../../src/autodj/static/modules/api-client.js", () => ({
  requestJson: mocks.request, captureAuthenticatedRequestEpoch: () => mocks.epoch,
  isAuthenticatedRequestCurrent: (epoch) => epoch === mocks.epoch,
}));
vi.mock("../../src/autodj/static/modules/stations.js", () => ({
  currentStation: () => mocks.station, stationName: () => mocks.station,
}));
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({ chooseAction: mocks.choose }));

let drafts, panel, input;
function snapshot(revision = "one", speed = 0) {
  return { revision, values: { playback: { music_speed_percent: speed } } };
}
function register(group = "playback", extras = {}) {
  return drafts.registerSettingsGroup({ panel, group, controls: [input],
    read: () => ({ music_speed_percent: Number(input.value) }),
    write: (body) => { input.value = body.music_speed_percent; }, ...extras });
}
function edit(value) {
  input.value = String(value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

beforeEach(async () => {
  vi.resetModules();
  mocks.request.mockReset(); mocks.choose.mockReset(); mocks.station = "default"; mocks.epoch = 1;
  document.body.innerHTML = '<div id="spanel-sound"><input type="number" value="0" min="-10" max="10"></div>';
  panel = document.querySelector("div"); input = document.querySelector("input");
  drafts = await import("../../src/autodj/static/modules/settings-drafts.js");
});

describe("Settings drafts", () => {
  it("preserves edits made before the initial settings load", () => {
    register(); edit(4);
    drafts.applySettingsDrafts(snapshot("one", 1));
    expect(input.value).toBe("4");
    expect(drafts.settingsHaveChanges()).toBe(true);
  });
  it("holds a stale push after saving until the matching echo arrives", async () => {
    const part = register(); drafts.applySettingsDrafts(snapshot()); edit(3);
    mocks.request.mockResolvedValue({ revision: "two", applied: ["playback"], settings: snapshot("two", 3) });
    await drafts.saveSettingsPanel(panel);
    part.accept({ music_speed_percent: 0 });
    expect(input.value).toBe("3");
    part.accept({ music_speed_percent: 3 });
    part.accept({ music_speed_percent: 2 });
    expect(input.value).toBe("2");
  });
  // ADR-0017-01
  it("does not write on edit and submits changed fields once on Save", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(3);
    expect(mocks.request).not.toHaveBeenCalled();
    expect(panel.querySelector('[data-settings-save]').disabled).toBe(false);
    mocks.request.mockResolvedValue({ revision: "two", applied: ["playback"], settings: snapshot("two", 3) });
    expect(await drafts.saveSettingsPanel(panel)).toBe(true);
    expect(JSON.parse(mocks.request.mock.calls[0][1].body)).toEqual({ revision: "one", changes: { playback: { music_speed_percent: 3 } } });
    expect(drafts.settingsHaveChanges()).toBe(false);
  });
  it("uses one bar and merges participants in the same group", async () => {
    register();
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; panel.append(checkbox);
    drafts.registerSettingsGroup({ panel, group: "playback", controls: [checkbox],
      read: () => ({ prefetch_next_track: checkbox.checked }), write: (value) => { checkbox.checked = value.prefetch_next_track; } });
    drafts.applySettingsDrafts(snapshot()); edit(2); checkbox.checked = true;
    expect(panel.querySelectorAll('[data-settings-save]')).toHaveLength(1);
    mocks.request.mockResolvedValue({ revision: "two", applied: ["playback"], settings: snapshot("two", 2) });
    await drafts.saveSettingsPanel(panel);
    expect(JSON.parse(mocks.request.mock.calls[0][1].body).changes.playback).toEqual({ music_speed_percent: 2, prefetch_next_track: true });
  });
  it("preserves drafts against pushes and discards to the latest server values", () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(4);
    drafts.applySettingsDrafts(snapshot("other", 1));
    expect(input.value).toBe("4");
    mocks.request.mockResolvedValue(snapshot("other", 1));
    drafts.discardSettingsPanel(panel);
    expect(input.value).toBe("1"); expect(drafts.settingsHaveChanges()).toBe(false);
  });
  it("retains a draft and reports a revision conflict", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(4);
    mocks.request.mockRejectedValue(Object.assign(new Error("changed"), { status: 409 }));
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    expect(input.value).toBe("4"); expect(drafts.settingsHaveChanges()).toBe(true);
    await vi.waitFor(() => expect(panel.textContent).toContain("changed elsewhere"));
  });
  it("retains invalid edits and focuses the invalid field without writing", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(20);
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    expect(document.activeElement).toBe(input);
    expect(mocks.request).not.toHaveBeenCalled(); expect(input.value).toBe("20");
  });
  it("links a refused server field to its error and preserves the draft", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(4);
    mocks.request.mockRejectedValue(Object.assign(new Error("Music speed was refused"), { field: "music_speed_percent", status: 422 }));
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    expect(document.activeElement).toBe(input);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toContain("spanel-sound-save-error");
    expect(drafts.settingsHaveChanges()).toBe(true);
  });
  // ADR-0017-02
  it("blocks duplicate submits and preserves edits made during a request", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    let finish; mocks.request.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const saving = drafts.saveSettingsPanel(panel);
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    edit(3);
    finish({ revision: "two", applied: ["playback"], settings: snapshot("two", 2) });
    await saving;
    expect(input.value).toBe("3"); expect(drafts.settingsHaveChanges()).toBe(true);
    expect(mocks.request).toHaveBeenCalledTimes(1);
  });
  it("drops a reply after station switching", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    let finish; mocks.request.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const saving = drafts.saveSettingsPanel(panel);
    drafts.resetSettingsDrafts(); mocks.station = "other";
    drafts.applySettingsDrafts(snapshot("other", 7));
    finish({ revision: "two", applied: ["playback"], settings: snapshot("two", 2) });
    expect(await saving).toBe(false); expect(input.value).toBe("7");
  });
  it("drops a reply after authentication changes", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    let finish; mocks.request.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const saving = drafts.saveSettingsPanel(panel); mocks.epoch += 1;
    finish({ revision: "two", applied: ["playback"], settings: snapshot("two", 2) });
    expect(await saving).toBe(false); expect(drafts.settingsHaveChanges()).toBe(true);
  });
  it("does not claim success for a failed storage owner", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    mocks.request.mockResolvedValue({ revision: "one", applied: [], failed: { playback: "Disk full" }, settings: snapshot() });
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    expect(drafts.settingsHaveChanges()).toBe(true);
    await vi.waitFor(() => expect(panel.textContent).toContain("Disk full"));
  });
  it("keeps browser preferences out of the server request", async () => {
    const commit = vi.fn(); register(null, { commit }); edit(3);
    expect(commit).not.toHaveBeenCalled();
    expect(await drafts.saveSettingsPanel(panel)).toBe(true);
    expect(commit).toHaveBeenCalledWith({ music_speed_percent: 3 }); expect(mocks.request).not.toHaveBeenCalled();
  });
  it("keeps a browser draft after storage rejects the save", async () => {
    register(null, { commit: () => { throw new Error("Storage blocked"); } }); edit(3);
    expect(await drafts.saveSettingsPanel(panel)).toBe(false);
    expect(input.value).toBe("3"); expect(drafts.settingsHaveChanges()).toBe(true);
    await vi.waitFor(() => expect(panel.textContent).toContain("Storage blocked"));
  });
  it("refuses conflicting browser preferences before writing", async () => {
    const commit = vi.fn(); register(null, { commit, persisted: () => ({ music_speed_percent: 1 }) }); edit(3);
    expect(await drafts.saveSettingsPanel(panel)).toBe(false); expect(commit).not.toHaveBeenCalled();
  });
  it("keeps navigation on the tab when Save fails", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    mocks.choose.mockResolvedValue("accept"); mocks.request.mockRejectedValue(new Error("offline"));
    expect(await drafts.confirmSettingsNavigation()).toBe(false); expect(drafts.settingsHaveChanges()).toBe(true);
  });
  it("lets the operator stay or discard before navigation", async () => {
    register(); drafts.applySettingsDrafts(snapshot()); edit(2);
    mocks.choose.mockResolvedValue(null);
    expect(await drafts.confirmSettingsNavigation()).toBe(false);
    mocks.choose.mockResolvedValue("alternate"); mocks.request.mockResolvedValue(snapshot());
    expect(await drafts.confirmSettingsNavigation()).toBe(true); expect(input.value).toBe("0");
  });
});
