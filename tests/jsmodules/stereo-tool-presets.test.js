import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installStereoToolPresets } from "../../src/autodj/static/modules/stereo-tool-presets.js";

const staticPath = (name) => join(process.cwd(), "src", "autodj", "static", name);
const html = readFileSync(staticPath("index.html"), "utf8");
const css = readFileSync(staticPath("app.css"), "utf8");
const app = readFileSync(staticPath("app.js"), "utf8");
// A template parses the page without loading its stylesheet or scripts.
const template = document.createElement("template");
template.innerHTML = html;
const page = template.content;

// The real Sound panel and the shared status region, so the tests check the
// markup app.js wires up, not a copy of it.
beforeEach(() => {
  document.body.innerHTML = `
    ${page.getElementById("spanel-sound").outerHTML}
    <div id="settings-status" role="status" aria-live="polite"></div>
    <div id="status-toast" aria-hidden="true" hidden></div>`;
});

afterEach(() => {
  vi.useRealTimers();
});

const $ = (id) => document.getElementById(id);
const statusText = () => $("settings-status").textContent;

function server({ active = "included", names = ["included", "Night.sts"] } = {}) {
  const state = { active, names: [...names], posts: [], deletes: [], uploads: [] };
  state.request = vi.fn(async (url, options = {}) => {
    if (url === "/api/stereo-tool-presets") {
      return { active: state.active, presets: state.names.map((name) => ({
        name, label: name === "included" ? "Included" : name, included: name === "included",
      })) };
    }
    if (url === "/api/settings" && !options.method) {
      return { revision: "current", values: { playback: { stereotool_preset: state.active } } };
    }
    if (url === "/api/settings" && options.method === "PATCH") {
      const chosen = JSON.parse(options.body).changes.playback.stereotool_preset;
      state.posts.push(chosen);
      if (state.failSelect) throw new Error(state.failSelect);
      state.active = chosen;
      return { revision: "next", applied: ["playback"], settings: { values: { playback: { stereotool_preset: chosen } } } };
    }
    if (url === "/api/stereo-tool-presets/upload") {
      const uploaded = options.body.get("file");
      state.uploads.push(uploaded.name);
      if (state.failUpload) throw new Error(state.failUpload);
      state.names.push(uploaded.name);
      return { name: uploaded.name };
    }
    if (options.method === "DELETE") {
      const name = decodeURIComponent(url.split("/").pop());
      state.deletes.push(name);
      if (state.failDelete) throw new Error(state.failDelete);
      state.names = state.names.filter((each) => each !== name);
      return { deleted: name };
    }
    throw new Error(`Unexpected request ${url}`);
  });
  return state;
}

// Stands in for confirmAction: answers, runs onConfirm and focuses its
// target the way the real dialog does as it closes.
function answer(accept) {
  return vi.fn(async (_doc, { onConfirm }) => {
    if (!accept) return false;
    const target = await onConfirm();
    target?.focus();
    return true;
  });
}

async function install(state, confirm = answer(true)) {
  const panel = installStereoToolPresets({ document, request: state.request, confirm,
    status: $("settings-status") });
  await panel.refresh();
  return { panel, confirm };
}

function chooseFile(name, size = 20) {
  const file = new window.File(["x".repeat(size)], name);
  Object.defineProperty($("stereo-preset-file"), "files",
    { configurable: true, value: [file] });
}

function pick(value) {
  $("stereo-preset").value = value;
  $("stereo-preset").dispatchEvent(new Event("change"));
}

describe("Stereo Tool preset markup", () => {
  const panel = page.getElementById("spanel-sound");
  const group = page.getElementById("stereo-preset").closest("fieldset");

  it("groups the preset controls under a legend shown only for Stereo Tool", () => {
    expect(group.querySelector(":scope > legend").textContent).toBe("Stereo Tool");
    expect(group.dataset.showWhen).toBe("pb-replaygain=stereotool");
    expect(css).toContain(
      'body:not(:has(#pb-replaygain option[value="stereotool"]:checked)) '
      + '[data-show-when="pb-replaygain=stereotool"]');

  });

  it("shows the separate intro lift only for ReplayGain", () => {
    expect(page.getElementById("pb-intro-lift").closest("[data-show-when]").dataset.showWhen)
      .toBe("pb-replaygain=replaygain");
    expect(page.getElementById("pb-intro-lift-desc").dataset.showWhen)
      .toBe("pb-replaygain=replaygain");
  });

  it("leaves Stereo Tool restart guidance without a duplicate Save button", () => {
    expect(page.getElementById("mastering-save")).toBeNull();
    expect(page.getElementById("mastering-save-note").textContent)
      .toMatch(/preset or AGC drive restarts Stereo Tool.*brief bypass/i);
  });

  it("keeps Sound controls named without focus help", () => {
    for (const control of panel.querySelectorAll("input, select, button")) {
      expect(control.hasAttribute("aria-describedby"), control.id).toBe(false);
      expect(control.hasAttribute("aria-label"), control.id).toBe(false);
      expect(control.hasAttribute("title"), control.id).toBe(false);
    }
  });

  it("wires AGC drive to the shared settings module", () => {
    expect(page.getElementById("pb-stereo-agc-drive")).not.toBeNull();
    expect(app).toMatch(/pbStereoAgcDrive:\s*document\.getElementById\("pb-stereo-agc-drive"\)/);
    expect(app).toMatch(/stereoPreset:\s*document\.getElementById\("stereo-preset"\)/);
    expect(app).not.toMatch(/masteringSave/);
  });

  it("labels every control and lists Included first", () => {
    for (const id of ["stereo-preset", "stereo-preset-file", "pb-stereo-trim", "pb-intro-lift"]) {
      const label = panel.querySelector(`label[for="${id}"]`);
      expect(label?.textContent.trim(), id).toBeTruthy();
    }
    expect(panel.querySelector('label[for="stereo-preset-file"]').textContent).toContain(".sts");
    const first = page.getElementById("stereo-preset").options[0];
    expect([first.value, first.textContent]).toEqual(["included", "Included"]);
    expect(page.getElementById("stereo-preset-file").getAttribute("accept")).toBe(".sts");
    for (const id of ["stereo-preset-upload", "stereo-preset-delete"]) {
      const button = page.getElementById(id);
      expect(button.tagName).toBe("BUTTON");
      expect(button.getAttribute("type")).toBe("button");
      expect(button.textContent.trim()).not.toBe("");
    }
  });

  it("states units and ranges on the number fields", () => {
    const lift = page.getElementById("pb-intro-lift");
    const trim = page.getElementById("pb-stereo-trim");
    expect([lift.min, lift.max]).toEqual(["0", "6"]);
    expect([trim.min, trim.max]).toEqual(["-12", "6"]);
    for (const input of [lift, trim]) {
      expect(panel.querySelector(`label[for="${input.id}"]`).textContent).toContain("decibels");
      const desc = page.getElementById(`${input.id}-desc`);
      expect(input.hasAttribute("aria-describedby")).toBe(false);
      expect(desc.hidden).toBe(false);
      expect(desc.textContent).toContain(`from ${input.min} to ${input.max}`);
    }
  });
});

describe("Stereo Tool preset controls", () => {
  it("loads the list when the Sound panel is shown", async () => {
    const state = server({ active: "Night.sts" });
    installStereoToolPresets({ document, request: state.request,
      status: $("settings-status") });
    expect(state.request).not.toHaveBeenCalled();
    const panel = $("spanel-sound");
    panel.open = true;
    panel.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect($("stereo-preset").options).toHaveLength(2));
    expect([...$("stereo-preset").options].map((option) => option.textContent))
      .toEqual(["Included", "Night.sts"]);
    expect($("stereo-preset").value).toBe("Night.sts");
    expect($("stereo-preset-delete").disabled).toBe(false);
  });

  it("says when the list cannot load", async () => {
    const request = vi.fn(async () => { throw new Error("Server error"); });
    const panel = $("spanel-sound");
    panel.open = true;
    installStereoToolPresets({ document, request, status: $("settings-status") });
    await vi.waitFor(() => expect(statusText())
      .toBe("Could not load Stereo Tool presets: Server error"));
  });

  it("keeps Delete off for the Included preset", async () => {
    await install(server());
    expect($("stereo-preset").value).toBe("included");
    expect($("stereo-preset-delete").disabled).toBe(true);
  });

  it("keeps a preset selection pending for Save sound settings", async () => {
    const state = server();
    const presets = await install(state);
    pick("Night.sts");
    expect(state.posts).toEqual([]);
    expect($("stereo-preset").value).toBe("Night.sts");
    expect($("stereo-preset-delete").disabled).toBe(false);
    presets.panel.setActive("Night.sts");
    expect(state.posts).toEqual([]);
  });

  it("drops a preset draft on station reset before loading the next station", async () => {
    const state = server();
    const presets = await install(state);
    pick("Night.sts");
    state.names.push("Day.sts");
    state.active = "Day.sts";
    presets.panel.reset();
    await presets.panel.refresh();
    expect($("stereo-preset").value).toBe("Day.sts");
    expect(state.posts).toEqual([]);
  });

  it("uploads a preset, says so and moves to the list", async () => {
    const state = server({ names: ["included"] });
    await install(state);
    chooseFile("Night.sts");
    $("stereo-preset-upload").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Uploaded Night.sts. Choose it in the Preset list to use it."));
    expect(state.uploads).toEqual(["Night.sts"]);
    expect([...$("stereo-preset").options].map((option) => option.value))
      .toEqual(["included", "Night.sts"]);
    expect($("stereo-preset").value).toBe("included");
    expect(state.posts).toEqual([]);
    expect(document.activeElement).toBe($("stereo-preset"));
  });

  it("returns to the file field when the server refuses an upload", async () => {
    const state = server();
    state.failUpload = "a preset named Night.sts already exists";
    await install(state);
    chooseFile("Night.sts");
    $("stereo-preset-upload").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Could not upload Night.sts: a preset named Night.sts already exists"));
    expect(document.activeElement).toBe($("stereo-preset-file"));
  });

  it.each([
    [null, 0, "Choose a Stereo Tool preset file first."],
    ["notes.txt", 20, "notes.txt is not a Stereo Tool preset. Choose a .sts file."],
    ["Big.sts", 2 * 1024 * 1024 + 1, "Big.sts is larger than 2 MB."],
  ])("refuses %s before sending it", async (name, size, message) => {
    const state = server();
    await install(state);
    if (name) chooseFile(name, size);
    $("stereo-preset-upload").click();
    await vi.waitFor(() => expect(statusText()).toBe(message));
    expect(state.uploads).toEqual([]);
    expect(document.activeElement).toBe($("stereo-preset-file"));
    expect($("status-toast").classList.contains("is-error")).toBe(true);
  });

  it("deletes the active upload after switching to Included", async () => {
    const state = server({ active: "Night.sts" });
    const { confirm } = await install(state);
    $("stereo-preset-delete").focus();
    $("stereo-preset-delete").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Preset Night.sts deleted. Included is active."));
    expect(confirm.mock.calls[0][1]).toMatchObject({
      title: "Delete preset Night.sts?", confirmLabel: "Delete preset",
    });
    expect(confirm.mock.calls[0][1].message).toContain("switches to the Included preset");
    expect(state.posts).toEqual(["included"]);
    expect(state.deletes).toEqual(["Night.sts"]);
    expect($("stereo-preset").value).toBe("included");
    expect($("stereo-preset").options).toHaveLength(1);
    expect(document.activeElement).toBe($("stereo-preset"));
  });

  it("deletes an inactive upload without changing the active preset", async () => {
    const state = server({ names: ["included", "Day.sts", "Night.sts"], active: "Day.sts" });
    await install(state);
    $("stereo-preset").value = "Night.sts";
    $("stereo-preset-delete").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Preset Night.sts deleted. Day.sts is active."));
    expect(state.posts).toEqual([]);
    expect($("stereo-preset").value).toBe("Day.sts");
  });

  it("changes nothing when the deletion is canceled", async () => {
    const state = server({ active: "Night.sts" });
    await install(state, answer(false));
    $("stereo-preset-delete").click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(state.posts).toEqual([]);
    expect(state.deletes).toEqual([]);
    expect(statusText()).toBe("");
  });

  it("says why a deletion failed and returns to the list", async () => {
    const state = server({ names: ["included", "Day.sts", "Night.sts"], active: "Day.sts" });
    state.failDelete = "preset Night.sts was not found";
    await install(state);
    $("stereo-preset").value = "Night.sts";
    $("stereo-preset-delete").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Could not delete preset Night.sts: preset Night.sts was not found"));
    expect($("stereo-preset").value).toBe("Day.sts");
    expect(document.activeElement).toBe($("stereo-preset"));
  });
});
