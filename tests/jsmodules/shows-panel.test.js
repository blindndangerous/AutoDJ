import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeOnAir, describeShow, installShowsPanel, summaryText } from "../../src/autodj/static/modules/shows-panel.js";
import { fmtTime } from "../../src/autodj/static/modules/dom-helpers.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => byId("shows-status").textContent;
const items = () => [...byId("shows-list").querySelectorAll("li[data-id]")];
const rowButton = (index, action) => items()[index].querySelector(`button[data-action="${action}"]`);
const songRows = () => [...byId("shows-songs").querySelectorAll("li[data-index]")];
const songButton = (index, action) => songRows()[index].querySelector(`button[data-action="${action}"]`);
const songTitles = () => songRows().map((row) => row.querySelector(".show-song").textContent.split(",").slice(0, 2).join(","));

const ID = "a".repeat(32);
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
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
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

describe("shows markup", () => {
  it("is its own card in Library tools, after Clockwheels, with labelled native controls", () => {
    const section = byId("shows-heading").closest("details");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    expect(section.previousElementSibling).toBe(byId("clockwheel-heading").closest("details"));
    expect(byId("shows-heading").tagName).toBe("H2");
    // One polite status; the lists and the on-air line are not live.
    const live = [...section.querySelectorAll('[role="status"], [aria-live]:not([aria-live="off"])')];
    expect(live.map((node) => node.id)).toEqual(["shows-status"]);
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
      expect(control.hasAttribute("aria-label"), control.id).toBe(false);
    }
    const fieldsets = [...section.querySelectorAll("fieldset")];
    expect(fieldsets.map((set) => set.querySelector(":scope > legend")?.textContent)).toEqual(["Add show", "Songs come from"]);
    expect(byId("shows-songs").tagName).toBe("OL");
    expect(byId("shows-songs").getAttribute("aria-labelledby")).toBe("shows-editor-heading");
    expect(byId("shows-results").getAttribute("aria-labelledby")).toBe("shows-add-heading");
    expect(byId("shows-start").getAttribute("type")).toBe("datetime-local");
    expect(byId("shows-start-help").textContent).toMatch(/^Optional\./);
    expect(byId("shows-form").hasAttribute("novalidate")).toBe(true);
    const ids = [...document.querySelectorAll('[id^="shows-"]')].map((node) => node.id);
    expect(ids.length).toBe(new Set(ids).size);
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
  it("adds a show, refusing an empty name with the error linked and focused", async () => {
    const server = await open();
    byId("shows-add").click();
    expect(document.activeElement).toBe(byId("shows-name"));
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(byId("shows-name").getAttribute("aria-invalid")).toBe("true");
    expect(byId("shows-name").getAttribute("aria-describedby")).toContain("shows-form-error");
    expect(byId("shows-form-error").textContent).toBe("Enter a name for the show.");
    expect(document.activeElement).toBe(byId("shows-name"));
    byId("shows-name").value = "Throwback hour";
    byId("shows-name").dispatchEvent(new Event("input"));
    expect(byId("shows-name").hasAttribute("aria-invalid")).toBe(false);
    byId("shows-target").value = "2";
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(document.activeElement).toBe(byId("shows-target"));
    expect(byId("shows-form-error").textContent).toBe("Target length must be a whole number of minutes from 5 to 360.");
    byId("shows-target").value = "45";
    byId("shows-source-clockwheel").checked = true;
    byId("shows-source-clockwheel").dispatchEvent(new Event("change"));
    expect(byId("shows-clockwheel-field").hidden).toBe(false);
    expect(byId("shows-category-field").hidden).toBe(true);
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(items()).toHaveLength(2));
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Throwback hour", source: "clockwheel", category: "", clockwheel: "Throwback", target_minutes: 45,
    });
    expect(byId("shows-form").hidden).toBe(true);
    await vi.waitFor(() => expect(document.activeElement).toBe(rowButton(1, "open")));
    await vi.waitFor(() => expect(status()).toContain("Saved Throwback hour."));
  });

  it("puts a field the server refuses in words, linked and focused", async () => {
    const server = await open();
    server.refuse = new FieldError("That category no longer exists; choose another", "category");
    rowButton(0, "edit").click();
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(byId("shows-category").getAttribute("aria-invalid")).toBe("true"));
    expect(document.activeElement).toBe(byId("shows-category"));
    expect(byId("shows-category").getAttribute("aria-describedby")).toBe("shows-form-error");
    expect(byId("shows-category-help").hidden).toBe(false);
  });
});

describe("editing a list", () => {
  it("shows numeric durations without hidden spoken alternatives, including hours", async () => {
    const server = fakeServer();
    server.songs[ID][0] = { ...song(1, 3725), length_text: "1:02:05" };
    await open(server);
    await openList(server);
    expect(songRows()[0].querySelector(".show-song").textContent).toBe("Artist 1, Song 1, 1:02:05");
    expect(songRows()[0].querySelector(".show-song .visually-hidden")).toBeNull();
    expect(byId("shows-summary").textContent).toContain("1:08:05");
  });

  it("moves songs by keyboard buttons, keeping focus on the moved song", async () => {
    const server = await open();
    await openList(server);
    expect(document.activeElement).toBe(byId("shows-editor-heading"));
    expect(songButton(0, "up").getAttribute("aria-disabled")).toBe("true");
    expect(songButton(2, "down").getAttribute("aria-disabled")).toBe("true");
    // The visible word starts the button's name; the song says which.
    expect(songButton(1, "up").getAttribute("aria-label")).toBe("Move up Artist 2, Song 2");
    songButton(1, "up").click();
    expect(songTitles()).toEqual(["Artist 2, Song 2", "Artist 1, Song 1", "Artist 3, Song 3"]);
    expect(document.activeElement).toBe(songButton(0, "up"));
    await vi.waitFor(() => expect(status()).toBe("Moved Artist 2, Song 2 to number 1 of 3. 3 songs, 9:00; target 60 minutes."));
    expect(server.calls.filter((call) => call.method === "PUT").at(-1).body.songs)
      .toEqual(["/music/2.flac", "/music/1.flac", "/music/3.flac"]);
    // At the top, Move up stays focusable and says why it does nothing.
    songButton(0, "up").click();
    await vi.waitFor(() => expect(status()).toBe("Already the first song."));
    songButton(0, "down").click();
    expect(document.activeElement).toBe(songButton(1, "down"));
  });

  it("removes a song, moving focus to the next one, and adds one from a search", async () => {
    const server = await open();
    await openList(server);
    songButton(1, "remove").click();
    expect(songTitles()).toEqual(["Artist 1, Song 1", "Artist 3, Song 3"]);
    expect(document.activeElement).toBe(songButton(1, "remove"));
    await vi.waitFor(() => expect(status()).toContain("Removed Artist 2, Song 2."));
    byId("shows-search").value = "nine";
    byId("shows-search-run").click();
    await vi.waitFor(() => expect(byId("shows-results").querySelectorAll("button")).toHaveLength(1));
    expect(status()).toBe("1 song found.");
    const add = byId("shows-results").querySelector("button");
    expect(add.getAttribute("aria-label")).toBe("Add Artist 9, Song 9");
    add.focus();
    add.click();
    expect(songTitles().at(-1)).toBe("Artist 9, Song 9");
    expect(document.activeElement).toBe(add);
    await vi.waitFor(() => expect(status()).toContain("Added Artist 9, Song 9 as number 3."));
    // Removing every song leaves focus in the search box.
    for (let i = 0; i < 3; i += 1) songButton(0, "remove").click();
    expect(document.activeElement).toBe(byId("shows-search"));
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
    expect(byId("shows-editor").hidden).toBe(true);
    expect(document.activeElement).toBe(rowButton(0, "open"));
  });
});

describe("review fixes", () => {
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
    songButton(1, "remove").click();
    await vi.waitFor(() => expect(status()).toBe("The list is being generated; change it when that is done."));
    expect(songRows()).toHaveLength(3);
    expect(server.calls.filter((call) => call.url.endsWith("/songs"))).toHaveLength(0);
    finish();
    await vi.waitFor(() => expect(songRows()).toHaveLength(2));
    expect(songButton(0, "remove").hasAttribute("aria-disabled")).toBe(false);
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
    rowButton(0, "edit").click();
    byId("shows-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status()).toBe("The shows file could not be saved"));
    expect(byId("shows-name").hasAttribute("aria-invalid")).toBe(false);
    expect(byId("shows-form-error").hidden).toBe(true);
  });

  it("warns about a song listed twice and focuses the previous song after removing the last", async () => {
    const server = await open();
    server.songs[ID] = [song(1), song(2), song(1)];
    await openList(server);
    expect(songRows()[0].querySelector(".show-song").textContent).toContain("warning: also number 3 in the list");
    expect(byId("shows-summary").textContent).toContain("Warning: 1 song is in the list more than once");
    songButton(2, "remove").click();
    expect(document.activeElement).toBe(songButton(1, "remove"));
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
