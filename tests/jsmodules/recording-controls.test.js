import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { createRecordingControls } from "../../src/autodj/static/modules/recording-controls.js";
import { requestJson } from "../../src/autodj/static/modules/api-client.js";

vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => {
  const original = await importOriginal();
  return { ...original, requestJson: vi.fn() };
});

const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    const part = { ...spec, baseline: null };
    groups.push(part);
    return {
      accept(body) {
        if (part.baseline !== null && JSON.stringify(part.read()) !== JSON.stringify(part.baseline)) return;
        part.write(body);
        part.baseline = JSON.parse(JSON.stringify(part.read()));
      },
      dirty: () => part.baseline !== null && JSON.stringify(part.read()) !== JSON.stringify(part.baseline),
    };
  },
}));

const htmlSource = readFileSync(
  join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8",
);

const byId = (id) => document.getElementById(id);
const said = (text) => vi.waitFor(() => expect(byId("sr-status").textContent).toBe(text));

// The real Stream panel from index.html, so the tests follow its markup.
function setup() {
  const template = document.createElement("template");
  template.innerHTML = htmlSource;
  const panel = template.content.getElementById("spanel-stream");
  document.body.innerHTML = `
    <div id="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
  document.body.prepend(panel);
  panel.hidden = false;
  byId("stream-settings").hidden = false;
  return createRecordingControls(document, byId("sr-status"));
}

const STOPPED = {
  running: false, file: null, bytes: 0, error: null,
  split: "hour", retention_days: 0, free_space_mb: 100,
};
const FILE = "Station-2026-01-01_00-00-00.mp3";

describe("recording controls", () => {
  beforeEach(() => {
    groups.length = 0;
    vi.clearAllMocks();
    document.body.innerHTML = "";
  });

  it("stays hidden until the server reports recording status, and hides on reset", () => {
    const controls = setup();
    expect(byId("recording-controls").hidden).toBe(true);
    controls.apply(undefined);
    expect(byId("recording-controls").hidden).toBe(true);
    controls.apply(STOPPED);
    expect(byId("recording-controls").hidden).toBe(false);
    expect(byId("recording-status").textContent).toBe("Not recording.");
    expect(byId("recording-retention-days").value).toBe("0");
    expect(byId("recording-free-space-mb").value).toBe("100");
    expect(byId("recording-split").value).toBe("hour");
    controls.reset();
    expect(byId("recording-controls").hidden).toBe(true);
    expect(byId("recording-status").textContent).toBe("");
  });

  it("is a no-op without its markup", () => {
    const controls = createRecordingControls(document, null);
    expect(() => controls.apply(STOPPED)).not.toThrow();
    expect(() => controls.reset()).not.toThrow();
  });

  it("keeps one toggle name and reports state through aria-pressed", () => {
    const controls = setup();
    const toggle = byId("recording-toggle");
    controls.apply(STOPPED);
    expect(toggle.textContent).toBe("Record broadcast");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(byId("recording-split-now").getAttribute("aria-disabled")).toBe("true");
    controls.apply({ ...STOPPED, running: true, file: FILE, bytes: 3 * 1024 * 1024 });
    expect(toggle.textContent).toBe("Record broadcast");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(byId("recording-split-now").getAttribute("aria-disabled")).toBe("false");
    expect(byId("recording-status").textContent).toBe(`Recording to ${FILE}, 3.0 MB so far.`);
    controls.apply({ ...STOPPED, running: true });
    expect(byId("recording-status").textContent).toBe("Recording, waiting for audio.");
  });

  it("speaks a new error once and says when recording resumes", async () => {
    const controls = setup();
    const running = { ...STOPPED, running: true, file: FILE, bytes: 10 };
    controls.apply(running);
    expect(byId("recording-error").hidden).toBe(true);
    controls.apply({ ...running, file: null, error: "Recording volume is full" });
    expect(byId("recording-error").hidden).toBe(false);
    expect(byId("recording-error").textContent)
      .toBe("Recording error: Recording volume is full");
    await said("Recording error: Recording volume is full");
    byId("sr-status").textContent = "";
    controls.apply({ ...running, file: null, error: "Recording volume is full" });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(byId("sr-status").textContent).toBe("");
    controls.apply(running);
    expect(byId("recording-error").hidden).toBe(true);
    await said("Recording resumed.");
  });

  it("starts, splits and stops through the paired API and speaks each result", async () => {
    const controls = setup();
    controls.apply(STOPPED);
    requestJson.mockResolvedValueOnce({ ...STOPPED, running: true });
    byId("recording-toggle").click();
    await said("Recording started.");
    expect(requestJson).toHaveBeenLastCalledWith("/api/recording/start", { method: "POST" });
    expect(byId("recording-toggle").getAttribute("aria-pressed")).toBe("true");

    requestJson.mockResolvedValueOnce({ ...STOPPED, running: true });
    byId("recording-split-now").click();
    await said("Recording split. The next audio starts a new file.");
    expect(requestJson).toHaveBeenLastCalledWith("/api/recording/split", { method: "POST" });

    requestJson.mockResolvedValueOnce(STOPPED);
    byId("recording-toggle").click();
    await said("Recording stopped.");
    expect(requestJson).toHaveBeenLastCalledWith("/api/recording/stop", { method: "POST" });
    expect(byId("recording-toggle").getAttribute("aria-pressed")).toBe("false");
  });

  it("answers a press of the unavailable Split button without a request", async () => {
    const controls = setup();
    controls.apply(STOPPED);
    const split = byId("recording-split-now");
    split.focus();
    split.click();
    await said("Recording is stopped; start it first.");
    expect(requestJson).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(split);
  });

  it("keeps saved settings editable off air and answers unavailable runtime actions", async () => {
    const controls = setup();
    const offAir = { ...STOPPED, available: false };
    controls.apply(offAir);

    const fieldset = byId("recording-controls");
    const toggle = byId("recording-toggle");
    const splitNow = byId("recording-split-now");
    expect(fieldset.hidden).toBe(false);
    expect(byId("recording-split").disabled).toBe(false);
    expect(byId("recording-retention-days").disabled).toBe(false);
    expect(byId("recording-free-space-mb").disabled).toBe(false);
    expect(toggle.getAttribute("aria-disabled")).toBe("true");
    expect(splitNow.getAttribute("aria-disabled")).toBe("true");

    byId("recording-split").value = "show";
    byId("recording-retention-days").value = "8";
    controls.apply(offAir);
    expect(byId("recording-split").value).toBe("show");
    expect(byId("recording-retention-days").value).toBe("8");

    toggle.click();
    await said("Recording is available only while stream mode is active.");
    byId("sr-status").textContent = "";
    splitNow.click();
    await said("Recording is available only while stream mode is active.");
    expect(requestJson).not.toHaveBeenCalled();
  });

  it("speaks a failed request", async () => {
    const controls = setup();
    controls.apply(STOPPED);
    requestJson.mockRejectedValueOnce(new Error("Recording requires stream mode"));
    byId("recording-toggle").click();
    await said("Could not change recording: Recording requires stream mode");
    controls.apply({ ...STOPPED, running: true });
    requestJson.mockRejectedValueOnce(new Error("Recording is stopped"));
    byId("recording-split-now").click();
    await said("Could not split recording: Recording is stopped");
  });

  it("reads whole-number settings into the draft without sending a folder or saving", async () => {
    const controls = setup();
    controls.apply(STOPPED);
    byId("recording-split").value = "show";
    byId("recording-retention-days").value = "7";
    byId("recording-free-space-mb").value = "200";
    expect(groups.find((group) => group.group === "recording").read()).toEqual(
      { split: "show", retention_days: 7, free_space_mb: 200 },
    );
    expect(byId("recording-save")).toBeNull();
    expect(requestJson).not.toHaveBeenCalled();
  });

  it("validates and focuses a field that is not a whole number", async () => {
    const controls = setup();
    controls.apply(STOPPED);
    const days = byId("recording-retention-days");
    const floor = byId("recording-free-space-mb");
    days.value = "1.5";
    expect(groups.find((group) => group.group === "recording").validate()).toBe(false);
    expect(requestJson).not.toHaveBeenCalled();
    expect(days.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(days);
    days.value = "3";
    floor.value = "";
    byId("sr-status").textContent = "";
    expect(groups.find((group) => group.group === "recording").validate()).toBe(false);
    expect(days.hasAttribute("aria-invalid")).toBe(false);
    expect(floor.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(floor);
    // An invalid edit remains until Discard; background state does not erase it.
    controls.apply(STOPPED);
    expect(floor.value).toBe("");
    expect(floor.getAttribute("aria-invalid")).toBe("true");
    floor.blur();
    controls.apply(STOPPED);
    expect(floor.value).toBe("");
    expect(floor.getAttribute("aria-invalid")).toBe("true");
  });
});
