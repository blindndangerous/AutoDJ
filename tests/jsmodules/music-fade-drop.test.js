import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installMusicFadeDrop, musicFadeDropProblem } from "../../src/autodj/static/modules/music-fade-drop.js";
import { resetSettingsDrafts } from "../../src/autodj/static/modules/settings-drafts.js";

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
function mount() {
  const page = document.createElement("template");
  page.innerHTML = html;
  document.body.replaceChildren(page.content.querySelector("#spanel-libraries").cloneNode(true));
}
const field = (id) => document.getElementById(id);
const saveButton = () => document.querySelector("#spanel-libraries [data-settings-save]");
const saveStatus = () => document.querySelector("#spanel-libraries .settings-save-status");
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("per-library music fade threshold", () => {
  beforeEach(() => { mount(); resetSettingsDrafts(document); });
  afterEach(() => vi.useRealTimers());

  it("uses a labelled whole-number field with browsable help", () => {
    const input = field("music-fade-drop");
    expect(document.querySelector('label[for="music-fade-drop"]').textContent).toBe("Fade detection threshold (dB)");
    expect([input.type, input.min, input.max, input.step, input.value]).toEqual(["number", "1", "30", "1", "10"]);
    expect(input.getAttribute("aria-describedby")).toBeNull();
    expect(field("music-fade-drop-help").textContent).toContain("new Segue suggestions");
  });

  it("accepts only whole numbers from 1 through 30", () => {
    for (const value of ["1", "10", "30", " 8 "]) expect(musicFadeDropProblem(value)).toBeNull();
    for (const value of ["", "0", "31", "2.5", "-1", "ten"]) expect(musicFadeDropProblem(value)).toBe("Enter a whole number from 1 to 30 dB.");
  });

  it("loads and saves the threshold for the selected library", async () => {
    const request = vi.fn(async (_url, options) => options ? JSON.parse(options.body) : { drop_db: 14 });
    const controls = installMusicFadeDrop({ document, request });
    await controls.load();
    expect(field("music-fade-drop").value).toBe("14");
    field("music-fade-drop").value = "20";
    field("music-fade-drop").dispatchEvent(new Event("input", { bubbles: true }));
    saveButton().click();
    await vi.waitFor(() => expect(saveStatus().textContent).toContain("Settings saved for Library: main."));
    expect(request).toHaveBeenLastCalledWith("/api/library/music-fade?library=main", expect.objectContaining({
      method: "PUT", body: JSON.stringify({ drop_db: 20 }),
    }));
    expect(field("music-fade-drop").value).toBe("20");
  });

  it("links and focuses validation errors, then clears them on valid input", () => {
    const request = vi.fn();
    installMusicFadeDrop({ document, request });
    const input = field("music-fade-drop");
    input.value = "31";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    saveButton().click();
    const error = field("music-fade-drop-error");
    expect(request).not.toHaveBeenCalled();
    expect(error.hidden).toBe(false);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toBe("music-fade-drop-error");
    expect(document.activeElement).toBe(input);
    input.value = "10";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    expect(error.hidden).toBe(true);
    expect(input.getAttribute("aria-describedby")).toBeNull();
  });

  it("ignores a late library response after the selector changes", async () => {
    let selected = "main";
    let finish;
    const request = vi.fn((url) => url.includes("main")
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ drop_db: 18 }));
    const controls = installMusicFadeDrop({ document, request, getLibrary: () => selected });
    const loading = controls.load();
    selected = "oldies";
    const next = controls.load();
    await next;
    finish({ drop_db: 5 });
    await loading;
    expect(field("music-fade-drop").value).toBe("18");
    expect(request).toHaveBeenNthCalledWith(1, "/api/library/music-fade?library=main");
    expect(request).toHaveBeenNthCalledWith(2, "/api/library/music-fade?library=oldies");
  });

  it("reports failed saves and keeps the requested value", async () => {
    const request = vi.fn(async (_url, options) => {
      if (options) throw Object.assign(new Error("Offline"), { status: 503 });
      return { drop_db: 14 };
    });
    installMusicFadeDrop({ document, request });
    await flush();
    field("music-fade-drop").value = "12";
    field("music-fade-drop").dispatchEvent(new Event("input", { bubbles: true }));
    saveButton().click();
    await vi.waitFor(() => expect(saveStatus().textContent).toContain("Could not save all settings: Offline"));
    expect(field("music-fade-drop").value).toBe("12");
    expect(saveStatus().textContent).toContain("Unsaved changes remain");
  });
});
