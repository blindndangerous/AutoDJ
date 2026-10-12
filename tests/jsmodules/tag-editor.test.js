import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installTagEditor, keyAccepted, tagFieldError } from "../../src/autodj/static/modules/tag-editor.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const section = html.match(/<section id="tags-editor"[\s\S]*?<\/section>/)[0];
const keyCases = JSON.parse(readFileSync(join(process.cwd(), "tests", "jsmodules", "golden", "key-cases.json"), "utf8"));

const RECORD = {
  path: "c:/music/song.flac",
  title: "Old Title",
  tags_writable: true,
  beets_writeback: false,
  tags: { title: "Old Title", artist: "Artist", album: "Album", genre: "", year: "2001", bpm: "120", key: "", isrc: "" },
};

// The shape requestJson rejects with: a message, the status and the field.
const apiError = (message, { status, field = null }) => Object.assign(new Error(message), { status, field });

const installed = [];
function install(options = {}) {
  const editor = installTagEditor({ document, ...options });
  installed.push(editor);
  return editor;
}
const field = (name) => document.getElementById(`tag-${name}`);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  document.body.innerHTML = `${section}<audio id="points-preview"></audio><div id="status-toast" hidden></div>`;
  document.getElementById("points-preview").pause = () => {};
  document.getElementById("points-preview").load = () => {};
});
afterEach(() => { for (const editor of installed.splice(0)) editor.destroy(); });

describe("tag field rules", () => {
  it("matches the server's rules", () => {
    expect(tagFieldError("title", " ")).toBe("Enter a title.");
    expect(tagFieldError("artist", "")).toBe("");
    expect(tagFieldError("album", "x".repeat(501))).toMatch(/500 characters/);
    expect(tagFieldError("genre", "a\tb")).toMatch(/line breaks/);
    expect(tagFieldError("year", "99")).toMatch(/four digits/);
    expect(tagFieldError("year", "0999")).toMatch(/four digits/);
    expect(tagFieldError("year", "")).toBe("");
    expect(tagFieldError("bpm", "127.5")).toBe("");
    expect(tagFieldError("bpm", "401")).toMatch(/1 to 400/);
    expect(tagFieldError("bpm", "fast")).toMatch(/1 to 400/);
    for (const key of ["Am", "F#m", "Db", "C major", "8A", "12b"]) expect(tagFieldError("key", key)).toBe("");
    expect(tagFieldError("key", "H")).toMatch(/Camelot/);
    expect(tagFieldError("isrc", "ab-1cd-26-00001")).toBe("");
    expect(tagFieldError("isrc", "123")).toMatch(/12 characters/);
  });
});

describe("key checks", () => {
  // The same list is run against the server's parser in tests/unit/test_tag_edit.py.
  it("takes exactly the keys the server takes", () => {
    for (const key of keyCases.accepted) expect(keyAccepted(key), key).toBe(true);
    for (const key of keyCases.refused) expect(keyAccepted(key), key).toBe(false);
    expect(keyAccepted(null)).toBe(false);
  });
});

describe("Track info tag editor", () => {
  it("says when beets keeps none of the changed tags", async () => {
    const editor = install({ request: vi.fn(async () => ({ ...RECORD, beets: "unchanged" })) });
    editor.load({ ...RECORD, beets_writeback: true });
    field("isrc").value = "AB1CD2600001";
    await editor.save();
    await settle();
    expect(document.getElementById("tags-status").textContent).toBe(
      "Saved the tags of Old Title in the file. Beets keeps none of the tags you changed, so beets did not change.",
    );
  });
  it("says a beets miss and a library warning in the one status", async () => {
    const request = vi.fn(async () => ({ ...RECORD, beets: "not_found", warning: "The library did not reload." }));
    const editor = install({ request });
    editor.load({ ...RECORD, beets_writeback: true });
    field("album").value = "Other";
    await editor.save();
    await settle();
    expect(document.getElementById("tags-status").textContent).toBe(
      "Saved the tags of Old Title in the file. Beets has no entry for this file, so beets did not change. The library did not reload.",
    );
  });

  it("fills the form and shows beets only when beets manages the library", () => {
    const editor = install();
    expect(document.getElementById("tags-editor").hidden).toBe(true);
    editor.load(RECORD);
    expect(document.getElementById("tags-editor").hidden).toBe(false);
    expect(field("title").value).toBe("Old Title");
    expect(field("year").value).toBe("2001");
    expect(document.getElementById("tags-beets-label").hidden).toBe(true);
    expect(editor.dirty()).toBe(false);
    editor.load({ ...RECORD, beets_writeback: true });
    expect(document.getElementById("tags-beets-label").hidden).toBe(false);
  });

  it("says when a file's tags cannot be edited", () => {
    const editor = install();
    editor.load({ ...RECORD, tags_writable: false });
    expect(document.getElementById("tags-form").hidden).toBe(true);
    expect(document.getElementById("tags-unavailable").hidden).toBe(false);
    expect(editor.dirty()).toBe(false);
  });

  it("sends only the changed fields, says the result once and keeps focus", async () => {
    const saved = { ...RECORD, title: "New Title", beets: "off", tags: { ...RECORD.tags, title: "New Title", key: "Am" } };
    const request = vi.fn(async () => saved);
    const onSaved = vi.fn();
    const editor = install({ request, onSaved });
    editor.load(RECORD);
    field("title").value = "New Title ";
    field("key").value = "8A";
    expect(editor.dirty()).toBe(true);
    const button = document.getElementById("tags-save");
    button.focus();
    button.click();
    await settle();
    await settle();
    expect(request).toHaveBeenCalledTimes(1);
    const [url, options] = request.mock.calls[0];
    expect(url).toBe("/api/library/tags");
    expect(JSON.parse(options.body)).toEqual({ path: RECORD.path, tags: { title: "New Title", key: "8A" }, beets: false });
    expect(onSaved).toHaveBeenCalledWith(saved);
    expect(document.getElementById("tags-status").textContent).toBe("Saved the tags of New Title.");
    expect(document.activeElement).toBe(button);
    expect(button.disabled).toBe(false);
    expect(field("key").value).toBe("Am");
    expect(editor.dirty()).toBe(false);
  });

  it("asks beets to follow when its box is ticked, and reports a beets failure", async () => {
    const request = vi.fn(async () => ({ ...RECORD, beets: "failed", beets_error: "database is locked" }));
    const editor = install({ request });
    editor.load({ ...RECORD, beets_writeback: true });
    field("artist").value = "Band";
    expect(await editor.save()).toBe(true);
    expect(JSON.parse(request.mock.calls[0][1].body).beets).toBe(true);
    await settle();
    expect(document.getElementById("tags-status").textContent).toBe(
      "Saved the tags of Old Title in the file, but beets was not updated: database is locked. Run beet update to read them into beets.",
    );
  });

  it("marks every refused field, links its error and focuses the first", async () => {
    const request = vi.fn();
    const editor = install({ request });
    editor.load(RECORD);
    field("year").value = "99";
    field("isrc").value = "123";
    expect(await editor.save()).toBe(false);
    expect(request).not.toHaveBeenCalled();
    for (const name of ["year", "isrc"]) {
      const error = document.getElementById(`tag-${name}-error`);
      expect(field(name).getAttribute("aria-invalid")).toBe("true");
      expect(field(name).getAttribute("aria-describedby").split(" ")).toEqual([`tag-${name}-help`, error.id]);
      expect(error.hidden).toBe(false);
      expect(error.textContent).not.toBe("");
    }
    expect(document.activeElement).toBe(field("year"));
    expect(document.getElementById("tags-status").textContent).toBe("");
    // Typing in a field takes its error away.
    field("year").value = "1999";
    field("year").dispatchEvent(new Event("input", { bubbles: true }));
    expect(field("year").hasAttribute("aria-invalid")).toBe(false);
    expect(field("year").getAttribute("aria-describedby")).toBe("tag-year-help");
    expect(document.getElementById("tag-year-error").hidden).toBe(true);
    // A field with no help loses aria-describedby altogether.
    field("title").value = "";
    await editor.save();
    expect(field("title").getAttribute("aria-describedby")).toBe("tag-title-error");
    field("title").value = "Back";
    field("title").dispatchEvent(new Event("input", { bubbles: true }));
    expect(field("title").hasAttribute("aria-describedby")).toBe(false);
  });

  it("focuses the field the server refuses", async () => {
    const request = vi.fn(async () => { throw apiError("Artist cannot contain line breaks or tabs.", { status: 400, field: "artist" }); });
    const editor = install({ request });
    editor.load(RECORD);
    field("artist").value = "Band";
    expect(await editor.save()).toBe(false);
    expect(field("artist").getAttribute("aria-invalid")).toBe("true");
    expect(field("artist").getAttribute("aria-describedby")).toBe("tag-artist-error");
    expect(document.getElementById("tag-artist-error").textContent).toBe("Artist cannot contain line breaks or tabs.");
    expect(document.activeElement).toBe(field("artist"));
  });

  it("says any other failure in the status and leaves focus alone", async () => {
    const request = vi.fn(async () => { throw apiError("This song is playing now.", { status: 409 }); });
    const editor = install({ request });
    editor.load(RECORD);
    field("title").value = "Changed";
    const button = document.getElementById("tags-save");
    button.focus();
    expect(await editor.save()).toBe(false);
    await settle();
    expect(document.getElementById("tags-status").textContent).toBe("Could not save the tags: This song is playing now.");
    expect(document.activeElement).toBe(button);
    expect(editor.dirty()).toBe(true);
  });

  it("writes dialog errors to the dialog instead of moving focus behind it", async () => {
    const target = document.createElement("p");
    const editor = install({ request: vi.fn(async () => { throw apiError("busy", { status: 409 }); }) });
    editor.load(RECORD);
    field("year").value = "x";
    expect(await editor.save(target)).toBe(false);
    expect(target.textContent).toMatch(/need correcting/);
    field("year").value = "2002";
    expect(await editor.save(target)).toBe(false);
    expect(target.textContent).toBe("Could not save the tags: busy Press Escape to return to editing.");
  });

  it("says when nothing changed and sends nothing", async () => {
    const request = vi.fn();
    const editor = install({ request });
    editor.load(RECORD);
    expect(await editor.save()).toBe(true);
    await settle();
    expect(request).not.toHaveBeenCalled();
    expect(document.getElementById("tags-status").textContent).toBe("No tags changed.");
  });

  it("lets go of the preview while saving and gives it back", async () => {
    const preview = document.getElementById("points-preview");
    preview.setAttribute("src", "/api/audio?path=x");
    let during = null;
    const request = vi.fn(async () => { during = preview.getAttribute("src"); return { ...RECORD, beets: "off" }; });
    const editor = install({ request });
    editor.load(RECORD);
    field("genre").value = "Ambient";
    await editor.save();
    expect(during).toBeNull();
    expect(preview.getAttribute("src")).toBe("/api/audio?path=x");
  });

  it("drops a reply that arrives after the track was cleared", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const onSaved = vi.fn();
    const editor = install({ request, onSaved });
    editor.load(RECORD);
    field("title").value = "Late";
    const pending = editor.save();
    editor.clear();
    finish({ ...RECORD, beets: "off" });
    expect(await pending).toBe(false);
    expect(onSaved).not.toHaveBeenCalled();
    expect(document.getElementById("tags-editor").hidden).toBe(true);
  });

  it("does nothing without the markup", () => {
    document.body.innerHTML = "";
    const editor = install();
    editor.load(RECORD);
    expect(editor.dirty()).toBe(false);
  });
});
