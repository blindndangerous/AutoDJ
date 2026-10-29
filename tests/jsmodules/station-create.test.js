
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";

import {
  checkNewStation,
  createdText,
  installStationCreator,
  slugFromName,
  stationCells,
} from "../../src/autodj/static/modules/station-create.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const AUTODJ = {
  slug: "default", name: "AutoDJ", state: "playing", listeners: 3, defined_in: "config",
  title: "Artist — Song", autostart: true, autostart_locked: true,
};
const OWL = {
  slug: "nightowl", name: "Night Owl Radio", state: "stopped", listeners: 0, defined_in: "web",
  autostart: false, autostart_locked: false,
};

function setup({ request, stations = [AUTODJ], onSwitch = vi.fn(async () => true) } = {}) {
  installIndex();
  // Each test models a fresh GET /api/stations response. The row editor updates
  // its station record after a successful PUT, so keep the shared fixtures pristine.
  let list = stations.map((station) => ({ ...station }));
  const onCreated = vi.fn(async (entry) => { list = [...list, entry]; });
  const onChanged = vi.fn(async () => {});
  const controls = installStationCreator({
    request, stations: () => list, onCreated, onChanged, onSwitch,
  });
  return { controls, onCreated, onChanged, onSwitch,
    refresh: (next) => { list = next.map((station) => ({ ...station })); controls.render(); } };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

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
    expect(stationCells(OWL)).toEqual(["Off air", ""]);
    expect(stationCells(AUTODJ)).toEqual(["Artist — Song", "3"]);
    expect(createdText({ name: "Owl", copied: [] })).toBe("Created Owl, off air.");
    expect(createdText({ name: "Owl", copied: ["saved settings"] }))
      .toBe("Created Owl, off air. Copied saved settings.");
    expect(createdText({ name: "Owl", copied: ["saved settings", "clockwheels", "carts"] }))
      .toBe("Created Owl, off air. Copied saved settings, clockwheels and carts.");
  });
});

describe("Settings > Station > Stations", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("lists the stations and offers a copy of each", () => {
    setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    const rows = [...byId("stations-list").children];
    expect(rows.map((row) => [...row.children].slice(0, 3).map((cell) => cell.textContent))).toEqual([
      ["AutoDJ", "Artist — Song", "3"],
      ["Night Owl Radio", "Off air", ""],
    ]);
    const [autodjBox, owlBox] = rows.map((row) => row.children[3].querySelector("input"));
    expect([autodjBox.checked, autodjBox.disabled, owlBox.checked, owlBox.disabled])
      .toEqual([true, true, false, false]);
    expect(owlBox.getAttribute("aria-label")).toBe("Start Night Owl Radio with the server");
    const [autodjButton, owlButton] = rows.map((row) => row.children[4].querySelector("button"));
    expect([autodjButton.hidden, autodjButton.textContent, autodjButton.getAttribute("aria-label")])
      .toEqual([false, "Stop", "Stop AutoDJ"]);
    expect([owlButton.textContent, owlButton.getAttribute("aria-label")]).toEqual(["Start", "Start Night Owl Radio"]);
    expect([...byId("station-create-source").options].map((option) => [option.value, option.textContent]))
      .toEqual([["", "AutoDJ defaults"], ["default", "Copy of AutoDJ"], ["nightowl", "Copy of Night Owl Radio"]]);
  });

  it("stages Start with the server until the named row is saved", async () => {
    const request = vi.fn(async (_url, options) => ({ ...OWL, autostart: JSON.parse(options.body).enabled }));
    const { onChanged } = setup({ request, stations: [AUTODJ, OWL] });
    const row = byId("stations-list").children[1];
    const box = row.children[3].querySelector("input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));
    expect(request).not.toHaveBeenCalled();
    const [save, discard] = row.children[3].querySelectorAll("button");
    expect(save.hidden).toBe(false);
    expect(save.getAttribute("aria-label")).toBe("Save Start with the server for Night Owl Radio");
    save.click();
    expect(save.disabled).toBe(true);
    expect(discard.disabled).toBe(true);
    await vi.waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(request).toHaveBeenCalledWith("/api/stations/nightowl/autostart", expect.objectContaining({ method: "PUT" }));
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ enabled: true });
    await vi.waitFor(() => expect(byId("stations-status").textContent.trim())
      .toBe("Night Owl Radio starts with the server."));
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("preserves a staged row value across refresh and discards it", () => {
    const { refresh } = setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    const row = byId("stations-list").children[1];
    const box = row.children[3].querySelector("input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));
    refresh([AUTODJ, { ...OWL, title: "Updated title" }]);
    expect(byId("stations-list").children[1].children[3].querySelector("input").checked).toBe(true);
    row.children[3].querySelectorAll("button")[1].click();
    expect(box.checked).toBe(false);
    expect(row.children[3].querySelector('[role="group"]').hidden).toBe(true);
  });

  it("reorders rows around focus while retaining an autostart draft and table navigation", () => {
    const { refresh } = setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    const list = byId("stations-list");
    const owl = list.children[1];
    const box = owl.children[3].querySelector("input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));
    box.focus();

    refresh([OWL, { ...AUTODJ, title: "Changed song" }]);

    expect(list.children[0]).toBe(owl);
    expect(document.activeElement).toBe(box);
    expect(box.checked).toBe(true);
    expect(owl.children[3].querySelector("button").hidden).toBe(false);
    box.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(list.children[1].children[3]);
  });

  it("recovers focus to the same column when the focused station row disappears", () => {
    const { refresh } = setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    const list = byId("stations-list");
    const box = list.children[1].children[3].querySelector("input");
    box.focus();

    refresh([AUTODJ]);

    expect(document.activeElement).toBe(list.children[0].children[3]);
  });

  it("retains a failed row edit and ignores a reply from an old login", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const { onChanged } = setup({ request, stations: [AUTODJ, OWL] });
    const row = byId("stations-list").children[1];
    const box = row.children[3].querySelector("input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));
    row.children[3].querySelector("button").click();
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    expect(box.disabled).toBe(true);
    invalidateAuthenticatedRequestEpoch();
    finish({ ...OWL, autostart: true });
    await settle();
    expect(box.checked).toBe(true);
    expect(box.disabled).toBe(false);
    expect(row.children[3].querySelector("button").hidden).toBe(false);
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("keeps a failed row save staged so it can be retried", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("The server is busy."));
    setup({ request, stations: [AUTODJ, OWL] });
    const row = byId("stations-list").children[1];
    const box = row.children[3].querySelector("input");
    box.checked = true;
    box.dispatchEvent(new window.Event("change"));
    row.children[3].querySelector("button").click();
    await vi.waitFor(() => expect(byId("stations-status").textContent)
      .toContain("Could not save the Start with the server change for Night Owl Radio."));
    expect(box.checked).toBe(true);
    expect(row.children[3].querySelector("button").hidden).toBe(false);
  });

  it("starts a station at once and asks before stopping one", async () => {
    const request = vi.fn(async () => ({ ...OWL, warnings: [] }));
    const { onChanged } = setup({ request, stations: [AUTODJ, OWL, { ...OWL, slug: "late", name: "Late", state: "playing" }] });
    const [, owl, late] = byId("stations-list").children;
    expect(late.children[4].querySelector("button").getAttribute("aria-label")).toBe("Stop Late");
    owl.children[4].querySelector("button").click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/stations/nightowl/start", { method: "POST" }));
    await vi.waitFor(() => expect(byId("stations-status").textContent.trim()).toBe("Started Night Owl Radio."));
    late.children[4].querySelector("button").click();
    await settle();
    expect(byId("confirm-dialog").open).toBe(true);
    expect(byId("confirm-title").textContent).toBe("Stop Late?");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/stations/late/stop", { method: "POST" }));
    await vi.waitFor(() => expect(byId("stations-status").textContent.trim()).toBe("Stopped Late."));
    expect(onChanged).toHaveBeenCalledTimes(2);
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
      .toEqual(["station-create-error", "station-create-slug-help"]);
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
