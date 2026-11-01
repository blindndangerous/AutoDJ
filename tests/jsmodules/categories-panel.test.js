
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import { countsText, installCategoriesPanel } from "../../src/autodj/static/modules/categories-panel.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => (byId("categories-form-dialog")?.open ? byId("categories-form-status") : byId("categories-status")).textContent;
const items = () => [...byId("categories-list").querySelectorAll("tbody tr[data-row-key]")];
const rowButton = (index, action) => items()[index].querySelector(`button[data-action="${action}"]`);

const DECADE = {
  id: "c1", name: "Remembering the 2000s", source: "query", query: "original_year:2000..2009", playlist: "",
  tracks: 1284, missing: 0, missing_examples: [], fallback: 12, error: null, used_by: ["Throwback"],
};
const ROTATION_A = {
  id: "c2", name: "A", source: "m3u", query: "", playlist: "A.m3u8",
  tracks: 300, missing: 7, missing_examples: ["one.flac", "two.flac", "three.flac", "four.flac", "five.flac", "six.flac"],
  fallback: 0, error: null, used_by: [],
};

function installIndex() {
  installIndexMarkup();
  byId("panel-library").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

class FieldError extends Error {
  constructor(message, field) { super(message); this.field = field; }
}

// A stand-in for the category routes.
function fakeServer() {
  const server = { categories: [copy(DECADE), copy(ROTATION_A)], calls: [], refuse: null };
  const listing = () => ({
    categories: copy(server.categories), playlists: ["A.m3u8", "B.m3u", "Gold/C.m3u8"],
    playlists_folder: "D:\\Radio\\playlists", beets: true,
    folders: [{ id: "music", kind: "music", path: "D:\\Music", recursive: true }],
    genres: ["Rock", "Pop"], beets_fields: ["mood"],
  });
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.refuse && method !== "GET") throw server.refuse;
    if (url === "/api/categories" && method === "GET") return listing();
    if (url === "/api/categories/check") return { tracks: 1, missing: 0, missing_examples: [], fallback: 1, error: null };
    if (url === "/api/categories" && method === "POST") {
      const category = { ...body, id: "c3", tracks: 42, missing: 0, missing_examples: [], fallback: 0, error: null, used_by: [] };
      server.categories.push(category);
      return { category, ...listing() };
    }
    const id = decodeURIComponent(url.split("/").pop());
    if (method === "PUT") {
      const index = server.categories.findIndex((item) => item.id === id);
      server.categories[index] = { ...server.categories[index], ...body };
      return { category: server.categories[index], ...listing() };
    }
    if (method === "DELETE") {
      server.categories = server.categories.filter((item) => item.id !== id);
      return listing();
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

let controls = null;

async function open(server = fakeServer(), options = {}) {
  controls = installCategoriesPanel({ document, request: server.request, ...options });
  await vi.waitFor(() => expect(items()).toHaveLength(server.categories.length));
  return server;
}

beforeEach(installIndex);
afterEach(() => { controls?.destroy(); controls = null; });

describe("categories markup", () => {
  it("is its own card in Library tools, before Clockwheels, with labelled native controls", () => {
    const section = byId("categories-heading").closest("details");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    expect(section.nextElementSibling).toBe(byId("clockwheel-heading").closest("details"));
    expect(byId("categories-heading").tagName).toBe("H2");
    expect(section.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')).toHaveLength(1);
    expect(byId("categories-status").getAttribute("role")).toBe("status");
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
    }
    // The form and the source choice are grouped with a legend each.
    const fieldsets = [...section.querySelectorAll("fieldset")];
    expect(fieldsets.map((set) => set.querySelector(":scope > legend")?.textContent)).toEqual(["Add category", "Songs come from", "When the songs may play", "Days"]);
    expect([...section.querySelectorAll('input[type="radio"]')].every((radio) => radio.name === "categories-source")).toBe(true);
    for (const id of ["categories-query", "categories-playlist"]) {
      expect(byId(id).hasAttribute("aria-describedby")).toBe(false);
      expect(byId(`${id}-help`).textContent.trim()).not.toBe("");
    }
    expect(byId("categories-check").hasAttribute("aria-describedby")).toBe(false);
    expect(byId("categories-list").getAttribute("aria-labelledby")).toBe("categories-list-heading");
    expect(byId("categories-match").getAttribute("aria-live")).toBe("off");
    expect(byId("categories-match").getAttribute("aria-hidden")).toBeNull();
    expect(section.querySelector("[aria-controls]")).toBeNull();
    expect(byId("categories-form").hidden).toBe(true);
    const ids = [...document.querySelectorAll('[id^="categories-"]')].map((node) => node.id);
    expect(ids.length).toBe(new Set(ids).size);
  });
});

describe("categories list", () => {
  // ADR-0021-27
  it("uses a named collection grid with one primary action and a Delete menu", async () => {
    await open();
    const table = byId("categories-list").querySelector("table.collection-table");
    expect(table).not.toBeNull();
    expect([...table.querySelector("thead tr").cells].map((cell) => cell.textContent)).toEqual(["Name", "Songs", "Source", "Availability", "Used by", "Actions", "Options"]);
    const row = table.querySelector("tbody tr");
    expect(row.cells[0].textContent).toBe(DECADE.name);
    expect(row.querySelector('[aria-haspopup="menu"]').getAttribute("aria-label")).toBe(`Options for ${DECADE.name}`);
    row.querySelector('[aria-haspopup="menu"]').click();
    expect(document.activeElement.textContent).toBe("Delete");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(document.activeElement).toBe(row.cells[6]);
  });
  it("says the counts in words", () => {
    expect(countsText(DECADE)).toBe("1,284 songs; 12 use the release year because the first-release year is missing");
    expect(countsText({ tracks: 1, missing: 1, fallback: 1 })).toBe(
      "1 song; 1 playlist line matches no library track; 1 uses the release year because the first-release year is missing");
    expect(countsText({ error: "The playlist file A.m3u is missing" })).toBe("The playlist file A.m3u is missing");

  });

  it("lists category data with primary Edit and Delete in Options without announcing", async () => {
    await open();
    expect(items()[0].cells[1].textContent).toBe(countsText(DECADE));
    expect(items()[0].cells[2].textContent).toBe("Query original_year:2000..2009");
    expect(items()[0].cells[4].textContent).toBe("Throwback");
    expect(items()[1].cells[1].textContent).toBe("300 songs; 7 playlist lines match no library track. Not in the library: one.flac, two.flac, three.flac, four.flac, five.flac, and 2 more.");
    expect(items()[1].cells[2].textContent).toBe("Playlist A.m3u8");
    expect(items()[1].cells[4].textContent).toBe("No clockwheel uses it.");
    expect(rowButton(1, "edit").textContent).toBe("Edit");
    expect(rowButton(1, "edit").getAttribute("aria-label")).toBe("Edit A");
    expect(rowButton(1, "delete").getAttribute("aria-label")).toBe("Delete");
    expect(status()).toBe("");
    const cells = [...byId("categories-list").querySelectorAll("tbody td")];
    expect(cells.filter((item) => item.tabIndex === 0)).toHaveLength(1);
    cells[0].focus();
    cells[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(cells[1]);
    expect(byId("categories-playlist").options).toHaveLength(3);
    expect(byId("categories-folder").textContent).toBe("D:\\Radio\\playlists");
  });
});

describe("categories form", () => {
  it("keeps a refused field's error when another field changes", async () => {
    await open();
    byId("categories-add").click();
    byId("categories-save").click();
    await vi.waitFor(() => expect(byId("categories-name").getAttribute("aria-invalid")).toBe("true"));
    byId("categories-query").value = "genre:Rock";
    byId("categories-query").dispatchEvent(new Event("input", { bubbles: true }));
    expect(byId("categories-form-error").hidden).toBe(false);
    expect(byId("categories-form-error").textContent).toBe("Enter a name for the category.");
  });
  it("reports incomplete filter conditions from Check and Save", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Filtered";
    byId("categories-source-filters").checked = true;
    byId("categories-source-filters").dispatchEvent(new Event("change"));
    const row = byId("categories-filter-conditions").firstElementChild;
    row.querySelector("select").value = "artist";
    row.querySelector("select").dispatchEvent(new Event("change"));
    byId("categories-check").click();
    expect(byId("categories-form-error").textContent).toContain("Enter a value for artist.");
    byId("categories-save").click();
    expect(byId("categories-form-error").textContent).toContain("Enter a value for artist.");
    expect(server.calls.some((call) => call.method === "POST" && call.url === "/api/categories")).toBe(false);
  });

  it("saves shared filter conditions and checks unsaved matches after a pause", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Late night";
    byId("categories-source-filters").checked = true;
    byId("categories-source-filters").dispatchEvent(new Event("change"));
    const row = byId("categories-filter-conditions").firstElementChild;
    const selects = row.querySelectorAll("select");
    selects[0].value = "beets";
    selects[0].dispatchEvent(new Event("change"));
    expect([...selects[2].options].map((option) => option.value)).toContain("is_not");
    selects[0].value = "bpm";
    selects[0].dispatchEvent(new Event("change"));
    selects[2].value = "not_eq";
    row.querySelectorAll('input[type="text"]')[1].value = "90";
    byId("categories-filter-match").value = "any";
    byId("categories-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "POST" && call.url === "/api/categories")).toBe(true));
    const saved = server.calls.find((call) => call.method === "POST" && call.url === "/api/categories").body;
    expect(saved).toMatchObject({
      name: "Late night", source: "filters", match: "any",
      conditions: [{ field: "bpm", operator: "not_eq", value: 90 }],
    });
  });

  it("adds a query category: Check counts it, Save lists it and moves focus to it", async () => {
    const changed = vi.fn();
    const server = await open(fakeServer(), { onChange: changed });
    byId("categories-add").click();
    expect(byId("categories-form").hidden).toBe(false);
    expect(byId("categories-form-legend").textContent).toBe("Add category");
    expect(document.activeElement).toBe(byId("categories-name"));
    expect(byId("categories-playlist-field").hidden).toBe(true);
    byId("categories-name").value = "Rock gold";
    byId("categories-query").value = "genre:rock original_year:..1989";
    byId("categories-check").click();
    await vi.waitFor(() => expect(status()).toBe(
      "Rock gold: 1 song; 1 uses the release year because the first-release year is missing."));
    expect(byId("categories-match").textContent).toBe("1 song; 1 uses the release year because the first-release year is missing.");
    expect(server.calls.at(-1)).toEqual({
      method: "POST", url: "/api/categories/check",
      body: { name: "Rock gold", source: "query", query: "genre:rock original_year:..1989", playlist: "" },
    });
    byId("categories-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Rock gold: 42 songs."));
    expect(byId("categories-form").hidden).toBe(true);
    expect(items()).toHaveLength(3);
    expect(document.activeElement).toBe(rowButton(2, "edit").closest("td"));
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it("announces a settled background count once without announcing edits", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Rock gold";
    byId("categories-name").dispatchEvent(new Event("input", { bubbles: true }));
    byId("categories-query").value = "genre:rock";
    byId("categories-query").dispatchEvent(new Event("input", { bubbles: true }));
    expect(status()).toBe("");
    await vi.waitFor(() => expect(status()).toBe(
      "1 song; 1 uses the release year because the first-release year is missing."), { timeout: 1500 });
    expect(server.calls.filter((call) => call.url === "/api/categories/check")).toHaveLength(1);
  });

  it("edits a playlist category with the playlist choice, and Cancel goes back to its Edit cell", async () => {
    const server = await open();
    rowButton(1, "edit").click();
    expect(byId("categories-form-legend").textContent).toBe("Edit category A");
    expect(byId("categories-source-m3u").checked).toBe(true);
    expect(byId("categories-query-field").hidden).toBe(true);
    expect(byId("categories-playlist-field").hidden).toBe(false);
    expect(byId("categories-playlist").value).toBe("A.m3u8");
    expect(byId("categories-match").textContent).toBe("Matching selection: 300 songs; 7 playlist lines match no library track.");
    byId("categories-cancel").click();
    expect(document.activeElement).toBe(rowButton(1, "edit").closest("td"));
    await vi.waitFor(() => expect(status()).toBe("Cancelled. Nothing was saved."));

    rowButton(1, "edit").click();
    byId("categories-playlist").value = "Gold/C.m3u8";
    byId("categories-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved A: 300 songs; 7 playlist lines match no library track."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/categories/c2");
    expect(put.body).toEqual({ name: "A", source: "m3u", query: "", playlist: "Gold/C.m3u8" });
    expect(document.activeElement).toBe(rowButton(1, "edit").closest("td"));
  });

  it("saves allowed days and hours, fills them in on Edit and says them in the list", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Late night";
    byId("categories-query").value = "genre:jazz";
    byId("categories-day-4").checked = true;
    byId("categories-day-5").checked = true;
    byId("categories-hours-from").value = "22:00";
    byId("categories-hours-until").value = "02:00";
    byId("categories-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved Late night: 42 songs."));
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Late night", source: "query", query: "genre:jazz", playlist: "",
      days: [4, 5], hours_from: "22:00", hours_until: "02:00",
    });

    server.categories[2] = {
      ...server.categories[2], hours_from: "22:00:00", hours_until: "02:00:00",
      daypart_text: "Plays only on Friday and Saturday from 22:00 until 02:00", allowed_now: false,
    };
    await controls.refresh();
    expect(items()[2].textContent).toContain(
      "Plays only on Friday and Saturday from 22:00 until 02:00. Outside its hours now.");
    rowButton(2, "edit").click();
    expect(byId("categories-day-4").checked).toBe(true);
    expect(byId("categories-day-0").checked).toBe(false);
    expect(byId("categories-hours-from").value).toBe("22:00");
    expect(byId("categories-hours-until").value).toBe("02:00");
  });

  it("saves a season, fills it in on Edit and says when its dates hold the songs back", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Christmas";
    byId("categories-query").value = "genre:xmas";
    byId("categories-not-before").value = "2026-12-01";
    byId("categories-not-after").value = "2026-12-26";
    byId("categories-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "POST")).toBe(true));
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Christmas", source: "query", query: "genre:xmas", playlist: "",
      not_before: "2026-12-01", not_after: "2026-12-26",
    });
    server.categories[2] = {
      ...server.categories[2], not_before: "2026-12-01", not_after: "2026-12-26",
      daypart_text: "Plays only between 1 December 2026 and 26 December 2026",
      allowed_now: false, outside_now: "dates",
    };
    await controls.refresh();
    expect(items()[2].textContent).toContain(
      "Plays only between 1 December 2026 and 26 December 2026. Outside its dates now.");
    rowButton(2, "edit").click();
    expect(byId("categories-not-before").value).toBe("2026-12-01");
    expect(byId("categories-not-after").value).toBe("2026-12-26");
  });

  it("says a category is now outside its dates in place, without moving focus or a sound", async () => {
    const server = await open();
    server.categories[0] = {
      ...server.categories[0], not_after: "2026-12-26",
      daypart_text: "Plays only until 26 December 2026", allowed_now: true, outside_now: null,
    };
    await controls.refresh();
    const row = items()[0];
    rowButton(0, "edit").focus();
    const button = document.activeElement;
    const said = status();
    // The station's midnight passes: the next poll finds the season over.
    server.categories[0] = { ...server.categories[0], allowed_now: false, outside_now: "dates" };
    await controls.refresh();
    expect(items()[0]).toBe(row);
    expect(row.textContent).toContain(
      "Plays only until 26 December 2026. Outside its dates now.");
    expect(document.activeElement).toBe(button);
    expect(status()).toBe(said);
  });

  it("names a backwards season and links the error to Do not play after", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Christmas";
    byId("categories-query").value = "genre:xmas";
    byId("categories-not-before").value = "2026-12-26";
    byId("categories-not-after").value = "2026-12-01";
    byId("categories-save").click();
    const until = byId("categories-not-after");
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("categories-form-error categories-dates-repeat-help");
    expect(byId("categories-form-error").textContent).toBe(
      "Do not play after must be on or after the Not before date.");
    until.value = "2026-12-31";
    until.dispatchEvent(new Event("input"));
    expect(until.hasAttribute("aria-invalid")).toBe(false);
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);
  });

  // ADR-0009-09
  it("repeats a season every week or every year, with help the date fields read too", async () => {
    const server = await open();
    byId("categories-add").click();
    const repeat = byId("categories-dates-repeat");
    const help = byId("categories-dates-repeat-help");
    expect(repeat.value).toBe("none");
    expect(repeat.labels[0].textContent).toBe("Repeat dates");
    expect([...repeat.options].map((item) => item.textContent)).toEqual(["Don't repeat", "Every week", "Every year"]);
    for (const id of ["categories-dates-repeat", "categories-not-before", "categories-not-after"]) {
      expect(byId(id).getAttribute("aria-describedby")).toBe("categories-dates-repeat-help");
    }
    expect(help.textContent).toBe("The songs play only between these dates, both included.");
    repeat.value = "yearly";
    repeat.dispatchEvent(new Event("change"));
    expect(help.textContent).toContain("ignoring the year; a range may run past New Year");

    // Every year: the last date may come before the first, past New Year.
    byId("categories-name").value = "Holidays";
    byId("categories-query").value = "genre:xmas";
    byId("categories-not-before").value = "2026-12-20";
    byId("categories-not-after").value = "2026-01-02";
    byId("categories-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "POST")).toBe(true));
    expect(server.calls.find((call) => call.method === "POST").body).toMatchObject({
      not_before: "2026-12-20", not_after: "2026-01-02", dates_repeat: "yearly",
    });
    await vi.waitFor(() => expect(byId("categories-form-dialog").open).toBe(false));
    rowButton(2, "edit").click();
    expect(byId("categories-dates-repeat").value).toBe("yearly");
    expect(help.textContent).toContain("ignoring the year");
    byId("categories-cancel").click();

    // Every week: Friday to Sunday; no more than 7 days.
    rowButton(0, "edit").click();
    repeat.value = "weekly";
    repeat.dispatchEvent(new Event("change"));
    expect(help.textContent).toContain("Repeats every week");
    byId("categories-not-before").value = "2026-11-06";
    byId("categories-not-after").value = "2026-11-13";
    byId("categories-save").click();
    const until = byId("categories-not-after");
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(byId("categories-form-error").textContent).toBe(
      "For Every week, Do not play after must be within 6 days of the Not before date.");
    byId("categories-not-after").value = "2026-11-08";
    await new Promise((resolve) => setTimeout(resolve, 0)); // the refused save settles
    byId("categories-save").click();
    await vi.waitFor(() => expect(server.calls.filter((call) => call.method === "PUT")).toHaveLength(1));
    expect(server.calls.find((call) => call.method === "PUT").body).toMatchObject({
      not_before: "2026-11-06", not_after: "2026-11-08", dates_repeat: "weekly",
    });
  });

  it("asks for Not before before the dates can repeat", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Fridays";
    byId("categories-query").value = "genre:party";
    byId("categories-dates-repeat").value = "weekly";
    byId("categories-save").click();
    const from = byId("categories-not-before");
    expect(document.activeElement).toBe(from);
    expect(from.getAttribute("aria-invalid")).toBe("true");
    expect(from.getAttribute("aria-describedby")).toBe("categories-form-error categories-dates-repeat-help");
    expect(byId("categories-form-error").textContent).toBe("Enter a Not before date to repeat the dates.");
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);
    // Don't repeat is never sent, as the server never saves it.
    byId("categories-dates-repeat").value = "none";
    await new Promise((resolve) => setTimeout(resolve, 0)); // the refused save settles
    byId("categories-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "POST")).toBe(true));
    expect(server.calls.find((call) => call.method === "POST").body).not.toHaveProperty("dates_repeat");
  });

  it("names a half-typed time instead of saving the category for all day", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Late night";
    byId("categories-query").value = "genre:jazz";
    // A browser reports a half-typed time as an empty value with bad input.
    const from = byId("categories-hours-from");
    Object.defineProperty(from, "validity", { configurable: true, value: { badInput: true } });
    byId("categories-save").click();
    expect(document.activeElement).toBe(from);
    expect(from.getAttribute("aria-invalid")).toBe("true");
    expect(from.getAttribute("aria-describedby")).toBe("categories-form-error");
    expect(byId("categories-form-error").textContent).toBe(
      "Allowed from is not a whole time. Enter hours and minutes, such as 22:00, or clear it.");
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("names a half-filled or empty time window and links the error to the field", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-name").value = "Late night";
    byId("categories-query").value = "genre:jazz";
    byId("categories-hours-from").value = "22:00";
    byId("categories-save").click();
    const until = byId("categories-hours-until");
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("categories-form-error");
    expect(byId("categories-form-error").textContent).toBe("Enter both Allowed from and Allowed until, or leave both empty.");
    until.value = "22:00";
    until.dispatchEvent(new Event("input", { bubbles: true }));
    expect(until.hasAttribute("aria-describedby")).toBe(false);
    await Promise.resolve();
    byId("categories-save").click();
    expect(byId("categories-form-error").textContent).toBe("Allowed until must differ from Allowed from.");
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);

    until.value = "02:00";
    await Promise.resolve();
    server.refuse = new FieldError("Days must be numbered 0 (Monday) to 6 (Sunday)", "days");
    byId("categories-save").click();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("categories-day-0")));
    for (let day = 0; day < 7; day += 1) {
      expect(byId(`categories-day-${day}`).getAttribute("aria-invalid")).toBe("true");
      expect(byId(`categories-day-${day}`).getAttribute("aria-describedby")).toBe("categories-form-error");
    }
    byId("categories-day-3").dispatchEvent(new Event("input", { bubbles: true }));
    expect(byId("categories-day-0").hasAttribute("aria-invalid")).toBe(false);
  });

  it("marks, focuses and says the field at fault, in the page and from the server", async () => {
    const server = await open();
    byId("categories-add").click();
    byId("categories-save").click();
    expect(byId("categories-name").getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(byId("categories-name"));
    expect(byId("categories-form-error").hidden).toBe(false);
    expect(byId("categories-form-error").textContent).toBe("Enter a name for the category.");
    await vi.waitFor(() => expect(status()).toBe("Enter a name for the category."));

    byId("categories-name").value = "Moods";
    byId("categories-query").value = "mood::sad";
    server.refuse = new FieldError('"mood::sad": regular expressions (field::pattern) are not supported', "query");
    byId("categories-save").click();
    await vi.waitFor(() => expect(byId("categories-query").getAttribute("aria-invalid")).toBe("true"));
    expect(byId("categories-name").hasAttribute("aria-invalid")).toBe(false);
    expect(document.activeElement).toBe(byId("categories-query"));
    expect(byId("categories-form-error").textContent).toContain("regular expressions");
    expect(byId("categories-form").hidden).toBe(false);
  });

  it("deletes after confirming, says which clockwheels used it, and moves focus on", async () => {
    const changed = vi.fn();
    await open(fakeServer(), { onChange: changed });
    rowButton(0, "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Delete category Remembering the 2000s?");
    expect(byId("confirm-message").textContent).toBe("Throwback has a step for it. Those steps are skipped until you change them.");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Deleted Remembering the 2000s."));
    expect(items()).toHaveLength(1);
    expect(document.activeElement).toBe(rowButton(0, "edit").closest("td"));
    expect(changed).toHaveBeenCalledTimes(1);
    rowButton(0, "delete").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Deleted A."));
    expect(byId("categories-list").querySelector(".no-results").textContent).toBe("No categories yet. Press Add category to make one.");
    expect(document.activeElement).toBe(byId("categories-add"));
  });

  it("names a clockwheel saved since the last refresh before deleting, keeping focus on Delete", async () => {
    const server = await open();
    // A clockwheel step for A was saved after the list was loaded.
    server.categories[1].used_by = ["Morning", "Evening"];
    const button = rowButton(1, "delete");
    button.focus();
    button.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-message").textContent).toBe("Morning, Evening have steps for it. Those steps are skipped until you change them.");
    // The row's words changed in place; Delete keeps focus under the dialog.
    expect(rowButton(1, "delete")).toBe(button);
    expect(items()[1].cells[4].textContent).toBe("Morning, Evening");
    byId("confirm-cancel").click();
    expect(server.calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
  });

  it("says why Delete did nothing when the categories cannot be loaded", async () => {
    const server = await open();
    const request = server.request.getMockImplementation();
    server.request.mockImplementation(async (url, options = {}) => {
      if (url === "/api/categories" && !options.method) throw new Error("The AutoDJ server is not reachable.");
      return request(url, options);
    });
    rowButton(0, "delete").click();
    await vi.waitFor(() => expect(status()).toBe("Could not delete Remembering the 2000s: The AutoDJ server is not reachable."));
    expect(byId("confirm-dialog").open).toBe(false);
  });

  it("forgets everything on sign-out", async () => {
    await open();
    rowButton(0, "edit").click();
    controls.clearProtectedData();
    expect(items()).toHaveLength(0);
    expect(byId("categories-form").hidden).toBe(true);
    expect(byId("categories-name").value).toBe("");
    expect(byId("categories-playlist").options).toHaveLength(0);
  });
});


describe("category modal lifecycle", () => {
  it("keeps unsaved edits on Escape until discard is confirmed", async () => {
    await open();
    byId("categories-add").focus();
    byId("categories-add").click();
    const dialog = byId("categories-form-dialog");
    expect(dialog.open).toBe(true);
    expect(dialog.getAttribute("aria-labelledby")).toBe("categories-form-legend");
    byId("categories-name").value = "Unfinished";
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(byId("confirm-dialog").open).toBe(true);
    byId("confirm-cancel").click();
    await Promise.resolve();
    expect(dialog.open).toBe(true);
    expect(byId("categories-name").value).toBe("Unfinished");
    byId("categories-cancel").click();
    byId("confirm-accept").click();
    await Promise.resolve();
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(byId("categories-add"));
  });

  it("ignores a pending save after station clear and prevents duplicate submissions", async () => {
    const server = fakeServer();
    const request = server.request;
    let settle;
    server.request = vi.fn((url, options) => options?.method === "PUT"
      ? new Promise((resolve) => { settle = resolve; }) : request(url, options));
    await open(server);
    rowButton(0, "edit").click();
    const form = byId("categories-form");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(server.request.mock.calls.filter(([, options]) => options?.method === "PUT")).toHaveLength(1);
    controls.clearProtectedData();
    settle({ categories: [DECADE], category: DECADE });
    await Promise.resolve();
    await Promise.resolve();
    expect(byId("categories-form-dialog").open).toBe(false);
    expect(items()).toHaveLength(0);
    expect(status()).toBe("");
  });

  it("retains a missing playlist choice rather than saving a different playlist", async () => {
    const server = fakeServer();
    server.categories[1].playlist = "Missing.m3u";
    await open(server);
    rowButton(1, "edit").click();
    expect(byId("categories-playlist").value).toBe("Missing.m3u");
    expect(byId("categories-playlist").selectedOptions[0].textContent).toContain("unavailable");
  });
});
