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
    <button id="tab-queue">Queue & Search</button><section id="panel-queue" data-view="queue" hidden><select id="library-kind"><option value="music">Music</option></select></section>
    <button id="tab-track-info">Track info</button>
    <section id="panel-track-info" hidden><h2 id="track-info-heading" tabindex="-1">Track info</h2></section>
    <p id="track-info-empty">Choose a track</p><dl id="track-info-details" hidden></dl>
    <section id="panel-library" hidden>
      <input id="source-folder-path"><select id="source-folder-kind"><option value="music">Music</option><option value="liner">Liner</option></select>
      <input id="source-folder-recursive" type="checkbox" checked><select id="source-folder-analysis"><option value="basic">Basic</option><option value="audio" selected>Audio</option></select>
      <input id="source-folder-override-tags" type="checkbox" aria-describedby="source-folder-override-tags-help"><p id="source-folder-override-tags-help">Rescan refreshes populated file tags. Hand-edited BPM and key values and cue points are retained.</p><button id="source-folder-add"></button><button id="source-folder-rescan"></button>
      <p id="source-folders-status"></p><p id="source-folders-progress"></p><table id="source-folders-table"><caption>Registered music and voice liner folders</caption><tbody id="source-folders"></tbody></table>
    </section>
    <button id="edit-current-points"></button><section id="points-editor" hidden>
      <h3 id="points-heading">Song points</h3><p id="points-liner-help" hidden></p><small id="negative-overlap-help" hidden>A negative value delays the liner. Preview and Use preview position are unavailable for negative values.</small><label id="points-allow-outro-label" hidden><input id="points-allow-outro" type="checkbox"> Allow start over outro</label>
      <input id="points-path"><button id="points-load"></button>
      ${["start", "intro", "outro", "segue", "end", "overlap_start"].map((name) => `<label><input data-point="${name}"><span data-point-source="${name}"></span></label><button data-preview="${name}"></button><button data-set-current="${name}"></button>`).join("")}
      <audio id="points-preview"></audio>
      <button id="points-save"></button><button id="points-rescan"></button><p id="points-rescan-status" role="status" aria-live="polite"></p><section id="points-rescan-results" hidden><h4 id="points-rescan-heading" tabindex="-1"></h4><div id="points-rescan-comparison"></div><button id="points-rescan-apply"></button><button id="points-rescan-discard"></button></section><p id="points-status"></p>
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
  it("names folder tables by caption and matches the queue column to its action", () => {
    const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const page = document.createElement("template");
    page.innerHTML = html;
    expect(page.content.querySelector("#source-folders").hasAttribute("aria-label")).toBe(false);
    expect(page.content.querySelector("#source-folders-table caption").textContent).toBe("Registered music and voice liner folders");
    expect(page.content.querySelectorAll("#library-results-table th")[3].textContent).toBe("Add to queue");
  });

  it("sends the embedded-tag override only when checked", async () => {
    const request = vi.fn(async () => ({}));
    installLibraryFolders({ document, request });
    const rescan = document.getElementById("source-folder-rescan");

    expect(document.getElementById("source-folder-override-tags").checked).toBe(false);
    rescan.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ override_tags: false });
    await vi.waitFor(() => expect(document.getElementById("source-folders-status").textContent).toBe("Rescan started."));

    document.getElementById("source-folder-override-tags").checked = true;
    rescan.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ override_tags: true });
    await vi.waitFor(() => expect(document.getElementById("source-folders-status").textContent).toBe("Rescan started; populated file tags will be refreshed."));
  });

  it("associates the embedded-tag override with its help in the page markup", () => {
    const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
    expect(html).toContain('id="source-folder-override-tags" type="checkbox" aria-describedby="source-folder-override-tags-help"');
    expect(html).toContain('id="source-folder-override-tags-help">Rescan refreshes populated file tags. Hand-edited BPM and key values and cue points are retained.</p>');
  });

  // ADR-0012-08: capability changes preserve the saved choice.
  it("offers similarity only when available and preserves unavailable existing folders", async () => {
    const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const page = document.createElement("template");
    page.innerHTML = html;
    expect(page.content.querySelector("#source-folder-analysis-group .setting-desc").textContent.trim())
      .toBe("Uses the MuQ music model to help AutoDJ choose songs that sound alike.");
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async () => ({
      similarity_available: false,
      folders: [{ id: "old", path: "M:/old", kind: "music", recursive: false, analysis: "similarity" }],
    }));
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...document.getElementById("source-folder-analysis").options].map(({ value }) => value)).toEqual(["basic", "audio"]);
    const existing = document.querySelector('#source-folders select[data-field="analysis"]');
    expect(existing.value).toBe("similarity");
    expect(existing.getAttribute("aria-label")).toBe("Analysis for M:/old");
    expect(existing.selectedOptions[0].textContent).toBe("Audio + similarity (unavailable)");
    expect(existing.selectedOptions[0].disabled).toBe(true);
  });

  it("announces the shared analysis limit from Settings while Library tools is hidden", async () => {
    const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const page = document.createElement("template");
    page.innerHTML = html;
    const settings = page.content.querySelector("#panel-settings").cloneNode(true);
    settings.hidden = false;
    settings.querySelector("#spanel-stations").hidden = false;
    document.body.append(settings);
    document.body.insertAdjacentHTML("beforeend", '<p id="sr-status" role="status" aria-live="polite"></p><div id="status-toast-box" hidden><div id="status-toast" aria-hidden="true" hidden></div></div>');
    expect(document.getElementById("panel-library").hidden).toBe(true);
    let finish;
    let failSave = false;
    const request = vi.fn((url, options) => {
      if (url === "/api/library/advanced-length" && !options) return Promise.resolve({ minutes: 20 });
      if (options?.method === "PUT") {
        if (failSave) return Promise.reject(Object.assign(new Error("Server unavailable"), { status: 503 }));
        return new Promise((resolve) => { finish = resolve; });
      }
      return Promise.resolve({ folders: [] });
    });
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("advanced-length").value).toBe("20");

    const input = document.getElementById("advanced-length");
    input.value = "30";
    document.getElementById("advanced-length-save").click();
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(document.getElementById("sr-status").textContent).toBe("Saving the similarity analysis limit…");
    expect(document.getElementById("status-toast").textContent).toBe("Saving the similarity analysis limit…");
    finish({ minutes: 30 });
    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toBe("Saved. Songs longer than 30 minutes get audio analysis only."));
    expect(document.getElementById("status-toast").textContent).toBe("Saved. Songs longer than 30 minutes get audio analysis only.");
    expect(document.getElementById("source-folders-status").textContent).toBe("");

    failSave = true;
    input.value = "40";
    document.getElementById("advanced-length-save").click();
    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toBe("Could not save the similarity analysis limit: Server unavailable"));
    expect(document.getElementById("status-toast").textContent).toBe("Could not save the similarity analysis limit: Server unavailable");
  });

  it("reconciles existing folder choices as similarity availability changes without losing focus", async () => {
    document.getElementById("panel-library").hidden = false;
    let available = false;
    const folders = [
      { id: "basic", path: "M:/basic", kind: "music", recursive: false, analysis: "basic" },
      { id: "similarity", path: "M:/similarity", kind: "music", recursive: false, analysis: "similarity" },
    ];
    const request = vi.fn(async () => ({ similarity_available: available, folders }));
    const controls = installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const basic = document.querySelector('tr[data-id="basic"] select');
    const similarity = document.querySelector('tr[data-id="similarity"] select');
    basic.focus();
    expect(basic.querySelector('option[value="similarity"]')).toBeNull();
    expect(similarity.selectedOptions[0].disabled).toBe(true);

    available = true;
    await controls.refresh();
    expect(document.querySelector('tr[data-id="basic"] select')).toBe(basic);
    expect(basic.querySelector('option[value="similarity"]').disabled).toBe(false);
    expect(similarity.selectedOptions[0].disabled).toBe(false);
    expect(document.activeElement).toBe(basic);

    available = false;
    await controls.refresh();
    expect(document.querySelector('tr[data-id="basic"] select')).toBe(basic);
    expect(basic.querySelector('option[value="similarity"]')).toBeNull();
    expect(similarity.value).toBe("similarity");
    expect(similarity.selectedOptions[0].disabled).toBe(true);
    expect(similarity.selectedOptions[0].textContent).toContain("unavailable");
    expect(document.activeElement).toBe(basic);

    available = true;
    await controls.refresh();
    expect(document.querySelector('tr[data-id="basic"] select')).toBe(basic);
    expect(basic.querySelector('option[value="similarity"]').disabled).toBe(false);
    expect(similarity.selectedOptions[0].disabled).toBe(false);
    expect(document.activeElement).toBe(basic);
  });

  it("sends the selected analysis mode for a new music folder", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async (url, options) => options?.method === "POST" ? {} : {
      similarity_available: true, folders: [],
    });
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const analysis = document.getElementById("source-folder-analysis");
    expect([...analysis.options].map(({ value }) => value)).toEqual(["basic", "audio", "similarity"]);
    analysis.value = "similarity";
    document.getElementById("source-folder-path").value = "M:/new";
    document.getElementById("source-folder-add").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls.find(([, options]) => options?.method === "POST")[1].body))
      .toEqual({ path: "M:/new", kind: "music", recursive: true, analysis: "similarity" });
  });

  it("parses seconds and MM:SS while preserving empty points", () => {
    expect(parsePointTime("5.3")).toBe(5.3);
    expect(parsePointTime("1:05.5")).toBe(65.5);
    expect(parsePointTime("-3.5")).toBe(-3.5);
    expect(parsePointTime("-0:03.5")).toBe(-3.5);
    expect(parsePointTime("")).toBeNull();
    expect(formatPointTime(65.5)).toBe("1:05.5");
    expect(formatPointTime(-65.5)).toBe("-1:05.5");
    expect(() => parsePointTime("1:75")).toThrow();
  });

  it("rejects nonfinite cue input and carries rounded centiseconds", () => {
    expect(() => parsePointTime("9".repeat(400))).toThrow();
    expect(formatPointTime(8.999)).toBe("0:09");
    expect(formatPointTime(59.999)).toBe("1:00");
  });

  it("round-trips a cue to within the half hundredth the catalog allows (#237)", () => {
    expect(parsePointTime(formatPointTime(213.4567))).toBe(213.46);
    for (const value of [0.004, 1.2351, 12.3456, 200.0049, 205.555, 213.4567, 3599.995]) {
      expect(Math.abs(parsePointTime(formatPointTime(value)) - value)).toBeLessThanOrEqual(0.005 + 1e-9);
    }
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

  // ADR-0008-22
  it("shows recovery progress without completion announcements", async () => {
    document.getElementById("panel-library").hidden = false;
    let running = true;
    const request = vi.fn(async () => ({
      folders: [], import: { running, phase: "recovering analysis", done: 2, total: 10 },
      tracks_ready: 2, tracks_indexed: 1,
    }));
    const controls = installLibraryFolders({ document, request });
    await controls.refresh();
    const progress = document.getElementById("source-folders-progress");
    const status = document.getElementById("source-folders-status");
    expect(progress.textContent).toBe("Recovering analysis: 2 of 10");
    expect(progress.hasAttribute("aria-live")).toBe(false);
    expect(status.getAttribute("aria-live")).toBe("polite");
    for (let run = 0; run < 2; run += 1) {
      running = false;
      await controls.refresh();
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(status.textContent).toBe("");
      running = true;
      await controls.refresh();
    }
  });

  it("announces import completion once after running", async () => {
    document.getElementById("panel-library").hidden = false;
    let running = true;
    const request = vi.fn(async () => ({ folders: [], import: { running }, tracks_ready: 2, tracks_indexed: 1 }));
    const controls = installLibraryFolders({ document, request });
    await controls.refresh();
    const status = document.getElementById("source-folders-status");
    expect(status.textContent).toBe("");
    running = false;
    await controls.refresh();
    await vi.waitFor(() => expect(status.textContent).toBe("2 tracks ready; 1 with similarity analysis."));
    const changes = [];
    const observer = new window.MutationObserver((records) => changes.push(...records));
    observer.observe(status, { childList: true, characterData: true, subtree: true });
    await controls.refresh();
    await new Promise((resolve) => setTimeout(resolve, 0));
    observer.disconnect();
    expect(changes).toHaveLength(0);
  });

  it("announces a finished scan when recovery starts before the next poll", async () => {
    document.getElementById("panel-library").hidden = false;
    let job = { running: true, phase: "reading tracks", done: 1, total: 4 };
    const request = vi.fn(async () => ({ folders: [], import: job, tracks_ready: 2, tracks_indexed: 1 }));
    const controls = installLibraryFolders({ document, request });
    await controls.refresh();
    const status = document.getElementById("source-folders-status");
    expect(status.textContent).toBe("");
    job = { running: true, phase: "recovering analysis", done: 0, total: 3 };
    await controls.refresh();
    await vi.waitFor(() => expect(status.textContent).toBe("2 tracks ready; 1 with similarity analysis."));
    expect(document.getElementById("source-folders-progress").textContent).toBe("Recovering analysis: 0 of 3");
  });

  it("reads a point in an hour-long mix typed as hours:minutes:seconds (#391)", () => {
    expect(parsePointTime("1:02:05.5")).toBe(3725.5);
    expect(parsePointTime("75:30")).toBe(4530);
    expect(parsePointTime(formatPointTime(4530))).toBe(4530);
    expect(() => parsePointTime("1:60:00")).toThrow("Enter seconds, minutes:seconds or hours:minutes:seconds, for example 1:05:00");
  });

  it("counts import errors in the summary and lists them in a details list that stays open (#391)", async () => {
    document.getElementById("panel-library").hidden = false;
    let errors = ["C:/music/a.flac: could not decode", "C:/music/b.mp3: could not decode"];
    const request = vi.fn(async (url) => url === "/api/library/folders" ? {
      folders: [], import: { running: false, errors }, tracks_ready: 1204, tracks_indexed: 1100,
    } : {});
    const controls = installLibraryFolders({ document, request });
    await controls.refresh();
    const progress = document.getElementById("source-folders-progress");
    const list = document.getElementById("source-folders-errors");
    expect(progress.textContent).toBe("Import finished with 2 errors; the list follows. 1,204 tracks ready; 1,100 with similarity analysis.");
    expect(progress.textContent).not.toContain("a.flac");
    expect(list.tagName).toBe("DETAILS");
    expect(list.hidden).toBe(false);
    expect(list.querySelector("summary").textContent).toBe("Import errors (2)");
    expect([...list.querySelectorAll("li")].map((item) => item.textContent)).toEqual(errors);
    list.open = true;
    const first = list.querySelector("li");
    await controls.refresh();
    expect(list.open).toBe(true);
    expect(list.querySelector("li")).toBe(first);
    errors = [];
    await controls.refresh();
    expect(list.hidden).toBe(true);
    expect(list.open).toBe(false);
    expect(progress.textContent).toBe("1,204 tracks ready; 1,100 with similarity analysis.");
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
    list.innerHTML = `<tr data-id="abc"><td><input type="checkbox" data-field="recursive"></td></tr><tr data-id="abc"><td><button data-action="remove"></button></td></tr>`;
    const recursion = list.querySelector("input");
    recursion.checked = true;
    recursion.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders/abc", expect.objectContaining({ method: "PATCH" }));
    list.querySelector("button").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/folders/abc", { method: "DELETE" });
  });

  it("keeps checkbox focus and blocks another toggle while saving, then keeps the checkbox (#330)", async () => {
    document.getElementById("panel-library").hidden = false;
    let folders = [{ id: "abc", path: "M:/", kind: "music", recursive: false, analysis: "basic" }];
    let finish;
    const request = vi.fn((url, options) => options?.method === "PATCH"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ similarity_available: true, folders }));
    const controls = installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const checkbox = document.querySelector('[data-field="recursive"]');
    checkbox.focus();
    checkbox.click();
    expect(checkbox.disabled).toBe(false);
    // Busy to the eye only: aria-disabled made NVDA say "unavailable" (#331).
    expect(checkbox.classList.contains("is-pending")).toBe(true);
    expect(checkbox.getAttribute("aria-busy")).toBe("true");
    expect(checkbox.hasAttribute("aria-disabled")).toBe(false);
    expect(document.activeElement).toBe(checkbox);
    checkbox.click();
    expect(checkbox.checked).toBe(true);
    expect(request.mock.calls.filter(([, options]) => options?.method === "PATCH")).toHaveLength(1);
    folders = [{ ...folders[0], recursive: true }];
    await controls.refresh();
    expect(document.activeElement).toBe(checkbox);
    const left = [];
    document.addEventListener("focusout", (event) => {
      if (!event.relatedTarget) left.push(event.target);
    });
    finish({});
    await vi.waitFor(() => expect(checkbox.hasAttribute("aria-busy")).toBe(false));
    await new Promise((resolve) => setTimeout(resolve, 0));
    // The redraw after the save keeps the same checkbox, so focus never
    // leaves it and NVDA stays in focus mode.
    expect(document.querySelector('[data-field="recursive"]')).toBe(checkbox);
    expect(checkbox.checked).toBe(true);
    expect(checkbox.classList.contains("is-pending")).toBe(false);
    expect(document.activeElement).toBe(checkbox);
    // Another page's change is set on it in place.
    folders = [{ ...folders[0], recursive: false, analysis: "similarity" }];
    await controls.refresh();
    expect(document.querySelector('[data-field="recursive"]')).toBe(checkbox);
    expect(checkbox.checked).toBe(false);
    expect(document.querySelector('[data-field="analysis"]').value).toBe("similarity");
    expect(document.activeElement).toBe(checkbox);
    expect(left).toEqual([]);
  });

  it("says a short line while a slow folder save runs (#384)", async () => {
    document.getElementById("panel-library").hidden = false;
    let finish;
    const request = vi.fn((url, options) => options?.method === "PATCH"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ folders: [{ id: "abc", path: "M:/", kind: "music", recursive: false, advanced: false }] }));
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const status = document.getElementById("source-folders-status");
    vi.useFakeTimers();
    try {
      document.querySelector('[data-field="recursive"]').click();
      await vi.advanceTimersByTimeAsync(250);
      expect(status.textContent).toBe("");
      await vi.advanceTimersByTimeAsync(100);
      expect(status.textContent).toBe("Saving folder settings…");
      finish({});
      await vi.advanceTimersByTimeAsync(10);
      expect(status.textContent).toBe("Folder settings saved.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("never says unavailable on Remove while it removes, and takes no second press (#331)", async () => {
    document.getElementById("panel-library").hidden = false;
    let finish;
    const request = vi.fn((url, options) => options?.method === "DELETE"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ folders: [{ id: "abc", path: "M:/", kind: "music", recursive: false, advanced: false }] }));
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const remove = document.querySelector('#source-folders button[data-action="remove"]');
    remove.focus();
    remove.click();
    expect(remove.hasAttribute("aria-disabled")).toBe(false);
    expect(remove.classList.contains("is-pending")).toBe(true);
    expect(remove.getAttribute("aria-busy")).toBe("true");
    remove.click();
    expect(request.mock.calls.filter(([, options]) => options?.method === "DELETE")).toHaveLength(1);
    finish({});
    await vi.waitFor(() => expect(remove.classList.contains("is-pending")).toBe(false));
    expect(remove.hasAttribute("aria-busy")).toBe(false);
  });

  it("restores the checkbox after a failed save without losing focus", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async (url, options) => {
      if (options?.method === "PATCH") throw new Error("save failed");
      return { folders: [{ id: "abc", path: "M:/", kind: "music", recursive: false, advanced: false }] };
    });
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const checkbox = document.querySelector('[data-field="recursive"]');
    checkbox.focus();
    checkbox.click();
    await vi.waitFor(() => expect(checkbox.classList.contains("is-pending")).toBe(false));
    expect(checkbox.checked).toBe(false);
    expect(document.activeElement).toBe(checkbox);
    expect(document.getElementById("source-folders-status").textContent).toContain("save failed");
  });

  it("saves the folder analysis selector", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async () => ({ similarity_available: true, folders: [{ id: "abc", path: "M:/", kind: "music", recursive: false, analysis: "basic" }] }));
    installLibraryFolders({ document, request });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const row = document.querySelector("#source-folders tr[data-id=abc]");
    const cells = row.querySelectorAll("td");
    expect(cells).toHaveLength(5);
    const recursive = cells[2];
    recursive.focus();
    recursive.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/library/folders/abc", expect.objectContaining({ body: JSON.stringify({ recursive: true }) })));
    const analysis = cells[3].querySelector("select");
    analysis.value = "similarity";
    analysis.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/library/folders/abc", expect.objectContaining({ body: JSON.stringify({ analysis: "similarity" }) })));
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

  it("loads folder choices when Queue opens and polls management only in Library tools", async () => {
    vi.useFakeTimers();
    try {
      const queue = document.getElementById("panel-queue");
      queue.hidden = true;
      const library = document.getElementById("panel-library");
      library.hidden = true;
      const request = vi.fn(async () => ({ folders: [] }));
      installLibraryFolders({ document, request });
      expect(request).not.toHaveBeenCalled();
      queue.hidden = false;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(request).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(10000);
      expect(request).toHaveBeenCalledTimes(1);
      queue.hidden = true;
      library.hidden = false;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(3000);
      expect(request).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(3000);
      expect(request).toHaveBeenCalledTimes(4);
      library.hidden = true;
      window.dispatchEvent(new window.HashChangeEvent("hashchange"));
      await vi.advanceTimersByTimeAsync(6000);
      expect(request).toHaveBeenCalledTimes(4);
    } finally { vi.useRealTimers(); }
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
    const pause = vi.fn();
    Object.defineProperty(preview, "pause", { configurable: true, value: pause });
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
    pause.mockClear();
    preview.currentTime = 170;
    document.getElementById("points-save").click();
    expect(pause).toHaveBeenCalledOnce();
    expect(preview.currentTime).toBe(170);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request).toHaveBeenCalledWith("/api/library/points", expect.objectContaining({ method: "POST" }));
  });

  it("carries the selected station through Track info reads and saves", async () => {
    const request = vi.fn(async (url) => url.startsWith("/api/library/points?")
      ? { path: "M:/song.flac", kind: "music", length: 200, points: { start: 1, end: 190 }, last_played: 20 }
      : { last_played: 20 });
    installLibraryFolders({ document, request, getStation: () => "nightowl" });
    await document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "M:/song.flac" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(request.mock.calls[0][0]).toBe("/api/library/points?path=M%3A%2Fsong.flac&station=nightowl");
    document.querySelector('[data-point="start"]').value = "2";
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls.find(([url]) => url === "/api/library/points")[1].body).station).toBe("nightowl");
  });

  it("rescans, compares provenance, defaults only unprotected changes, and applies selected cues", async () => {
    const request = vi.fn(async (url) => {
      if (url.startsWith("/api/library/points?")) return { path: "M:/song.flac", title: "Song", kind: "music", points: { start: 1, intro: 5, end: 180 }, point_sources: { start: "user", intro: "analysis", end: "imported" } };
      if (url === "/api/library/points/rescan") return {
        token: "scan-token", points: { start: 2, intro: 7, end: 175, segue: 170 }, point_sources: { start: "analysis", intro: "analysis", end: "analysis", segue: "analysis" },
        current_points: { start: 1, intro: 5, end: 180 }, current_sources: { start: "user", intro: "analysis", end: "imported" },
      };
      if (url === "/api/library/points/rescan/apply") return { path: "M:/song.flac", title: "Song", kind: "music", length: 200, points: { start: 1, intro: 7, end: 180, segue: 170 }, point_sources: { start: "user", intro: "analysis", end: "imported", segue: "analysis" } };
      return {};
    });
    installLibraryFolders({ document, request, getStation: () => "nightowl" });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "M:/song.flac" } }));
    await vi.waitFor(() => expect(document.querySelector('[data-point="intro"]').value).toBe("0:05"));
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(document.getElementById("points-rescan-results").hidden).toBe(false));
    const body = JSON.parse(request.mock.calls.find(([url]) => url === "/api/library/points/rescan")[1].body);
    expect(body).toEqual({ path: "M:/song.flac", station: "nightowl" });
    expect(document.querySelector('[data-rescan-field="start"]').checked).toBe(false);
    expect(document.querySelector('[data-rescan-field="intro"]').checked).toBe(true);
    expect(document.querySelector('[data-rescan-field="end"]').checked).toBe(false);
    expect(document.getElementById("points-rescan-comparison").textContent).toContain("user");
    document.querySelector('[data-rescan-field="start"]').checked = true;
    document.getElementById("points-rescan-apply").click();
    await vi.waitFor(() => expect(document.querySelector('[data-point="intro"]').value).toBe("0:07"));
    const apply = request.mock.calls.find(([url]) => url === "/api/library/points/rescan/apply");
    expect(JSON.parse(apply[1].body)).toEqual({ path: "M:/song.flac", station: "nightowl", token: "scan-token", fields: ["start", "intro", "segue"] });
  });

  it("requires cue edits to be saved before rescanning and ignores a scan after cue edits", async () => {
    let finishScan;
    const request = vi.fn((url) => {
      if (url.startsWith("/api/library/points?")) return Promise.resolve({ path: "M:/song.flac", kind: "music", points: { start: 1 } });
      if (url === "/api/library/points/rescan") return new Promise((resolve) => { finishScan = resolve; });
      return Promise.resolve({});
    });
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "M:/song.flac" } }));
    await vi.waitFor(() => expect(document.querySelector('[data-point="start"]').value).toBe("0:01"));
    document.querySelector('[data-point="start"]').value = "2";
    document.getElementById("points-rescan").click();
    expect(document.getElementById("points-rescan-status").textContent).toContain("Save your cue edits");
    document.querySelector('[data-point="start"]').value = "0:01";
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(finishScan).toBeTypeOf("function"));
    document.querySelector('[data-point="start"]').value = "3";
    document.querySelector('[data-point="start"]').dispatchEvent(new Event("input", { bubbles: true }));
    finishScan({ token: "late", points: { start: 4 }, point_sources: {}, current_points: { start: 1 }, current_sources: {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("points-rescan-results").hidden).toBe(true);
  });

  it("keeps suggestions when none are selected or apply fails, then focuses the right control", async () => {
    const request = vi.fn(async (url) => {
      if (url.startsWith("/api/library/points?")) return { path: "M:/song.flac", kind: "music", points: { intro: 5 } };
      if (url === "/api/library/points/rescan") return { token: "t", points: { intro: 7 }, point_sources: { intro: "analysis" }, current_points: { intro: 5 }, current_sources: { intro: "analysis" } };
      throw new Error("Offline");
    });
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "M:/song.flac" } }));
    await vi.waitFor(() => expect(document.querySelector('[data-point="intro"]').value).toBe("0:05"));
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(document.getElementById("points-rescan-results").hidden).toBe(false));
    const field = document.querySelector('[data-rescan-field="intro"]');
    field.checked = false;
    document.getElementById("points-rescan-apply").click();
    expect(document.getElementById("points-rescan-results").hidden).toBe(false);
    expect(document.activeElement).toBe(field);
    field.checked = true;
    document.getElementById("points-rescan-apply").click();
    await vi.waitFor(() => expect(document.getElementById("points-rescan-status").textContent).toContain("Offline"));
    expect(document.getElementById("points-rescan-results").hidden).toBe(false);
    expect(document.activeElement).toBe(document.getElementById("points-rescan-apply"));
    document.getElementById("points-rescan-discard").click();
    expect(document.getElementById("points-rescan-results").hidden).toBe(true);
    expect(document.activeElement).toBe(document.getElementById("points-rescan"));
  });

  it("limits liner proposals to supported cues and ignores late scans after track navigation or auth change", async () => {
    let finishScan;
    const request = vi.fn((url) => {
      if (url.startsWith("/api/library/points?")) {
        const path = decodeURIComponent(url.split("path=")[1].split("&")[0]);
        return Promise.resolve({ path, kind: path.includes("voice") ? "liner" : "music", points: { start: 0, segue: 2, end: 4 } });
      }
      if (url === "/api/library/points/rescan") return new Promise((resolve) => { finishScan = resolve; });
      return Promise.resolve({});
    });
    installLibraryFolders({ document, request });
    await document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "N:/voice.wav", kind: "liner" } }));
    await vi.waitFor(() => expect(document.querySelector('[data-point="start"]').value).toBe("0:00"));
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(finishScan).toBeTypeOf("function"));
    finishScan({ token: "old", points: { start: 0.2, segue: 2.6, end: 3.8 }, point_sources: {}, current_points: {}, current_sources: {} });
    await vi.waitFor(() => expect(document.getElementById("points-rescan-results").hidden).toBe(false));
    expect(document.querySelectorAll("[data-rescan-field]")).toHaveLength(3);
    expect(document.querySelector("[data-rescan-field=intro]")).toBeNull();
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(finishScan).toBeTypeOf("function"));
    const finishLate = finishScan;
    await document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "M:/next.wav" } }));
    await vi.waitFor(() => expect(document.querySelector('[data-point="start"]').value).toBe("0:00"));
    finishLate({ token: "late", points: { start: 9 }, point_sources: {}, current_points: {}, current_sources: {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("points-rescan-results").hidden).toBe(true);
    document.getElementById("points-rescan").click();
    await vi.waitFor(() => expect(finishScan).toBeTypeOf("function"));
    const finishSignedOut = finishScan;
    invalidateAuthenticatedRequestEpoch();
    finishSignedOut({ token: "private", points: { start: 10 }, point_sources: {}, current_points: {}, current_sources: {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("points-rescan-results").hidden).toBe(true);
  });

  it("refreshes only play time on station switch and ignores a late prior-station reply", async () => {
    let station = "default";
    let finishOldRefresh;
    let pointsReads = 0;
    const request = vi.fn((url) => {
      if (!url.startsWith("/api/library/points?")) return Promise.resolve({});
      pointsReads += 1;
      if (pointsReads === 1) return Promise.resolve({ path: "M:/song.flac", kind: "music", length: 200, points: { start: 1, end: 190 }, last_played: 10 });
      if (url.includes("station=nightowl")) return Promise.resolve({ path: "M:/song.flac", last_played: 172800 });
      return new Promise((resolve) => { finishOldRefresh = () => resolve({ path: "M:/song.flac", last_played: 10 }); });
    });
    const controls = installLibraryFolders({ document, request, getStation: () => station });
    void controls.openEditor("M:/song.flac");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const lastPlayed = () => [...document.querySelectorAll("#track-info-details dt")]
      .find((term) => term.textContent === "Last played").nextElementSibling.textContent;
    const initialTimestamp = lastPlayed();
    document.querySelector('[data-point="start"]').value = "2";
    const oldRefresh = controls.refreshIntros();
    station = "nightowl";
    await controls.refreshIntros();
    finishOldRefresh();
    await oldRefresh;
    expect(document.querySelector('[data-point="start"]').value).toBe("2");
    expect(lastPlayed()).not.toBe(initialTimestamp);
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
    expect(document.getElementById("points-status").textContent).toContain("Enter seconds, minutes:seconds or hours:minutes:seconds");
  });

  it("saves negative liner overlap as a song-relative delay and disables track preview controls", async () => {
    const request = vi.fn(async (url) => url === "/api/library/points"
      ? { points: { start: 0, segue: 5, end: 8, overlap_start: -3 } }
      : { path: "N:/voice/same.wav", kind: "liner", length: 8, points: { start: 0, segue: 5, end: 8, overlap_start: -3 } });
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "N:/voice/same.wav", kind: "liner" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.getElementById("negative-overlap-help").hidden).toBe(false);
    expect(document.querySelector('[data-preview="overlap_start"]').disabled).toBe(true);
    expect(document.querySelector('[data-set-current="overlap_start"]').disabled).toBe(true);
    document.getElementById("points-save").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(JSON.parse(request.mock.calls.find(([url]) => url === "/api/library/points")[1].body).points.overlap_start).toBe(-3);
  });

  it("announces preview availability when a liner overlap changes sign", async () => {
    const request = vi.fn(async () => ({ path: "N:/voice/id.wav", kind: "liner", length: 8, points: { overlap_start: 0 } }));
    installLibraryFolders({ document, request });
    document.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: "N:/voice/id.wav", kind: "liner" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const input = document.querySelector('[data-point="overlap_start"]');
    expect(input.hasAttribute("aria-describedby")).toBe(false);
    input.focus();
    input.value = "-3";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    const status = document.getElementById("points-status");
    await vi.waitFor(() => expect(status.textContent).toContain("unavailable for negative values"));
    expect(input.getAttribute("aria-describedby")).toBe("negative-overlap-help");
    expect(document.activeElement).toBe(input);
    input.value = "0";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.waitFor(() => expect(status.textContent).toContain("available again"));
    expect(input.hasAttribute("aria-describedby")).toBe(false);
    expect(document.querySelector('[data-preview="overlap_start"]').disabled).toBe(false);
    expect(document.querySelector('[data-set-current="overlap_start"]').disabled).toBe(false);
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
    document.getElementById("panel-track-info").insertAdjacentHTML("beforeend", new window.DOMParser().parseFromString(html, "text/html").getElementById("tags-editor").outerHTML);
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
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ path: "M:/one.flac", tags: { title: "Uno" }, beets: false, write_file: false });
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

  it("says the save result once, after the other track has opened (#332)", async () => {
    document.body.insertAdjacentHTML("beforeend", '<div id="sr-status" role="status" aria-live="polite"></div>');
    const region = document.getElementById("sr-status");
    const said = [];
    new window.MutationObserver(() => { if (region.textContent) said.push(region.textContent); })
      .observe(region, { childList: true, characterData: true, subtree: true });
    const request = trackRequest();
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.querySelector('[data-point="intro"]').value = "0:10";
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-save").click();
    await switching;
    expect(document.activeElement).toBe(document.getElementById("track-info-heading"));
    await vi.waitFor(() => expect(said).toEqual(["Saved changes to Artist — One."]));
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(said).toHaveLength(1);
    // Discard says nothing about a save.
    document.querySelector('[data-point="intro"]').value = "0:20";
    const discarding = controls.openEditor("M:/one.flac");
    document.getElementById("track-info-discard").click();
    await discarding;
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(said).toHaveLength(1);
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
    const save = document.getElementById("track-info-save");
    save.focus();
    save.click();
    const keep = document.getElementById("track-info-keep");
    // Nothing turned off under focus: NVDA said Save "unavailable" (#332).
    expect(keep.disabled).toBe(false);
    expect(save.disabled).toBe(false);
    expect(save.getAttribute("aria-busy")).toBe("true");
    expect(document.activeElement).toBe(save);
    keep.click();
    expect(document.getElementById("track-info-dialog").open).toBe(true);
    finish({ points: { intro: 10, end: 180 } });
    await switching;
    expect(save.hasAttribute("aria-busy")).toBe(false);
    expect(document.getElementById("track-info-dialog").returnValue).toBe("save");
  });

  it("says Saving… in its own line, and lets go of focus before Save and Discard close it", async () => {
    let finish;
    const request = vi.fn((url, options) => options?.method === "POST"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve({ path: decodeURIComponent(url.split("path=")[1] || "M:/one.flac"), title: "One", kind: "music", points: { end: 180 } }));
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    const dialog = document.getElementById("track-info-dialog");
    // As a browser closes it: focus still in the dialog goes back to the opener.
    const close = dialog.close;
    const focusInDialogAtClose = [];
    dialog.close = (value) => { focusInDialogAtClose.push(dialog.contains(document.activeElement)); close(value); };
    const opener = document.createElement("button");
    document.getElementById("panel-now").append(opener);
    opener.focus();
    document.querySelector('[data-point="intro"]').value = "0:10";
    vi.useFakeTimers();
    try {
      const switching = controls.openEditor("M:/two.flac", "music", opener);
      document.getElementById("track-info-save").focus();
      document.getElementById("track-info-save").click();
      await vi.advanceTimersByTimeAsync(350);
      expect(document.getElementById("track-info-progress").textContent).toBe("Saving…");
      expect(dialog.contains(document.getElementById("track-info-progress"))).toBe(true);
      expect(document.getElementById("track-info-error").textContent).toBe("");
      finish({ points: { intro: 10, end: 180 } });
      await vi.advanceTimersByTimeAsync(10);
      await switching;
      expect(document.getElementById("track-info-progress").textContent).toBe("");
    } finally {
      vi.useRealTimers();
    }
    document.querySelector('[data-point="intro"]').value = "0:20";
    opener.focus();
    const discarding = controls.openEditor("M:/one.flac", "music", opener);
    document.getElementById("track-info-discard").focus();
    document.getElementById("track-info-discard").click();
    await discarding;
    expect(focusInDialogAtClose).toEqual([false, false]);
  });

  it("leaves focus somewhere steady when a switch stops short", async () => {
    let respond;
    const request = vi.fn((url) => (url.includes("two")
      ? new Promise((resolve) => { respond = resolve; })
      : Promise.resolve({ path: "M:/one.flac", title: "One", kind: "music", points: { end: 180 } })));
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("M:/one.flac");
    document.getElementById("panel-track-info").hidden = false;
    document.querySelector('[data-point="intro"]').value = "0:10";
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-discard").click();
    await vi.waitFor(() => expect(respond).toBeTypeOf("function"));
    // Signed out while the other track loads, with focus lost to the page.
    invalidateAuthenticatedRequestEpoch();
    document.activeElement?.blur?.();
    respond({ path: "M:/two.flac", title: "Two", kind: "music", points: {} });
    await switching;
    expect(document.activeElement).toBe(document.getElementById("track-info-heading"));
  });

  it("discards a liner draft without saving, and offers the same liner fields", async () => {
    const request = vi.fn(async (url) => ({ path: decodeURIComponent(url.split("path=")[1]), title: "Test jingle", kind: "liner", points: { start: 0, segue: 5.3, end: 8.8, overlap_start: 5.3 } }));
    const controls = installLibraryFolders({ document, request });
    await controls.openEditor("N:/jingle.wav", "liner");
    const preview = document.getElementById("points-preview");
    preview.pause = vi.fn();
    document.querySelector('[data-point="overlap_start"]').value = "0:05.6";
    const switching = controls.openEditor("N:/stinger.wav", "liner");
    expect(document.getElementById("track-info-dialog-title").textContent).toContain("Test jingle");
    document.getElementById("track-info-discard").click();
    expect(preview.pause).toHaveBeenCalledOnce();
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
    const preview = document.getElementById("points-preview");
    const pause = vi.fn();
    Object.defineProperty(preview, "pause", { configurable: true, value: pause });
    preview.currentTime = 17;
    document.querySelector('[data-point="intro"]').value = "0:10";
    request.mockRejectedValueOnce(new Error("Offline"));
    const switching = controls.openEditor("M:/two.flac");
    document.getElementById("track-info-save").click();
    await vi.waitFor(() => expect(document.getElementById("track-info-error").textContent).toContain("Offline"));
    expect(pause).toHaveBeenCalledOnce();
    expect(preview.currentTime).toBe(17);
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
    document.getElementById("track-info-save").click();
    document.getElementById("track-info-discard").click();
    expect(document.getElementById("track-info-dialog").open).toBe(true);
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

describe("beets paths in Library tools", () => {
  it("shows a beets path mismatch as static text after the progress line", async () => {
    document.getElementById("panel-library").hidden = false;
    const request = vi.fn(async () => ({
      folders: [], import: {}, tracks_ready: 3, tracks_indexed: 0,
      beets_paths: { summary: "0 of 3 library songs have a beets entry", problem: "No song in the beets database matches a song in AutoDJ's library.", fix: 'Set music_dir = "/music" under [library].', note: null, mismatch: true },
    }));
    const controls = installLibraryFolders({ document, request });
    await controls.refresh();
    const beets = document.getElementById("source-folders-beets");
    expect(beets.previousElementSibling.id).toBe("source-folders-progress");
    expect(beets.hidden).toBe(false);
    expect(beets.textContent).toBe(`Beets: No song in the beets database matches a song in AutoDJ's library. Set music_dir = "/music" under [library].`);
    expect(beets.hasAttribute("aria-live")).toBe(false);
    expect(document.getElementById("source-folders-status").textContent).toBe("");
    request.mockResolvedValue({ folders: [], import: {}, tracks_ready: 3, tracks_indexed: 0, beets_paths: null });
    await controls.refresh();
    expect(beets.hidden).toBe(true);
  });
});
