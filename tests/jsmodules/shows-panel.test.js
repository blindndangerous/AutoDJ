import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { installShowsPanel, matchText, showLine, untilText } from "../../src/autodj/static/modules/shows-panel.js";
import { initSubtabs } from "../../src/autodj/static/modules/subtabs.js";

const byId = (id) => document.getElementById(id);
const status = () => byId("shows-status").textContent;
const NOW = Date.parse("2026-10-11T20:00:00Z");
const LINER = "a".repeat(32);

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  byId("settings-tabs").closest("section").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

class FakeError extends Error {
  constructor(message, { status = 400, field = null } = {}) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

const REPORT = {
  count: 1284, count_text: "1,284 songs match", fallback: 96,
  fallback_text: "96 use the release year because the first-release year is missing",
  fallback_examples: [], warning: null, errors: [], beets: true,
};

function show(name, changes = {}) {
  return {
    id: `id-${name.replace(/\W/g, "")}`, name, year_from: 2000, year_to: 2009, genres_include: [],
    genres_exclude: [], query: "", clockwheel: null, intro: null, outro: null, liner_folder: null,
    start: "soft", end: "soft", default_minutes: 60,
    summary: "First released 2000 to 2009. Soft start, soft end. 60 minutes when started now.",
    ...changes,
  };
}

// A stand-in for the show routes.
function fakeServer() {
  const server = { shows: [], active: null, calls: [], fail: null, report: REPORT };
  const activeOf = (item) => ({
    id: item.id, name: item.name, started_at: "2026-10-11T20:00:00+00:00",
    ends_at: "2026-10-11T21:00:00+00:00", count: 1284, fallback: 96,
  });
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.fail) throw server.fail;
    if (method === "GET" && url === "/api/shows") {
      return {
        timezone: "UTC", shows: server.shows, active: server.active,
        options: {
          genres: ["Pop", "Rock"], liners: [{ id: LINER, name: "intro.mp3" }],
          liner_folders: [{ id: "src", path: "C:\\liners" }], clockwheels: ["Decades"],
        },
      };
    }
    if (url === "/api/shows/check") return server.report;
    if (url === "/api/shows/stop") {
      const ended = server.active;
      server.active = null;
      return { stopped: ended.id, name: ended.name };
    }
    const id = decodeURIComponent(url.split("/")[3] || "");
    const index = server.shows.findIndex((item) => item.id === id);
    if (url.endsWith("/start")) {
      server.active = activeOf(server.shows[index]);
      return server.active;
    }
    if (method === "POST") {
      const saved = show(body.name, body);
      server.shows.push(saved);
      return { show: saved, check: server.report };
    }
    if (method === "PUT") {
      server.shows[index] = show(body.name, { ...body, id });
      return { show: server.shows[index], check: server.report };
    }
    if (method === "DELETE") return { deleted: server.shows.splice(index, 1)[0].id };
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

async function openPanel(server) {
  const controls = installShowsPanel({ doc: document, request: server.request, now: () => NOW });
  initSubtabs(byId("settings-tabs"), { storage: null });
  byId("stab-shows").click();
  await vi.waitFor(() => expect(server.request).toHaveBeenCalled());
  await vi.waitFor(() => expect(byId("shows-list").children.length).toBeGreaterThan(0));
  return controls;
}

const row = (name) => [...byId("shows-list").children]
  .find((item) => item.querySelector("span")?.textContent.startsWith(name));
const rowButton = (name, role) => row(name)?.querySelector(`[data-role="${role}"]`);

function submit() {
  byId("shows-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

beforeEach(() => {
  installIndex();
});

describe("Shows panel words", () => {
  it("says the end time and the show as the status page does", () => {
    expect(untilText("2026-10-11T21:00:00-06:00")).toBe("21:00");
    expect(untilText("soon")).toBe("");
    expect(showLine({ name: "Remembering the 2000s", ends_at: "2026-10-11T21:00:00+00:00" }))
      .toBe("On air: Remembering the 2000s, until 21:00.");
    expect(showLine({ name: "  " })).toBe("");
    expect(showLine(null)).toBe("");
    expect(matchText(REPORT)).toBe(
      "1,284 songs match. 96 use the release year because the first-release year is missing.",
    );
    expect(matchText({ ...REPORT, fallback_text: "", warning: "about 30 songs needed; only 22 fit your artist rule" }))
      .toBe("1,284 songs match. Warning: about 30 songs needed; only 22 fit your artist rule.");
    expect(matchText(null)).toBe("");
  });
});

describe("Shows panel", () => {
  it("is a Settings subtab next to Schedule, labelled by its tab, with no aria-controls", () => {
    const tab = byId("stab-shows");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(tab.previousElementSibling.id).toBe("stab-schedule");
    expect(byId("spanel-shows").getAttribute("aria-labelledby")).toBe("stab-shows");
    expect(byId("spanel-shows").querySelector(":scope > h3").textContent).toBe("Shows");
  });

  it("labels every control and groups the form with legends", () => {
    const form = byId("shows-form");
    for (const control of form.querySelectorAll("input, select")) {
      expect(form.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
    }
    expect([...form.querySelectorAll("legend")].map((legend) => legend.textContent)).toEqual([
      "Add show", "Songs", "Genres to include", "Genres to leave out", "Music, liners and length",
    ]);
    for (const id of ["shows-query", "shows-year-from", "shows-clockwheel", "shows-minutes"]) {
      const described = byId(id).getAttribute("aria-describedby");
      expect(described && byId(described), id).toBeTruthy();
    }
    // The match count is plain text; Check and Save say it once.
    expect(byId("shows-match").getAttribute("aria-live")).toBeNull();
    expect(byId("shows-check").getAttribute("aria-describedby")).toBe("shows-match");
    expect(byId("shows-on-air").getAttribute("aria-live")).toBeNull();
    expect(byId("now-playing-show").getAttribute("aria-live")).toBeNull();
    expect(byId("shows-status").getAttribute("aria-live")).toBe("polite");
  });

  it("lists shows as plain text with named buttons, and Stop only for the show on air", async () => {
    const server = fakeServer();
    server.shows.push(show("Remembering the 2000s"), show("Nineties"));
    server.active = { id: "id-Nineties", name: "Nineties", started_at: "2026-10-11T19:00:00+00:00", ends_at: "2026-10-11T21:00:00+00:00", count: 40 };
    await openPanel(server);
    expect(row("Remembering").querySelector("span").textContent)
      .toBe("Remembering the 2000s: First released 2000 to 2009. Soft start, soft end. 60 minutes when started now.");
    expect(row("Nineties").querySelector("span").textContent).toMatch(/^Nineties, on air: /);
    expect([...row("Remembering").querySelectorAll("button")].map((button) => [button.textContent, button.getAttribute("aria-label"), button.hidden])).toEqual([
      ["Edit", "Edit Remembering the 2000s", false],
      ["Start now", "Start now, Remembering the 2000s, for 60 minutes", false],
      ["Stop", "Stop Remembering the 2000s", true],
      ["Delete", "Delete Remembering the 2000s", false],
    ]);
    expect(rowButton("Nineties", "stop").hidden).toBe(false);
    expect(byId("shows-on-air").textContent).toBe("On air: Nineties, until 21:00. 40 songs in it.");
  });

  it("says when there are no shows", async () => {
    await openPanel(fakeServer());
    expect(byId("shows-list").textContent).toBe("No shows yet.");
    expect(byId("shows-on-air").textContent).toBe("No show is on air.");
  });

  it("adds a show with genres, says the match count and focuses its Edit button", async () => {
    const server = fakeServer();
    server.report = { ...REPORT, warning: "about 30 songs needed; only 22 fit your artist rule" };
    await openPanel(server);
    byId("shows-add").click();
    expect(byId("shows-form").hidden).toBe(false);
    expect(document.activeElement).toBe(byId("shows-name"));
    expect([...byId("shows-genre-options").options].map((item) => item.value)).toEqual(["Pop", "Rock"]);
    expect([...byId("shows-intro").options].map((item) => item.textContent)).toEqual(["None", "intro.mp3"]);
    expect([...byId("shows-clockwheel").options].map((item) => item.value)).toEqual(["", "Decades"]);

    byId("shows-name").value = "Remembering the 2000s";
    byId("shows-year-from").value = "2000";
    byId("shows-year-to").value = "2009";
    byId("shows-include-input").value = "rock";
    byId("shows-include-add").click();
    await vi.waitFor(() => expect(status()).toBe("Rock added to genres to include"));
    expect(document.activeElement).toBe(byId("shows-include-input"));
    expect(byId("shows-include-input").value).toBe("");
    byId("shows-include-input").value = "Pop";
    byId("shows-include-input").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await vi.waitFor(() => expect(status()).toBe("Pop added to genres to include"));
    const removes = byId("shows-include-list").querySelectorAll("button");
    expect([...removes].map((button) => [button.textContent, button.getAttribute("aria-label")])).toEqual([
      ["Remove", "Remove Rock from genres to include"],
      ["Remove", "Remove Pop from genres to include"],
    ]);
    removes[0].click();
    await vi.waitFor(() => expect(status()).toBe("Rock removed from genres to include"));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Remove Pop from genres to include");
    byId("shows-exclude-input").value = "pop";
    byId("shows-exclude-add").click();
    await vi.waitFor(() => expect(status()).toBe("Pop is in genres to include; remove it there first"));
    expect(byId("shows-exclude-input").getAttribute("aria-invalid")).toBe("true");
    byId("shows-exclude-input").value = "Christmas";
    byId("shows-exclude-add").click();
    byId("shows-query").value = '^artist:"bon jovi"';
    byId("shows-intro").value = LINER;
    byId("shows-liner-source").value = "src";
    byId("shows-liner-folder").value = "2000s";
    byId("shows-start").value = "hard";
    byId("shows-minutes").value = "120";
    submit();
    await vi.waitFor(() => expect(status()).toMatch(/^Show Remembering the 2000s saved\./));
    expect(status()).toBe(
      "Show Remembering the 2000s saved. 1,284 songs match. 96 use the release year because the first-release year is missing. Warning: about 30 songs needed; only 22 fit your artist rule.",
    );
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Remembering the 2000s", year_from: 2000, year_to: 2009, genres_include: ["Pop"],
      genres_exclude: ["Christmas"], query: '^artist:"bon jovi"', clockwheel: null, intro: LINER,
      outro: null, liner_folder: { source: "src", folder: "2000s" }, start: "hard", end: "soft",
      default_minutes: 120,
    });
    expect(byId("shows-form").hidden).toBe(true);
    expect(document.activeElement).toBe(rowButton("Remembering", "edit"));
  });

  it("checks the filter and says the count once, in plain text", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("shows-add").click();
    byId("shows-year-from").value = "2000";
    byId("shows-check").click();
    await vi.waitFor(() => expect(byId("shows-match").textContent).toMatch(/^1,284 songs match/));
    await vi.waitFor(() => expect(status()).toBe(byId("shows-match").textContent));
    expect(server.calls.at(-1).body).toEqual({
      year_from: 2000, year_to: null, genres_include: [], genres_exclude: [], query: "", minutes: 60,
    });
  });

  it("marks and focuses the field at fault, with the words on screen", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("shows-add").click();
    submit();
    await vi.waitFor(() => expect(status()).toBe("Enter a name"));
    expect(document.activeElement).toBe(byId("shows-name"));
    expect(byId("shows-name").getAttribute("aria-invalid")).toBe("true");
    expect(byId("shows-form-error").textContent).toBe("Enter a name");
    byId("shows-name").value = "A";
    byId("shows-name").dispatchEvent(new Event("input", { bubbles: true }));
    expect(byId("shows-name").hasAttribute("aria-invalid")).toBe(false);
    byId("shows-year-from").value = "2010";
    byId("shows-year-to").value = "2000";
    submit();
    await vi.waitFor(() => expect(status()).toBe("The year range ends before it starts"));
    expect(document.activeElement).toBe(byId("shows-year-to"));
    byId("shows-year-to").value = "";
    byId("shows-minutes").value = "0";
    byId("shows-check").click();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("shows-minutes")));
    byId("shows-minutes").value = "60";
    byId("shows-liner-folder").value = "x";
    submit();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("shows-liner-source")));
    byId("shows-liner-folder").value = "";
    byId("shows-include-input").value = " ";
    byId("shows-include-add").click();
    await vi.waitFor(() => expect(status()).toBe("Enter a genre to add to genres to include"));
    server.fail = new FakeError('"genre::x": regular expressions are not supported', { field: "query" });
    submit();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("shows-query")));
    expect(byId("shows-query").getAttribute("aria-invalid")).toBe("true");
    expect(byId("shows-form-error").textContent).toBe('"genre::x": regular expressions are not supported');
    server.fail = new FakeError("Something else", { field: null });
    byId("shows-check").click();
    await vi.waitFor(() => expect(status()).toBe("Could not check: Something else"));
  });

  it("edits a show, keeping a liner that is gone visible", async () => {
    const server = fakeServer();
    server.shows.push(show("Nineties", { outro: "b".repeat(32), genres_exclude: ["Christmas"], clockwheel: "Old" }));
    await openPanel(server);
    rowButton("Nineties", "edit").click();
    expect(byId("shows-form-legend").textContent).toBe("Edit Nineties");
    expect(byId("shows-name").value).toBe("Nineties");
    expect(byId("shows-outro").selectedOptions[0].textContent).toBe("A liner that is no longer registered");
    expect(byId("shows-clockwheel").selectedOptions[0].textContent).toBe("Old (not found)");
    expect(byId("shows-exclude-list").textContent).toContain("Christmas");
    byId("shows-cancel").click();
    expect(byId("shows-form").hidden).toBe(true);
    expect(document.activeElement).toBe(rowButton("Nineties", "edit"));
    rowButton("Nineties", "edit").click();
    byId("shows-name").value = "Late nineties";
    submit();
    await vi.waitFor(() => expect(status()).toMatch(/^Show Late nineties saved/));
    expect(server.calls.find((call) => call.method === "PUT").url).toBe("/api/shows/id-Nineties");
    server.fail = new FakeError("That show no longer exists", { status: 404 });
    rowButton("Late nineties", "edit").click();
    submit();
    await vi.waitFor(() => expect(status()).toBe("Could not save: That show no longer exists"));
    expect(document.activeElement).toBe(byId("shows-add"));
  });

  it("starts and stops a show, moving focus between Stop and Start now", async () => {
    const server = fakeServer();
    server.shows.push(show("Remembering the 2000s"));
    const controls = await openPanel(server);
    controls.applyState({ show: null });
    rowButton("Remembering", "start").click();
    await vi.waitFor(() => expect(status()).toBe("Show Remembering the 2000s started, until 21:00"));
    expect(server.calls.find((call) => call.url.endsWith("/start")).body).toEqual({ minutes: 60 });
    expect(document.activeElement).toBe(rowButton("Remembering", "stop"));
    expect(byId("now-playing-show").textContent).toBe("On air: Remembering the 2000s, until 21:00.");
    expect(byId("now-playing-show").hidden).toBe(false);
    // The page caused it, so the push that follows is not said again.
    controls.applyState({ show: server.active });
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(byId("sr-status").textContent).toBe("");
    rowButton("Remembering", "stop").click();
    await vi.waitFor(() => expect(status()).toBe("Show Remembering the 2000s stopped"));
    expect(document.activeElement).toBe(rowButton("Remembering", "start"));
    expect(rowButton("Remembering", "stop").hidden).toBe(true);
    expect(byId("now-playing-show").hidden).toBe(true);
    controls.applyState({ show: null });
    expect(byId("sr-status").textContent).toBe("");
    server.fail = new FakeError("No song matches it");
    rowButton("Remembering", "start").click();
    await vi.waitFor(() => expect(status()).toBe("Could not start Remembering the 2000s: No song matches it"));
  });

  it("says a show's start and end once, from any tab, but not on page load", async () => {
    const server = fakeServer();
    server.shows.push(show("Remembering the 2000s"));
    const controls = await openPanel(server);
    const onAir = { id: "id-Rememberingthe2000s", name: "Remembering the 2000s", started_at: "2026-10-11T20:00:00+00:00", ends_at: "2026-10-11T21:00:00+00:00" };
    controls.applyState({ show: onAir }); // the first state: a page loaded mid-show
    expect(byId("sr-status").textContent).toBe("");
    expect(byId("now-playing-show").textContent).toBe("On air: Remembering the 2000s, until 21:00.");
    controls.applyState({ show: null });
    expect(byId("sr-status").textContent).toBe("Show Remembering the 2000s ended");
    controls.applyState({ show: null });
    controls.applyState({ show: onAir });
    expect(byId("sr-status").textContent).toBe("Show Remembering the 2000s started, until 21:00");
    expect(rowButton("Remembering", "stop").hidden).toBe(false);
    // A show ending on its own takes Stop away; focus there goes to Start now.
    rowButton("Remembering", "stop").focus();
    controls.applyState({ show: null });
    expect(document.activeElement).toBe(rowButton("Remembering", "start"));
  });

  it("deletes a show after confirming and focuses the next Edit button", async () => {
    const server = fakeServer();
    server.shows.push(show("One"), show("Two"));
    server.active = { id: "id-One", name: "One", started_at: "2026-10-11T20:00:00+00:00", ends_at: "2026-10-11T21:00:00+00:00", count: 3 };
    await openPanel(server);
    rowButton("One", "delete").click();
    expect(byId("confirm-title").textContent).toBe("Delete show One?");
    expect(byId("confirm-message").textContent).toMatch(/^The show is on air: it ends now/);
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Show One deleted; it has ended"));
    expect(document.activeElement).toBe(rowButton("Two", "edit"));
    rowButton("Two", "delete").click();
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(document.activeElement).toBe(rowButton("Two", "delete")));
    server.fail = new FakeError("disk full");
    rowButton("Two", "delete").click();
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Could not delete Two: disk full"));
  });

  it("says when the list cannot load, and forgets everything on sign out", async () => {
    const server = fakeServer();
    server.shows.push(show("One"));
    const controls = await openPanel(server);
    controls.applyState({ show: { id: "id-One", name: "One", started_at: "x", ends_at: "2026-10-11T21:00:00+00:00" } });
    server.fail = new FakeError("offline");
    await controls.refresh();
    await vi.waitFor(() => expect(status()).toBe("Could not load the shows: offline"));
    controls.clear();
    expect(byId("shows-list").children.length).toBe(0);
    expect(byId("shows-on-air").textContent).toBe("No show is on air.");
    expect(byId("now-playing-show").hidden).toBe(true);
    expect(status()).toBe("");
  });

  it("keeps the Now Playing line without the Settings panel", () => {
    byId("spanel-shows").remove();
    const controls = installShowsPanel({ doc: document, request: vi.fn(), now: () => NOW });
    controls.applyState({ show: { id: "x", name: "Late", started_at: "a", ends_at: "2026-10-11T23:00:00+00:00" } });
    expect(byId("now-playing-show").textContent).toBe("On air: Late, until 23:00.");
    controls.clear();
    return controls.refresh();
  });
});
