import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installMatchedIntros, introFormError, introText } from "../../src/autodj/static/modules/matched-intros.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const section = new window.DOMParser().parseFromString(html, "text/html").getElementById("intros-editor").outerHTML;
const dialog = html.match(/<dialog id="confirm-dialog"[\s\S]*?<\/dialog>/)[0];

const RECORD = { id: "a".repeat(32), kind: "music", title: "New Single", artist: "Example Artist", path: "/music/new.flac" };
const ITEM = {
  id: "b".repeat(32), applies_to: "song", label: "Brand new", rules_text: "Plays every time this song comes up, at most once every 1 hour.",
  dates_text: "", playable: true, unplayable_text: "", speech_s: 3.2,
};
const ARTIST_ITEM = { ...ITEM, id: "c".repeat(32), applies_to: "artist", label: "", rules_text: "Plays every time a song by Example Artist comes up, with no wait between plays." };
const LISTING = {
  active: true, reason: null, limits: { max_bytes: 40 * 1024 * 1024, max_seconds: 60 },
  track: { id: RECORD.id, title: RECORD.title, artist: RECORD.artist }, song: [ITEM], artist: [ARTIST_ITEM],
};

const apiError = (message, { status, field = null }) => Object.assign(new Error(message), { status, field });
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const el = (id) => document.getElementById(id);
const installed = [];
function install(request) {
  const editor = installMatchedIntros({ document, request });
  installed.push(editor);
  return editor;
}
function chooseFile(name = "intro.wav", size = 10) {
  const file = new window.File([new Uint8Array(size)], name, { type: "audio/wav" });
  Object.defineProperty(el("intro-file"), "files", { value: [file], configurable: true });
  return file;
}

beforeEach(() => {
  document.body.innerHTML = `${section}${dialog}<div id="status-toast" hidden></div>`;
  const audio = el("intro-playback");
  audio.play = vi.fn(async () => {});
  audio.pause = vi.fn();
  audio.load = vi.fn();
  const confirm = el("confirm-dialog");
  confirm.showModal = function showModal() { this.open = true; };
  confirm.close = function close(value) { this.open = false; this.returnValue = value; this.dispatchEvent(new Event("close")); };
});
afterEach(() => { for (const editor of installed.splice(0)) editor.destroy(); });

describe("intro form rules", () => {
  it("matches the server's rules", () => {
    const file = { name: "intro.WAV", size: 10 };
    expect(introFormError({ file: null })).toEqual(["file", "Choose an audio file for the intro."]);
    expect(introFormError({ file: { name: "intro.txt", size: 1 } })[0]).toBe("file");
    expect(introFormError({ file: { name: "a.mp3", size: 3 * 1024 * 1024 } }, 1024 * 1024)[1]).toMatch(/at most 1 MB/);
    expect(introFormError({ file, chance: "0" })[0]).toBe("chance");
    expect(introFormError({ file, chance: "101" })[0]).toBe("chance");
    expect(introFormError({ file, chance: "50", cooldown: "10081" })[0]).toBe("cooldown");
    expect(introFormError({ file, cooldown: "-1" })[0]).toBe("cooldown");
    expect(introFormError({ file, notBefore: "2026-12-26", notAfter: "2026-12-01" })[0]).toBe("not-after");
    expect(introFormError({ file, chance: "", cooldown: "0", notBefore: "2026-12-01", notAfter: "2026-12-26" })).toBeNull();
  });

  it("says an intro in words, dates and skips included", () => {
    expect(introText(ITEM)).toBe("Brand new. Plays every time this song comes up, at most once every 1 hour. 3 seconds of speech.");
    expect(introText({ ...ITEM, label: "", speech_s: 1, dates_text: "Plays until 26 December 2026", playable: false, unplayable_text: "expired after 26 December 2026" }))
      .toBe("Untitled intro. Plays every time this song comes up, at most once every 1 hour. Plays until 26 December 2026. Skipped now: expired after 26 December 2026. 1 second of speech.");
  });
});

describe("Track info matched intros", () => {
  it("shows a song's intros and its artist's, with named Listen and Remove buttons", async () => {
    const request = vi.fn(async () => LISTING);
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    expect(request).toHaveBeenCalledWith(`/api/intros?track_id=${RECORD.id}`);
    expect(el("intros-editor").hidden).toBe(false);
    expect(el("intros-artist-heading").textContent).toBe("For any song by Example Artist");
    expect(el("intro-applies-artist-label").textContent).toBe("Any song by Example Artist");
    expect(el("intros-song-list").querySelector(".intro-text")).not.toBeNull();
    const buttons = [...el("intros-song-list").querySelectorAll("button")];
    expect(buttons.map((button) => [button.textContent, button.getAttribute("aria-label")])).toEqual([
      ["Listen", "Listen to Brand new"], ["Remove", "Remove Brand new"],
    ]);
    expect(buttons[0].getAttribute("aria-pressed")).toBe("false");
    expect(buttons.filter((button) => button.tabIndex === 0)).toHaveLength(1);
    buttons[0].focus();
    buttons[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    expect(document.activeElement).toBe(buttons[1]);
    expect(el("intros-song-empty").hidden).toBe(true);
    expect(el("intros-artist-list").children).toHaveLength(1);
    expect(el("intros-inactive").hidden).toBe(true);
  });

  it("shows, in words and never live, intros the station's file skips", async () => {
    const said = "1 matched intro in matched_intros.json is not valid and skipped until the file is corrected: broken.";
    const editor = install(vi.fn(async () => ({ ...LISTING, problem: said })));
    editor.load(RECORD);
    await settle();
    expect(el("intros-problem").hidden).toBe(false);
    expect(el("intros-problem").textContent).toBe(said);
    expect(el("intros-problem").closest("[aria-live], [role=status]")).toBeNull();
    expect(el("intros-song-list").children).toHaveLength(1);
    editor.clear();
    expect(el("intros-problem").hidden).toBe(true);
  });

  it("hides itself for a liner and says why a station does not play intros", async () => {
    const editor = install(vi.fn(async () => ({ ...LISTING, active: false, reason: "Matched intros play only with saved segue playback." })));
    editor.load({ ...RECORD, kind: "liner" });
    expect(el("intros-editor").hidden).toBe(true);
    editor.load(RECORD);
    await settle();
    expect(el("intros-inactive").hidden).toBe(false);
    expect(el("intros-inactive").textContent).toMatch(/saved segue playback/);
  });

  it("offers only this song when the song has no artist", async () => {
    const editor = install(vi.fn(async () => ({ ...LISTING, track: { ...LISTING.track, artist: null }, artist: [] })));
    editor.load({ ...RECORD, artist: "" });
    await settle();
    expect(el("intro-applies-artist").disabled).toBe(true);
    expect(el("intro-applies-song").checked).toBe(true);
  });

  it("plays and reloads the intros of the station the page controls (#70)", async () => {
    const stations = await import("../../src/autodj/static/modules/stations.js");
    stations.setKnownStations([
      { slug: "default", name: "AutoDJ", state: "playing" },
      { slug: "nightowl", name: "Night Owl Radio", state: "playing" },
    ]);
    const request = vi.fn(async () => LISTING);
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    stations.selectStation("nightowl");
    try {
      el("intros-song-list").querySelector("button").click();
      await settle();
      expect(el("intro-playback").getAttribute("src"))
        .toBe(`/api/stations/nightowl/intros/${ITEM.id}/audio`);
      request.mockClear();
      await editor.refresh();
      expect(request).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/intros\?track_id=/));
    } finally {
      stations.selectStation("default");
    }
  });

  it("Listen is a toggle that plays on the preview player", async () => {
    const editor = install(vi.fn(async () => LISTING));
    editor.load(RECORD);
    await settle();
    const listen = el("intros-song-list").querySelector("button");
    listen.click();
    await settle();
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(el("intro-playback").getAttribute("src")).toBe(`/api/intros/${ITEM.id}/audio`);
    expect(el("intro-playback").play).toHaveBeenCalled();
    listen.click();
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(el("intro-playback").getAttribute("src")).toBeNull();
    listen.click();
    await settle();
    el("intro-playback").dispatchEvent(new Event("ended"));
    expect(listen.getAttribute("aria-pressed")).toBe("false");
  });

  it("keeps the same focused and pressed Listen button when refreshed item details change", async () => {
    const request = vi.fn(async () => LISTING);
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    const listen = el("intros-song-list").querySelector("button");
    listen.focus();
    listen.click();
    await settle();
    request.mockResolvedValueOnce({ ...LISTING, song: [{ ...ITEM, label: "Updated intro" }] });
    await editor.refresh();
    expect(el("intros-song-list").querySelector("button")).toBe(listen);
    expect(document.activeElement).toBe(listen);
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(listen.getAttribute("aria-label")).toBe("Listen to Updated intro");
  });

  it("does not mutate the saved list on an unchanged refresh", async () => {
    const editor = install(vi.fn(async () => LISTING));
    editor.load(RECORD);
    await settle();
    const list = el("intros-song-list");
    const before = [...list.querySelectorAll("li")].map((row) => [row, row.textContent]);
    const observer = new window.MutationObserver(() => {});
    observer.observe(list, { subtree: true, childList: true, characterData: true, attributes: true });
    await editor.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...list.querySelectorAll("li")].map((row) => [row, row.textContent])).toEqual(before);
    expect(observer.takeRecords()).toEqual([]);
    observer.disconnect();
  });

  it("moves focus and announces once when a refresh removes its focused intro", async () => {
    const request = vi.fn().mockResolvedValueOnce(LISTING).mockResolvedValueOnce({ ...LISTING, song: [] });
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    const list = el("intros-song-list");
    const remove = list.querySelector('[data-action="remove"]');
    remove.focus();
    await editor.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const heading = el("intros-song-heading");
    expect(document.activeElement).toBe(heading);
    expect(el("intros-status").textContent).toBe("The matched intro list changed. Focus moved to For this song.");
  });

  it("refuses a missing file at the field, linked and focused, without asking the server", async () => {
    const request = vi.fn(async () => LISTING);
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    request.mockClear();
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    expect(request).not.toHaveBeenCalled();
    const input = el("intro-file");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby").split(" ")).toContain("intro-file-error");
    expect(el("intro-file-error").textContent).toBe("Choose an audio file for the intro.");
    expect(document.activeElement).toBe(input);
  });

  it("uploads the file with its rules and says what was added", async () => {
    const request = vi.fn(async (url, options) => (options?.method === "POST" ? { ...ITEM, label: "Brand new" } : LISTING));
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    const file = chooseFile();
    el("intro-applies-artist").checked = true;
    el("intro-label").value = " Brand new ";
    el("intro-chance").value = "50";
    el("intro-cooldown").value = "0";
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    await settle();
    const [url, options] = request.mock.calls.find(([, opts]) => opts?.method === "POST");
    expect(url).toBe("/api/intros");
    const body = options.body;
    expect(body.get("track_id")).toBe(RECORD.id);
    expect(body.get("applies_to")).toBe("artist");
    expect(body.get("label")).toBe("Brand new");
    expect(body.get("chance")).toBe("50");
    expect(body.get("cooldown_minutes")).toBe("0");
    expect(body.get("file").name).toBe(file.name);
    await settle();
    expect(el("intros-status").textContent).toBe("Added the intro Brand new for any song by Example Artist.");
    expect(el("intro-chance").value).toBe("100");
  });

  it("never says unavailable on Upload while it sends, and takes no second press (#331)", async () => {
    let release;
    const request = vi.fn((url, options) => (options?.method === "POST"
      ? new Promise((resolve) => { release = () => resolve(ITEM); })
      : Promise.resolve(LISTING)));
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    chooseFile();
    const upload = el("intro-upload");
    upload.focus();
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    expect(upload.hasAttribute("aria-disabled")).toBe(false);
    expect(upload.classList.contains("is-pending")).toBe(true);
    expect(upload.getAttribute("aria-busy")).toBe("true");
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    release();
    await vi.waitFor(() => expect(upload.classList.contains("is-pending")).toBe(false));
    expect(upload.hasAttribute("aria-busy")).toBe(false);
  });

  it("does not reset a new track's upload draft when an old upload finishes", async () => {
    let finish;
    const request = vi.fn((url, options) => options?.method === "POST"
      ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(LISTING));
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    chooseFile();
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    editor.load({ ...RECORD, id: "other-track", title: "New track" });
    await settle();
    el("intro-label").value = "Draft for new track";
    finish(ITEM);
    await settle();
    expect(el("intro-label").value).toBe("Draft for new track");
    expect(el("intros-status").textContent).not.toContain("Added the intro");
  });

  it("shows a field the server refused at that field", async () => {
    const request = vi.fn(async (url, options) => {
      if (options?.method === "POST") throw apiError("Chance: must be at most 100", { status: 422, field: "chance" });
      return LISTING;
    });
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    chooseFile();
    el("intros-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await settle();
    await settle();
    expect(el("intro-chance").getAttribute("aria-invalid")).toBe("true");
    expect(el("intro-chance-error").textContent).toBe("Chance: must be at most 100");
    expect(document.activeElement).toBe(el("intro-chance"));
    // Typing in the field clears its error.
    el("intro-chance").dispatchEvent(new Event("input", { bubbles: true }));
    expect(el("intro-chance").hasAttribute("aria-invalid")).toBe(false);
  });

  it("Remove asks first, removes, and puts focus on the next control", async () => {
    let listing = LISTING;
    const request = vi.fn(async (url, options) => {
      if (options?.method === "DELETE") { listing = { ...LISTING, song: [] }; return { deleted: ITEM.id }; }
      return listing;
    });
    const editor = install(request);
    editor.load(RECORD);
    await settle();
    const remove = el("intros-song-list").querySelectorAll("button")[1];
    remove.click();
    await settle();
    expect(el("confirm-dialog").open).toBe(true);
    expect(el("confirm-title").textContent).toBe("Remove intro Brand new?");
    el("confirm-accept").click();
    await settle();
    await settle();
    await settle();
    expect(request).toHaveBeenCalledWith(`/api/intros/${ITEM.id}`, { method: "DELETE" });
    expect(el("intros-song-list").children).toHaveLength(0);
    expect(el("intros-song-empty").hidden).toBe(false);
    expect(document.activeElement).toBe(el("intros-song-heading"));
    expect(el("intros-status").textContent).toBe("Removed Brand new.");
  });

  it("clears and hides itself", async () => {
    const editor = install(vi.fn(async () => LISTING));
    editor.load(RECORD);
    await settle();
    editor.clear();
    expect(el("intros-editor").hidden).toBe(true);
    expect(el("intros-song-list").children).toHaveLength(0);
  });
});
