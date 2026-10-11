// Music and liner source folders plus the saved song-points editor.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { fmtTrack } from "./dom-helpers.js";
import { lastSeenWords } from "./devices.js";
import { formatPlayedAt } from "./history-format.js";

const POINTS = ["start", "intro", "outro", "segue", "end", "overlap_start"];

export function parsePointTime(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^\d+(?:\.\d+)?$/.test(text)) {
    const seconds = Number(text);
    if (Number.isFinite(seconds)) return seconds;
  }
  const match = /^(\d+):([0-5]?\d(?:\.\d+)?)$/.exec(text);
  if (match) {
    const seconds = Number(match[1]) * 60 + Number(match[2]);
    if (Number.isFinite(seconds)) return seconds;
  }
  throw new Error("Enter seconds or minutes:seconds, for example 5.3 or 1:05.");
}

export function formatPointTime(value) {
  if (value == null) return "";
  const centiseconds = Math.round(value * 100);
  const minutes = Math.floor(centiseconds / 6000);
  const seconds = Math.floor(centiseconds / 100) % 60;
  const fractionValue = centiseconds % 100;
  const whole = String(seconds).padStart(2, "0");
  const fraction = fractionValue ? `.${String(fractionValue).padStart(2, "0").replace(/0$/, "")}` : "";
  return `${minutes}:${whole}${fraction}`;
}

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

export function installLibraryFolders({ document: doc = document, request = requestJson, getCurrentTrack = () => null, getCurrentPosition = () => null, onFolders }) {
  const panel = doc.getElementById("panel-library");
  const list = doc.getElementById("source-folders");
  const status = doc.getElementById("source-folders-status");
  const pathInput = doc.getElementById("source-folder-path");
  const kindInput = doc.getElementById("source-folder-kind");
  const recursiveInput = doc.getElementById("source-folder-recursive");
  const advancedInput = doc.getElementById("source-folder-advanced");
  const editor = doc.getElementById("points-editor");
  const pointStatus = doc.getElementById("points-status");
  const pointPath = doc.getElementById("points-path");
  let timer = null;
  let currentPath = null;
  let currentKind = "music";
  let currentName = "this track";
  let savedFields = "";
  let opening = false;
  let saving = false;
  let folderSignature = null;
  let protectedGeneration = 0;
  let suspended = false;
  let addError = null;

  function requestCurrent(epoch, generation) {
    return !suspended && generation === protectedGeneration && isAuthenticatedRequestCurrent(epoch);
  }

  function say(message, error = false) {
    status.textContent = message;
    status.dataset.tone = error ? "error" : "info";
  }
  function clearAddError() {
    addError = null;
    pathInput.removeAttribute("aria-invalid");
    pathInput.removeAttribute("aria-describedby");
  }
  function reportAddError(message) {
    addError = message;
    pathInput.setAttribute("aria-invalid", "true");
    pathInput.setAttribute("aria-describedby", status.id);
    say(message, true);
    pathInput.focus();
  }
  function pointSay(message, error = false) {
    pointStatus.textContent = message;
    pointStatus.dataset.tone = error ? "error" : "info";
  }
  function renderFolders(data) {
    const folders = data.folders || [];
    const signature = JSON.stringify(folders.map(({ id, path, kind, recursive, advanced }) => ({ id, path, kind, recursive, advanced })));
    if (signature !== folderSignature) {
      const focused = list.contains(doc.activeElement) ? {
        id: doc.activeElement.closest("li")?.dataset.id,
        field: doc.activeElement.dataset.field,
        action: doc.activeElement.dataset.action,
      } : null;
      list.innerHTML = folders.map((folder) => `<li data-id="${esc(folder.id)}">
        <strong>${folder.kind === "liner" ? "Voice liner" : "Music"}:</strong> ${esc(folder.path)}
        <label><input type="checkbox" data-field="recursive" ${folder.recursive ? "checked" : ""}> Add subfolders</label>
        ${folder.kind === "music" ? `<label><input type="checkbox" data-field="advanced" ${folder.advanced ? "checked" : ""}> Perform advanced analysis</label>` : ""}
        <button type="button" data-action="remove">Remove folder</button>
      </li>`).join("") || "<li>No folders added yet.</li>";
      folderSignature = signature;
      onFolders?.(folders);
      if (focused) {
        const selector = focused.field ? `input[data-field="${focused.field}"]` : `button[data-action="${focused.action}"]`;
        const replacement = [...list.querySelectorAll("li")].find((row) => row.dataset.id === focused.id)?.querySelector(selector);
        (replacement || doc.getElementById("source-folder-add")).focus();
      }
    }
    if (addError) {
      say(addError, true);
    } else if (data.import?.running) {
      const job = data.import;
      say(`Importing tracks (${job.phase || "working"}): ${job.done || 0} of ${job.total || 0}${job.current ? `; ${job.current}` : ""}${job.errors?.length ? `; errors: ${job.errors.join("; ")}` : ""}`, Boolean(job.errors?.length));
    } else if (data.import?.errors?.length) {
      say(`Import finished with ${data.import.errors.length} error(s): ${data.import.errors.join("; ")}`, true);
    } else {
      say(`${data.tracks_ready ?? 0} tracks ready; ${data.tracks_indexed ?? 0} indexed.`);
    }
  }
  async function refresh() {
    if (!panel || panel.hidden || suspended) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try {
      const data = await request("/api/library/folders");
      if (requestCurrent(epoch, generation)) renderFolders(data);
    } catch (error) {
      if (requestCurrent(epoch, generation)) say(`Could not load folders: ${error.message}`, true);
    }
  }
  function visible() { return !suspended && !panel.hidden && !doc.hidden; }
  function updatePolling() {
    if (visible() && timer == null) {
      void refresh();
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
      if (status.textContent === previousError) say("");
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
      say("Folder added.");
      await refresh();
    } catch (error) { if (requestCurrent(epoch, generation)) reportAddError(`Could not add folder: ${error.message}`); }
  });
  list.addEventListener("change", async (event) => {
    if (suspended) return;
    const input = event.target.closest("input[data-field]");
    if (!input) return;
    const row = input.closest("li");
    const body = { [input.dataset.field]: input.checked };
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    input.disabled = true;
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (requestCurrent(epoch, generation)) say("Folder settings saved.");
    } catch (error) {
      if (requestCurrent(epoch, generation)) { say(`Could not save folder settings: ${error.message}`, true); await refresh(); }
    } finally {
      if (requestCurrent(epoch, generation)) input.disabled = false;
    }
  });
  list.addEventListener("click", async (event) => {
    if (suspended) return;
    const button = event.target.closest('button[data-action="remove"]');
    if (!button) return;
    const row = button.closest("li");
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    button.disabled = true;
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "DELETE" });
      if (!requestCurrent(epoch, generation)) return;
      say("Folder removed from the library.");
      await refresh();
    } catch (error) { if (requestCurrent(epoch, generation)) say(`Could not remove folder: ${error.message}`, true); }
    finally { if (requestCurrent(epoch, generation)) button.disabled = false; }
  });
  doc.getElementById("source-folder-rescan").addEventListener("click", async () => {
    if (suspended) return;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try { await request("/api/library/rescan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); if (requestCurrent(epoch, generation)) { say("Rescan started."); await refresh(); } }
    catch (error) { if (requestCurrent(epoch, generation)) say(`Could not start rescan: ${error.message}`, true); }
  });

  function setPointFields(points, sources = {}) {
    const heading = doc.getElementById("points-heading");
    if (heading) heading.textContent = "Cue points";
    const linerHelp = doc.getElementById("points-liner-help");
    if (linerHelp) linerHelp.hidden = currentKind !== "liner";
    for (const name of POINTS) {
      const field = doc.querySelector(`[data-point="${name}"]`);
      field.value = formatPointTime(currentKind === "liner" && name === "overlap_start" && points?.[name] == null ? 0 : points?.[name]);
      const applicable = currentKind === "liner" ? ["start", "segue", "end", "overlap_start"].includes(name) : name !== "overlap_start";
      field.closest("label").hidden = !applicable;
      doc.querySelector(`[data-preview="${name}"]`).hidden = !applicable;
      const setCurrent = doc.querySelector(`[data-set-current="${name}"]`);
      if (setCurrent) setCurrent.hidden = !applicable;
      const provenance = doc.querySelector(`[data-point-source="${name}"]`);
      if (provenance) provenance.textContent = sources[name] ? ` (${String(sources[name]).replaceAll("_", " ")})` : " (not set)";
    }
    savedFields = fieldSnapshot();
  }
  function fieldSnapshot() {
    return JSON.stringify(POINTS.map((name) => doc.querySelector(`[data-point="${name}"]`).value));
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
      ["Audio preparation", data.prepared == null ? "Not available" : data.prepared ? "Ready" : "Not prepared yet"],
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
    saving = true;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    const wasInert = editor.inert;
    editor.inert = true;
    try {
      const points = {};
      for (const name of POINTS) {
        const field = doc.querySelector(`[data-point="${name}"]`);
        if (!field.closest("label").hidden) points[name] = parsePointTime(field.value);
      }
      const data = await request("/api/library/points", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: currentPath, points }) });
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
    const error = doc.getElementById("track-info-error");
    doc.getElementById("track-info-dialog-title").textContent = `Save changes to ${currentName}?`;
    error.textContent = "";
    dialog.returnValue = "";
    return new Promise((resolve) => {
      async function accept() {
        if (saving) return;
        save.disabled = discard.disabled = true;
        const saved = await savePoints(error);
        save.disabled = discard.disabled = false;
        if (saved) dialog.close("save");
      }
      function reject() { if (!saving) dialog.close("discard"); }
      function cancel(event) { if (saving) event.preventDefault(); }
      function closed() {
        save.removeEventListener("click", accept);
        discard.removeEventListener("click", reject);
        dialog.removeEventListener("cancel", cancel);
        dialog.removeEventListener("close", closed);
        resolve(dialog.returnValue === "save" || dialog.returnValue === "discard");
      }
      save.addEventListener("click", accept);
      discard.addEventListener("click", reject);
      dialog.addEventListener("cancel", cancel);
      dialog.addEventListener("close", closed);
      dialog.showModal();
      save.focus();
    });
  }
  async function openEditor(path, kind = "music") {
    if (suspended || opening || saving) return;
    if (!path) { showTrackInfo(); pointSay("Choose a track first.", true); return; }
    if (path === currentPath && savedFields !== fieldSnapshot()) { showTrackInfo(); return; }
    opening = true;
    const epoch = captureAuthenticatedRequestEpoch();
    const generation = protectedGeneration;
    try {
      if (currentPath && savedFields !== fieldSnapshot() && !await confirmTrackChange()) return;
      if (!requestCurrent(epoch, generation)) return;
      editor.inert = true;
      const data = await request(`/api/library/points?path=${encodeURIComponent(path)}`);
      if (!requestCurrent(epoch, generation)) return;
      currentPath = data.path || path;
      currentKind = data.kind || kind;
      currentName = data.title ? fmtTrack(data) : currentPath.split(/[\\/]/).pop();
      pointPath.value = currentPath;
      setPointFields(data.points || {}, data.point_sources || {});
      renderDetails(data);
      editor.hidden = false;
      const preview = doc.getElementById("points-preview");
      if (preview) {
        preview.pause();
        preview.src = `/api/audio?path=${encodeURIComponent(currentPath)}`;
      }
      showTrackInfo();
      pointSay("");
    } catch (error) {
      if (requestCurrent(epoch, generation)) { showTrackInfo(); pointSay(`Could not load track info: ${error.message}`, true); }
    }
    finally { if (generation === protectedGeneration) { editor.inert = false; opening = false; } }
  }
  doc.getElementById("edit-current-points").addEventListener("click", () => {
    const track = getCurrentTrack();
    void openEditor(track?.path, track?.kind || "music");
  });
  const onEditPoints = (event) => {
    void openEditor(event.detail?.path, event.detail?.kind || "music");
  };
  doc.addEventListener("autodj:edit-points", onEditPoints);
  doc.getElementById("points-load")?.addEventListener("click", () => void openEditor(pointPath.value.trim(), currentKind));
  doc.getElementById("points-save").addEventListener("click", () => void savePoints());
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
      const time = parsePointTime(doc.querySelector(`[data-point="${button.dataset.preview}"]`).value);
      if (time == null) throw new Error("Enter a time before previewing it.");
      const preview = doc.getElementById("points-preview");
      if (!preview) throw new Error("Preview audio is unavailable.");
      preview.currentTime = currentKind === "liner" && button.dataset.preview === "overlap_start"
        ? (parsePointTime(doc.querySelector('[data-point="start"]').value) ?? 0) + time
        : time;
      await preview.play();
    }
    catch (error) { pointSay(error.message, true); }
  });
  doc.getElementById("source-folder-advanced").disabled = false;
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  updatePolling();
  function clearProtectedData() {
    suspended = true;
    protectedGeneration += 1;
    if (timer != null) clearInterval(timer);
    timer = null;
    opening = saving = false;
    const dialog = doc.getElementById("track-info-dialog");
    if (dialog?.open) dialog.close();
    const dialogTitle = doc.getElementById("track-info-dialog-title");
    if (dialogTitle) dialogTitle.textContent = "";
    const dialogError = doc.getElementById("track-info-error");
    if (dialogError) dialogError.textContent = "";
    editor.inert = false;
    editor.hidden = true;
    currentPath = null;
    currentKind = "music";
    currentName = "this track";
    savedFields = "";
    list.replaceChildren();
    folderSignature = null;
    pathInput.value = "";
    clearAddError();
    status.textContent = "";
    pointStatus.textContent = "";
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
  return { refresh, openEditor, clearProtectedData, resume, destroy() { clearProtectedData(); window.removeEventListener("hashchange", updatePolling); doc.removeEventListener("visibilitychange", updatePolling); doc.removeEventListener("autodj:edit-points", onEditPoints); } };
}
