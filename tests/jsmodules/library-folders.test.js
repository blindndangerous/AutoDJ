import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatPointTime, installLibraryFolders as installControls, parsePointTime } from "../../src/autodj/static/modules/library-folders.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";

const installed = [];
function installLibraryFolders(options) {
  const controls = installControls(options);
  installed.push(controls);
  return controls;
}
afterEach(() => { for (const controls of installed.splice(0)) controls.destroy(); });

function mount() {
  document.body.innerHTML = `
    <button id="tab-now">Now Playing</button><section id="panel-now" hidden></section>
    <button id="tab-track-info">Track info</button>
    <section id="panel-track-info" hidden><h2 id="track-info-heading" tabindex="-1">Track info</h2></section>
    <p id="track-info-empty">Choose a track</p><dl id="track-info-details" hidden></dl>
    <section id="panel-library" hidden>
      <input id="source-folder-path"><select id="source-folder-kind"><option value="music">Music</option><option value="liner">Liner</option></select>
      <input id="source-folder-recursive" type="checkbox" checked><input id="source-folder-advanced" type="checkbox" checked>
      <button id="source-folder-add"></button><button id="source-folder-rescan"></button>
      <p id="source-folders-status"></p><ul id="source-folders"></ul>
    </section>
    <button id="edit-current-points"></button><section id="points-editor" hidden>
      <h3 id="points-heading">Song points</h3><p id="points-liner-help" hidden></p><label id="points-allow-outro-label" hidden><input id="points-allow-outro" type="checkbox"> Allow start over outro</label>
      <input id="points-path"><button id="points-load"></button>
      ${["start", "intro", "outro", "segue", "end", "overlap_start"].map((name) => `<label><input data-point="${name}"><span data-point-source="${name}"></span></label><button data-preview="${name}"></button><button data-set-current="${name}"></button>`).join("")}
      <audio id="points-preview"></audio>
      <button id="points-save"></button><p id="points-status"></p>
    </section>
    <dialog id="track-info-dialog" aria-labelledby="track-info-dialog-title">
      <h2 id="track-info-dialog-title"></h2><p id="track-info-error" role="status"></p>
      <button id="track-info-save" type="button">Save</button><button id="track-info-discard" type="button">Discard</button><button id="track-info-keep" type="button">Keep editing</button>
    </dialog>`;
  const dialog = document.getElementById("track-info-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
  return document;
}

beforeEach(() => { mount(); });
document.addEventListener("click", (event) => {
  if (event.target.id === "tab-now") document.getElementById("panel-now").hidden = false;
  if (event.target.id === "tab-track-info") document.getElementById("panel-track-info").hidden = false;
});

describe("clock folder and points controls", () => {
  it("parses seconds and MM:SS while preserving empty points", () => {
    expect(parsePointTime("5.3")).toBe(5.3);
    expect(parsePointTime("1:05.5")).toBe(65.5);
    expect(parsePointTime("")).toBeNull();
    expect(formatPointTime(65.5)).toBe("1:05.5");
    expect(() => parsePointTime("1:75")).toThrow();
  });

  it("rejects nonfinite cue input and carries rounded centiseconds", () => {
    expect(() => parsePointTime("9".repeat(400))).toThrow();
    expect(formatPointTime(8.999)).toBe("0:09");
    expect(formatPointTime(59.999)).toBe("1:00");
  });

  it("announces an added folder once and keeps polling progress outside the live region", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async (url, options) => url === "/api/library/folders" && !options ? {
      folders: [{ id: 4, path: "C:/music", kind: "music", recursive: true, advanced: true }],
      import: { running: true, phase: "scan", done: 2, total: 10, current: "track.flac" },
    } : {});
    const controls = installLibraryFolders({ document, request });
    const status = document.getElementById("source-folders-status");
    const progress = document.getElementById("source-folders-progress");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.getAttribute("aria-atomic")).toBe("true");
    expect(progress.hasAttribute("role")).toBe(false);
    expect(progress.hasAttribute("aria-live")).toBe(false);
    document.getElementById("source-folder-path").value = "C:/music";
    document.getElementById("source-folder-add").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(request.mock.calls[1][1].body).path).toBe("C:/music");
    expect(document.getElementById("source-folders").textContent).toContain("C:/music");
    expect(status.textContent).toBe("Folder added; import queued for C:/music.");
    expect(progress.textContent).toContain("Importing tracks (scan): 2 of 10");
    await controls.refresh();
    expect(status.textContent).toBe("Folder added; import queued for C:/music.");
    expect(progress.textContent).toContain("track.flac");
    controls.destroy();
  });

  it("removes surrounding quotes from pasted Windows paths and preserves literal Unix quotes", async () => {
    const request = vi.fn(async () => ({}));
    installLibraryFolders({ document, request });
    const pathInput = document.getElementById("source-folder-path");
    const add = document.getElementById("source-folder-add");

    pathInput.value = '  "C:\\station\\specialty\\holiday"  ';
    add.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[0][1].body).path).toBe("C:\\station\\specialty\\holiday");

    pathInput.value = '/music/"live"';
    add.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[1][1].body).path).toBe('/music/"live"');

    pathInput.value = '"\\\\server\\share"';
    add.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[2][1].body).path).toBe("\\\\server\\share");

    pathInput.value = "/music/'live'";
    add.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[3][1].body).path).toBe("/music/'live'");
  });

  it("keeps Add failures visible through refresh and associates them with the path field", async () => {
    const request = vi.fn(async (_url, options) => {
      if (options?.method === "POST") throw new Error("Folder is unavailable");
      return { folders: [], tracks_ready: 0, tracks_indexed: 0 };
    });
    const controls = installLibraryFolders({ document, request });
    const pathInput = document.getElementById("source-folder-path");
    pathInput.value = "C:/missing";
    document.getElementById("source-folder-add").click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(pathInput.getAttribute("aria-invalid")).toBe("true");
    expect(pathInput.getAttribute("aria-describedby")).toBe("source-folders-status");
    expect(document.activeElement).toBe(pathInput);
    document.getElementById("panel-library").hidden = false;
    await controls.refresh();
    expect(request).toHaveBeenCalledWith("/api/library/folders");
    await vi.waitFor(() => expect(document.getElementById("source-folders-status").textContent).toContain("Folder is unavailable"));
    expect(document.getElementById("source-folders-status").getAttribute("role")).toBe("status");

    pathInput.value = "C:/fixed";
    pathInput.dispatchEvent(new Event("input", { bubbles: true }));
    expect(pathInput.hasAttribute("aria-invalid")).toBe(false);
    expect(pathInput.hasAttribute("aria-describedby")).toBe(false);
  });

  it("saves changed folder scan choices and removes the source", async () => {
    const request = vi.fn(async (url) => url === "/api/library/folders" ? { folders: [{ id: "abc", path: "M:/", kind: "music", recursive: false, advanced: false }] } : {});
    installLibraryFolders({ document, request });
    await request.mock.results[0]?.value;
    const list = document.getElementById("source-folders");
    list.innerHTML = `<li data-id="abc"><input type="checkbox" data-field="recursive"></li><li data-id="abc"><button data-action="remove"></button></li>`;
    const recursion = list.querySelector("input");
    recursion.checked = true;
    recursion.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders/abc", expect.objectContaining({ method: "PATCH" }));
    list.querySelector("button").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders/abc", { method: "DELETE" });
  });

  it("holds a folder checkbox while its update is in flight", async () => {
    document.getElementById("source-folders").innerHTML = '<li data-id="abc"><input type="checkbox" data-field="recursive"></li>';
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    installLibraryFolders({ document, request });
    const checkbox = document.querySelector('[data-field="recursive"]');
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    expect(checkbox.disabled).toBe(true);
    expect(request).toHaveBeenCalledTimes(1);
    finish({});
    await vi.waitFor(() => expect(checkbox.disabled).toBe(false));
  });

  it("keeps folder-control focus across unchanged polls and restores focus if a folder disappears", async () => {
    document.getElementById("panel-library").hidden = false;
    let folders = [{ id: "abc", path: "M:/", kind: "music", recursive: false, advanced: false }];
    const request = vi.fn(async () => ({ folders }));
    const controls = installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const checkbox = document.querySelector('[data-field="recursive"]');
    checkbox.focus();
    await controls.refresh();
    expect(document.activeElement).toBe(checkbox);
    folders = [];
    await controls.refresh();
    expect(document.activeElement.id).toBe("source-folder-add");
    controls.destroy();
  });

  it("loads the Browse library folder choice once each time Queue & Search is shown", async () => {
    vi.useFakeTimers();
    try {
      const queue = document.createElement("section");
      queue.dataset.view = "queue";
      queue.id = "panel-queue";
      queue.hidden = true;
      queue.innerHTML = '<select id="library-browse-folder"><option value="all-music">All music</option></select>';
      document.body.append(queue);
      const folders = [{ id: 3, path: "M:/rock", kind: "music", recursive: true, advanced: true }];
      const request = vi.fn(async () => ({ folders }));
      const onFolders = vi.fn();
      installLibraryFolders({ document, request, onFolders });
      expect(request).not.toHaveBeenCalled();

      queue.hidden = false;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(request).toHaveBeenCalledTimes(1);
      expect(onFolders).toHaveBeenCalledWith(folders);
      // No polling while the tab stays open, and no second load for a
      // hashchange that leaves it shown.
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(10000);
      expect(request).toHaveBeenCalledTimes(1);

      queue.hidden = true;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      queue.hidden = false;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(request).toHaveBeenCalledTimes(2);
      // Same folders: the choice is not rebuilt under the user's focus.
      expect(onFolders).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("updates browser folders only when the registered folder signature changes", async () => {
    document.getElementById("panel-library").hidden = false;
    const folders = [{ id: 2, path: "N:/voice", kind: "liner", recursive: true, advanced: false }];
    const onFolders = vi.fn();
    const controls = installLibraryFolders({ document, request: vi.fn(async () => ({ folders })), onFolders });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onFolders).toHaveBeenCalledTimes(1);
    expect(onFolders).toHaveBeenCalledWith(folders);
    await controls.refresh();
    expect(onFolders).toHaveBeenCalledTimes(1);
    controls.destroy();
  });

  it("loads music provenance, hides overlap start, previews the source file, and saves values", async () => {
    const request = vi.fn(async (url) => url.startsWith("/api/library/points?")
      ? { path: "M:/song.flac", kind: "music", length: 200, points: { start: 1.5, intro: 12, segue: 170, end: 190 }, point_sources: { intro: "detected", segue: "user" } }
      : {});
    installLibraryFolders({ document, request, getCurrentTrack: () => ({ path: "M:/song.flac" }) });
    const preview = document.getElementById("points-preview");
    Object.defineProperty(preview, "play", { configurable: true, value: vi.fn(async () => {}) });
    document.getElementById("edit-current-points").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector('[data-point="intro"]').value).toBe("0:12");
    expect(document.querySelector('[data-point-source="intro"]').textContent).toBe(" (detected)");
    expect(document.querySelector('[data-point="overlap_start"]').closest("label").hidden).toBe(true);
    expect(preview.getAttribute("src")).toBe("/api/audio?path=M%3A%2Fsong.flac");
    document.querySelector('[data-point="segue"]').value = "2:50";
    document.querySelector('[data-preview="segue"]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(preview.currentTime).toBe(170);
    expect(preview.play).toHaveBeenCalledOnce();
    document.querySelector('[data-set-current="intro"]').click();
    expect(document.querySelector('[data-point="intro"]').value).toBe("2:50");
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/points", expect.objectContaining({ method: "POST" }));
  });

  it("opens liner points from a folder row event and exposes only its source window", async () => {
    const request = vi.fn(async (url) => url.startsWith("/api/library/points?")
      ? { path: "N:/voice/same.wav", kind: "liner", length: 4, points: { start: 0.2, segue: 2.6, end: 3.8 } }
      : {});
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", {
      detail: { path: "N:/voice/same.wav", kind: "liner" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("points-editor").hidden).toBe(false);
    expect(document.getElementById("points-heading").textContent).toBe("Cue points");
    expect(document.getElementById("points-liner-help").hidden).toBe(false);
    expect(document.querySelector('[data-point="intro"]').closest("label").hidden).toBe(true);
    expect(document.querySelector('[data-point="outro"]').closest("label").hidden).toBe(true);
    expect(document.querySelector('[data-point="segue"]').closest("label").hidden).toBe(false);
    expect(document.querySelector('[data-point="overlap_start"]').value).toBe("");
    expect(document.getElementById("points-allow-outro-label").hidden).toBe(false);
    expect(document.getElementById("points-allow-outro").checked).toBe(false);
    const preview = document.getElementById("points-preview");
    Object.defineProperty(preview, "play", { configurable: true, value: vi.fn(async () => {}) });
    document.querySelector('[data-preview="overlap_start"]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(preview.currentTime).toBe(2.6);
    expect(preview.play).toHaveBeenCalledOnce();
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const save = request.mock.calls.find(([url]) => url === "/api/library/points");
    expect(JSON.parse(save[1].body)).toEqual({ path: "N:/voice/same.wav", points: { start: 0.2, segue: 2.6, end: 3.8, overlap_start: null }, allow_outro: false });
  });

  it("treats liner overlap start as relative to saved Start for preview and setting current", async () => {
    const request = vi.fn(async (url) => url.startsWith("/api/library/points?")
      ? { path: "N:/voice/same.wav", kind: "liner", length: 8, points: { start: 1.5, end: 7, overlap_start: 2.5 } }
      : {});
    installLibraryFolders({ document, request, getCurrentTrack: () => ({ path: "N:/voice/same.wav" }), getCurrentPosition: () => 5.25 });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", {
      detail: { path: "N:/voice/same.wav", kind: "liner" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const preview = document.getElementById("points-preview");
    Object.defineProperty(preview, "play", { configurable: true, value: vi.fn(async () => {}) });
    document.querySelector('[data-preview="overlap_start"]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(preview.currentTime).toBe(4);
    preview.currentTime = 5.25;
    document.querySelector('[data-set-current="overlap_start"]').click();
    expect(document.querySelector('[data-point="overlap_start"]').value).toBe("0:03.75");
    document.querySelector('[data-point="start"]').value = "invalid";
    document.querySelector('[data-set-current="overlap_start"]').click();
    expect(document.getElementById("points-status").textContent).toContain("Enter seconds or minutes:seconds");
  });

  it("saves the liner outro option and keeps it checked when saved points reload", async () => {
    const request = vi.fn(async (url) => url === "/api/library/points"
      ? { points: { start: 0, segue: 5, end: 8, overlap_start: null } }
      : { path: "N:/voice/same.wav", kind: "liner", allow_outro: true, points: { start: 0, segue: 5, end: 8 } });
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "N:/voice/same.wav", kind: "liner" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const allowOutro = document.getElementById("points-allow-outro");
    expect(allowOutro.checked).toBe(true);
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls.find(([url]) => url === "/api/library/points")[1].body).allow_outro).toBe(true);
    expect(allowOutro.checked).toBe(true);
    expect(document.querySelector('[data-point="overlap_start"]').value).toBe("");
  });

  function trackRequest() {
    return vi.fn(async (url) => ({
      path: decodeURIComponent(url.split("path=")[1] || "M:/one.flac"),
      title: url.includes("two") ? "Two" : "One", artist: "Artist", kind: "music",
      last_played: 172800, analysis: "ready", loudness_ready: true,
      points: { intro: 8, end: 180 }, point_sources: { intro: "analysis" },
    }));
  }

  it("shows metadata safely and keeps drafts when revisiting the selected track", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    expect(document.getElementById("panel-track-info").hidden).toBe(false);
    expect(document.getElementById("panel-now").hidden).toBe(true);
    const details = document.getElementById("track-info-details");
    expect(details.textContent).toContain("Last played");
    expect(details.textContent).toContain("days ago");
    expect(details.textContent).toContain("Complete");
    document.querySelector('[data-point="intro"]').value = "0:10";
    document.getElementById("panel-track-info").hidden = true;
    await controls.openEditor("M:/one.flac");
    expect(document.querySelector('[data-point="intro"]').value).toBe("0:10");
    expect(request).toHaveBeenCalledTimes(1);
    expect(document.getElementById("track-info-dialog").open).toBe(false);
  });

  it("announces when a History recording has left the library", async () => {
    const missing = new Error("Recording not in library");
    missing.status = 404;
    const controls = installLibraryFolders({ document, request: vi.fn(async () => { throw missing; }) });
    await controls.openEditor("M:/removed.flac");
    expect(document.getElementById("panel-track-info").hidden).toBe(false);
    await vi.waitFor(() => expect(document.getElementById("points-status").textContent).toContain("no longer in the library"));
    expect(document.getElementById("points-editor").hidden).toBe(true);
  });

  it("saves the old track before switching and names it in the modal", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    const switching = controls.openEditor("M:/two.flac");
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    expect(document.getElementById("track-info-dialog-title").textContent).toBe("Save changes to Artist — One?");
    document.getElementById("track-info-save").click();
    await switching;
    expect(request.mock.calls.map(([url]) => url)).toEqual([
      "/api/library/points?path=M%3A%2Fone.flac", "/api/library/points", "/api/library/points?path=M%3A%2Ftwo.flac",
    ]);
    const body = JSON.parse(request.mock.calls[1][1].body);
    expect(body.path).toBe("M:/one.flac");
    expect(body.points.intro).toBe(10);
    expect(document.getElementById("points-path").value).toBe("M:/two.flac");
  });

  it("loads the tags with the track, saves unsaved tags before switching and updates the details", async () => {
    const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
    document.getElementById("panel-track-info").insertAdjacentHTML("beforeend", html.match(/<section id="tags-editor"[\s\S]*?<\/section>/)[0]);
    const tagsOf = (title) => ({ title, artist: "Artist", album: "", genre: "", year: "", bpm: "", key: "", isrc: "" });
    const request = vi.fn(async (url, options) => {
      if (url === "/api/library/tags") {
        const body = JSON.parse(options.body);
        return { path: body.path, title: body.tags.title, artist: "Artist", kind: "music", points: {}, tags: tagsOf(body.tags.title), tags_writable: true, beets: "off" };
      }
      const two = url.includes("two");
      return { path: decodeURIComponent(url.split("path=")[1]), title: two ? "Two" : "One", artist: "Artist", kind: "music", points: { end: 180 }, tags: tagsOf(two ? "Two" : "One"), tags_writable: true };
    });
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    const title = document.getElementById("tag-title");
    expect(document.getElementById("tags-editor").hidden).toBe(false);
    expect(title.value).toBe("One");
    title.value = "Uno";
    const switching = controls.openEditor("M:/two.flac");
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    document.getElementById("track-info-save").click();
    await switching;
    const urls = request.mock.calls.map(([url]) => url);
    expect(urls).toEqual(["/api/library/points?path=M%3A%2Fone.flac", "/api/library/tags", "/api/library/points?path=M%3A%2Ftwo.flac"]);
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ path: "M:/one.flac", tags: { title: "Uno" }, beets: false });
    expect(title.value).toBe("Two");
    // Saving here updates the details and the heading in place.
    title.value = "Dos";
    document.getElementById("tags-save").click();
    await vi.waitFor(() => expect(request.mock.calls.map(([url]) => url)).toHaveLength(4));
    await vi.waitFor(() => expect(document.getElementById("track-info-heading").textContent).toBe("Track info: Artist — Dos"));
    expect(document.getElementById("track-info-details").textContent).toContain("Dos");
    controls.clearProtectedData();
    expect(document.getElementById("tags-editor").hidden).toBe(true);
    expect(title.value).toBe("");
  });

  it("keeps editing like Escape: no save, the draft kept and focus back on the opener", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    const opener = document.createElement("button");
    opener.textContent = "Track info for Two";
    document.getElementById("panel-now").append(opener);
    opener.focus();
    const switching = controls.openEditor("M:/two.flac", "music", opener);
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    document.getElementById("track-info-save").focus();
    document.getElementById("track-info-keep").click();
    await switching;
    expect(document.getElementById("track-info-dialog").open).toBe(false);
    expect(document.getElementById("track-info-dialog").returnValue).toBe("keep");
    expect(request).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-point="intro"]').value).toBe("0:10");
    expect(document.getElementById("points-path").value).toBe("M:/one.flac");
    expect(document.activeElement).toBe(opener);
  });

  it("holds Keep editing while a save is in flight", async () => {
    let finish;
    const request = vi.fn((url, options) => options?.method === "POST"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ path: "M:/one.flac", title: "One", kind: "music", points: { end: 180 } }));
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-save").click();
    const keep = document.getElementById("track-info-keep");
    expect(keep.disabled).toBe(true);
    keep.click();
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    finish({ points: { intro: 10, end: 180 } });
    await switching;
    expect(keep.disabled).toBe(false);
    expect(document.getElementById("track-info-dialog").returnValue).toBe("save");
  });

  it("discards a liner draft without saving, and offers the same liner fields", async () => {
    const request = vi.fn(async (url) => ({ path: decodeURIComponent(url.split("path=")[1]), title: "Test jingle", kind: "liner", points: { start: 0, segue: 5.3, end: 8.8, overlap_start: 5.3 } }));
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("N:/jingle.wav", "liner");
    document.querySelector('[data-point="overlap_start"]').value = "0:05.6";
    const switching = controls.openEditor("N:/stinger.wav", "liner");
    expect(document.getElementById("track-info-dialog-title").textContent).toContain("Test jingle");
    document.getElementById("track-info-discard").click();
    await switching;
    expect(request.mock.calls.every(([, options]) => !options)).toBe(true);
    expect(document.getElementById("points-path").value).toBe("N:/stinger.wav");
    expect(document.querySelector('[data-point="overlap_start"]').closest("label").hidden).toBe(false);
    expect(document.getElementById("track-info-details").textContent).toContain("Not needed for liners");
  });

  it("keeps the original draft after a failed save or Escape", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    request.mockRejectedValueOnce(new Error("Offline"));
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-save").click();
    await vi.waitFor(() => expect(document.getElementById("track-info-error").textContent).toContain("Offline"));
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    expect(document.getElementById("points-path").value).toBe("M:/one.flac");
    expect(document.querySelector('[data-point="intro"]').value).toBe("0:10");
    document.getElementById("track-info-dialog").close();
    await switching;
    expect(request).toHaveBeenCalledTimes(2);
    expect(document.getElementById("points-path").value).toBe("M:/one.flac");
  });

  it("prevents concurrent switches and keeps draft values when loading the replacement fails", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    request.mockRejectedValueOnce(new Error("Missing file"));
    const switching = controls.openEditor("M:/two.flac");
    await controls.openEditor("M:/three.flac");
    document.getElementById("track-info-discard").click();
    await switching;
    expect(request).toHaveBeenCalledTimes(2);
    expect(document.getElementById("points-path").value).toBe("M:/one.flac");
    expect(document.querySelector('[data-point="intro"]').value).toBe("0:10");
    await vi.waitFor(() => expect(document.getElementById("points-status").textContent).toContain("Missing file"));
  });

  it("holds Save, Discard and Escape while the save is in flight", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    let finishSave;
    request.mockImplementationOnce(() => new Promise(resolve => { finishSave = resolve; }));
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-save").click();
    expect(document.getElementById("track-info-save").disabled).toBe(true);
    expect(document.getElementById("track-info-discard").disabled).toBe(true);
    const escape = new Event("cancel", { cancelable: true });
    document.getElementById("track-info-dialog").dispatchEvent(escape);
    expect(escape.defaultPrevented).toBe(true);
    expect(request).toHaveBeenCalledTimes(2);
    finishSave({});
    await switching;
    expect(document.getElementById("points-path").value).toBe("M:/two.flac");
  });

  it("does not expose a stale detail response after authentication changes", async () => {
    let respond;
    const controls = installLibraryFolders({ document, request: () => new Promise(resolve => { respond = resolve; }) });
    const opening = controls.openEditor("M:/one.flac");
    invalidateAuthenticatedRequestEpoch();
    respond({ path: "M:/one.flac", title: "Private title", points: {} });
    await opening;
    expect(document.getElementById("points-editor").hidden).toBe(true);
    expect(document.getElementById("track-info-details").textContent).not.toContain("Private title");
  });

  it("clears protected paths, cue drafts, metadata and preview audio on auth reset", async () => {
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    const preview = document.getElementById("points-preview");
    preview.pause = vi.fn();
    preview.load = vi.fn();
    preview.src = "/api/audio?path=private";
    document.querySelector('[data-point="intro"]').value = "0:10";
    controls.clearProtectedData();
    expect(document.getElementById("points-editor").hidden).toBe(true);
    expect(document.getElementById("points-path").value).toBe("");
    expect(document.getElementById("track-info-details").textContent).toBe("");
    expect(document.getElementById("track-info-dialog-title").textContent).toBe("");
    expect(document.querySelector('[data-point="intro"]').value).toBe("");
    expect(preview.pause).toHaveBeenCalled();
    expect(preview.hasAttribute("src")).toBe(false);
  });

  it("ignores a track detail response that arrives after auth reset", async () => {
    let respond;
    const controls = installLibraryFolders({ document, request: () => new Promise(resolve => { respond = resolve; }) });
    const opening = controls.openEditor("M:/one.flac");
    controls.clearProtectedData();
    respond({ path: "M:/one.flac", title: "Private title", points: {} });
    await opening;
    expect(document.getElementById("points-editor").hidden).toBe(true);
    expect(document.getElementById("track-info-details").textContent).not.toContain("Private title");
  });

  it("ignores a folder refresh that arrives after auth reset", async () => {
    document.getElementById("panel-library").hidden = false;
    let respond;
    const controls = installLibraryFolders({ document, request: () => new Promise(resolve => { respond = resolve; }) });
    controls.clearProtectedData();
    respond({ folders: [{ id: 1, path: "M:/private", kind: "music" }] });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("source-folders").textContent).toBe("");
    expect(document.getElementById("source-folders-status").textContent).toBe("");
    expect(document.getElementById("source-folders-progress").textContent).toBe("");
  });

  it("keeps the cached older page usable until its server restarts", async () => {
    for (const id of ["tab-track-info", "panel-track-info", "track-info-details", "track-info-dialog"]) document.getElementById(id).remove();
    document.getElementById("points-allow-outro-label").remove();
    const request = vi.fn(async (url) => url === "/api/library/points"
      ? { points: { start: 0, segue: 5, end: 8 } }
      : { path: decodeURIComponent(url.split("path=")[1]), title: "Old liner", kind: "liner", points: { start: 0, segue: 5, end: 8 } });
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("N:/one.wav", "liner");
    expect(document.getElementById("panel-now").hidden).toBe(false);
    expect(document.getElementById("points-editor").hidden).toBe(false);
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const save = request.mock.calls.find(([url]) => url === "/api/library/points");
    expect(JSON.parse(save[1].body)).not.toHaveProperty("allow_outro");
    document.querySelector('[data-point="segue"]').value = "0:04";
    await controls.openEditor("N:/two.wav", "liner");
    expect(document.getElementById("points-path").value).toBe("N:/one.wav");
    expect(document.getElementById("points-status").textContent).toContain("Save your changes");
  });
});
