
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import {
  dayWords, describeRule, installIntroRulesPanel, ruleFormError, waitWords,
} from "../../src/autodj/static/modules/intro-rules-panel.js";

const byId = (id) => document.getElementById(id);
const copy = (value) => JSON.parse(JSON.stringify(value));
const status = () => byId("intro-rules-status").textContent;
const items = () => [...byId("intro-rules-list").querySelectorAll("li")];
const button = (index, action) => items()[index].querySelector(`button[data-action="${action}"]`);

const EIGHTIES = {
  id: "a".repeat(32), label: "Eighties", match: "all", priority: "after_artist",
  conditions: [{ field: "year", operator: "between", value: 1980, upper: 1989 }],
  clip_ids: ["l1"], chance: 50, cooldown_minutes: 90, created_at: 1, last_aired: null,
};
const ROCK = {
  id: "b".repeat(32), label: "", match: "any", priority: "before_song",
  conditions: [{ field: "genre", operator: "is", value: "Rock" }, { field: "category", category: "c1" }],
  clip_ids: [], liner_folder: "D:\\Liners\\Rock", chance: 100, cooldown_minutes: 0,
  not_before: "2026-12-01", not_after: "2026-12-26", created_at: 2, last_aired: null,
};
const CHOICES = {
  liners: [{ id: "l1", label: "eighties.wav" }, { id: "l2", label: "rock.wav" }],
  liner_folders: ["D:\\Liners", "D:\\Liners\\Rock"],
  sources: [{ id: "music", path: "D:\\Music" }],
  genres: ["Pop", "Rock"],
  beets_fields: ["original_year"],
  beets_error: null,
  categories: [{ id: "c1", name: "Gold" }],
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

// A stand-in for the intro rule routes.
function fakeServer() {
  const server = { rules: [copy(EIGHTIES), copy(ROCK)], calls: [], refuse: null, testRefuse: null, testResult: null, testDeferred: null };
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (url.startsWith("/api/search?")) return { results: [{ path: "D:\\Music\\song.flac", artist: "Artist", title: "Song" }] };
    if (url.startsWith("/api/intros/rules/test?")) {
      if (server.testRefuse) throw server.testRefuse;
      if (server.testDeferred) return server.testDeferred;
      return server.testResult || { expanded_pattern: "Artist - Song.wav", missing_fields: ["year"], matches: [
        { id: "liner-1", label: "Artist - Song.wav", eligible: true, reason: null },
        { id: "liner-2", label: "other.wav", eligible: false, reason: "Filename does not match." },
      ], note: "One liner matches." };
    }
    if (method === "GET") return { rules: copy(server.rules), problem: null, max_rules: 2000, ...copy(CHOICES) };
    if (server.refuse) throw server.refuse;
    if (method === "POST") {
      const rule = { ...body, id: "c".repeat(32), created_at: 3, last_aired: null };
      server.rules.push(rule);
      return rule;
    }
    const id = decodeURIComponent(url.split("/").pop());
    if (method === "PUT") {
      const index = server.rules.findIndex((item) => item.id === id);
      server.rules[index] = { ...body, id, created_at: server.rules[index].created_at, last_aired: null };
      return server.rules[index];
    }
    if (method === "DELETE") {
      server.rules = server.rules.filter((item) => item.id !== id);
      return { deleted: id };
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

let controls = null;

async function open(server = fakeServer()) {
  controls = installIntroRulesPanel({ document, request: server.request });
  await vi.waitFor(() => expect(items()).toHaveLength(server.rules.length));
  return server;
}

beforeEach(installIndex);
afterEach(() => { controls?.destroy(); controls = null; });

describe("intro rules markup", () => {
  it("is its own card in Library tools with labelled native controls and one status", () => {
    const section = byId("intro-rules");
    expect(section.parentElement).toBe(byId("panel-library"));
    expect(section.classList.contains("card")).toBe(true);
    expect(section.querySelector(":scope > summary > h2").id).toBe("intro-rules-heading");
    const live = [...section.querySelectorAll("[aria-live], [role='status'], [role='alert']")];
    expect(live.map((node) => node.id)).toEqual(["intro-rules-status"]);
    expect(live[0].getAttribute("aria-atomic")).toBe("true");
    for (const control of section.querySelectorAll("input, select")) {
      expect(section.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
      expect(control.hasAttribute("aria-label"), control.id).toBe(false);
    }
    const list = byId("intro-rules-list");
    expect(list.getAttribute("role")).toBe("list");
    expect(byId(list.getAttribute("aria-labelledby")).textContent).toBe("Saved intro rules");
    expect(byId("intro-rules-form").hidden).toBe(true);
    expect(byId("intro-rules-form").hasAttribute("novalidate")).toBe(true);
    expect([...section.querySelectorAll("fieldset")].map((set) => set.querySelector(":scope > legend").textContent))
      .toEqual(["Add intro rule", "Voice liners to choose from"]);
    expect(byId("intro-rules-conditions").getAttribute("role")).toBe("group");
    // Errors are words on the page, hidden, never live, linked only while shown.
    for (const error of section.querySelectorAll(".field-error")) {
      expect(error.hidden, error.id).toBe(true);
      expect(error.hasAttribute("role"), error.id).toBe(false);
      expect(document.querySelector(`[aria-describedby~="${error.id}"]`), error.id).toBeNull();
    }
    const ids = [...document.querySelectorAll('[id^="intro-rules"]')].map((node) => node.id);
    expect(ids.length).toBe(new Set(ids).size);
  });
});

describe("intro rules in words", () => {
  it("says each rule, its liners, chance, wait, priority and dates", () => {
    expect(dayWords("2026-12-01")).toBe("1 December 2026");
    expect(waitWords(0)).toBe("with no wait between plays");
    expect(waitWords(90)).toBe("at most once every 1 hour 30 minutes");
    expect(waitWords(1441)).toBe("at most once every 1 day 1 minute");
    expect(describeRule(EIGHTIES, 0, CHOICES)).toBe(
      "Eighties: when the next song matches Year between 1980 and 1989. Plays eighties.wav. "
      + "50% chance, at most once every 1 hour 30 minutes, after the song’s and the artist’s own intros.");
    expect(describeRule(ROCK, 1, CHOICES)).toMatch(/^Rule 2: when the next song matches .*Rock.* or .*Gold.*\. Plays any liner in D:\\Liners\\Rock\. 100% chance, with no wait between plays, before the song’s and the artist’s own intros\. Plays from 1 December 2026 until 26 December 2026\.$/);
    expect(describeRule({ ...EIGHTIES, clip_ids: ["gone", "l2"] }, 0, CHOICES))
      .toContain("Plays one of a liner that is no longer registered, rock.wav.");
    expect(describeRule({ ...EIGHTIES, clip_ids: [], filename_pattern: "{artist} - *.wav", liner_folder: "D:\\Liners" }, 0, CHOICES))
      .toContain('Plays liners matching filename pattern "{artist} - *.wav" in D:\\Liners.');
  });

  it("checks the form's values before sending", () => {
    const fine = { clipIds: ["l1"], chance: "50", cooldown: "0" };
    expect(ruleFormError(fine)).toBeNull();
    expect(ruleFormError({ ...fine, clipIds: [] })[0]).toBe("clips");
    expect(ruleFormError({ ...fine, clipIds: [], folder: "D:\\Liners" })).toBeNull();
    expect(ruleFormError({ ...fine, chance: "0" })[0]).toBe("chance");
    expect(ruleFormError({ ...fine, chance: "1.5" })[0]).toBe("chance");
    expect(ruleFormError({ ...fine, cooldown: "10081" })[0]).toBe("cooldown");
    expect(ruleFormError({ ...fine, notBefore: "2026-12-26", notAfter: "2026-12-01" })[0]).toBe("not-after");
  });
});

describe("intro rules list and form", () => {
  it("lists each rule as plain text with named Edit and Delete buttons, and says nothing", async () => {
    await open();
    expect(items()[0].querySelector(".intro-text").textContent).toBe(describeRule(EIGHTIES, 0, CHOICES));
    expect(button(0, "edit").textContent).toBe("Edit");
    expect(button(0, "edit").getAttribute("aria-label")).toBe("Edit intro rule Eighties");
    expect(button(1, "delete").getAttribute("aria-label")).toBe("Delete intro rule Rule 2");
    expect(byId("intro-rules-empty").hidden).toBe(true);
    expect(status()).toBe("");
  });

  it("adds a rule: focus to Label, then to the saved rule's Edit button, said once", async () => {
    const server = await open();
    byId("intro-rules-add").click();
    expect(byId("intro-rules-form").hidden).toBe(false);
    expect(document.activeElement).toBe(byId("intro-rules-label"));
    // The shared condition rows, with categories offered.
    const row = byId("intro-rules-conditions").firstElementChild;
    const type = row.querySelector("select");
    expect([...type.options].map((option) => option.value)).toContain("category");
    type.value = "artist";
    type.dispatchEvent(new Event("change"));
    row.querySelectorAll('input[type="text"]')[1].value = "Example";
    byId("intro-rules-label").value = "Name drop";
    const boxes = byId("intro-rules-liners").querySelectorAll('input[type="checkbox"]');
    expect([...boxes].map((box) => document.querySelector(`label[for="${box.id}"]`).textContent))
      .toEqual(["eighties.wav", "rock.wav"]);
    boxes[1].checked = true;
    byId("intro-rules-priority").value = "before_artist";
    byId("intro-rules-chance").value = "75";
    byId("intro-rules-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved intro rule Name drop."));
    const sent = server.calls.find((call) => call.method === "POST").body;
    expect(sent).toEqual({
      label: "Name drop", match: "all", priority: "before_artist", chance: 75, cooldown_minutes: 60,
      conditions: [{ field: "artist", operator: "is", value: "Example" }], clip_ids: ["l2"], filename_pattern: null,
    });
    expect(byId("intro-rules-form").hidden).toBe(true);
    expect(document.activeElement).toBe(button(2, "edit"));
  });

  it("refuses a rule with no liner in the page: error linked, field focused, nothing sent", async () => {
    const server = await open();
    byId("intro-rules-add").click();
    const row = byId("intro-rules-conditions").firstElementChild;
    row.querySelector("select").value = "genre";
    row.querySelector("select").dispatchEvent(new Event("change"));
    row.querySelectorAll("select")[3].value = "Rock";
    byId("intro-rules-save").click();
    const first = byId("intro-rules-liners").querySelector('input[type="checkbox"]');
    const error = byId("intro-rules-clips-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Check at least one voice liner or choose a liner folder.");
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute("aria-invalid")).toBe("true");
    expect(first.getAttribute("aria-describedby").split(" ")).toContain(error.id);
    expect(server.calls.filter((call) => call.method !== "GET")).toEqual([]);
    expect(status()).toBe("");
    // Checking one takes the error away.
    first.checked = true;
    first.dispatchEvent(new Event("change", { bubbles: true }));
    expect(error.hidden).toBe(true);
    expect(first.hasAttribute("aria-invalid")).toBe(false);
    expect(first.getAttribute("aria-describedby")).toBeNull();
  });

  it("links an incomplete condition's error to its field", async () => {
    await open();
    byId("intro-rules-add").click();
    const row = byId("intro-rules-conditions").firstElementChild;
    row.querySelector("select").value = "artist";
    row.querySelector("select").dispatchEvent(new Event("change"));
    byId("intro-rules-save").click();
    const value = row.querySelectorAll('input[type="text"]')[1];
    expect(document.activeElement).toBe(value);
    expect(value.getAttribute("aria-invalid")).toBe("true");
    expect(byId("intro-rules-conditions-error").textContent).toBe("Enter a value for artist.");
    expect(value.getAttribute("aria-describedby").split(" ")).toContain("intro-rules-conditions-error");
  });

  it("shows a field the server refuses on that field", async () => {
    const server = await open();
    server.refuse = new FieldError("Chance: Input should be less than or equal to 100", "chance");
    await button(0, "edit").click();
    expect(byId("intro-rules-form-legend").textContent).toBe("Edit intro rule Eighties");
    expect(byId("intro-rules-chance").value).toBe("50");
    expect(byId("intro-rules-liners").querySelector('input[value="l1"]').checked).toBe(true);
    byId("intro-rules-save").click();
    await vi.waitFor(() => expect(byId("intro-rules-chance-error").hidden).toBe(false));
    expect(document.activeElement).toBe(byId("intro-rules-chance"));
    expect(status()).toBe("");
  });

  it("edits a rule in place, keeping a folder that is no longer offered", async () => {
    const server = await open();
    button(1, "edit").click();
    expect(byId("intro-rules-folder").value).toBe("D:\\Liners\\Rock");
    expect(byId("intro-rules-not-after").value).toBe("2026-12-26");
    byId("intro-rules-cooldown").value = "15";
    byId("intro-rules-save").click();
    await vi.waitFor(() => expect(status()).toBe("Saved intro rule Rule 2."));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe(`/api/intros/rules/${ROCK.id}`);
    expect(put.body).toMatchObject({
      liner_folder: "D:\\Liners\\Rock", cooldown_minutes: 15, match: "any",
      not_before: "2026-12-01", not_after: "2026-12-26",
      filename_pattern: null,
      conditions: [{ field: "genre", operator: "is", value: "Rock" }, { field: "category", category: "c1" }],
    });
    expect(document.activeElement).toBe(button(1, "edit"));
  });

  it("saves filename patterns, inserts a token at the caret, and tests a chosen song", async () => {
    const server = await open();
    byId("intro-rules-add").click();
    const row = byId("intro-rules-conditions").firstElementChild;
    row.querySelector("select").value = "artist";
    row.querySelector("select").dispatchEvent(new Event("change"));
    row.querySelectorAll('input[type="text"]')[1].value = "Artist";
    byId("intro-rules-selection-mode").value = "pattern";
    byId("intro-rules-selection-mode").dispatchEvent(new Event("change"));
    const pattern = byId("intro-rules-pattern");
    pattern.value = "prefix suffix";
    pattern.setSelectionRange(7, 7);
    byId("intro-rules-pattern-field").value = "artist";
    byId("intro-rules-pattern-insert").click();
    expect(pattern.value).toBe("prefix {artist}suffix");
    pattern.value = "{artist} - {title}.wav";
    byId("intro-rules-pattern-folder").value = "D:\\Liners";
    byId("intro-rules-test-query").value = "Song";
    byId("intro-rules-test-query").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(byId("intro-rules-test-results").querySelectorAll("button")).toHaveLength(1));
    byId("intro-rules-test-results").querySelector("button").click();
    server.testRefuse = new FieldError("Track not found", "path");
    byId("intro-rules-test").click();
    await vi.waitFor(() => expect(byId("intro-rules-test-song-error").hidden).toBe(false));
    expect(document.activeElement).toBe(byId("intro-rules-test-query"));
    server.testRefuse = null;
    byId("intro-rules-test").click();
    await vi.waitFor(() => expect(status()).toBe("Pattern test complete: 1 eligible, 1 excluded; 1 missing song fields."));
    expect(byId("intro-rules-test-output").hidden).toBe(false);
    expect(byId("intro-rules-test-output").textContent).toContain("Missing song fields: year.");
    expect(byId("intro-rules-test-output").textContent).toContain("other.wav: Filename does not match.");
    expect(status()).not.toContain("other.wav");
    server.testResult = { expanded_pattern: "Artist - Song.wav", missing_fields: [], matches: [], note: "Folder is empty." };
    byId("intro-rules-test").click();
    await vi.waitFor(() => expect(byId("intro-rules-test-output").textContent).toContain("No matching liners."));
    let resolveTest;
    server.testDeferred = new Promise((resolve) => { resolveTest = resolve; });
    byId("intro-rules-test").click();
    await vi.waitFor(() => expect(byId("intro-rules-test").disabled).toBe(true));
    pattern.value += "!";
    pattern.dispatchEvent(new Event("input", { bubbles: true }));
    expect(byId("intro-rules-test").disabled).toBe(false);
    resolveTest({ expanded_pattern: "stale", missing_fields: [], matches: [], note: "Stale." });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(byId("intro-rules-test-output").hidden).toBe(true);
    server.testDeferred = null;
    pattern.value = "{artist} - {title}.wav";
    server.refuse = new FieldError("Folder unavailable", "liner_folder");
    byId("intro-rules-save").click();
    await vi.waitFor(() => expect(byId("intro-rules-pattern-folder-error").hidden).toBe(false));
    expect(document.activeElement).toBe(byId("intro-rules-pattern-folder"));
    server.refuse = null;
    const tested = server.calls.find((call) => call.url.startsWith("/api/intros/rules/test?"));
    expect(tested.url).toBe("/api/intros/rules/test?path=D%3A%5CMusic%5Csong.flac");
    expect(tested.body).toMatchObject({ clip_ids: [], filename_pattern: "{artist} - {title}.wav", liner_folder: "D:\\Liners" });
    byId("intro-rules-save").click();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "POST" && call.url === "/api/intros/rules")).toBe(true));
    const saved = server.calls.find((call) => call.method === "POST" && call.url === "/api/intros/rules");
    expect(saved.body).toMatchObject({ clip_ids: [], filename_pattern: "{artist} - {title}.wav", liner_folder: "D:\\Liners" });
    await vi.waitFor(() => expect(byId("intro-rules-form").hidden).toBe(true));
    button(2, "edit").click();
    expect(byId("intro-rules-selection-mode").value).toBe("pattern");
    expect(byId("intro-rules-pattern").value).toBe("{artist} - {title}.wav");
    expect(byId("intro-rules-test-selected").dataset.trackPath).toBeUndefined();
    byId("intro-rules-cancel").click();
  });

  it("deletes after confirming, moves focus to the next Delete, and says so once", async () => {
    const server = await open();
    button(0, "delete").click();
    const dialog = byId("confirm-dialog");
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Delete intro rule Eighties?");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Deleted intro rule Eighties."));
    expect(server.rules.map((rule) => rule.id)).toEqual([ROCK.id]);
    expect(document.activeElement).toBe(button(0, "delete"));
  });

  it("cancels back to the opener, and removing a condition moves focus to the next one", async () => {
    await open();
    byId("intro-rules-add").click();
    byId("intro-rules-condition-add").click();
    const rows = () => [...byId("intro-rules-conditions").children];
    expect(rows()).toHaveLength(2);
    expect(document.activeElement).toBe(rows()[1].querySelector("select"));
    rows()[0].querySelector("button[data-remove-condition]").click();
    expect(rows()).toHaveLength(1);
    expect(rows()[0].querySelector("legend").textContent).toBe("Condition 1");
    expect(document.activeElement).toBe(rows()[0].querySelector("select"));
    await vi.waitFor(() => expect(status()).toBe("Condition 1 removed, 1 condition left."));
    byId("intro-rules-cancel").click();
    expect(byId("intro-rules-form").hidden).toBe(true);
    expect(document.activeElement).toBe(byId("intro-rules-add"));
  });

  it("forgets one station's rules when another is chosen, and loads the next station's", async () => {
    const server = await open();
    controls.clear();
    expect(items()).toEqual([]);
    expect(byId("intro-rules-empty").hidden).toBe(false);
    server.rules = [copy(ROCK)];
    controls.refresh();
    await vi.waitFor(() => expect(items()).toHaveLength(1));
  });
});
