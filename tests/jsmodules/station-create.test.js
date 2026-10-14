import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  checkNewStation,
  createdText,
  installStationCreator,
  slugFromName,
  stationRowText,
} from "../../src/autodj/static/modules/station-create.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.
function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

const byId = (id) => document.getElementById(id);
const AUTODJ = { slug: "default", name: "AutoDJ", state: "playing", listeners: 3, defined_in: "config" };
const OWL = { slug: "nightowl", name: "Night Owl Radio", state: "stopped", listeners: 0, defined_in: "web" };

function setup({ request, stations = [AUTODJ], onSwitch = vi.fn(async () => true) } = {}) {
  installIndex();
  let list = [...stations];
  const onCreated = vi.fn(async (entry) => { list = [...list, entry]; });
  const controls = installStationCreator({
    request, stations: () => list, onCreated, onSwitch,
  });
  return { controls, onCreated, onSwitch };
}

function submit() {
  byId("station-create-form").dispatchEvent(new window.Event("submit", { cancelable: true }));
}

function fill({ name = "", slug = "", source = "" } = {}) {
  byId("station-create-name").value = name;
  byId("station-create-slug").value = slug;
  byId("station-create-source").value = source;
  byId("station-create-source").dispatchEvent(new window.Event("change"));
}

describe("station IDs and checks", () => {
  it("makes an ID from a name", () => {
    expect(slugFromName("Night Owl Radio")).toBe("night-owl-radio");
    expect(slugFromName("  Café Ünïcode!! ")).toBe("cafe-unicode");
    expect(slugFromName("---")).toBe("");
    expect(slugFromName("x".repeat(40))).toHaveLength(32);
  });

  it("refuses an empty, shared or badly formed name or ID", () => {
    const stations = [AUTODJ, OWL];
    expect(checkNewStation({ name: " ", slug: "a" }, stations).field).toBe("name");
    expect(checkNewStation({ name: "night owl radio", slug: "owl2" }, stations))
      .toEqual({ field: "name", message: "Night Owl Radio already has that name. Choose another name." });
    expect(checkNewStation({ name: "Owl 2", slug: "Owl" }, stations).field).toBe("slug");
    expect(checkNewStation({ name: "Owl 2", slug: "-owl" }, stations).field).toBe("slug");
    expect(checkNewStation({ name: "Owl 2", slug: "default" }, stations).field).toBe("slug");
    expect(checkNewStation({ name: "Owl 2", slug: "nightowl" }, stations).field).toBe("slug");
    expect(checkNewStation({ name: "Owl 2", slug: "owl-2" }, stations)).toBeNull();
  });

  it("words a row and a created station plainly", () => {
    expect(stationRowText(OWL)).toBe("Night Owl Radio, off air, ID nightowl");
    expect(createdText({ name: "Owl", copied: [] })).toBe("Created Owl, off air.");
    expect(createdText({ name: "Owl", copied: ["saved settings"] }))
      .toBe("Created Owl, off air. Copied saved settings.");
    expect(createdText({ name: "Owl", copied: ["saved settings", "clockwheels", "carts"] }))
      .toBe("Created Owl, off air. Copied saved settings, clockwheels and carts.");
  });
});

describe("Settings > Stations", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("lists the stations and offers a copy of each", () => {
    setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    expect([...byId("stations-list").children].map((li) => li.textContent)).toEqual([
      "AutoDJ, on air, 3 listeners, ID default",
      "Night Owl Radio, off air, ID nightowl",
    ]);
    expect([...byId("station-create-source").options].map((option) => [option.value, option.textContent]))
      .toEqual([["", "AutoDJ defaults"], ["default", "Copy of AutoDJ"], ["nightowl", "Copy of Night Owl Radio"]]);
  });

  it("shows Also copy only for a copy, and sends the copies only then", async () => {
    const request = vi.fn(async (_url, options) => ({
      ...OWL, slug: "owl2", name: "Owl 2", copied: ["saved settings", "carts"],
      echo: JSON.parse(options.body),
    }));
    setup({ request });
    expect(byId("station-create-copies").hidden).toBe(true);
    fill({ name: "Owl 2", slug: "owl2", source: "default" });
    expect(byId("station-create-copies").hidden).toBe(false);
    byId("station-create-carts").checked = true;
    submit();
    await vi.waitFor(() => expect(request).toHaveBeenCalled());
    const [url, options] = request.mock.calls[0];
    expect(url).toBe("/api/stations");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({
      slug: "owl2", name: "Owl 2", source: "default", autostart: false,
      copy_clockwheels: false, copy_schedule: false, copy_carts: true,
    });

    fill({ name: "Owl 3", slug: "owl3", source: "" });
    byId("station-create-carts").checked = true;
    expect(byId("station-create-copies").hidden).toBe(true);
    submit();
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({
      source: null, copy_clockwheels: false, copy_schedule: false, copy_carts: false,
    });
  });

  it("makes the ID from the name when it is left empty", async () => {
    const request = vi.fn(async () => ({ ...OWL, slug: "late-show", name: "Late Show", copied: [] }));
    setup({ request });
    fill({ name: "Late Show" });
    byId("station-create-name").dispatchEvent(new window.Event("change"));
    expect(byId("station-create-slug").value).toBe("late-show");
    byId("station-create-slug").value = "";
    submit();
    await vi.waitFor(() => expect(request).toHaveBeenCalled());
    expect(JSON.parse(request.mock.calls[0][1].body).slug).toBe("late-show");
  });

  it("marks, links and focuses a field refused in the browser, saying nothing else", async () => {
    const request = vi.fn();
    setup({ request, stations: [AUTODJ, OWL] });
    fill({ name: "Owl 2", slug: "nightowl" });
    submit();
    const slug = byId("station-create-slug");
    await vi.waitFor(() => expect(slug.getAttribute("aria-invalid")).toBe("true"));
    expect(request).not.toHaveBeenCalled();
    expect(slug.getAttribute("aria-describedby").split(" "))
      .toEqual(["station-create-slug-help", "station-create-error"]);
    expect(byId("station-create-error").hidden).toBe(false);
    expect(byId("station-create-error").textContent).toMatch(/already has the ID nightowl/);
    expect(document.activeElement).toBe(slug);
    expect(byId("stations-status").textContent).toBe("");
    // Typing clears the mark and the link, keeping the format hint.
    slug.value = "owl2";
    slug.dispatchEvent(new window.Event("input"));
    expect(slug.hasAttribute("aria-invalid")).toBe(false);
    expect(slug.getAttribute("aria-describedby")).toBe("station-create-slug-help");
  });

  it("puts the server's refusal on the field it names", async () => {
    const error = Object.assign(new Error("Night Owl Radio already uses recordings/owl2."),
      { status: 409, field: "name" });
    const request = vi.fn(async () => { throw error; });
    setup({ request });
    fill({ name: "Owl 2", slug: "owl2" });
    submit();
    const name = byId("station-create-name");
    await vi.waitFor(() => expect(name.getAttribute("aria-invalid")).toBe("true"));
    expect(name.getAttribute("aria-describedby")).toBe("station-create-error");
    expect(document.activeElement).toBe(name);
    expect(byId("station-create-error").textContent)
      .toBe("Could not create the station. Night Owl Radio already uses recordings/owl2.");
  });

  it("says a refusal that names no field once, in the status region", async () => {
    const request = vi.fn(async () => { throw Object.assign(new Error("Eight stations is the most."), { status: 409 }); });
    setup({ request });
    fill({ name: "Owl 2", slug: "owl2" });
    submit();
    await vi.waitFor(() => expect(byId("stations-status").textContent)
      .toBe("Could not create the station. Eight stations is the most."));
    for (const id of ["station-create-name", "station-create-slug", "station-create-source"]) {
      expect(byId(id).hasAttribute("aria-invalid"), id).toBe(false);
    }
  });

  it("says what was created, lists it and moves focus to Switch", async () => {
    const request = vi.fn(async () => ({ ...OWL, copied: ["saved settings"] }));
    const { onCreated, onSwitch } = setup({ request });
    fill({ name: "Night Owl Radio", slug: "nightowl", source: "default" });
    byId("station-create-autostart").checked = true;
    submit();
    const switchButton = byId("station-create-switch");
    await vi.waitFor(() => expect(document.activeElement).toBe(switchButton));
    expect(onCreated).toHaveBeenCalledTimes(1);
    expect(switchButton.textContent).toBe("Switch to Night Owl Radio");
    expect(byId("station-create-done").hidden).toBe(false);
    expect(byId("station-create-done-text").textContent).toBe("Night Owl Radio is off air until you start it.");
    await vi.waitFor(() => expect(byId("stations-status").textContent)
      .toBe("Created Night Owl Radio, off air. Copied saved settings."));
    expect([...byId("stations-list").children].map((li) => li.dataset.station)).toEqual(["default", "nightowl"]);
    // The form is ready for another station.
    expect(byId("station-create-name").value).toBe("");
    expect(byId("station-create-autostart").checked).toBe(false);
    expect(byId("station-create-source").value).toBe("");

    switchButton.click();
    await vi.waitFor(() => expect(onSwitch).toHaveBeenCalledWith("nightowl"));
    await vi.waitFor(() => expect(byId("station-create-done").hidden).toBe(true));
  });

  it("keeps Switch when the switch is turned down", async () => {
    const request = vi.fn(async () => ({ ...OWL, copied: [] }));
    const onSwitch = vi.fn(async () => false);
    setup({ request, onSwitch });
    fill({ name: "Night Owl Radio", slug: "nightowl" });
    submit();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("station-create-switch")));
    byId("station-create-switch").click();
    await vi.waitFor(() => expect(onSwitch).toHaveBeenCalled());
    expect(byId("station-create-done").hidden).toBe(false);
  });
});
