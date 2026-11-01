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

  it("blocks duplicate saves and rejects a save reply after the controls are cleared", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const controls = installAutoRescan({ document, request, say: vi.fn() });
    field("auto-rescan-hours").value = "2";
    field("auto-rescan-save").focus();
    field("auto-rescan-save").click();
    field("auto-rescan-save").click();
    expect(request).toHaveBeenCalledTimes(1);
    expect(field("auto-rescan-save").getAttribute("aria-busy")).toBe("true");
    controls.clear();
    finish({ hours: 2, minutes: 0 });
    await flush();
    expect(field("auto-rescan-hours").value).toBe("0");
    expect(field("auto-rescan-save").hasAttribute("aria-busy")).toBe(false);
  });

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
    expect(group.querySelector(".setting-desc").textContent).toMatch(/Minimum 5 minutes; 0 turns it off/);
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
    // As in every settings row that uses the shared field error: straight
    // after the control, inside the row that holds its label.
    expect(minutes.nextElementSibling).toBe(error);
    expect(error.parentElement).toBe(minutes.closest(".settings-row"));
    expect(error.parentElement.querySelector("label").htmlFor).toBe("auto-rescan-minutes");
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
    expect(say).toHaveBeenCalledWith("Could not save automatic rescan: Network down", "error");
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

it("does not expose the removed independent Find beats setting", async () => {
  const page = document.createElement("template");
  page.innerHTML = html;
  expect(page.content.querySelector("#dj-analysis")).toBeNull();
  document.body.replaceChildren(page.content.querySelector("#auto-rescan").cloneNode(true));
  const request = vi.fn(async () => ({ hours: 0, minutes: 0 }));
  const controls = installAutoRescan({ document, request, say: vi.fn() });
  await controls.load();
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith("/api/library/auto-rescan");
});
