import { beforeEach, describe, expect, it, vi } from "vitest";
import { installClockwheelPanel, parseResyncMinutes } from "../../src/autodj/static/modules/clockwheel-panel.js";

function mount() {
  document.body.innerHTML = `
    <section id="panel-library" hidden>
      <input id="clock-name" value="Clock"><input id="clock-pal-file" type="file"><button id="clock-import"></button>
      <ol id="clock-slots"></ol><p id="clock-next"></p>
      <input id="clock-resync-minutes"><button id="clock-save"></button><p id="clock-status"></p>
    </section>`;
}

beforeEach(mount);

describe("clockwheel configuration panel", () => {
  it("parses resync minute lists and reports invalid input", () => {
    expect(parseResyncMinutes("30, 0, 30")).toEqual([0, 30]);
    expect(parseResyncMinutes("")).toEqual([]);
    expect(() => parseResyncMinutes("0, sixty")).toThrow("whole minutes");
    expect(() => parseResyncMinutes("60")).toThrow("0 to 59");
  });

  it("imports selected PAL text and renders slots as text", async () => {
    const response = {
      name: "Night clock", slots: [{ folder: "<music>", kind: "music", recursive: true, selection: "least_recent", enforce_rules: true, ready_count: 4 }],
      resync_minutes: [0, 30], cursor: 0,
    };
    const request = vi.fn(async () => response);
    installClockwheelPanel({ document, request });
    const fileInput = document.getElementById("clock-pal-file");
    Object.defineProperty(fileInput, "files", { value: [{ size: 20, text: async () => "Dir['/music'].QueueBottom(smLRP, EnforceRules);" }] });
    document.getElementById("clock-name").value = "Night clock";
    document.getElementById("clock-resync-minutes").value = "0, 30";
    document.getElementById("clock-import").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/clock/import", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ text: "Dir['/music'].QueueBottom(smLRP, EnforceRules);", name: "Night clock", resync_minutes: [0, 30] });
    expect(document.getElementById("clock-slots").textContent).toContain("<music>");
    expect(document.getElementById("clock-slots").querySelector("script")).toBeNull();
    expect(document.getElementById("clock-next").textContent).toContain("Next slot: 1 of 1");
  });

  it("saves displayed settings without sending response-only ready counts", async () => {
    document.getElementById("panel-library").hidden = false;
    const saved = { name: "Clock", slots: [{ folder: "/music", kind: "music", recursive: false, selection: "random", enforce_rules: false, ready_count: 12 }], resync_minutes: [15], cursor: 0 };
    const request = vi.fn(async (url, options) => options ? saved : saved);
    const controls = installClockwheelPanel({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    document.getElementById("clock-resync-minutes").value = "15";
    document.getElementById("clock-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const body = JSON.parse(request.mock.calls.at(-1)[1].body);
    expect(body.slots[0]).not.toHaveProperty("ready_count");
    expect(body.resync_minutes).toEqual([15]);
    controls.destroy();
  });

  it("preserves edits and list nodes across unchanged status polls", async () => {
    document.getElementById("panel-library").hidden = false;
    const response = { name: "Clock", slots: [{ folder: "/music", kind: "music", recursive: false, selection: "random", enforce_rules: false, ready_count: 12 }], resync_minutes: [0], cursor: 0 };
    const controls = installClockwheelPanel({ document, request: vi.fn(async () => response) });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const row = document.querySelector("#clock-slots li");
    const name = document.getElementById("clock-name");
    name.value = "Editing name";
    name.focus();
    await controls.refresh();
    expect(document.querySelector("#clock-slots li")).toBe(row);
    expect(name.value).toBe("Editing name");
    controls.destroy();
  });
});
