
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import { describeOnAir, describeShow, installShowsPanel, summaryText } from "../../src/autodj/static/modules/shows-panel.js";
import { fmtTime } from "../../src/autodj/static/modules/dom-helpers.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => (byId("shows-form-dialog")?.open ? byId("shows-form-status") : byId("shows-status")).textContent;
const items = () => [...byId("shows-list").querySelectorAll("tbody tr[data-row-key]")];
const rowButton = (index, action) => items()[index].querySelector(`button[data-action="${action}"]`);
const songRows = () => [...byId("shows-songs").querySelectorAll("tbody tr[data-row-key]")];
const songButton = (index, action) => songRows()[index].querySelector(`button[data-action="${action}"]`);
const songTitles = () => songRows().map((row) => row.querySelector('[data-action="title"]').textContent);


const songOptions = (index) => songRows()[index].querySelector('td[data-action="options"]');
function clickAction(row, action) {
  const options = row.querySelector('button[aria-haspopup="menu"]');
  if (options.getAttribute("aria-expanded") !== "true") options.click();
  row.querySelector(`button[data-action="${action}"]`).click();
}
async function removeAt(index) {
  clickAction(songRows()[index], "remove");
  expect(byId("confirm-dialog").open).toBe(true);
  byId("confirm-accept").click();
  await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
}

const ID = "a".repeat(32);
it("shows a deferred feed reason in the ordinary on-air description", () => {
  expect(describeOnAir({ name: "Evening", played: 0, total: 4, waiting: 1, to_come: 3,
    deferred_reason: "Show feed deferred: Waiting take. Resolve the voice track conflict in the queue." }))
    .toContain("Show feed deferred: Waiting take.");
});
const song = (n, seconds = 180) => ({
  path: `/music/${n}.flac`, title: `Artist ${n}, Song ${n}`, missing: false, seconds,
  length_text: fmtTime(seconds),
});
const SHOW = {
  id: ID, name: "Remembering the 2000s", source: "category", category: "hits", clockwheel: "",
  source_text: "Category Remembering the 2000s", target_minutes: 60, count: 3, missing: 0,
  total_seconds: 540, total_text: "9:00", generated_at: null, note: "",
  events: ["Saturday throwback"],
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

// A stand-in for the show routes.
function fakeServer() {
  const server = {
    shows: [copy(SHOW)], songs: { [ID]: [song(1), song(2), song(3)] }, calls: [], refuse: null, onAir: null,
  };
  const view = (show) => {
    const songs = server.songs[show.id] || [];
    const total = songs.reduce((sum, item) => sum + item.seconds, 0);
    return { ...show, count: songs.length, total_seconds: total, total_text: fmtTime(total), songs: copy(songs) };
  };
  const listing = () => ({
    shows: server.shows.map((show) => { const { songs: _songs, ...rest } = view(show); return rest; }),
    categories: [{ id: "hits", name: "Remembering the 2000s" }, { id: "gold", name: "Gold" }],
    clockwheels: ["Throwback"], on_air: server.onAir, problem: null,
    limits: { max_shows: 200, max_songs: 500, min_minutes: 5, max_minutes: 360 }, timezone: "UTC",
  });
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.refuse && method !== "GET") throw server.refuse;
    if (url === "/api/shows" && method === "GET") return listing();
    if (url === "/api/shows" && method === "POST") {
      const show = { ...SHOW, ...body, id: "b".repeat(32), events: [] };
      server.shows.push(show);
      server.songs[show.id] = [];
      return view(show);
    }
    if (url === "/api/shows/on-air") { server.onAir = null; return { stopped: "Remembering the 2000s" }; }
    if (url.startsWith("/api/search")) return { results: [{ path: "/music/9.flac", title: "Song 9", artist: "Artist 9", length: 200 }] };
    const parts = url.split("/");
    const id = parts[3];
    const show = server.shows.find((item) => item.id === id);
    if (parts[4] === "songs") {
      server.songs[id] = body.songs.map((path) => [...Object.values(server.songs).flat(), { ...song(9, 200), path: "/music/9.flac", title: "Artist 9, Song 9" }].find((item) => item.path === path));
      return view(show);
    }
    if (parts[4] === "generate") {
      server.songs[id] = [song(4), song(5)];
      return { ...view(show), note: "", seed: 5 };
    }
    if (parts[4] === "start") return { message: "Show Remembering the 2000s started: 3 songs." };
    if (method === "GET") return view(show);
    if (method === "PUT") { Object.assign(show, body); return view(show); }
    if (method === "DELETE") { server.shows = server.shows.filter((item) => item.id !== id); return { deleted: id, name: show.name }; }
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

let controls = null;

async function open(server = fakeServer()) {
  controls = installShowsPanel({ document, request: server.request });
  await vi.waitFor(() => expect(items()).toHaveLength(server.shows.length));
  return server;
}

async function openList(server) {
  rowButton(0, "open").click();
  await vi.waitFor(() => expect(byId("shows-editor").hidden).toBe(false));
  await vi.waitFor(() => expect(songRows()).toHaveLength(server.songs[ID].length));
}

beforeEach(installIndex);
afterEach(() => { controls?.destroy(); controls = null; });

it("renders a shared empty row before the first shows load finishes", async () => {
  let finish;
  const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
  controls = installShowsPanel({ document, request });
  const table = byId("shows-list").querySelector("table.collection-table");
  const empty = table.querySelector("tbody td.no-results");
  expect(empty).not.toBeNull();
  expect(empty.colSpan).toBe(table.querySelectorAll("thead th").length);
  expect(empty.textContent).toBe("No shows yet. Press Add show to plan one.");
  expect(table.querySelectorAll('tbody [tabindex="0"]')).toHaveLength(1);
  expect(status()).toBe("");
  finish(await fakeServer().request("/api/shows"));
  await vi.waitFor(() => expect(items()).toHaveLength(1));
  expect(table.querySelector("td.no-results")).toBeNull();
});

// ADR-0021-10
it("uses shared show, song and search tables with one primary action", async () => {
  const server = fakeServer();
  controls = installShowsPanel({ document, request: server.request });
  await vi.waitFor(() => expect(byId("shows-list").textContent).toContain(SHOW.name));
  const table = byId("shows-list").querySelector("table.collection-table");
  expect(table).not.toBeNull();
  const row = table.querySelector("tbody tr");
  expect(row.querySelectorAll('button:not([role="menuitem"])')).toHaveLength(2);
  expect(row.querySelector('button[data-action="open"]').textContent).toBe("Open list");
  row.querySelector('button[aria-haspopup="menu"]').click();
  row.querySelector('[data-action="edit"]').click();
  expect(byId("shows-form").closest("dialog").open).toBe(true);
  byId("shows-cancel").click();
  await vi.waitFor(() => expect(byId("shows-form").closest("dialog").open).toBe(false));
  row.querySelector('button[data-action="open"]').click();
  await vi.waitFor(() => expect(byId("shows-editor").hidden).toBe(false));
  expect(byId("shows-songs").querySelector("table.collection-table")).not.toBeNull();
  const songRow = songRows()[0];
  expect(songRow.querySelectorAll('button:not([role="menuitem"])')).toHaveLength(2);
  expect(songRow.querySelector('button[data-action="info"]').textContent).toBe("Track info");
  expect([...songRow.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent))
    .toEqual(["Move up", "Move down", "Remove"]);
  byId("shows-search").value = "nine";
  byId("shows-search-run").click();
  await vi.waitFor(() => expect(byId("shows-results").querySelector('button[data-action="add"]')).not.toBeNull());
  expect(byId("shows-results").querySelector("table.collection-table")).not.toBeNull();
  expect(document.querySelectorAll('#shows-list [role="rowheader"], #shows-songs [role="rowheader"], #shows-results [role="rowheader"]')).toHaveLength(0);
});

describe("shows markup", () => {
  it("is its own card in Library tools, after Clockwheels, with labelled native controls", () => {
    const section = byId("shows-heading").closest("details");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    expect(section.previousElementSibling).toBe(byId("clockwheel-heading").closest("details"));
    expect(byId("shows-heading").tagName).toBe("H2");
    // Show and nested edition actions each own one polite status.
    const live = [...section.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')];
    expect(live.map((node) => node.id)).toEqual(["shows-status", "edition-status"]);
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
      expect(control.hasAttribute("aria-label"), control.id).toBe(false);
    }
    const fieldsets = [...section.querySelectorAll("fieldset")];
    expect(fieldsets.map((set) => set.querySelector(":scope > legend")?.textContent)).toEqual(["Add show", "Songs come from", "Record or import speech"]);
    expect(byId("shows-songs").tagName).toBe("DIV");
    // Collection hosts are neutral; their generated tables carry their names.
    expect(byId("shows-results").tagName).toBe("DIV");
    expect(byId("shows-start").getAttribute("type")).toBe("datetime-local");
    expect(byId("shows-start-help").textContent).toMatch(/^Optional\./);
    expect(byId("shows-form").hasAttribute("novalidate")).toBe(true);
    const ids = [...document.querySelectorAll('[id^="shows-"]')].map((node) => node.id);
    expect(ids.length).toBe(new Set(ids).size);
  });
});

describe("show list keyboard navigation", () => {
  it.each(["start", "stop"])("resolves %s through the shared voice choice", async (action) => {
    const server = await open();
    await openList(server);
    server.onAir = { name: "Current programme", total: 3, played: 0, waiting: 2, to_come: 1 };
    await controls.refresh();
    const original = server.request.getMockImplementation();
    const route = action === "start" ? `/api/shows/${ID}/start` : "/api/shows/on-air";
    const attempts = [];
    server.request.mockImplementation(async (url, options) => {
      if (url === route) {
        const body = options?.body ? JSON.parse(options.body) : {};
        attempts.push(body);
        if (!body.voice_action) throw Object.assign(new Error("Voice choice needed"), {
          status: 409, detail: { code: "voice_track_conflict", revision: "choice-1" },
        });
      }
      return original(url, options);
    });
    const button = byId(action === "start" ? "shows-play-now" : "shows-stop");
    button.focus(); button.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(document.activeElement).toBe(byId("confirm-cancel"));
    byId("confirm-accept").focus(); byId("confirm-accept").click();
    await vi.waitFor(() => expect(attempts).toHaveLength(2));
    expect(attempts[1]).toEqual({ voice_action: "keep", voice_revision: "choice-1" });
  });

  it("keeps one table tab stop and moves between data cells", async () => {
    await open();
    const cells = [...byId("shows-list").querySelectorAll("tbody td")];
    expect(cells.filter((item) => item.tabIndex === 0)).toHaveLength(1);
    expect([...byId("shows-list").querySelectorAll("button")].every((button) => button.tabIndex === -1)).toBe(true);
    cells[0].focus();
    cells[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(cells[1]);
  });
});

describe("shows use numeric durations", () => {
  it("says a show, its list and the show on the air", () => {
    expect(describeShow(SHOW)).toBe(
      "Remembering the 2000s: 3 songs, 9:00. From Category Remembering the 2000s, target 60 minutes. Played by Saturday throwback.",
    );
    expect(describeShow({ ...SHOW, count: 0, events: [], missing: 0 })).toContain("0 songs, no list yet. ");
    expect(summaryText({ ...SHOW, missing: 1 })).toBe(
      "3 songs, 9:00; target 60 minutes. 1 song is no longer in the library and will be skipped.",
    );
    expect(describeOnAir(null)).toBe("No show is on the air.");
    expect(describeOnAir({
      name: "Remembering the 2000s", total: 15, played: 3, waiting: 2, to_come: 10, skipped_count: 1,
      skipped: [{ title: "Artist, Song", reason: "it would break the DMCA limit: 5 songs by Artist" }],
    })).toBe(
      "Remembering the 2000s is on the air: 3 songs of 15 played, 2 waiting in the queue, 10 to come. "
      + "Skipped: Artist, Song (it would break the DMCA limit: 5 songs by Artist).",
    );
  });
});

describe("planning a show", () => {
  it("asks before discarding an edited show and returns directly to its Options cell", async () => {
    await open();
    clickAction(items()[0], "edit");
    byId("shows-name").value = "Changed show";
    byId("shows-cancel").click();
    expect(byId("confirm-dialog").open).toBe(true);
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(byId("shows-form-dialog").open).toBe(true);
    expect(byId("shows-name").value).toBe("Changed show");
    byId("shows-cancel").click();
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("shows-form-dialog").open).toBe(false));
    expect(document.activeElement.closest("td")).toBe(items()[0].querySelector('td[data-action="options"]'));
    expect(items()[0].textContent).toContain(SHOW.name);
  });

  it("confirms deleting a show and returns to Add show when none remain", async () => {
    const server = await open();
    clickAction(items()[0], "delete");
    expect(byId("confirm-dialog").open).toBe(true);
    expect(server.calls.some((call) => call.method === "DELETE")).toBe(false);
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(items()).toHaveLength(0));
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("shows-add")));
    expect(byId("shows-list").querySelector("td.no-results").colSpan).toBe(10);
    await vi.waitFor(() => expect(status()).toContain(`Deleted ${SHOW.name}.`));
  });

  it("keeps one save pending and ignores a duplicate submit", async () => {
    const server = fakeServer();
    const original = server.request;
    let finish;
    server.request = vi.fn((url, options = {}) => options.method === "POST"
      ? new Promise((resolve) => { finish = resolve; }) : original(url, options));
    await open(server);
    byId("shows-add").click();
    byId("shows-name").value = "Throwback hour";
    const form = byId("shows-form");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(server.request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    expect(form.getAttribute("aria-busy")).toBe("true");
    finish({ ...SHOW, name: "Throwback hour", id: "b".repeat(32), events: [] });
    await vi.waitFor(() => expect(form.hidden).toBe(true));
  });

  it("keeps a pending editor open until the save completes", async () => {
    const server = fakeServer();
    const original = server.request;
    let finish;
    server.request = vi.fn((url, options = {}) => url === "/api/shows" && options.method === "POST"
      ? new Promise((resolve) => { finish = resolve; }) : original(url, options));
    await open(server);
    byId("shows-add").click();
    byId("shows-name").value = "First draft";
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    byId("shows-cancel").click();
    expect(byId("shows-form-dialog").open).toBe(true);
    expect(byId("confirm-dialog").open).toBe(false);
    expect(byId("shows-name").value).toBe("First draft");
    finish({ ...SHOW, name: "First draft", id: "b".repeat(32), events: [] });
    await vi.waitFor(() => expect(byId("shows-form-dialog").open).toBe(false));
    expect(status()).toContain("Saved First draft.");
  });

  it("adds a show, refusing an empty name with the error linked and focused", async () => {
    const server = await open();
    byId("shows-add").click();
    expect(document.activeElement).toBe(byId("shows-name"));
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(byId("shows-name").getAttribute("aria-invalid")).toBe("true");
    expect(byId("shows-name").getAttribute("aria-describedby")).toContain("shows-form-error");
    expect(byId("shows-form-error").textContent).toBe("Enter a name for the show.");
    expect(document.activeElement).toBe(byId("shows-name"));
    await vi.waitFor(() => expect(byId("shows-form").hasAttribute("aria-busy")).toBe(false));
    byId("shows-name").value = "Throwback hour";
    byId("shows-name").dispatchEvent(new Event("input"));
    expect(byId("shows-name").hasAttribute("aria-invalid")).toBe(false);
    byId("shows-target").value = "2";
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(document.activeElement).toBe(byId("shows-target"));
    expect(byId("shows-form-error").textContent).toBe("Target length must be a whole number of minutes from 5 to 360.");
    await vi.waitFor(() => expect(byId("shows-form").hasAttribute("aria-busy")).toBe(false));
    byId("shows-target").value = "45";
    byId("shows-source-clockwheel").checked = true;
    byId("shows-source-clockwheel").dispatchEvent(new Event("change"));
    expect(byId("shows-clockwheel-field").hidden).toBe(false);
    expect(byId("shows-category-field").hidden).toBe(true);
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(items()).toHaveLength(2));
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Throwback hour", source: "clockwheel", category: "", clockwheel: "Throwback", target_minutes: 45,
      station_name: "",
    });
    expect(byId("shows-form").hidden).toBe(true);
    await vi.waitFor(() => expect(document.activeElement).toBe(rowButton(1, "open").closest("td")));
    await vi.waitFor(() => expect(status()).toContain("Saved Throwback hour."));
  });

  // ADR-0009-08
  it("saves a show's station name while active and puts a refusal on the field", async () => {
    const server = await open();
    const field = byId("shows-station-name");
    expect(document.querySelector('label[for="shows-station-name"]').textContent).toBe("Station name while active");
    expect(field.maxLength).toBe(100);
    expect(field.getAttribute("aria-describedby")).toBe("shows-station-name-help");
    clickAction(items()[0], "edit");
    expect(field.value).toBe("");
    field.value = "Live\tnow";
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby").split(" ")).toEqual(["shows-form-error", "shows-station-name-help"]);
    expect(byId("shows-form-error").textContent).toBe("Station name while active cannot contain tabs, line breaks or other control characters.");
    expect(document.activeElement).toBe(field);
    expect(server.calls.some((call) => call.method === "PUT")).toBe(false);
    await vi.waitFor(() => expect(byId("shows-form").hasAttribute("aria-busy")).toBe(false));
    field.value = " Example FM Live ";
    field.dispatchEvent(new Event("input"));
    expect(field.hasAttribute("aria-invalid")).toBe(false);
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status()).toContain("Saved Remembering the 2000s."));
    expect(server.calls.find((call) => call.method === "PUT").body.station_name).toBe("Example FM Live");
    expect(items()[0].querySelector('[data-action="station"]').textContent).toBe("Example FM Live");
    // The server names the field it refuses; the page marks and focuses it.
    clickAction(items()[0], "edit");
    expect(field.value).toBe("Example FM Live");
    server.refuse = new FieldError("Station name while active can be at most 100 characters", "station_name");
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(field.getAttribute("aria-invalid")).toBe("true"));
    expect(byId("shows-form-error").textContent).toBe("Station name while active can be at most 100 characters");
    expect(document.activeElement).toBe(field);
  });

  it("keeps focus on Save show until the saved show's Open list button takes it", async () => {
    // Chrome drops focus to the page when the focused button is hidden, and
    // NVDA then reads the whole card; the list reload must not run with the
    // form, and so Save show, hidden.
    const server = fakeServer();
    const reloads = [];
    const request = server.request;
    server.request = vi.fn(async (url, options = {}) => {
      if (url === "/api/shows" && !options.method && server.calls.some((call) => call.method === "POST")) {
        reloads.push({ focused: document.activeElement, formHidden: byId("shows-form").hidden });
      }
      return request(url, options);
    });
    await open(server);
    byId("shows-add").click();
    byId("shows-name").value = "Throwback hour";
    byId("shows-save").focus();
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status()).toContain("Saved Throwback hour."));
    expect(reloads).toEqual([{ focused: byId("shows-save"), formHidden: false }]);
    expect(byId("shows-form").hidden).toBe(true);
    expect(document.activeElement).toBe(rowButton(1, "open").closest("td"));
  });

  it("puts a field the server refuses in words, linked and focused", async () => {
    const server = await open();
    server.refuse = new FieldError("That category no longer exists; choose another", "category");
    clickAction(items()[0], "edit");
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(byId("shows-category").getAttribute("aria-invalid")).toBe("true"));
    expect(document.activeElement).toBe(byId("shows-category"));
    expect(byId("shows-category").getAttribute("aria-describedby")).toBe("shows-form-error");
    expect(byId("shows-category-help").hidden).toBe(false);
  });
});

describe("editing a list", () => {
  it("confirms removal and preserves the song and menu focus when cancelled", async () => {
    const server = await open();
    await openList(server);
    clickAction(songRows()[1], "remove");
    expect(byId("confirm-dialog").open).toBe(true);
    expect(songRows()).toHaveLength(3);
    expect(server.calls.filter((call) => call.url.endsWith("/songs"))).toHaveLength(0);
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(songRows()).toHaveLength(3);
    expect(document.activeElement).toBe(songOptions(1));
  });

  it("reorders with Alt arrows and opens track info with the correct return control", async () => {
    const server = await open();
    await openList(server);
    const original = songRows()[1];
    const title = original.querySelector('[data-action="title"]');
    title.focus();
    title.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", altKey: true, bubbles: true }));
    expect(songRows()[0]).toBe(original);
    expect(document.activeElement).toBe(songOptions(0));
    const events = [];
    const listener = (event) => events.push(event.detail);
    document.addEventListener("autodj:edit-points", listener);
    const info = original.querySelector('button[data-action="info"]');
    info.click();
    document.removeEventListener("autodj:edit-points", listener);
    expect(events).toEqual([{ path: "/music/2.flac", kind: "music", opener: info }]);
    await vi.waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/songs"))).toBe(true));
  });

  it("keeps unavailable track info reachable and explains its reason", async () => {
    const server = fakeServer();
    server.songs[ID][0].missing = true;
    await open(server);
    await openList(server);
    const info = songRows()[0].querySelector('button[data-action="info"]');
    expect(info.getAttribute("aria-disabled")).toBe("true");
    expect(info.disabled).toBe(false);
    expect(byId(info.getAttribute("aria-describedby")).textContent).toBe("This song is no longer in the library.");
    info.click();
    await vi.waitFor(() => expect(status()).toBe("This song is no longer in the library."));
  });

  it("shows numeric durations without hidden spoken alternatives, including hours", async () => {
    const server = fakeServer();
    server.songs[ID][0] = { ...song(1, 3725), length_text: "1:02:05" };
    await open(server);
    await openList(server);
    expect(songRows()[0].querySelector('[data-action="length"]').textContent).toBe("1:02:05");
    expect(songRows()[0].querySelector(".visually-hidden")).toBeNull();
    expect(byId("shows-summary").textContent).toContain("1:08:05");
  });

  it("moves songs by keyboard buttons, keeping focus on the moved song", async () => {
    const server = await open();
    await openList(server);
    expect(document.activeElement).toBe(byId("shows-editor-heading"));
    expect(songButton(0, "up").getAttribute("aria-disabled")).toBe("true");
    expect(songButton(2, "down").getAttribute("aria-disabled")).toBe("true");
    // The menu is named for its song; each choice has a concise label.
    expect(songButton(1, "up").getAttribute("aria-label")).toBe("Move up");
    clickAction(songRows()[1], "up");
    expect(songTitles()).toEqual(["Artist 2, Song 2", "Artist 1, Song 1", "Artist 3, Song 3"]);
    expect(document.activeElement).toBe(songOptions(0));
    await vi.waitFor(() => expect(status()).toBe("Moved Artist 2, Song 2 to number 1 of 3. 3 songs, 9:00; target 60 minutes."));
    expect(server.calls.filter((call) => call.method === "PUT").at(-1).body.songs)
      .toEqual(["/music/2.flac", "/music/1.flac", "/music/3.flac"]);
    // At the top, Move up stays focusable and says why it does nothing.
    clickAction(songRows()[0], "up");
    await vi.waitFor(() => expect(status()).toBe("Already the first song."));
    clickAction(songRows()[0], "down");
    expect(document.activeElement).toBe(songOptions(1));
  });

  it("removes a song, moving focus to the next one, and adds one from a search", async () => {
    const server = await open();
    await openList(server);
    await removeAt(1);
    expect(songTitles()).toEqual(["Artist 1, Song 1", "Artist 3, Song 3"]);
    expect(document.activeElement).toBe(songOptions(1));
    await vi.waitFor(() => expect(status()).toContain("Removed Artist 2, Song 2."));
    byId("shows-search").value = "nine";
    byId("shows-search-run").click();
    await vi.waitFor(() => expect(byId("shows-results").querySelectorAll('button[data-action="add"]')).toHaveLength(1));
    expect(status()).toBe("1 song found.");
    const add = byId("shows-results").querySelector("button");
    expect(add.getAttribute("aria-label")).toBe("Add Artist 9, Song 9");
    add.focus();
    add.click();
    expect(songTitles().at(-1)).toBe("Artist 9, Song 9");
    expect(document.activeElement).toBe(add.closest("td"));
    await vi.waitFor(() => expect(status()).toContain("Added Artist 9, Song 9 as number 3."));
    // Removing every song leaves focus in the search box.
    for (let i = 0; i < 3; i += 1) await removeAt(0);
    expect(document.activeElement).toBe(byId("shows-search"));
  });

  // #328: the list drawn again from scratch destroyed the focused button, and
  // NVDA read a cut-off line of the list after the status message.
  it("changes the song list in place, never leaving focus on the page", async () => {
    const server = await open();
    await openList(server);
    const [first, second, third] = songRows();
    const left = [];
    document.addEventListener("focusout", (event) => {
      if (!event.relatedTarget) left.push(event.target);
    });
    songOptions(1).focus();
    clickAction(songRows()[1], "down");
    // The same rows, moved around the focused one, renumbered in place.
    expect(songRows()).toEqual([first, third, second]);
    expect(document.activeElement).toBe(second.querySelector('td[data-action="options"]'));
    expect(second.querySelector('[data-action="down"]').getAttribute("aria-disabled")).toBe("true");
    expect(third.querySelector('[data-action="down"]').getAttribute("aria-disabled")).toBe("false");
    await vi.waitFor(() => expect(status()).toContain("Moved Artist 2, Song 2 to number 3 of 3."));
    // Confirmed removal returns directly to the next row's Options cell.
    await removeAt(1);
    expect(songRows()).toEqual([first, second]);
    expect(document.activeElement).toBe(second.querySelector('td[data-action="options"]'));
    expect(second.querySelector('[data-action="up"]').getAttribute("aria-label")).toBe("Move up");
    await vi.waitFor(() => expect(status()).toContain("Removed Artist 3, Song 3."));
    expect(left).toEqual([]);
  });

  it("keeps each copy of a song added twice as its own row (#328)", async () => {
    const server = fakeServer();
    server.songs[ID] = [song(1), song(2), song(1)];
    await open(server);
    await openList(server);
    const [first, middle, copyRow] = songRows();
    expect(songTitles()).toEqual(["Artist 1, Song 1", "Artist 2, Song 2", "Artist 1, Song 1"]);
    expect(first.textContent).toContain("also number 3");
    expect(copyRow.textContent).toContain("also number 1");
    // Move the first copy down: the same rows, the warnings renumbered.
    first.querySelector('td[data-action="options"]').focus();
    clickAction(first, "down");
    expect(songRows()).toEqual([middle, first, copyRow]);
    expect(document.activeElement).toBe(first.querySelector('td[data-action="options"]'));
    expect(first.textContent).toContain("also number 3");
    expect(copyRow.textContent).toContain("also number 2");
    await vi.waitFor(() => expect(status()).toContain("Moved Artist 1, Song 1 to number 2 of 3."));
    // Remove the second copy: the first keeps its row and loses its warning.
    await removeAt(2);
    expect(songRows()).toEqual([middle, first]);
    expect(document.activeElement).toBe(first.querySelector('td[data-action="options"]'));
    expect(first.textContent).not.toContain("also number");
    await vi.waitFor(() => expect(server.calls.filter((call) => call.method === "PUT")).toHaveLength(2));
    expect(server.calls.filter((call) => call.method === "PUT").at(-1).body.songs)
      .toEqual(["/music/2.flac", "/music/1.flac"]);
  });

  it("generates the list again, saying the result once", async () => {
    const server = await open();
    await openList(server);
    byId("shows-start").value = "2026-12-24T20:00";
    byId("shows-generate").click();
    await vi.waitFor(() => expect(songRows()).toHaveLength(2));
    expect(server.calls.find((call) => call.url.endsWith("/generate")).body).toEqual({ start: "2026-12-24T20:00" });
    await vi.waitFor(() => expect(status()).toBe("Generated 2 songs, 6:00; target 60 minutes."));
    expect(byId("shows-generate").hasAttribute("aria-disabled")).toBe(false);
  });

  it("starts a show and stops it, keeping focus on the page", async () => {
    const server = await open();
    await openList(server);
    byId("shows-play-now").click();
    await vi.waitFor(() => expect(status()).toBe("Show Remembering the 2000s started: 3 songs."));
    server.onAir = { name: "Remembering the 2000s", total: 3, played: 0, waiting: 2, to_come: 1, skipped: [], skipped_count: 0 };
    await controls.refresh();
    expect(byId("shows-stop").hidden).toBe(false);
    expect(byId("shows-on-air").textContent).toContain("is on the air");
    byId("shows-stop").focus();
    byId("shows-stop").click();
    await vi.waitFor(() => expect(byId("shows-stop").hidden).toBe(true));
    expect(document.activeElement).toBe(byId("shows-add"));
    expect(status()).toContain("Stopped Remembering the 2000s.");
  });

  it("closes the list back to the show's Open list button", async () => {
    const server = await open();
    await openList(server);
    byId("shows-close").click();
    await vi.waitFor(() => expect(byId("shows-editor").hidden).toBe(true));
    expect(document.activeElement).toBe(rowButton(0, "open").closest("td"));
  });
});

describe("review fixes", () => {
  it("keeps a refreshed show's menu and focus while updating its edit action", async () => {
    const server = await open();
    const row = items()[0];
    row.querySelector('button[aria-haspopup="menu"]').click();
    const edit = row.querySelector('button[data-action="edit"]');
    edit.focus();
    server.shows[0].name = "Updated show";
    await controls.refresh();
    expect(items()[0]).toBe(row);
    expect(document.activeElement).toBe(edit);
    expect(row.querySelector('button[aria-haspopup="menu"]').getAttribute("aria-expanded")).toBe("true");
    edit.click();
    expect(byId("shows-name").value).toBe("Updated show");
  });

  it("discards protected editor state and ignores an in-flight save after sign-out", async () => {
    const server = fakeServer();
    const original = server.request;
    let finish;
    server.request = vi.fn((url, options = {}) => options.method === "PUT"
      ? new Promise((resolve) => { finish = resolve; }) : original(url, options));
    await open(server);
    clickAction(items()[0], "edit");
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    controls.clearProtectedData();
    expect(byId("shows-form-dialog").open).toBe(false);
    expect(items()).toHaveLength(0);
    finish({ ...SHOW, name: "Stale save" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(items()).toHaveLength(0);
    expect(byId("shows-name").value).toBe("");
    expect(status()).toBe("");
  });

  it("marks list edits unavailable while the list is generated, and says why", async () => {
    const server = await open();
    await openList(server);
    let finish;
    const original = server.request;
    server.request = vi.fn((url, options = {}) => (url.endsWith("/generate")
      ? new Promise((resolve) => { finish = () => resolve(original(url, options)); })
      : original(url, options)));
    controls.destroy();
    installIndex();
    controls = installShowsPanel({ document, request: server.request });
    await vi.waitFor(() => expect(items()).toHaveLength(1));
    await openList(server);
    byId("shows-generate").click();
    await vi.waitFor(() => expect(songButton(1, "remove").getAttribute("aria-disabled")).toBe("true"));
    expect(songButton(1, "up").getAttribute("aria-disabled")).toBe("true");
    // The pressed Generate looks busy but is not marked unavailable: NVDA
    // spoke that on the focused button (#331).
    expect(byId("shows-generate").hasAttribute("aria-disabled")).toBe(false);
    expect(byId("shows-generate").classList.contains("is-pending")).toBe(true);
    expect(byId("shows-generate").getAttribute("aria-busy")).toBe("true");
    clickAction(songRows()[1], "remove");
    await vi.waitFor(() => expect(status()).toBe("The list is being generated; change it when that is done."));
    expect(songRows()).toHaveLength(3);
    expect(server.calls.filter((call) => call.url.endsWith("/songs"))).toHaveLength(0);
    finish();
    await vi.waitFor(() => expect(songRows()).toHaveLength(2));
    expect(songButton(0, "remove").getAttribute("aria-disabled")).toBe("false");
    expect(byId("shows-generate").classList.contains("is-pending")).toBe(false);
    expect(byId("shows-generate").hasAttribute("aria-busy")).toBe(false);
  });

  it("says a failed refresh once, not on every retry", async () => {
    const server = await open();
    const original = server.request;
    server.request.mockImplementation(async (url, options) => {
      if (url === "/api/shows") throw new Error("The AutoDJ server is not reachable.");
      return original(url, options);
    });
    byId("shows-status").textContent = "";
    await controls.refresh();
    await vi.waitFor(() => expect(status()).toBe("Could not load shows: The AutoDJ server is not reachable."));
    byId("shows-status").textContent = "";
    await controls.refresh();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(status()).toBe("");
  });

  it("says an error that names no field without marking one", async () => {
    const server = await open();
    server.refuse = new FieldError("The shows file could not be saved", null);
    clickAction(items()[0], "edit");
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status()).toBe("The shows file could not be saved"));
    expect(byId("shows-name").hasAttribute("aria-invalid")).toBe(false);
    expect(byId("shows-form-error").hidden).toBe(true);
  });

  it("warns about a song listed twice and focuses the previous song after removing the last", async () => {
    const server = await open();
    server.songs[ID] = [song(1), song(2), song(1)];
    await openList(server);
    expect(songRows()[0].querySelector('[data-action="repeated"]').textContent).toContain("Warning: also number 3 in the list");
    expect(byId("shows-summary").textContent).toContain("Warning: 1 song is in the list more than once");
    await removeAt(2);
    expect(document.activeElement).toBe(songOptions(1));
    expect(byId("shows-summary").textContent).not.toContain("Warning");
  });
});

describe("a show waiting to resume or stopping", () => {
  it("says a pending resume and a show still being taken out of the queue", () => {
    expect(describeOnAir({ name: "Remembering the 2000s", pending_resume: true, resume_at: 4, total: 15 }))
      .toBe("Remembering the 2000s was on the air before AutoDJ restarted and will resume at the next song, "
        + "from song 4 of 15. Stop show cancels it.");
    expect(describeOnAir({ name: "Remembering the 2000s", stopping: true, waiting: 2 }))
      .toBe("Remembering the 2000s is stopping: 2 songs are still in the queue, and AutoDJ takes them out at the next song.");
  });

  it("cancels a pending resume with Stop show and says so once", async () => {
    const server = await open();
    server.onAir = { name: "Remembering the 2000s", pending_resume: true, resume_at: 2, total: 3 };
    await controls.refresh();
    expect(byId("shows-stop").hidden).toBe(false);
    byId("shows-stop").focus();
    byId("shows-stop").click();
    await vi.waitFor(() => expect(status()).toBe("Remembering the 2000s will not resume."));
    expect(byId("shows-stop").hidden).toBe(true);
    expect(document.activeElement).toBe(byId("shows-add"));
  });
});
