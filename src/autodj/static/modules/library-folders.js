// Music and liner source folders plus the saved song-points editor.
import { withDisabled, failureText, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { closeDialog, openDialog } from "./dialog-guard.js";
import {
  showFieldError, clearFieldError, dialogReturnPlace, dialogStatusLine, fmtTrack, focusAfterDialog, focusSteadyPlace, placeRows,
} from "./dom-helpers.js";
import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { collectionHost } from "./collection-table.js";
import { lastSeenWords } from "./devices.js";
import { formatPlayedAt } from "./history-format.js";
import { makeSay, clearAnnouncedStatus, announceStatus } from "./live-region.js";
import { installAutoRescan } from "./auto-rescan.js";
import { installAdvancedLength } from "./advanced-length.js";
import { installMusicFadeDrop } from "./music-fade-drop.js";
import { showBeetsPaths } from "./beets-paths.js";
import { installMatchedIntros } from "./matched-intros.js";
import { installTagEditor } from "./tag-editor.js";
import { returnToView } from "./tabs.js";
import { addBrowseButton, serverFolderSource } from "./path-picker.js";

const POINTS = ["start", "intro", "outro", "segue", "end", "overlap_start"];

export function parsePointTime(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^-?\d+(?:\.\d+)?$/.test(text)) {
    const seconds = Number(text);
    if (Number.isFinite(seconds)) return seconds;
  }
  const match = /^(-?)(\d+):([0-5]?\d(?:\.\d+)?)$/.exec(text);
  if (match) {
    const seconds = Number(match[2]) * 60 + Number(match[3]);
    if (Number.isFinite(seconds)) return match[1] ? -seconds : seconds;
  }
  // A point in an hour-long mix may be typed as hours:minutes:seconds.
  const long = /^(-?)(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)$/.exec(text);
  if (long) {
    const seconds = Number(long[2]) * 3600 + Number(long[3]) * 60 + Number(long[4]);
    if (Number.isFinite(seconds)) return long[1] ? -seconds : seconds;
  }
  throw new Error("Enter seconds, minutes:seconds or hours:minutes:seconds, for example 1:05:00");
}

export function formatPointTime(value) {
  if (value == null) return "";
  const sign = value < 0 ? "-" : "";
  const centiseconds = Math.round(Math.abs(value) * 100);
  const minutes = Math.floor(centiseconds / 6000);
  const seconds = Math.floor(centiseconds / 100) % 60;
  const fractionValue = centiseconds % 100;
  const whole = String(seconds).padStart(2, "0");
  const fraction = fractionValue ? `.${String(fractionValue).padStart(2, "0").replace(/0$/, "")}` : "";
  return `${sign}${minutes}:${whole}${fraction}`;
}


const NO_LANDMARKS = { load() {}, clear() {}, refresh() {} };

const count = (n, one, many = `${one}s`) => `${Number(n || 0).toLocaleString("en-US")} ${Number(n) === 1 ? one : many}`;

function syncSimilarityOption(select, available, keepUnavailable) {
  let option = select.querySelector('option[value="similarity"]');
  if (!available && !keepUnavailable) { option?.remove(); return; }
  if (!option) {
    option = select.ownerDocument.createElement("option");
    option.value = "similarity";
    select.add(option);
  }
  option.textContent = available ? "Audio + similarity" : "Audio + similarity (unavailable)";
  option.disabled = !available;
}

// One line about the library import: counts only, never the joined paths;
// the files with errors are listed below it (renderErrors).
export function importSummary(data) {
  const job = data?.import || {};
  const errors = job.errors?.length || 0;
  const long = data?.too_long_for_analysis || 0;
  const longNote = long
    ? ` ${count(long, "song")} longer than ${data.advanced_max_minutes} minutes ${long === 1 ? "has" : "have"} audio analysis only.`
    : "";
  if (job.running) {
    const where = job.current ? `; ${job.current}` : "";
    const failed = errors ? `; ${count(errors, "error")} so far` : "";
    const activity = job.phase === "recovering analysis" ? "Recovering analysis" : `Importing tracks (${job.phase || "working"})`;
    return `${activity}: ${job.done || 0} of ${job.total || 0}${where}${failed}`;
  }
  // tracks_indexed counts songs with similarity analysis, which a no-model
  // install never has: "indexed" read as if no song had been imported.
  const ready = `${count(data?.tracks_ready, "track")} ready; ${Number(data?.tracks_indexed || 0).toLocaleString("en-US")} with similarity analysis.`;
  if (errors) return `Import finished with ${count(errors, "error")}; the list follows. ${ready}${longNote}`;
  return `${ready}${longNote}`;
}

// landmarks: Track info's Song structure (modules/landmarks.js, #60).
export function installLibraryFolders({ document: doc = document, request = requestJson, getCurrentTrack = () => null, getCurrentPosition = () => null, getStation = () => "default", onBackgroundError = () => {}, onFolders, landmarks = NO_LANDMARKS }) {
  const panel = doc.getElementById("panel-library");
  const settingsPanel = doc.getElementById("panel-settings");
  const searchPanel = doc.getElementById("library-kind")?.closest("section[data-view]") || null;
  let searchShown = false;
  let settingsShown = false;
  const list = doc.getElementById("source-folders");
  const tableNavigation = installTableNavigation(list);
  const pendingFolderEdits = new Set();
  const status = doc.getElementById("source-folders-status");
  status.textContent = "";
  let progress = doc.getElementById("source-folders-progress");
  if (!progress) {
    progress = doc.createElement("p");
    progress.id = "source-folders-progress";
    status.insertAdjacentElement("afterend", progress);
  }
  // Files the last import could not read, listed on demand under the summary.
  let errorList = doc.getElementById("source-folders-errors");
  if (!errorList) {
    errorList = doc.createElement("details");
    errorList.id = "source-folders-errors";
    errorList.hidden = true;
    errorList.append(doc.createElement("summary"), doc.createElement("ul"));
    progress.insertAdjacentElement("afterend", errorList);
  }
  let errorSignature = "";
  let importRunning = false;
  // Whether beets' songs match the library's: static text, not a live region,
  // so the 3-second poll never repeats a problem that lasts.
  let beetsPaths = doc.getElementById("source-folders-beets");
  if (!beetsPaths) {
    beetsPaths = doc.createElement("p");
    beetsPaths.id = "source-folders-beets";
    beetsPaths.hidden = true;
    progress.insertAdjacentElement("afterend", beetsPaths);
  }
  const pathInput = doc.getElementById("source-folder-path");
  // The folder is on the AutoDJ machine, so the server lists its folders.
  if (pathInput) addBrowseButton(pathInput, { source: serverFolderSource({ request }), label: "folder path" });
  const kindInput = doc.getElementById("source-folder-kind");
  const recursiveInput = doc.getElementById("source-folder-recursive");
  const analysisInput = doc.getElementById("source-folder-analysis");
  const analysisGroup = doc.getElementById("source-folder-analysis-group");
  const analysisHelp = doc.getElementById("source-folder-analysis-help");
  let similarityAvailable = false;
  const editor = doc.getElementById("points-editor");
  const pointStatus = doc.getElementById("points-status");
  const pointPath = doc.getElementById("points-path");
  const returnButton = doc.createElement("button");
  returnButton.type = "button";
  returnButton.id = "track-info-return";
  returnButton.hidden = true;
  returnButton.textContent = "Return to track list";
  doc.getElementById("track-info-heading")?.insertAdjacentElement("afterend", returnButton);
  const allowOutro = doc.getElementById("points-allow-outro");
  const allowOutroLabel = doc.getElementById("points-allow-outro-label");
  let timer = null;
  let currentPath = null;
  let currentStation = "default";
  let currentRecord = null;
  let currentKind = "music";
  let currentName = "this track";
  let savedFields = "";
  let opening = false;
  let saving = false;
  let folderSignature = null;
  let protectedGeneration = 0;
  let busyController = new AbortController();
  let rescanResult = null;
  let rescanGeneration = 0;
  let rescanController = new AbortController();
  let rescanBusy = false;
  let suspended = false;
  let addError = null;
  let returnTarget = null;
  let returnView = null;
  let returnMatch = null;
  let returnContainer = null;
  const tags = installTagEditor({
    document: doc,
    request,
    getStation: () => currentStation,
    isCurrent: () => !suspended,
    onSaved(record) {
      if (record.title) currentName = fmtTrack(record);
      renderDetails(record);
    },
  });
  const intros = installMatchedIntros({ document: doc, request, isCurrent: () => !suspended });
  // Unsaved cue points or tags: switching tracks asks first.
  const unsaved = () => savedFields !== fieldSnapshot() || tags.dirty();

  returnButton.addEventListener("click", () => {
    if (!returnView) return;
    returnToView(returnView, () => {
      const panel = doc.querySelector(`section[data-view="${returnView}"]`);
      // Queue & Search holds several lists that can show the same track
      // (Search results, Browse library): look for the redrawn button in
      // the list it came from.
      const listNow = returnContainer?.id ? doc.getElementById(returnContainer.id) : null;
      const scope = listNow && panel.contains(listNow) ? listNow : panel;
      const replacement = returnMatch && [...scope.querySelectorAll("button")]
        .filter((button) => !button.disabled && returnMatch.matches(button))[returnMatch.occurrence];
      if (returnTarget?.isConnected && !returnTarget.disabled && panel.contains(returnTarget)) return returnTarget;
      if (replacement) return replacement;
      const list = returnContainer?.isConnected ? returnContainer
        : returnContainer?.id ? doc.getElementById(returnContainer.id) : null;
      const heading = Array.from(panel.children).find((child) => child.tagName === "H2");
      const fallback = list || heading || panel.querySelector("summary") || doc.getElementById(`tab-${returnView}`);
      fallback?.setAttribute("tabindex", "-1");
      announceStatus(doc.getElementById("sr-status"), {
        "queue-list": "That track is no longer in the queue.",
        "search-results": "That track is no longer in the search results.",
        "library-results-table": "That track is no longer in the library list.",
        "history-table": "That play is no longer in History.",
        "ln-file-list": "That liner is no longer in the list.",
      }[returnContainer?.id] || "That track is no longer in this list.",
      { dwellMs: 6000, force: true, tone: "error" });
      return fallback;
    });
  });

  function returnCollection(opener) {
    // Keep the named host for matching replacement rows and empty-list fallback.
    return collectionHost(opener) || opener?.closest?.("ul, ol, table") || null;
  }

  function rememberReturn(opener) {
    const panel = opener?.closest?.("section[data-view]");
    returnView = panel?.dataset.view !== "track-info" ? panel?.dataset.view : null;
    returnTarget = returnView ? opener : null;
    returnContainer = returnCollection(returnTarget);
    if (returnTarget) {
      const label = opener.getAttribute("aria-label");
      const path = opener.dataset.path;
      const action = opener.dataset.action;
      const matches = (button) => button.getAttribute("aria-label") === label
        && button.dataset.path === path && button.dataset.action === action;
      const scope = returnContainer?.id ? returnContainer : panel;
      returnMatch = {
        matches,
        occurrence: [...scope.querySelectorAll("button")].filter(matches).indexOf(opener),
      };
      returnButton.textContent = `Return to ${doc.getElementById(`tab-${returnView}`)?.textContent?.trim() || returnView}`;
    } else returnMatch = null;
    returnButton.hidden = !returnTarget;
  }

  function requestCurrent(epoch, generation) {
    return !suspended && generation === protectedGeneration && isAuthenticatedRequestCurrent(epoch);
  }

  const announceAction = makeSay(() => suspended ? null : status);
  const pointSay = makeSay(pointStatus, { force: false });
  const folderError = doc.getElementById("source-folder-error") || doc.createElement("p");
  folderError.id = "source-folder-error";
  folderError.classList.add("field-error");
  folderError.hidden = true;
  if (!folderError.isConnected) pathInput.after(folderError);
  function clearAddError() {
    if (addError) clearAnnouncedStatus(status);
    addError = null;
    clearFieldError(pathInput, folderError);
  }
  function reportAddError(message) {
    addError = message;
    showFieldError(pathInput, message, folderError, status);
  }
  function folderRow(folder) {
    const recursive = doc.createElement("input");
    recursive.type = "checkbox"; recursive.dataset.field = "recursive";
    const recursiveLabel = doc.createElement("label"); recursiveLabel.append(recursive, " Add subfolders");
    let analysis = null;
    if (folder.kind === "music") {
      analysis = doc.createElement("select"); analysis.dataset.field = "analysis";
      analysis.setAttribute("aria-label", `Analysis for ${folder.path}`);
      analysis.innerHTML = '<option value="basic">Basic</option><option value="audio">Audio</option>';
      syncSimilarityOption(analysis, similarityAvailable, folder.analysis === "similarity" || folder.advanced);
    }
    const remove = doc.createElement("button"); remove.type = "button"; remove.dataset.action = "remove"; remove.textContent = "Remove folder";
    const row = tableRow(doc, [folder.kind === "liner" ? "Voice liner" : "Music", folder.path, recursiveLabel, analysis || "Basic", remove], { nameIndex: 1 });
    row.dataset.id = folder.id;
    return { key: JSON.stringify([folder.id, folder.kind, folder.path]), row, recursive, analysis };
  }
  // Keep folder controls in place while polling so updates do not move focus.
  let folderRows = new Map();
  function renderFolders(data) {
    const folders = data.folders || [];
    similarityAvailable = data.similarity_available === true;
    if (analysisHelp) analysisHelp.textContent = "Basic imports tags and cue suggestions; Audio adds beats, key and song structure."
      + (similarityAvailable ? " Audio + similarity adds MuQ matching for songs that sound alike." : "");
    const addMode = analysisInput.value;
    syncSimilarityOption(analysisInput, similarityAvailable, false);
    analysisInput.value = !similarityAvailable && addMode === "similarity" ? "audio" : addMode;
    const signature = JSON.stringify({ available: similarityAvailable, folders: folders.map(({ id, path, kind, recursive, analysis, advanced }) => ({ id, path, kind, recursive, analysis, advanced })) });
    if (signature !== folderSignature && pendingFolderEdits.size === 0) {
      const focused = list.contains(doc.activeElement) ? {
        id: doc.activeElement.closest("tr")?.dataset.id,
        column: doc.activeElement.closest("td")?.cellIndex,
      } : null;
      const records = folders.map((folder) => {
        const kept = folderRows.get(folder.id);
        const record = kept?.key === JSON.stringify([folder.id, folder.kind, folder.path]) ? kept : folderRow(folder);
        if (record.recursive.checked !== !!folder.recursive) record.recursive.checked = !!folder.recursive;
        if (record.analysis) {
          const selected = folder.analysis || (folder.advanced === false ? "basic" : "similarity");
          syncSimilarityOption(record.analysis, similarityAvailable, selected === "similarity");
          record.analysis.value = selected;
        }
        return record;
      });
      folderRows = new Map(records.map((record, index) => [folders[index].id, record]));
      if (records.length) placeRows(list, records.map((record) => record.row));
      else list.replaceChildren(tableRow(doc, ["No folders added yet."], { colspan: [5] }));
      folderSignature = signature;
      tableNavigation.refresh();
      onFolders?.(folders);
      if (focused && !list.contains(doc.activeElement)) {
        const cell = [...list.querySelectorAll("tr")].find((row) => row.dataset.id === focused.id)?.cells[focused.column];
        const replacement = cell?.querySelector("button:not(:disabled), input:not(:disabled), select:not(:disabled)") || cell;
        (replacement || doc.getElementById("source-folder-add")).focus();
      }
    }
    const summary = importSummary(data);
    progress.textContent = summary;
    // Background recovery can start between two polls, right after a scan
    // the user started: that scan has still finished, so say so.
    const scanRunning = !!data.import?.running && data.import?.phase !== "recovering analysis";
    if (importRunning && !scanRunning) {
      announceAction(scanRunning === !!data.import?.running ? summary : importSummary({ ...data, import: { ...data.import, running: false } }));
    }
    importRunning = scanRunning;
    renderErrors(data.import?.errors || []);
    showBeetsPaths(beetsPaths, data.beets_paths, { full: true });
  }
  // The real failures (unreadable or undecodable files, folders that could
  // not be read), as a list that keeps its open state while polling.
  function renderErrors(errors) {
    const signature = JSON.stringify(errors);
    if (signature === errorSignature) return;
    errorSignature = signature;
    errorList.hidden = errors.length === 0;
    errorList.querySelector("summary").textContent = `Import errors (${errors.length})`;
    errorList.querySelector("ul").replaceChildren(...errors.map((text) => {
      const item = doc.createElement("li");
      item.textContent = text;
      return item;
    }));
    if (!errors.length) errorList.open = false;
  }
  async function refresh() {
    if (suspended || ((!panel || panel.closest("[hidden]")) && (!searchPanel || searchPanel.closest("[hidden]")))) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try {
      const data = await request("/api/library/folders");
      if (requestCurrent(epoch, generation)) renderFolders(data);
    } catch (error) {
      if (requestCurrent(epoch, generation)) progress.textContent = failureText("load folders", error);
    }
  }
  function visible() { return !suspended && !!panel && !panel.closest("[hidden]") && !doc.hidden; }
  function updatePolling() {
    const searching = !suspended && !!searchPanel && !searchPanel.closest("[hidden]") && !doc.hidden;
    const showSettings = !suspended && !!settingsPanel && !settingsPanel.closest("[hidden]") && !doc.hidden;
    if (searching && !searchShown && !visible()) void refresh();
    if (showSettings && !settingsShown) void advancedLength?.load();
    searchShown = searching;
    settingsShown = showSettings;
    if (visible() && timer == null) {
      void refresh();
      void autoRescan?.load();
      void advancedLength?.load();
      void musicFadeDrop?.load();
      timer = setInterval(refresh, 3000);
    } else if (!visible() && timer != null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function updateFolderAnalysisControl() {
    analysisInput.disabled = kindInput.value === "liner";
    if (analysisGroup) analysisGroup.hidden = kindInput.value === "liner";
    analysisInput.value = similarityAvailable ? "similarity" : "audio";
  }
  kindInput.addEventListener("change", updateFolderAnalysisControl);
  updateFolderAnalysisControl();
  pathInput.addEventListener("input", () => {
    if (addError) clearAddError();
  });
  doc.getElementById("source-folder-add").addEventListener("click", async () => {
    if (suspended) return;
    clearAddError();
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    let path = pathInput.value.trim();
    if (/^"(?:[A-Za-z]:[\\/]|\\\\)/.test(path) && path.endsWith('"')) path = path.slice(1, -1);
    if (!path) { reportAddError("Enter a folder path on the AutoDJ machine."); return; }
    try {
      await request("/api/library/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, kind: kindInput.value, recursive: recursiveInput.checked, analysis: kindInput.value === "music" ? analysisInput.value : "basic" }) });
      if (!requestCurrent(epoch, generation)) return;
      pathInput.value = "";
      announceAction(`Folder added; import queued for ${path}.`);
      await refresh();
    } catch (error) { if (requestCurrent(epoch, generation)) reportAddError(failureText("add folder", error)); }
  });
  list.addEventListener("change", async (event) => {
    if (suspended) return;
    const input = event.target.closest("input[data-field], select[data-field]");
    if (!input || pendingFolderEdits.has(input)) return;
    const row = input.closest("tr");
    const body = { [input.dataset.field]: input.type === "checkbox" ? input.checked : input.value };
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    // pendingFolderEdits holds off another toggle (see the click guard).
    // No aria-disabled: NVDA speaks the state change on the focused
    // control, so the press was heard as "unavailable" (#331).  aria-busy
    // says it is busy instead, and a slow reply says "Saving…" (#384).
    pendingFolderEdits.add(input);
    await withDisabled(input, async () => {
      try {
        await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (requestCurrent(epoch, generation)) announceAction("Folder settings saved.");
      } catch (error) {
        if (requestCurrent(epoch, generation)) { if (input.type === "checkbox") input.checked = !body[input.dataset.field]; else await refresh(); announceAction(failureText("save folder settings", error), "error"); }
      } finally {
        pendingFolderEdits.delete(input);
      }
    }, { signal: busyController.signal, pendingText: "Saving folder settings…", say: announceAction });
    if (requestCurrent(epoch, generation)) await refresh();
  });
  list.addEventListener("click", async (event) => {
    if (suspended) return;
    // A checkbox or Remove still saving takes no second press.
    if (pendingFolderEdits.has(event.target.closest("input[data-field], select[data-field], button[data-action]"))) { event.preventDefault(); return; }
    const button = event.target.closest('button[data-action="remove"]');
    if (!button) return;
    const row = button.closest("tr");
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    pendingFolderEdits.add(button);
    await withDisabled(button, async () => {
      try {
        await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "DELETE" });
        if (!requestCurrent(epoch, generation)) return;
        announceAction("Folder removed from the library.");
      } catch (error) { if (requestCurrent(epoch, generation)) announceAction(failureText("remove folder", error), "error"); }
      finally {
        pendingFolderEdits.delete(button);
      }
    }, { signal: busyController.signal, pendingText: "Removing the folder…", say: announceAction });
    if (requestCurrent(epoch, generation)) await refresh();
  });
  const autoRescan = installAutoRescan({
    document: doc,
    request,
    say: announceAction,
    isCurrent: () => !suspended,
  });
  const advancedLength = installAdvancedLength({
    document: doc,
    request,
    say: makeSay(doc.getElementById("sr-status"), { dwellMs: 4000, errorDwellMs: 6000 }),
    isCurrent: () => !suspended,
  });
  const musicFadeDrop = installMusicFadeDrop({
    document: doc,
    request,
    say: announceAction,
    isCurrent: () => !suspended,
  });
  doc.getElementById("source-folder-rescan").addEventListener("click", async () => {
    if (suspended) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    const overrideTags = doc.getElementById("source-folder-override-tags").checked;
    try { await request("/api/library/rescan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ override_tags: overrideTags }) }); if (requestCurrent(epoch, generation)) { announceAction(overrideTags ? "Rescan started; populated file tags will be refreshed." : "Rescan started."); await refresh(); } }
    catch (error) { if (requestCurrent(epoch, generation)) announceAction(failureText("start rescan", error), "error"); }
  });

  function setPointFields(points, sources = {}) {
    clearRescan();
    const heading = doc.getElementById("points-heading");
    if (heading) heading.textContent = "Cue points";
    const linerHelp = doc.getElementById("points-liner-help");
    if (linerHelp) linerHelp.hidden = currentKind !== "liner";
    if (allowOutroLabel) allowOutroLabel.hidden = currentKind !== "liner";
    for (const name of POINTS) {
      const field = doc.querySelector(`[data-point="${name}"]`);
      field.value = formatPointTime(points?.[name]);
      const applicable = currentKind === "liner" ? ["start", "segue", "end", "overlap_start"].includes(name) : name !== "overlap_start";
      field.closest("label").hidden = !applicable;
      const help = doc.getElementById(`points-help-${name.replace("_", "-")}`);
      if (help) help.hidden = !applicable;
      doc.querySelector(`[data-preview="${name}"]`).hidden = !applicable;
      const setCurrent = doc.querySelector(`[data-set-current="${name}"]`);
      if (setCurrent) setCurrent.hidden = !applicable;
      const provenance = doc.querySelector(`[data-point-source="${name}"]`);
      if (provenance) provenance.textContent = sources[name] ? ` (${String(sources[name]).replaceAll("_", " ")})` : " (not set)";
    }
    savedFields = fieldSnapshot();
    updateNegativeOverlapHelp();
  }
  function updateNegativeOverlapHelp() {
    const field = doc.querySelector('[data-point="overlap_start"]');
    const negative = currentKind === "liner"
      && field.value.trim().startsWith("-");
    const help = doc.getElementById("negative-overlap-help");
    if (help) {
      const changed = help.hidden === negative;
      help.hidden = !negative;
      if (negative) field.setAttribute("aria-describedby", help.id);
      else field.removeAttribute("aria-describedby");
      if (changed) announceStatus(doc.getElementById("points-status"), negative
        ? help.textContent
        : "Preview and Use preview position are available again.");
    }
    for (const selector of ['[data-preview="overlap_start"]', '[data-set-current="overlap_start"]']) {
      const button = doc.querySelector(selector);
      if (button) button.disabled = negative;
    }
  }
  function clearRescan(message = "") {
    rescanGeneration += 1;
    rescanController.abort();
    rescanController = new AbortController();
    rescanBusy = false;
    rescanResult = null;
    const results = doc.getElementById("points-rescan-results");
    if (results) results.hidden = true;
    const comparison = doc.getElementById("points-rescan-comparison");
    if (comparison) comparison.replaceChildren();
    const status = doc.getElementById("points-rescan-status");
    if (status) status.textContent = message;
  }
  const pointLabel = (name) => `${name[0].toUpperCase()}${name.slice(1)}`;
  function renderRescan(data, identity) {
    const available = currentKind === "liner" ? ["start", "segue", "end"] : ["start", "intro", "outro", "segue", "end"];
    const comparison = doc.getElementById("points-rescan-comparison");
    const rows = [];
    for (const name of available) {
      const current = data.current_points?.[name] ?? null;
      const proposed = data.points?.[name] ?? null;
      const source = data.current_sources?.[name] || "not set";
      const proposedSource = data.point_sources?.[name] || "not set";
      const changed = current !== proposed;
      const protectedValue = ["user", "imported"].includes(source);
      const row = doc.createElement("div");
      const label = doc.createElement("label");
      const checkbox = doc.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.rescanField = name;
      checkbox.checked = changed && !protectedValue;
      checkbox.disabled = !changed;
      label.append(checkbox, doc.createTextNode(` ${pointLabel(name)}: current ${formatPointTime(current) || "Not set"} (${source}); proposed ${formatPointTime(proposed) || "Not set"} (${proposedSource})`));
      row.append(label);
      if (proposed != null) {
        const preview = doc.createElement("button");
        preview.type = "button";
        preview.dataset.rescanPreview = name;
        preview.textContent = `Preview proposed ${pointLabel(name)}`;
        row.append(preview);
      }
      rows.push(row);
    }
    comparison.replaceChildren(...rows);
    rescanResult = identity;
    doc.getElementById("points-rescan-results").hidden = false;
    doc.getElementById("points-rescan-status").textContent = "Cue point suggestions are ready.";
    doc.getElementById("points-rescan-heading").focus();
  }
  async function rescanPoints() {
    if (suspended || !currentPath || saving || opening || rescanBusy) return;
    if (savedFields !== fieldSnapshot()) {
      doc.getElementById("points-rescan-status").textContent = "Save your cue edits before rescanning.";
      doc.getElementById("points-save").focus();
      return;
    }
    clearRescan("Scanning cue points…");
    rescanBusy = true;

    const identity = { path: currentPath, station: currentStation, epoch: captureAuthenticatedRequestEpoch(), generation: protectedGeneration, scan: rescanGeneration };
    const body = { path: identity.path, ...(identity.station === "default" ? {} : { station: identity.station }) };
    let focusAfter = null;
    try {
      const data = await withDisabled(doc.getElementById("points-rescan"), () => request("/api/library/points/rescan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }), { signal: rescanController.signal });
      if (!requestCurrent(identity.epoch, identity.generation) || identity.scan !== rescanGeneration || currentPath !== identity.path || getStation() !== identity.station) return;
      renderRescan(data, { ...identity, token: data.token, points: data.points, point_sources: data.point_sources, current_points: data.current_points, current_sources: data.current_sources });
    } catch (error) {
      if (requestCurrent(identity.epoch, identity.generation) && identity.scan === rescanGeneration && currentPath === identity.path && getStation() === identity.station) {
        const status = doc.getElementById("points-rescan-status");
        status.textContent = failureText("rescan cue points", error);
        focusAfter = doc.getElementById("points-rescan");
      }
    } finally {
      if (identity.scan === rescanGeneration) {
        rescanBusy = false;
        focusAfter?.focus();
      }
    }
  }
  async function applyRescan() {
    const result = rescanResult;
    if (!result || saving) return;
    const fields = [...doc.querySelectorAll("[data-rescan-field]:checked")].map((input) => input.dataset.rescanField);
    if (!fields.length) {
      doc.getElementById("points-rescan-status").textContent = "Choose at least one cue point to apply.";
      doc.querySelector("[data-rescan-field]:not(:disabled)")?.focus();
      return;
    }
    if (savedFields !== fieldSnapshot()) {
      doc.getElementById("points-rescan-status").textContent = "Save your cue edits before applying these suggestions.";
      doc.getElementById("points-save").focus();
      return;
    }
    saving = true;
    editor.inert = true;
    const button = doc.getElementById("points-rescan-apply");
    let focusAfter = null;
    doc.getElementById("points-rescan-status").textContent = "Applying cue points…";
    try {
      const body = { path: result.path, ...(result.station === "default" ? {} : { station: result.station }), token: result.token, fields };
      const data = await withDisabled(button, () => request("/api/library/points/rescan/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }), { signal: busyController.signal });
      if (!requestCurrent(result.epoch, result.generation) || currentPath !== result.path || getStation() !== result.station) return;
      currentRecord = { ...(currentRecord || {}), ...data };
      setPointFields(data.points || {}, data.point_sources || {});
      renderDetails(currentRecord);
      landmarks.refresh();
      doc.getElementById("points-rescan-status").textContent = "Selected cue points applied.";
      focusAfter = doc.getElementById("points-rescan");
    } catch (error) {
      if (requestCurrent(result.epoch, result.generation) && currentPath === result.path && getStation() === result.station) {
        const status = doc.getElementById("points-rescan-status");
        status.textContent = failureText("apply cue points", error);
        focusAfter = doc.getElementById("points-rescan-apply");
      }
    } finally {
      if (result.generation === protectedGeneration) { saving = false; editor.inert = false; }
      focusAfter?.focus();
    }
  }
  function fieldSnapshot() {
    return JSON.stringify([...POINTS.map((name) => doc.querySelector(`[data-point="${name}"]`).value), allowOutro?.checked ?? false]);
  }
  function showTrackInfo() {
    const tab = doc.getElementById("tab-track-info") || doc.getElementById("tab-now");
    tab?.click();
    const heading = doc.getElementById("track-info-heading");
    if (heading && !doc.getElementById("panel-track-info").hidden) heading.focus();
  }
  function renderDetails(data) {
    const details = doc.getElementById("track-info-details");
    if (!details) return;
    currentRecord = data;
    const path = data.path || currentPath;
    const split = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    const lastPlayed = data.last_played == null ? "Unavailable" : data.last_played > 0
      ? `${formatPlayedAt(new Date(data.last_played * 1000).toISOString(), true)} (${lastSeenWords(data.last_played)})`
      : "Never";
    const analysis = currentKind === "liner" ? "Not needed for liners" : {
      ready: "Complete", pending: "Pending — available for playback", disabled: "Not enabled for this folder",
    }[data.analysis] || "Not available";
    const rows = [
      ["Title", data.title || path.slice(split + 1)], ["Artist", data.artist],
      ["Album", data.album], ["Year", data.year || null], ["Genre", data.genre],
      ["Type", currentKind === "liner" ? "Voice liner" : "Music"],
      ["Duration", formatPointTime(data.length)], ["Last played", lastPlayed],
      ["BPM", data.bpm > 0 ? data.bpm : null], ["Key", data.key_label === "--" ? "Unknown" : data.key_label],
      ["Similarity analysis", analysis],
      ["Loudness analysis", data.loudness_ready == null ? "Not available" : data.loudness_ready ? "Ready" : "Not measured yet"],
      ["Folder", path.slice(0, split + 1)], ["Filename", path.slice(split + 1)],
    ];
    details.replaceChildren(...rows.flatMap(([label, value]) => {
      const term = doc.createElement("dt");
      const description = doc.createElement("dd");
      term.textContent = label;
      description.textContent = value == null || value === "" ? "Not available" : String(value);
      return [term, description];
    }));
    details.hidden = false;
    doc.getElementById("track-info-heading").textContent = `Track info: ${currentName}`;
    doc.getElementById("track-info-empty").hidden = true;
  }
  // said, from the Save or Discard dialog, collects the result to be said
  // once the other track has opened.
  async function savePoints(errorTarget = null, said = null) {
    if (saving || !currentPath) return false;
    doc.getElementById("points-preview")?.pause();
    saving = true;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    const wasInert = editor.inert;
    editor.inert = true;
    try {
      const points = {};
      const station = currentStation;
      for (const name of POINTS) {
        const field = doc.querySelector(`[data-point="${name}"]`);
        if (!field.closest("label").hidden) {
          points[name] = parsePointTime(field.value);
          if (points[name] < 0 && (currentKind !== "liner" || name !== "overlap_start")) {
            throw new Error("Only a liner Overlap start can be negative.");
          }
          if (name === "overlap_start" && points[name] < -15) {
            throw new Error("Overlap start cannot be less than -15 seconds.");
          }
        }
      }
      const body = { path: currentPath, points };
      if (station !== "default") body.station = station;
      if (currentKind === "liner" && allowOutro) body.allow_outro = allowOutro.checked;
      const data = await request("/api/library/points", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!requestCurrent(epoch, generation) || currentStation !== station) return false;
      if (currentRecord) { currentRecord.last_played = data.last_played; }
      if (data.points) setPointFields(data.points, data.point_sources || {});
      else savedFields = fieldSnapshot();
      // New cue points can move the intro, outro and segue landmarks.
      landmarks.refresh();
      pointSay(`Saved changes to ${currentName}.`);
      said?.push(`Saved changes to ${currentName}.`);
      return true;
    } catch (error) {
      if (!requestCurrent(epoch, generation)) return false;
      const message = failureText("save changes", error);
      if (errorTarget) errorTarget.textContent = `${message} Press Escape to return to editing.`;
      else pointSay(message, "error");
      return false;
    } finally {
      if (generation === protectedGeneration) {
        saving = false;
        editor.inert = wasInert;
      }
    }
  }
  // The Save or Discard dialog's save result, said after the switch (#332).
  let savedBeforeSwitch = "";
  function takeSavedBeforeSwitch() {
    const text = savedBeforeSwitch;
    savedBeforeSwitch = "";
    return text;
  }
  function confirmTrackChange() {
    const dialog = doc.getElementById("track-info-dialog");
    if (!dialog || dialog.open) {
      showTrackInfo();
      pointSay("Save your changes before choosing another track.", "error");
      return Promise.resolve(false);
    }
    const save = doc.getElementById("track-info-save");
    const discard = doc.getElementById("track-info-discard");
    const keep = doc.getElementById("track-info-keep");
    const error = doc.getElementById("track-info-error");
    // Where Keep editing and Escape put focus back: the control that asked
    // to switch tracks.
    const returnFocus = doc.activeElement;
    doc.getElementById("track-info-dialog-title").textContent = `Save changes to ${currentName}?`;
    error.textContent = "";
    dialog.returnValue = "";
    savedBeforeSwitch = "";
    // Opened with show() and aria-modal (#631, see dialog-guard.js), Keep
    // editing last.  Every answer puts focus straight back on the control
    // that asked: a switch keeps focus there while the other track loads,
    // rather than on the page, until the track's heading takes it.
    function closeTo(value) {
      const opener = returnFocus === doc.body ? null : returnFocus;
      closeDialog(dialog, { opener, place: dialogReturnPlace(opener), value });
    }
    return new Promise((resolve) => {
      // While Save runs, no button is turned off: NVDA said the focused Save
      // "unavailable" (#332).  pending refuses a second press, and aria-busy
      // marks Save.
      let pending = false;
      async function accept() {
        if (saving || pending) return;
        pending = true;
        // Its own status line: #track-info-error keeps the errors.
        const progress = dialogStatusLine(dialog, "track-info-progress");
        const said = [];
        try {
          const saved = await withDisabled(save, async () => (savedFields === fieldSnapshot() || await savePoints(error, said))
            && (!tags.dirty() || await tags.save(error, said)), {
            signal: busyController.signal, pendingText: "Saving\u2026", say: makeSay(progress),
          });
          if (!saved) return;
          // The result is said once the other track has opened, not as
          // the dialog closes and focus moves.
          savedBeforeSwitch = said.join(" ");
          closeTo("save");
        } finally {
          clearAnnouncedStatus(progress);
          pending = false;
        }
      }
      function reject() {
        if (saving || pending) return;
        doc.getElementById("points-preview")?.pause();
        closeTo("discard");
      }
      function stay() {
        if (saving || pending) return;
        closeTo("keep");
      }
      // Escape keeps editing, held while Save runs.
      function cancel(event) {
        event.preventDefault();
        stay();
      }
      function closed() {
        save.removeEventListener("click", accept);
        discard.removeEventListener("click", reject);
        keep?.removeEventListener("click", stay);
        dialog.removeEventListener("cancel", cancel);
        dialog.removeEventListener("close", closed);
        resolve(dialog.returnValue === "save" || dialog.returnValue === "discard");
      }
      save.addEventListener("click", accept);
      discard.addEventListener("click", reject);
      keep?.addEventListener("click", stay);
      dialog.addEventListener("cancel", cancel);
      dialog.addEventListener("close", closed);
      openDialog(dialog, { home: keep || save });
      save.focus();
    });
  }
  async function openEditor(path, kind = "music", opener = null) {
    if (suspended || opening || saving) return;
    const sourceList = returnCollection(opener);
    const sourcePanel = opener?.closest?.("section[data-view]");
    if (!path) { rememberReturn(opener); showTrackInfo(); pointSay("Choose a track first.", "error"); return; }
    if (path === currentPath && unsaved()) { rememberReturn(opener); showTrackInfo(); return; }
    opening = true;
    clearRescan();
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    let switched = false;
    try {
      if (currentPath && unsaved()) {
        if (!await confirmTrackChange()) return;
        switched = true;
      }
      if (!requestCurrent(epoch, generation)) return;
      editor.inert = true;
      const station = getStation();
      const stationQuery = station === "default" ? "" : `&station=${encodeURIComponent(station)}`;
      const data = await request(`/api/library/points?path=${encodeURIComponent(path)}${stationQuery}`);
      if (!requestCurrent(epoch, generation) || getStation() !== station) return;
      currentStation = station;
      currentPath = data.path || path;
      currentKind = data.kind || kind;
      currentName = data.title ? fmtTrack(data) : currentPath.split(/[\\/]/).pop();
      if (allowOutro) allowOutro.checked = currentKind === "liner" && data.allow_outro === true;
      pointPath.value = currentPath;
      setPointFields(data.points || {}, data.point_sources || {});
      renderDetails(data);
      landmarks.load({ path: currentPath, kind: currentKind });
      tags.load(data);
      intros.load(data);
      editor.hidden = false;
      const preview = doc.getElementById("points-preview");
      if (preview) {
        preview.pause();
        preview.src = `/api/audio?path=${encodeURIComponent(currentPath)}`;
      }
      rememberReturn(opener);
      showTrackInfo();
      pointSay("");
      // Said once, after focus has reached the other track: said before,
      // the focus change would cut it off.
      const saved = takeSavedBeforeSwitch();
      if (saved) announceStatus(doc.getElementById("sr-status"), saved, { dwellMs: 6000, force: true });
    } catch (error) {
      if (requestCurrent(epoch, generation)) {
        const saved = takeSavedBeforeSwitch();
        const message = (saved ? `${saved} ` : "") + (error.status === 404
          ? "This recording is no longer in the library. Its current details and cue points are unavailable."
          : failureText("load track info", error));
        if (error.status === 404 && opener) {
          if (opener.isConnected && !opener.disabled) opener.focus();
          else {
            const list = sourceList?.isConnected ? sourceList
              : sourceList?.id ? doc.getElementById(sourceList.id) : null;
            const fallback = list || Array.from(sourcePanel?.children || [])
              .find((child) => child.tagName === "H2") || sourcePanel?.querySelector("summary");
            if (fallback?.tagName !== "SUMMARY") fallback?.setAttribute("tabindex", "-1");
            fallback?.focus();
          }
          announceStatus(doc.getElementById("sr-status"), message, { force: true, tone: "error" });
          return;
        }
        rememberReturn(opener);
        if (error.status === 404) {
          currentPath = null;
          tags.clear();
          intros.clear();
          landmarks.clear();
          editor.hidden = true;
          pointPath.value = "";
          const details = doc.getElementById("track-info-details");
          if (details) { details.replaceChildren(); details.hidden = true; }
          const preview = doc.getElementById("points-preview");
          if (preview) { preview.pause(); preview.removeAttribute("src"); preview.load(); }
          doc.getElementById("track-info-heading").textContent = "Track info";
        }
        showTrackInfo();
        announceStatus(pointStatus, message, { force: true, tone: "error" });
      }
    }
    finally {
      if (generation === protectedGeneration) { editor.inert = false; opening = false; }
      // A switch that stopped short (signed out, or the track went) leaves
      // focus somewhere steady, never on the page.
      if (switched) steadyAfterSwitch(opener);
    }
  }
  function steadyAfterSwitch(opener) {
    const active = doc.activeElement;
    if (active && active !== doc.body) return;
    const heading = doc.getElementById("track-info-heading");
    if (heading && !heading.closest("[hidden]") && focusAfterDialog(heading)) return;
    focusSteadyPlace(doc, dialogReturnPlace(opener));
  }
  doc.getElementById("edit-current-points").addEventListener("click", (event) => {
    const track = getCurrentTrack();
    void openEditor(track?.path, track?.kind || "music", event.currentTarget);
  });
  const onEditPoints = (event) => {
    void openEditor(event.detail?.path, event.detail?.kind || "music", event.detail?.opener || doc.activeElement);
  };
  doc.addEventListener("autodj:edit-points", onEditPoints);
  doc.getElementById("points-load")?.addEventListener("click", () => void openEditor(pointPath.value.trim(), currentKind));
  doc.getElementById("points-save").addEventListener("click", () => void savePoints());
  doc.getElementById("points-rescan")?.addEventListener("click", () => void rescanPoints());
  doc.getElementById("points-rescan-apply")?.addEventListener("click", () => void applyRescan());
  doc.getElementById("points-rescan-discard")?.addEventListener("click", () => {
    clearRescan("Cue point suggestions discarded.");
    doc.getElementById("points-rescan").focus();
  });
  editor.addEventListener("input", (event) => { if (event.target.matches("[data-point]")) clearRescan(); });
  allowOutro?.addEventListener("change", () => clearRescan());
  doc.querySelector('[data-point="overlap_start"]').addEventListener("input", updateNegativeOverlapHelp);
  editor.addEventListener("click", async (event) => {
    const setButton = event.target.closest("button[data-set-current]");
    if (setButton) {
      const preview = doc.getElementById("points-preview");
      const time = preview?.getAttribute("src") === `/api/audio?path=${encodeURIComponent(currentPath)}`
        ? preview.currentTime
        : (getCurrentTrack()?.path === currentPath ? getCurrentPosition() : null);
      if (!Number.isFinite(time)) { pointSay("This track is not playing in the preview or on the station.", "error"); return; }
      const name = setButton.dataset.setCurrent;
      try {
        const value = currentKind === "liner" && name === "overlap_start"
          ? Math.max(0, time - (parsePointTime(doc.querySelector('[data-point="start"]').value) ?? 0))
          : time;
        clearRescan();
        doc.querySelector(`[data-point="${name}"]`).value = formatPointTime(value);
      } catch (error) { pointSay(error.message, "error"); }
      return;
    }
    const button = event.target.closest("button[data-preview], button[data-rescan-preview]");
    if (!button) return;
    try {
      const proposed = button.dataset.rescanPreview;
      const name = proposed || button.dataset.preview;
      let time = proposed ? rescanResult?.points?.[name] : parsePointTime(doc.querySelector(`[data-point="${name}"]`).value);
      if (proposed && time != null && ["outro", "segue", "end"].includes(name)) time = Math.max(0, time - 2);
      if (currentKind === "liner" && name === "overlap_start") {
        if (time == null) {
          time = parsePointTime(doc.querySelector('[data-point="segue"]').value)
            ?? parsePointTime(doc.querySelector('[data-point="end"]').value);
          if (time == null) throw new Error("Set Segue or End before previewing the blank Overlap start.");
        } else {
          time += parsePointTime(doc.querySelector('[data-point="start"]').value) ?? 0;
        }
      }
      if (time == null) throw new Error("Enter a time before previewing it.");
      const preview = doc.getElementById("points-preview");
      if (!preview) throw new Error("Preview audio is unavailable.");
      preview.currentTime = time;
      await preview.play();
    }
    catch (error) { pointSay(error.message, "error"); }
  });
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  updatePolling();
  function clearProtectedData() {
    pendingFolderEdits.clear();
    suspended = true;
    clearRescan();
    protectedGeneration += 1;
    busyController.abort();
    busyController = new AbortController();
    clearAnnouncedStatus(status);
    clearAnnouncedStatus(pointStatus);
    if (timer != null) clearInterval(timer);
    timer = null;
    searchShown = false;
    settingsShown = false;
    opening = saving = false;
    const dialog = doc.getElementById("track-info-dialog");
    if (dialog?.open) dialog.close();
    const dialogTitle = doc.getElementById("track-info-dialog-title");
    if (dialogTitle) dialogTitle.textContent = "";
    const dialogError = doc.getElementById("track-info-error");
    if (dialogError) dialogError.textContent = "";
    editor.inert = false;
    editor.hidden = true;
    tags.clear();
    intros.clear();
    landmarks.clear();
    currentPath = null;
    currentRecord = null;
    currentKind = "music";
    currentName = "this track";
    if (allowOutro) allowOutro.checked = false;
    savedFields = "";
    list.replaceChildren();
    folderSignature = null;
    pathInput.value = "";
    clearAddError();
    autoRescan?.clear();
    advancedLength?.clear();
    musicFadeDrop?.clear();
    errorSignature = "";
    importRunning = false;
    errorList.hidden = true;
    errorList.open = false;
    errorList.querySelector("ul").replaceChildren();
    status.textContent = "";
    progress.textContent = "";
    pointStatus.textContent = "";
    returnTarget = null;
    returnView = null;
    returnMatch = null;
    returnContainer = null;
    returnButton.hidden = true;
    pointPath.value = "";
    for (const name of POINTS) {
      doc.querySelector(`[data-point="${name}"]`).value = "";
      const source = doc.querySelector(`[data-point-source="${name}"]`);
      if (source) source.textContent = "";
    }
    doc.getElementById("track-info-details")?.replaceChildren();
    const details = doc.getElementById("track-info-details");
    if (details) details.hidden = true;
    const heading = doc.getElementById("track-info-heading");
    if (heading) heading.textContent = "Track info";
    const empty = doc.getElementById("track-info-empty");
    if (empty) empty.hidden = false;
    const preview = doc.getElementById("points-preview");
    if (preview) { preview.pause(); preview.removeAttribute("src"); preview.load(); }
  }
  function resume() { suspended = false; updatePolling(); }
  // Refresh station-owned details without discarding shared cue or tag edits.
  const refreshIntros = () => {
    landmarks.refresh();
    const station = getStation();
    const changedStation = station !== currentStation;
    if (changedStation) clearRescan();
    currentStation = station;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    const path = currentPath;
    if (changedStation && currentRecord) {
      currentRecord.last_played = null;
      renderDetails(currentRecord);
    }
    const timestamp = path
      ? request(`/api/library/points?path=${encodeURIComponent(path)}${station === "default" ? "" : `&station=${encodeURIComponent(station)}`}`)
        .then((data) => {
          if (!requestCurrent(epoch, generation) || currentStation !== station || currentPath !== path || data.path !== path) return;
          if (currentRecord) currentRecord.last_played = data.last_played;
          renderDetails(currentRecord || data);
        }).catch((error) => {
          if (!requestCurrent(epoch, generation) || currentStation !== station || currentPath !== path) return;
          if (currentRecord) { currentRecord.last_played = null; renderDetails(currentRecord); }
          onBackgroundError(error);
        })
      : Promise.resolve();
    return Promise.all([intros.refresh?.(), timestamp]);
  };
  return { refresh, openEditor, clearProtectedData, resume, refreshIntros, destroy() { clearProtectedData(); tags.destroy(); intros.destroy(); returnButton.remove(); window.removeEventListener("hashchange", updatePolling); doc.removeEventListener("visibilitychange", updatePolling); doc.removeEventListener("autodj:edit-points", onEditPoints); } };
}
