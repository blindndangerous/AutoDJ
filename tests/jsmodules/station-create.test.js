
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

describe("Settings > Global > Stations management", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("keeps station management in Global > Stations and selected-station settings in General", () => {
    setup({ request: vi.fn(), stations: [AUTODJ, OWL] });
    expect(byId("stations-list").closest("#spanel-station-management")).not.toBeNull();
    expect(byId("station-create-form").closest("#spanel-station-management")).not.toBeNull();
    expect(byId("time-settings").closest("#spanel-stations")).not.toBeNull();
    expect(byId("station-output").closest("#spanel-stations")).not.toBeNull();
    expect(byId("station-log-label").closest("#spanel-stations")).not.toBeNull();
    expect(byId("stab-stations").textContent).toBe("General");
    expect(byId("stab-station-management").textContent).toBe("Stations");
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

  // ADR-0005-06 and ADR-0010-09.
  it("archives and restores from the same row, preserving focus and the stopped state", async () => {
    const request = vi.fn(async () => ({}));
    const { onChanged, refresh } = setup({ request, stations: [AUTODJ, OWL] });
    onChanged.mockImplementation(async (station, action) => {
      refresh([AUTODJ, { ...station, archived: action === "archive" }]);
    });
    const row = byId("stations-list").children[1];
    const archive = row.querySelector('button[aria-label="Archive Night Owl Radio"]');
    archive.focus();
    archive.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Archive Night Owl Radio?");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/stations/nightowl/archive", { method: "POST" }));
    await vi.waitFor(() => expect(row.station.archived).toBe(true));
    expect(document.activeElement).toBe(row.children[5]);
    expect(row.children[4].querySelector("button").hidden).toBe(true);

    const restore = row.querySelector('button[aria-label="Restore Night Owl Radio"]');
    restore.focus();
    restore.click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/stations/nightowl/restore", { method: "POST" }));
    await vi.waitFor(() => expect(row.station.archived).toBe(false));
    expect(document.activeElement).toBe(row.children[5]);
    await vi.waitFor(() => expect(byId("stations-status").textContent)
      .toContain("It remains off air until you start it."));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  // ADR-0005-06.
  it("cancels archive cleanly and does not send a confirmation from an old login", async () => {
    const request = vi.fn(async () => ({}));
    setup({ request, stations: [AUTODJ, OWL] });
    const archive = byId("stations-list").children[1]
      .querySelector('button[aria-label="Archive Night Owl Radio"]');
    archive.focus();
    archive.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(request).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(archive.closest("td"));

    archive.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    invalidateAuthenticatedRequestEpoch();
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(request).not.toHaveBeenCalled();
  });

  // ADR-0005-06 and ADR-0010-10.
  it("confirms permanent deletion in a modal, defaults voice removal off, and returns focus after removal", async () => {
    const archived = { ...OWL, archived: true };
    const request = vi.fn(async () => ({ cleanup_pending: true }));
    const { onChanged, refresh } = setup({ request, stations: [AUTODJ, archived] });
    onChanged.mockImplementation(async () => refresh([AUTODJ]));
    const row = byId("stations-list").children[1];
    expect(byId("stations-list").children[0]
      .querySelector('[aria-label="Delete permanently: AutoDJ"]')?.hidden).toBe(true);
    const toggle = row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]');
    toggle.focus();
    toggle.click();
    const dialog = byId("station-delete-dialog");
    expect(dialog.open).toBe(true);
    expect(dialog.querySelector("h1").textContent).toBe("Delete Night Owl Radio permanently?");
    expect(dialog.querySelector("h1").id).toBe("station-delete-title");
    expect(dialog.textContent).not.toContain("Voice recordings are retained outside this folder");
    const confirmation = dialog.querySelector('input[type="text"]');
    const voice = [...dialog.querySelectorAll('input[type="checkbox"]')].at(-1);
    const deleteButton = dialog.querySelector('button[type="submit"]');
    expect(voice.checked).toBe(false);
    confirmation.value = "delete";
    deleteButton.click();
    expect(confirmation.getAttribute("aria-invalid")).toBe("true");
    expect(confirmation.getAttribute("aria-describedby")).toContain("station-delete-error");
    expect(request).not.toHaveBeenCalled();
    confirmation.value = "DELETE";
    confirmation.dispatchEvent(new window.Event("input", { bubbles: true }));
    deleteButton.focus();
    deleteButton.click();
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledWith(archived, "delete"));
    expect(request).toHaveBeenCalledWith("/api/stations/nightowl", expect.objectContaining({
      method: "DELETE",
      body: JSON.stringify({ confirmation: "DELETE", delete_voice_recordings: false }),
    }));
    expect(byId("stations-status").textContent)
      .toContain("Cleanup is pending.");
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(byId("stations-list").children[0].children[5]);
  });

  // ADR-0011-03.
  it("Cancel and Escape close deletion without a request and return focus to the owning cell", async () => {
    const request = vi.fn();
    setup({ request, stations: [AUTODJ, { ...OWL, archived: true }] });
    const row = byId("stations-list").children[1];
    const toggle = row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]');
    const dialog = byId("station-delete-dialog");

    toggle.focus();
    toggle.click();
    dialog.querySelector('button[type="button"]').click();
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(document.activeElement).toBe(toggle.closest("td"));

    toggle.click();
    dialog.querySelector('input[type="text"]').dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
    );
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(request).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(toggle.closest("td"));
  });

  it("keeps deletion errors in the modal and allows retry", async () => {
    const request = vi.fn()
      .mockRejectedValueOnce(new Error("The station is busy."))
      .mockResolvedValueOnce({});
    const archived = { ...OWL, archived: true };
    const { onChanged, refresh } = setup({ request, stations: [AUTODJ, archived] });
    onChanged.mockImplementation(async () => refresh([AUTODJ]));
    const row = byId("stations-list").children[1];
    row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]').click();
    const dialog = byId("station-delete-dialog");
    dialog.querySelector('input[type="text"]').value = "DELETE";
    dialog.querySelector('button[type="submit"]').click();
    await vi.waitFor(() => expect(dialog.querySelector("#station-delete-status").textContent)
      .toContain("Could not permanently delete Night Owl Radio."));
    expect(dialog.open).toBe(true);
    expect(dialog.querySelector("#station-delete-error").hidden).toBe(false);
    expect(byId("stations-status").textContent.trim()).toBe("");

    dialog.querySelector('button[type="submit"]').click();
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledWith(archived, "delete"));
    expect(request).toHaveBeenCalledTimes(2);
  });

  // ADR-0010-10.
  it("holds the modal while deletion is pending and ignores an old-login reply", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const { onChanged } = setup({ request, stations: [AUTODJ, { ...OWL, archived: true }] });
    const row = byId("stations-list").children[1];
    row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]').click();
    const dialog = byId("station-delete-dialog");
    const confirmation = dialog.querySelector('input[type="text"]');
    confirmation.value = "DELETE";
    const deleteButton = dialog.querySelector('button[type="submit"]');
    deleteButton.focus();
    deleteButton.click();
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));
    expect(dialog.open).toBe(true);
    expect(deleteButton.textContent).toBe("Deleting…");
    dialog.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(dialog.open).toBe(true);
    invalidateAuthenticatedRequestEpoch();
    finish({});
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(byId("stations-status").textContent.trim()).toBe("");
    expect(dialog.open).toBe(true);
    expect(deleteButton.hasAttribute("aria-disabled")).toBe(false);
    expect(row.isConnected).toBe(true);
    dialog.querySelector('button[type="button"]').click();
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(document.activeElement).toBe(row.children[5]);
  });

  it("does not submit a deletion after authentication changes while the modal is open", async () => {
    const request = vi.fn();
    setup({ request, stations: [AUTODJ, { ...OWL, archived: true }] });
    const row = byId("stations-list").children[1];
    row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]').click();
    const dialog = byId("station-delete-dialog");
    dialog.querySelector('input[type="text"]').value = "DELETE";
    invalidateAuthenticatedRequestEpoch();
    dialog.querySelector('button[type="submit"]').click();
    expect(request).not.toHaveBeenCalled();
    expect(dialog.open).toBe(true);
    expect(dialog.querySelector("#station-delete-status").textContent)
      .toContain("Your sign-in or this station changed.");
  });

  it("closes deletion safely if the row disappears during a pending request", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const { onChanged, refresh } = setup({ request, stations: [AUTODJ, { ...OWL, archived: true }] });
    const row = byId("stations-list").children[1];
    row.querySelector('[aria-label="Delete permanently: Night Owl Radio"]').click();
    const dialog = byId("station-delete-dialog");
    dialog.querySelector('input[type="text"]').value = "DELETE";
    dialog.querySelector('button[type="submit"]').click();
    await vi.waitFor(() => expect(finish).toBeTypeOf("function"));

    refresh([AUTODJ]);
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(byId("stations-list").children[0].children[5]);
    finish({});
    await settle();
    expect(onChanged).not.toHaveBeenCalled();
    expect(byId("stations-status").textContent.trim()).toBe("");
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

  // ADR-0022-06: an already-focused field needs one status announcement, without a focus jump.
  it("announces a server refusal once when its field already has focus", async () => {
    const message = "Could not create the station. An earlier station folder still exists.";
    const request = vi.fn(async () => { throw Object.assign(new Error("An earlier station folder still exists."), { field: "slug" }); });
    setup({ request });
    fill({ name: "Owl 2", slug: "owl2" });
    const slug = byId("station-create-slug");
    const region = byId("stations-status");
    slug.focus();
    const said = [], focused = [];
    const observer = new window.MutationObserver(() => { if (region.textContent) said.push(region.textContent); });
    observer.observe(region, { childList: true, characterData: true, subtree: true });
    const recordFocus = (event) => focused.push(event.target);
    document.addEventListener("focusin", recordFocus);
    try {
      submit();
      await vi.waitFor(() => expect(said).toEqual([message]));
      expect(document.activeElement).toBe(slug);
      expect(focused).toEqual([]);
      expect(slug.getAttribute("aria-invalid")).toBe("true");
      expect(slug.getAttribute("aria-describedby").split(" ")).toEqual(["station-create-error", "station-create-slug-help"]);
      expect(byId("station-create-error").textContent).toBe(message);
    } finally {
      observer.disconnect();
      document.removeEventListener("focusin", recordFocus);
    }
  });

  it("puts the server's refusal on the field it names", async () => {
    const error = Object.assign(new Error("Night Owl Radio already uses recordings/owl2."),
      { status: 409, field: "name" });
    const request = vi.fn(async () => { throw error; });
    setup({ request });
    fill({ name: "Owl 2", slug: "owl2" });
    byId("station-create-submit").focus();
    submit();
    const name = byId("station-create-name");
    await vi.waitFor(() => expect(name.getAttribute("aria-invalid")).toBe("true"));
    expect(name.getAttribute("aria-describedby")).toBe("station-create-error");
    expect(document.activeElement).toBe(name);
    expect(byId("stations-status").textContent).toBe("");
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
