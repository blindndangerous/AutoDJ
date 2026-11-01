import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installMusicFadeDrop, musicFadeDropProblem } from "../../src/autodj/static/modules/music-fade-drop.js";

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
function mount() {
  const page = document.createElement("template");
  page.innerHTML = html;
  document.body.replaceChildren(page.content.querySelector("#music-fade-drop-group").cloneNode(true));
}
const field = (id) => document.getElementById(id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("music fade drop setting", () => {
  beforeEach(mount);
  afterEach(() => vi.useRealTimers());

  it("uses a labelled whole-number field with concise linked help", () => {
    const input = field("music-fade-drop");
    expect(document.querySelector('label[for="music-fade-drop"]').textContent).toBe("Fade detection threshold (dB)");
    expect([input.type, input.min, input.max, input.step, input.value]).toEqual(["number", "1", "30", "1", "10"]);
    expect(input.getAttribute("aria-describedby")).toBe("music-fade-drop-help");
    expect(field("music-fade-drop-help").textContent).toContain("AutoDJ suggests a Segue");
  });

  it("accepts only whole numbers from 1 through 30", () => {
    for (const value of ["1", "10", "30", " 8 "]) expect(musicFadeDropProblem(value)).toBeNull();
    for (const value of ["", "0", "31", "2.5", "-1", "ten"]) expect(musicFadeDropProblem(value)).toBe("Enter a whole number from 1 to 30 dB.");
  });

  it("loads and saves drop_db through the shared library endpoint", async () => {
    const request = vi.fn(async (url, options) => options ? JSON.parse(options.body) : { drop_db: 14 });
    const say = vi.fn();
    const controls = installMusicFadeDrop({ document, request, say });
    await controls.load();
    expect(field("music-fade-drop").value).toBe("14");
    field("music-fade-drop").value = "20";
    field("music-fade-drop-save").click();
    await flush();
    expect(request).toHaveBeenLastCalledWith("/api/library/music-fade", expect.objectContaining({
      method: "PUT", body: JSON.stringify({ drop_db: 20 }),
    }));
    expect(say).toHaveBeenLastCalledWith("Saved. Fade detection threshold is 20 dB.");
    expect(field("music-fade-drop").value).toBe("20");
  });

  it("links and focuses validation errors, then clears them on valid input", () => {
    const request = vi.fn();
    installMusicFadeDrop({ document, request, say: vi.fn() });
    const input = field("music-fade-drop");
    input.value = "31";
    field("music-fade-drop-save").click();
    expect(request).not.toHaveBeenCalled();
    expect(field("music-fade-drop-error").hidden).toBe(false);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toBe("music-fade-drop-error music-fade-drop-help");
    expect(document.activeElement).toBe(input);
    input.value = "10";
    input.dispatchEvent(new Event("input"));
    expect(field("music-fade-drop-error").hidden).toBe(true);
    expect(input.getAttribute("aria-describedby")).toBe("music-fade-drop-help");
  });

  it("clears auth-scoped values and ignores a late load", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const controls = installMusicFadeDrop({ document, request, say: vi.fn() });
    field("music-fade-drop").value = "23";
    const load = controls.load();
    controls.clear();
    finish({ drop_db: 5 });
    await load;
    expect(field("music-fade-drop").value).toBe("10");
  });

  it("ignores a pre-sign-out save without unlocking a later session save", async () => {
    const finish = [];
    const request = vi.fn(() => new Promise((resolve) => finish.push(resolve)));
    const say = vi.fn();
    const controls = installMusicFadeDrop({ document, request, say });
    field("music-fade-drop").value = "12";
    field("music-fade-drop-save").click();
    controls.clear();
    field("music-fade-drop").value = "16";
    field("music-fade-drop-save").click();
    finish[0]({ drop_db: 4 });
    await flush();
    expect(field("music-fade-drop").value).toBe("16");
    expect(field("music-fade-drop-save").getAttribute("aria-busy")).toBe("true");
    expect(say).not.toHaveBeenCalled();
    finish[1]({ drop_db: 16 });
    await flush();
    expect(field("music-fade-drop").value).toBe("16");
    expect(field("music-fade-drop-save").hasAttribute("aria-busy")).toBe(false);
  });

  it("reports failed saves and keeps the requested value", async () => {
    const request = vi.fn(async () => { throw Object.assign(new Error("Offline"), { status: 503 }); });
    const say = vi.fn();
    installMusicFadeDrop({ document, request, say });
    field("music-fade-drop").value = "12";
    field("music-fade-drop-save").click();
    await flush();
    expect(field("music-fade-drop").value).toBe("12");
    expect(say).toHaveBeenLastCalledWith("Could not save the fade detection threshold: Offline", "error");
  });
});
