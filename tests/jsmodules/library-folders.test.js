import { beforeEach, describe, expect, it, vi } from "vitest";
import { formatPointTime, installLibraryFolders, parsePointTime } from "../../src/autodj/static/modules/library-folders.js";

function mount() {
  document.body.innerHTML = `
    <button id="tab-now">Now Playing</button><section id="panel-now" hidden></section>
    <section id="panel-library" hidden>
      <input id="source-folder-path"><select id="source-folder-kind"><option value="music">Music</option><option value="liner">Liner</option></select>
      <input id="source-folder-recursive" type="checkbox" checked><input id="source-folder-advanced" type="checkbox" checked>
      <button id="source-folder-add"></button><button id="source-folder-rescan"></button>
      <p id="source-folders-status"></p><ul id="source-folders"></ul>
    </section>
    <button id="edit-current-points"></button><section id="points-editor" hidden>
      <input id="points-path"><button id="points-load"></button>
      ${["start", "intro", "outro", "segue", "end", "overlap_start"].map((name) => `<label><input data-point="${name}"><span data-point-source="${name}"></span></label><button data-preview="${name}"></button><button data-set-current="${name}"></button>`).join("")}
      <audio id="points-preview"></audio>
      <button id="points-save"></button><p id="points-status"></p>
    </section>`;
  return document;
}

beforeEach(() => { mount(); });
document.addEventListener("click", (event) => {
  if (event.target.id === "tab-now") document.getElementById("panel-now").hidden = false;
});

describe("clock folder and points controls", () => {
  it("parses seconds and MM:SS while preserving empty points", () => {
    expect(parsePointTime("5.3")).toBe(5.3);
    expect(parsePointTime("1:05.5")).toBe(65.5);
    expect(parsePointTime("")).toBeNull();
    expect(formatPointTime(65.5)).toBe("1:05.5");
    expect(() => parsePointTime("1:75")).toThrow();
  });

  it("posts folder choices and displays the returned import status", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async (url, options) => url === "/api/library/folders" && !options ? {
      folders: [{ id: 4, path: "C:/music", kind: "music", recursive: true, advanced: true }],
      tracks_ready: 12, tracks_indexed: 8,
    } : {});
    const controls = installLibraryFolders({ document, request });
    document.getElementById("source-folder-path").value = "C:/music";
    document.getElementById("source-folder-add").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders", expect.objectContaining({ method: "POST" }));
    expect(document.getElementById("source-folders").textContent).toContain("C:/music");
    expect(document.getElementById("source-folders-status").textContent).toContain("12 tracks ready");
    controls.destroy();
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
      ? { path: "N:/voice/same.wav", kind: "liner", length: 4, points: { start: 0.2, end: 3.8 } }
      : {});
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", {
      detail: { path: "N:/voice/same.wav", kind: "liner" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("points-editor").hidden).toBe(false);
    expect(document.querySelector('[data-point="intro"]').closest("label").hidden).toBe(true);
    expect(document.querySelector('[data-point="segue"]').closest("label").hidden).toBe(true);
    expect(document.querySelector('[data-point="overlap_start"]').value).toBe("0:00");
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const save = request.mock.calls.find(([url]) => url === "/api/library/points");
    expect(JSON.parse(save[1].body).points).toEqual({ start: 0.2, end: 3.8, overlap_start: 0 });
  });
});
