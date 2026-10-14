// Music and liner source folders plus the saved song-points editor.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { fmtTrack } from "./dom-helpers.js";
import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { lastSeenWords } from "./devices.js";
import { formatPlayedAt } from "./history-format.js";
import { announceStatus } from "./live-region.js";
import { installAutoRescan } from "./auto-rescan.js";
import { installMatchedIntros } from "./matched-intros.js";
import { installTagEditor } from "./tag-editor.js";
import { returnToView } from "./tabs.js";

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
  throw new Error("Enter seconds or minutes:seconds, for example 5.3 or 1:05.");
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


export function installLibraryFolders({ document: doc = document, request = requestJson, getCurrentTrack = () => null, getCurrentPosition = () => null, onFolders }) {
  const panel = doc.getElementById("panel-library");
  const searchPanel = doc.getElementById("library-kind")?.closest("section[data-view]") || null;
  let searchShown = false;
  const list = doc.getElementById("source-folders");
  const tableNavigation = installTableNavigation(list);
  const pendingFolderEdits = new Set();
  const status = doc.getElementById("source-folders-status");
  status.textContent = "";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.setAttribute("aria-atomic", "true");
  let progress = doc.getElementById("source-folders-progress");
  if (!progress) {
    progress = doc.createElement("p");
    progress.id = "source-folders-progress";
    status.insertAdjacentElement("afterend", progress);
  }
  const pathInput = doc.getElementById("source-folder-path");
  const kindInput = doc.getElementById("source-folder-kind");
  const recursiveInput = doc.getElementById("source-folder-recursive");
  const advancedInput = doc.getElementById("source-folder-advanced");
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
  let currentKind = "music";
  let currentName = "this track";
  let savedFields = "";
  let opening = false;
  let saving = false;
  let folderSignature = null;
  let protectedGeneration = 0;
  let actionAnnouncement = 0;
  let suspended = false;
  let addError = null;
  let returnTarget = null;
  let returnView = null;
  let returnMatch = null;
  let returnContainer = null;
  const tags = installTagEditor({
    document: doc,
    request,
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

  function rememberReturn(opener) {
    const panel = opener?.closest?.("section[data-view]");
    returnView = panel?.dataset.view !== "track-info" ? panel?.dataset.view : null;
    returnTarget = returnView ? opener : null;
    returnContainer = returnTarget?.closest("ul, ol, table") || null;
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

  function announceAction(message, error = false) {
    status.dataset.tone = error ? "error" : "info";
    status.textContent = "";
    const announcement = ++actionAnnouncement;
    setTimeout(() => {
      if (!suspended && announcement === actionAnnouncement) status.textContent = message;
    }, 0);
  }
  function clearAddError() {
    actionAnnouncement += 1;
    addError = null;
    pathInput.removeAttribute("aria-invalid");
    pathInput.removeAttribute("aria-describedby");
  }
  function reportAddError(message) {
    addError = message;
    pathInput.setAttribute("aria-invalid", "true");
    pathInput.setAttribute("aria-describedby", status.id);
    announceAction(message, true);
    pathInput.focus();
  }
  function pointSay(message, error = false) {
    pointStatus.textContent = message;
    pointStatus.dataset.tone = error ? "error" : "info";
  }
  function renderFolders(data) {
    const folders = data.folders || [];
    const signature = JSON.stringify(folders.map(({ id, path, kind, recursive, advanced }) => ({ id, path, kind, recursive, advanced })));
    if (signature !== folderSignature && pendingFolderEdits.size === 0) {
      const focused = list.contains(doc.activeElement) ? {
        id: doc.activeElement.closest("tr")?.dataset.id,
        column: doc.activeElement.closest("td")?.cellIndex,
      } : null;
      list.replaceChildren(...(folders.length ? folders.map((folder) => {
        const recursive = doc.createElement("input");
        recursive.type = "checkbox"; recursive.dataset.field = "recursive"; recursive.checked = !!folder.recursive;
        const recursiveLabel = doc.createElement("label"); recursiveLabel.append(recursive, " Add subfolders");
        let advancedLabel = "";
        if (folder.kind === "music") {
          const advanced = doc.createElement("input");
          advanced.type = "checkbox"; advanced.dataset.field = "advanced"; advanced.checked = !!folder.advanced;
          advancedLabel = doc.createElement("label"); advancedLabel.append(advanced, " Perform advanced analysis");
        }
        const remove = doc.createElement("button"); remove.type = "button"; remove.dataset.action = "remove"; remove.textContent = "Remove folder";
        const row = tableRow(doc, [folder.kind === "liner" ? "Voice liner" : "Music", folder.path, recursiveLabel, advancedLabel, remove], { rowHeaderIndex: 1 });
        row.dataset.id = folder.id;
        return row;
      }) : [tableRow(doc, ["No folders added yet."], { colspan: [5] })]));
      folderSignature = signature;
      tableNavigation.refresh();
      onFolders?.(folders);
      if (focused) {
        const cell = [...list.querySelectorAll("tr")].find((row) => row.dataset.id === focused.id)?.cells[focused.column];
        const replacement = cell?.querySelector("button:not(:disabled), input:not(:disabled)") || cell;
        (replacement || doc.getElementById("source-folder-add")).focus();
      }
    }
    if (data.import?.running) {
      const job = data.import;
      progress.textContent = `Importing tracks (${job.phase || "working"}): ${job.done || 0} of ${job.total || 0}${job.current ? `; ${job.current}` : ""}${job.errors?.length ? `; errors: ${job.errors.join("; ")}` : ""}`;
    } else if (data.import?.errors?.length) {
      progress.textContent = `Import finished with ${data.import.errors.length} error(s): ${data.import.errors.join("; ")}`;
    } else {
      progress.textContent = `${data.tracks_ready ?? 0} tracks ready; ${data.tracks_indexed ?? 0} indexed.`;
    }
  }
  async function refresh() {
    if (suspended || ((!panel || panel.closest("[hidden]")) && (!searchPanel || searchPanel.closest("[hidden]")))) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try {
      const data = await request("/api/library/folders");
      if (requestCurrent(epoch, generation)) renderFolders(data);
    } catch (error) {
      if (requestCurrent(epoch, generation)) progress.textContent = `Could not load folders: ${error.message}`;
    }
  }
  function visible() { return !suspended && !!panel && !panel.closest("[hidden]") && !doc.hidden; }
  function updatePolling() {
    const searching = !suspended && !!searchPanel && !searchPanel.closest("[hidden]") && !doc.hidden;
    if (searching && !searchShown && !visible()) void refresh();
    searchShown = searching;
    if (visible() && timer == null) {
      void refresh();
      void autoRescan?.load();
      timer = setInterval(refresh, 3000);
    } else if (!visible() && timer != null) {
      clearInterval(timer);
      timer = null;
    }
  }

  kindInput.addEventListener("change", () => {
    advancedInput.disabled = kindInput.value === "liner";
    advancedInput.checked = kindInput.value !== "liner";
  });
  pathInput.addEventListener("input", () => {
    if (addError) {
      const previousError = addError;
      clearAddError();
      if (status.textContent === previousError) status.textContent = "";
    }
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
      await request("/api/library/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, kind: kindInput.value, recursive: recursiveInput.checked, advanced: kindInput.value === "music" && advancedInput.checked }) });
      if (!requestCurrent(epoch, generation)) return;
      pathInput.value = "";
      announceAction(`Folder added; import queued for ${path}.`);
      await refresh();
    } catch (error) { if (requestCurrent(epoch, generation)) reportAddError(`Could not add folder: ${error.message}`); }
  });
  list.addEventListener("change", async (event) => {
    if (suspended) return;
    const input = event.target.closest("input[data-field]");
    if (!input || pendingFolderEdits.has(input)) return;
    const row = input.closest("tr");
    const body = { [input.dataset.field]: input.checked };
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    pendingFolderEdits.add(input);
    input.setAttribute("aria-disabled", "true");
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (requestCurrent(epoch, generation)) announceAction("Folder settings saved.");
    } catch (error) {
      if (requestCurrent(epoch, generation)) { input.checked = !body[input.dataset.field]; announceAction(`Could not save folder settings: ${error.message}`, true); }
    } finally {
      pendingFolderEdits.delete(input);
      if (requestCurrent(epoch, generation)) { input.removeAttribute("aria-disabled"); await refresh(); }
    }
  });
  list.addEventListener("click", async (event) => {
    if (suspended) return;
    if (event.target.closest('[aria-disabled="true"]')) { event.preventDefault(); return; }
    const button = event.target.closest('button[data-action="remove"]');
    if (!button) return;
    const row = button.closest("tr");
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    pendingFolderEdits.add(button);
    button.setAttribute("aria-disabled", "true");
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "DELETE" });
      if (!requestCurrent(epoch, generation)) return;
      announceAction("Folder removed from the library.");
    } catch (error) { if (requestCurrent(epoch, generation)) announceAction(`Could not remove folder: ${error.message}`, true); }
    finally {
      pendingFolderEdits.delete(button);
      if (requestCurrent(epoch, generation)) { button.removeAttribute("aria-disabled"); await refresh(); }
    }
  });
  const autoRescan = installAutoRescan({
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
    catch (error) { if (requestCurrent(epoch, generation)) announceAction(`Could not start rescan: ${error.message}`, true); }
  });

  function setPointFields(points, sources = {}) {
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
    const path = data.path || currentPath;
    const split = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
    const lastPlayed = data.last_played > 0
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
      ["Advanced analysis", analysis],
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
  async function savePoints(errorTarget = null) {
    if (saving || !currentPath) return false;
    doc.getElementById("points-preview")?.pause();
    saving = true;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    const wasInert = editor.inert;
    editor.inert = true;
    try {
      const points = {};
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
      if (currentKind === "liner" && allowOutro) body.allow_outro = allowOutro.checked;
      const data = await request("/api/library/points", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!requestCurrent(epoch, generation)) return false;
      if (data.points) setPointFields(data.points, data.point_sources || {});
      else savedFields = fieldSnapshot();
      pointSay(`Saved changes to ${currentName}.`);
      return true;
    } catch (error) {
      if (!requestCurrent(epoch, generation)) return false;
      const message = `Could not save changes: ${error.message}`;
      if (errorTarget) errorTarget.textContent = `${message} Press Escape to return to editing.`;
      else pointSay(message, true);
      return false;
    } finally {
      if (generation === protectedGeneration) {
        saving = false;
        editor.inert = wasInert;
      }
    }
  }
  function confirmTrackChange() {
    const dialog = doc.getElementById("track-info-dialog");
    if (!dialog || dialog.open) {
      showTrackInfo();
      pointSay("Save your changes before choosing another track.", true);
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
    return new Promise((resolve) => {
      async function accept() {
        if (saving) return;
        save.disabled = discard.disabled = true;
        if (keep) keep.disabled = true;
        const saved = (savedFields === fieldSnapshot() || await savePoints(error))
          && (!tags.dirty() || await tags.save(error));
        save.disabled = discard.disabled = false;
        if (keep) keep.disabled = false;
        if (saved) dialog.close("save");
      }
      function reject() {
        if (saving) return;
        doc.getElementById("points-preview")?.pause();
        dialog.close("discard");
      }
      function stay() { if (!saving) dialog.close("keep"); }
      function cancel(event) { if (saving) event.preventDefault(); }
      function closed() {
        save.removeEventListener("click", accept);
        discard.removeEventListener("click", reject);
        keep?.removeEventListener("click", stay);
        dialog.removeEventListener("cancel", cancel);
        dialog.removeEventListener("close", closed);
        const switching = dialog.returnValue === "save" || dialog.returnValue === "discard";
        if (!switching && returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus();
        resolve(switching);
      }
      save.addEventListener("click", accept);
      discard.addEventListener("click", reject);
      keep?.addEventListener("click", stay);
      dialog.addEventListener("cancel", cancel);
      dialog.addEventListener("close", closed);
      dialog.showModal();
      save.focus();
    });
  }
  async function openEditor(path, kind = "music", opener = null) {
    if (suspended || opening || saving) return;
    const sourceList = opener?.closest?.("ul, ol, table");
    const sourcePanel = opener?.closest?.("section[data-view]");
    if (!path) { rememberReturn(opener); showTrackInfo(); pointSay("Choose a track first.", true); return; }
    if (path === currentPath && unsaved()) { rememberReturn(opener); showTrackInfo(); return; }
    opening = true;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try {
      if (currentPath && unsaved() && !await confirmTrackChange()) return;
      if (!requestCurrent(epoch, generation)) return;
      editor.inert = true;
      const data = await request(`/api/library/points?path=${encodeURIComponent(path)}`);
      if (!requestCurrent(epoch, generation)) return;
      currentPath = data.path || path;
      currentKind = data.kind || kind;
      currentName = data.title ? fmtTrack(data) : currentPath.split(/[\\/]/).pop();
      if (allowOutro) allowOutro.checked = currentKind === "liner" && data.allow_outro === true;
      pointPath.value = currentPath;
      setPointFields(data.points || {}, data.point_sources || {});
      renderDetails(data);
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
    } catch (error) {
      if (requestCurrent(epoch, generation)) {
        const message = error.status === 404
          ? "This recording is no longer in the library. Its current details and cue points are unavailable."
          : `Could not load track info: ${error.message}`;
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
    finally { if (generation === protectedGeneration) { editor.inert = false; opening = false; } }
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
  doc.querySelector('[data-point="overlap_start"]').addEventListener("input", updateNegativeOverlapHelp);
  editor.addEventListener("click", async (event) => {
    const setButton = event.target.closest("button[data-set-current]");
    if (setButton) {
      const preview = doc.getElementById("points-preview");
      const time = preview?.getAttribute("src") === `/api/audio?path=${encodeURIComponent(currentPath)}`
        ? preview.currentTime
        : (getCurrentTrack()?.path === currentPath ? getCurrentPosition() : null);
      if (!Number.isFinite(time)) { pointSay("This track is not playing in the preview or on the station.", true); return; }
      const name = setButton.dataset.setCurrent;
      try {
        const value = currentKind === "liner" && name === "overlap_start"
          ? Math.max(0, time - (parsePointTime(doc.querySelector('[data-point="start"]').value) ?? 0))
          : time;
        doc.querySelector(`[data-point="${name}"]`).value = formatPointTime(value);
      } catch (error) { pointSay(error.message, true); }
      return;
    }
    const button = event.target.closest("button[data-preview]");
    if (!button) return;
    try {
      const name = button.dataset.preview;
      let time = parsePointTime(doc.querySelector(`[data-point="${name}"]`).value);
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
    catch (error) { pointSay(error.message, true); }
  });
  doc.getElementById("source-folder-advanced").disabled = false;
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  updatePolling();
  function clearProtectedData() {
    pendingFolderEdits.clear();
    suspended = true;
    protectedGeneration += 1;
    actionAnnouncement += 1;
    if (timer != null) clearInterval(timer);
    timer = null;
    searchShown = false;
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
    currentPath = null;
    currentKind = "music";
    currentName = "this track";
    if (allowOutro) allowOutro.checked = false;
    savedFields = "";
    list.replaceChildren();
    folderSignature = null;
    pathInput.value = "";
    clearAddError();
    autoRescan?.clear();
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
  // Matched intros are each station's own (#70); the rest of Track info is shared.
  const refreshIntros = () => intros.refresh();
  return { refresh, openEditor, clearProtectedData, resume, refreshIntros, destroy() { clearProtectedData(); tags.destroy(); intros.destroy(); returnButton.remove(); window.removeEventListener("hashchange", updatePolling); doc.removeEventListener("visibilitychange", updatePolling); doc.removeEventListener("autodj:edit-points", onEditPoints); } };
}
