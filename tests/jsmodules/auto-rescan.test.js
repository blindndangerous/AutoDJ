import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";
import { autoRescanProblem, installAutoRescan } from "../../src/autodj/static/modules/auto-rescan.js";

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");

function mount() {
  const page = document.createElement("template");
  page.innerHTML = html;
  document.body.replaceChildren(page.content.querySelector("#auto-rescan").cloneNode(true));
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const field = (id) => document.getElementById(id);

describe("automatic rescan", () => {
  beforeEach(mount);

  it("labels hours and minutes in a named group with plain help", () => {
    const group = field("auto-rescan");
    expect(group.querySelector("legend").textContent).toBe("Automatic rescan every");
    for (const [id, label, max] of [["auto-rescan-hours", "Hours", "168"], ["auto-rescan-minutes", "Minutes", "59"]]) {
      expect(field(id).type).toBe("number");
      expect(field(id).min).toBe("0");
      expect(field(id).max).toBe(max);
      expect(field(id).value).toBe("0");
      expect(document.querySelector(`label[for="${id}"]`).textContent).toBe(label);
      expect(field(id).hasAttribute("aria-describedby")).toBe(false);
    }
    expect(group.querySelector(".setting-desc").textContent).toMatch(/turns it off; the shortest is 5 minutes/);
    expect(field("auto-rescan-error").hidden).toBe(true);
    expect(field("auto-rescan-error").hasAttribute("aria-live")).toBe(false);
    expect(field("auto-rescan-save").textContent).toBe("Save");
  });

  it("refuses values the server would refuse", () => {
    expect(autoRescanProblem("0", "0")).toBeNull();
    expect(autoRescanProblem("0", "5")).toBeNull();
    expect(autoRescanProblem("168", "59")).toBeNull();
    expect(autoRescanProblem("169", "0")[0]).toBe("hours");
    expect(autoRescanProblem("", "0")[0]).toBe("hours");
    expect(autoRescanProblem("1.5", "0")[0]).toBe("hours");
    expect(autoRescanProblem("0", "60")[0]).toBe("minutes");
    expect(autoRescanProblem("0", "-1")[0]).toBe("minutes");
    expect(autoRescanProblem("0", "4")).toEqual(["minutes", "Use at least 5 minutes, or 0 to turn it off."]);
  });

  it("loads the saved interval and says Saved once after a save", async () => {
    const request = vi.fn(async (url, options) => (options ? JSON.parse(options.body) : { hours: 2, minutes: 30 }));
    const say = vi.fn();
    const controls = installAutoRescan({ document, request, say });
    await controls.load();
    expect(field("auto-rescan-hours").value).toBe("2");
    expect(field("auto-rescan-minutes").value).toBe("30");

    field("auto-rescan-hours").value = "1";
    field("auto-rescan-minutes").value = "0";
    field("auto-rescan-save").click();
    await flush();
    expect(request).toHaveBeenLastCalledWith("/api/library/auto-rescan", expect.objectContaining({ method: "PUT" }));
    expect(JSON.parse(request.mock.calls.at(-1)[1].body)).toEqual({ hours: 1, minutes: 0 });
    expect(say.mock.calls).toEqual([["Saved."]]);
  });

  it("links a refused value to its field, focuses it and sends nothing", async () => {
    const request = vi.fn();
    const say = vi.fn();
    installAutoRescan({ document, request, say });
    const minutes = field("auto-rescan-minutes");
    minutes.value = "3";
    field("auto-rescan-save").click();
    await flush();
    const error = field("auto-rescan-error");
    expect(request).not.toHaveBeenCalled();
    expect(say).not.toHaveBeenCalled();
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Use at least 5 minutes, or 0 to turn it off.");
    expect(minutes.getAttribute("aria-invalid")).toBe("true");
    expect(minutes.getAttribute("aria-describedby")).toBe("auto-rescan-error");
    expect(minutes.closest(".settings-row").nextElementSibling).toBe(error);
    expect(document.activeElement).toBe(minutes);

    minutes.value = "0";
    minutes.dispatchEvent(new Event("input"));
    expect(error.hidden).toBe(true);
    expect(minutes.hasAttribute("aria-invalid")).toBe(false);
    expect(minutes.hasAttribute("aria-describedby")).toBe(false);
  });

  it("shows a server refusal under Minutes and other failures in the status", async () => {
    const refused = Object.assign(new Error("Use at least 5 minutes, or 0 to turn it off."), { status: 400 });
    const request = vi.fn().mockRejectedValueOnce(refused).mockRejectedValueOnce(new Error("Network down"));
    const say = vi.fn();
    installAutoRescan({ document, request, say });
    field("auto-rescan-minutes").value = "10";
    field("auto-rescan-save").click();
    await flush();
    expect(field("auto-rescan-minutes").getAttribute("aria-invalid")).toBe("true");
    expect(field("auto-rescan-error").textContent).toBe(refused.message);
    field("auto-rescan-save").click();
    await flush();
    expect(say).toHaveBeenCalledWith("Could not save automatic rescan: Network down", true);
  });

  it("clears to the defaults when the session ends", async () => {
    const controls = installAutoRescan({ document, request: vi.fn(async () => ({ hours: 3, minutes: 0 })), say: vi.fn() });
    await controls.load();
    controls.clear();
    expect(field("auto-rescan-hours").value).toBe("0");
    expect(field("auto-rescan-minutes").value).toBe("0");
  });

  it("does nothing on a page without the fields", () => {
    document.body.replaceChildren();
    expect(installAutoRescan({ document, request: vi.fn(), say: vi.fn() })).toBeNull();
  });
});

describe("find beats and song structure", () => {
  it("loads the saved setting and saves a change with one Saved", async () => {
    const page = document.createElement("template");
    page.innerHTML = html;
    document.body.replaceChildren(
      page.content.querySelector("#auto-rescan").cloneNode(true),
      page.content.querySelector("#dj-analysis").closest("label").cloneNode(true),
    );
    const box = field("dj-analysis");
    expect(box.labels[0].textContent.trim()).toBe("Find beats and song structure");
    expect(box.checked).toBe(true);
    const request = vi.fn(async (url, options) => {
      if (options) return JSON.parse(options.body);
      return url.endsWith("dj-analysis") ? { enabled: false } : { hours: 0, minutes: 0 };
    });
    const say = vi.fn();
    const controls = installAutoRescan({ document, request, say });
    await controls.load();
    expect(box.checked).toBe(false);
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    await flush();
    expect(request).toHaveBeenLastCalledWith("/api/library/dj-analysis", expect.objectContaining({ method: "PUT" }));
    expect(JSON.parse(request.mock.lastCall[1].body)).toEqual({ enabled: true });
    expect(say).toHaveBeenCalledOnce();
    expect(say).toHaveBeenCalledWith("Saved.");
  });
});
