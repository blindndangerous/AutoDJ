// Music and liner source folders plus the saved song-points editor.
import { requestJson } from "./api-client.js";

const POINTS = ["start", "intro", "outro", "segue", "end", "overlap_start"];

export function parsePointTime(value) {
  const text = String(value ?? "").trim();
  if (!text) return null;
  if (/^\d+(?:\.\d+)?$/.test(text)) return Number(text);
  const match = /^(\d+):([0-5]?\d(?:\.\d+)?)$/.exec(text);
  if (match) return Number(match[1]) * 60 + Number(match[2]);
  throw new Error("Enter seconds or minutes:seconds, for example 5.3 or 1:05.");
}

export function formatPointTime(value) {
  if (value == null) return "";
  const minutes = Math.floor(value / 60);
  const seconds = value - minutes * 60;
  const whole = String(Math.floor(seconds)).padStart(2, "0");
  const fraction = seconds % 1 ? `.${(seconds % 1).toFixed(2).slice(2).replace(/0+$/, "")}` : "";
  return `${minutes}:${whole}${fraction}`;
}

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

export function installLibraryFolders({ document: doc = document, request = requestJson, getCurrentTrack = () => null, getCurrentPosition = () => null }) {
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
  let folderSignature = null;

  function say(message, error = false) {
    status.textContent = message;
    status.dataset.tone = error ? "error" : "info";
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
      if (focused) {
        const selector = focused.field ? `input[data-field="${focused.field}"]` : `button[data-action="${focused.action}"]`;
        const replacement = [...list.querySelectorAll("li")].find((row) => row.dataset.id === focused.id)?.querySelector(selector);
        (replacement || doc.getElementById("source-folder-add")).focus();
      }
    }
    if (data.import?.running) {
      const job = data.import;
      say(`Importing tracks (${job.phase || "working"}): ${job.done || 0} of ${job.total || 0}${job.current ? `; ${job.current}` : ""}${job.errors?.length ? `; errors: ${job.errors.join("; ")}` : ""}`, Boolean(job.errors?.length));
    } else if (data.import?.errors?.length) {
      say(`Import finished with ${data.import.errors.length} error(s): ${data.import.errors.join("; ")}`, true);
    } else {
      say(`${data.tracks_ready ?? 0} tracks ready; ${data.tracks_indexed ?? 0} indexed.`);
    }
  }
  async function refresh() {
    if (!panel || panel.hidden) return;
    try { renderFolders(await request("/api/library/folders")); }
    catch (error) { say(`Could not load folders: ${error.message}`, true); }
  }
  function visible() { return !panel.hidden && !doc.hidden; }
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
  doc.getElementById("source-folder-add").addEventListener("click", async () => {
    const path = pathInput.value.trim();
    if (!path) { say("Enter a folder path on the AutoDJ machine.", true); pathInput.focus(); return; }
    try {
      await request("/api/library/folders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, kind: kindInput.value, recursive: recursiveInput.checked, advanced: kindInput.value === "music" && advancedInput.checked }) });
      pathInput.value = "";
      say("Folder added.");
      await refresh();
    } catch (error) { say(`Could not add folder: ${error.message}`, true); }
  });
  list.addEventListener("change", async (event) => {
    const input = event.target.closest("input[data-field]");
    if (!input) return;
    const row = input.closest("li");
    const body = { [input.dataset.field]: input.checked };
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      say("Folder settings saved.");
    } catch (error) { say(`Could not save folder settings: ${error.message}`, true); await refresh(); }
  });
  list.addEventListener("click", async (event) => {
    const button = event.target.closest('button[data-action="remove"]');
    if (!button) return;
    const row = button.closest("li");
    try {
      await request(`/api/library/folders/${encodeURIComponent(row.dataset.id)}`, { method: "DELETE" });
      say("Folder removed from the library.");
      await refresh();
    } catch (error) { say(`Could not remove folder: ${error.message}`, true); }
  });
  doc.getElementById("source-folder-rescan").addEventListener("click", async () => {
    try { await request("/api/library/rescan", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); say("Rescan started."); await refresh(); }
    catch (error) { say(`Could not start rescan: ${error.message}`, true); }
  });

  function setPointFields(points, sources = {}) {
    for (const name of POINTS) {
      const field = doc.querySelector(`[data-point="${name}"]`);
      field.value = formatPointTime(currentKind === "liner" && name === "overlap_start" && points?.[name] == null ? 0 : points?.[name]);
      const applicable = currentKind === "liner" ? ["start", "end", "overlap_start"].includes(name) : name !== "overlap_start";
      field.closest("label").hidden = !applicable;
      doc.querySelector(`[data-preview="${name}"]`).hidden = !applicable;
      const setCurrent = doc.querySelector(`[data-set-current="${name}"]`);
      if (setCurrent) setCurrent.hidden = !applicable;
      const provenance = doc.querySelector(`[data-point-source="${name}"]`);
      if (provenance) provenance.textContent = sources[name] ? ` (${String(sources[name]).replaceAll("_", " ")})` : " (not set)";
    }
  }
  async function openEditor(path, kind = "music") {
    if (!path) { pointSay("Choose a track first.", true); return; }
    currentPath = path;
    currentKind = kind;
    pointPath.value = path;
    try {
      const data = await request(`/api/library/points?path=${encodeURIComponent(path)}`);
      currentKind = data.kind || kind;
      setPointFields(data.points || {}, data.point_sources || {});
      editor.hidden = false;
      const nowPanel = doc.getElementById("panel-now");
      if (nowPanel?.hidden) doc.getElementById("tab-now")?.click();
      const preview = doc.getElementById("points-preview");
      if (preview) preview.src = `/api/audio?path=${encodeURIComponent(path)}`;
      pointSay(`Editing ${data.path || path}${data.length ? ` (${formatPointTime(data.length)})` : ""}.`);
      editor.querySelector("[data-point='start']").focus();
    } catch (error) { pointSay(`Could not load song points: ${error.message}`, true); }
  }
  doc.getElementById("edit-current-points").addEventListener("click", () => {
    const track = getCurrentTrack();
    void openEditor(track?.path, track?.kind || "music");
  });
  const onEditPoints = (event) => {
    if (doc.getElementById("panel-now")?.hidden) doc.getElementById("tab-now")?.click();
    void openEditor(event.detail?.path, event.detail?.kind || "music");
  };
  doc.addEventListener("autodj:edit-points", onEditPoints);
  doc.getElementById("points-load").addEventListener("click", () => void openEditor(pointPath.value.trim(), currentKind));
  doc.getElementById("points-save").addEventListener("click", async () => {
    try {
      const points = {};
      for (const name of POINTS) {
        const field = doc.querySelector(`[data-point="${name}"]`);
        if (!field.closest("label").hidden) points[name] = parsePointTime(field.value);
      }
      await request("/api/library/points", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: currentPath, points }) });
      pointSay(`${currentKind === "liner" ? "Liner" : "Song"} points saved.`);
    } catch (error) { pointSay(error.message.startsWith("Enter seconds") ? error.message : `Could not save ${currentKind} points: ${error.message}`, true); }
  });
  editor.addEventListener("click", async (event) => {
    const setButton = event.target.closest("button[data-set-current]");
    if (setButton) {
      const preview = doc.getElementById("points-preview");
      const time = preview?.getAttribute("src") === `/api/audio?path=${encodeURIComponent(currentPath)}`
        ? preview.currentTime
        : (getCurrentTrack()?.path === currentPath ? getCurrentPosition() : null);
      if (!Number.isFinite(time)) { pointSay("This track is not playing in the preview or on the station.", true); return; }
      doc.querySelector(`[data-point="${setButton.dataset.setCurrent}"]`).value = formatPointTime(time);
      return;
    }
    const button = event.target.closest("button[data-preview]");
    if (!button) return;
    try {
      const time = parsePointTime(doc.querySelector(`[data-point="${button.dataset.preview}"]`).value);
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
  return { refresh, openEditor, destroy() { if (timer != null) clearInterval(timer); window.removeEventListener("hashchange", updatePolling); doc.removeEventListener("visibilitychange", updatePolling); doc.removeEventListener("autodj:edit-points", onEditPoints); } };
}
