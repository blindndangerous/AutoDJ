import { onCardOpen } from "./card-state.js";
// Station settings: station time zone and output in Stations; shared time
// format in Server (#282, #286).
// Loads each setting when its owning Settings tab is shown.
import { requestJson } from "./api-client.js";
import { confirmAction, chooseAction } from "./confirm-dialog.js";
import { currentStation, onStationChange, stationName } from "./stations.js";
import { setTimeFormat } from "./time-format.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const ENDPOINT = "/api/time-settings";

export function installTimeSettings({ doc = document, request = requestJson } = {}) {
  const stationsPanel = doc.getElementById("spanel-stations");
  const zone = doc.getElementById("time-zone");
  const format = doc.getElementById("time-format");
  if (!stationsPanel || !zone || !format) return { load: async () => {} };
  const zonePanel = zone.closest('[role="tabpanel"]') || stationsPanel;
  const formatPanel = format.closest('[role="tabpanel"]') || stationsPanel;
  const output = doc.getElementById("station-output");
  const stationDisplayName = doc.getElementById("stream-station-name");
  const monitor = doc.getElementById("station-monitor");
  const cap = doc.getElementById("station-upload-cap");
  const library = doc.getElementById("station-library");
  // Optional, so a page without the field still saves the other options.
  const logLabel = doc.getElementById("station-log-label");
  // The hint names the ID this station logs as while the label is empty.
  const logLabelId = doc.getElementById("station-log-label-id");
  if (logLabelId) {
    logLabelId.textContent = currentStation();
    onStationChange((slug) => { logLabelId.textContent = slug; });
  }
  function showOptions(value) {
    output.value = value.output;
    monitor.checked = value.server_audio_too;
    monitor.disabled = output.value !== "stream";
    cap.value = value.liner_upload_max_mib;
    if (logLabel) logLabel.value = value.log_label ?? "";
  }
  output?.addEventListener("change", () => {
    monitor.disabled = output.value !== "stream";
    if (monitor.disabled) monitor.checked = false;
  });

  function zoneOption(name) {
    const option = doc.createElement("option");
    option.value = name;
    option.textContent = name.replaceAll("_", " ");
    return option;
  }

  function showZones(names) {
    if (!Array.isArray(names)) return;
    const selected = zone.value;
    if (names.length !== zone.options.length || names.some((name, index) => zone.options[index].value !== name)) {
      zone.replaceChildren(...names.map(zoneOption));
      if (selected && !names.includes(selected)) zone.add(zoneOption(selected));
      zone.value = selected;
    }
  }

  function show(value) {
    if (value.timezone != null) {
      // The saved draft can arrive before the list of available zones.
      if (![...zone.options].some((option) => option.value === value.timezone)) {
        zone.add(zoneOption(value.timezone));
      }
      zone.value = value.timezone;
    }
    if (value.time_format != null) {
      format.value = value.time_format;
      setTimeFormat(value.time_format);
    }
  }

  const timeDraft = registerSettingsGroup({
    panel: zonePanel, group: "time", controls: [zone],
    read: () => ({ timezone: zone.value }),
    write: (value) => show({ ...value, time_format: null }),
  });
  const formatDraft = registerSettingsGroup({
    panel: formatPanel, group: "time", scope: "server", controls: [format],
    read: () => ({ time_format: format.value }),
    write: (value) => show({ ...value, timezone: null }),
  });
  const stationDraft = output && monitor && cap ? registerSettingsGroup({
    panel: stationsPanel, group: "station", controls: [output, monitor, cap, logLabel].filter(Boolean),
    fields: { output, server_audio_too: monitor, liner_upload_max_mib: cap, log_label: logLabel },
    read: () => ({ output: output.value, server_audio_too: monitor.checked,
      liner_upload_max_mib: Number(cap.value),
      ...(logLabel ? { log_label: logLabel.value.trim() } : {}) }),
    write: showOptions,
  }) : null;
  if (stationDisplayName) registerSettingsGroup({
    panel: stationsPanel, group: "stream", controls: [stationDisplayName],
    fields: { station_name: stationDisplayName },
    read: () => ({ station_name: stationDisplayName.value }),
    write: (value) => { if (value.station_name != null) stationDisplayName.value = value.station_name; },
  });

  function showLibrary(value) {
    if (!library || value.library_id == null) return;
    const id = String(value.library_id || "main");
    if (![...library.options].some((option) => option.value === id)) {
      const option = doc.createElement("option");
      option.value = id;
      option.textContent = `${id} (unavailable)`;
      library.add(option);
    }
    library.value = id;
  }

  if (library) {
    registerSettingsGroup({
    panel: stationsPanel, group: "station", controls: [library], fields: { library_id: library },
    read: () => ({ library_id: library.value || "main" }), write: showLibrary,
    beforeSave: async (value, baseline, { station, current }) => {
      if (value.library_id === baseline.library_id) return "next";
      const check = await request(`/api/libraries/${encodeURIComponent(value.library_id)}/check?station=${encodeURIComponent(station)}`);
      if (!current()) return false;
      const warnings = Array.isArray(check.warnings) ? check.warnings : [];
      const detail = warnings.length ? `\n\n${warnings.join("\n")}` : "";
      if (!check.running) {
        if (!warnings.length) return "next";
        return await confirmAction(doc, {
          title: "Save music library",
          message: `Some saved programming will not work in ${value.library_id}.${detail}`,
          confirmLabel: "Save selection",
        }) ? "next" : false;
      }
      const choice = await chooseAction(doc, {
        title: "Restart station to change library",
        message: `Changing the music library for ${stationName()} requires a restart.${detail}`,
        choices: ["Save and restart now", "Save for next start", "Cancel"],
      });
      if (!current()) return false;
      return choice === "accept" ? "restart" : choice === "alternate" ? "next" : false;
    },
    afterSave: async (_value, { station, current, decision }) => {
      if (decision !== "restart" || !current()) return "";
      await request(`/api/stations/${encodeURIComponent(station)}/stop`, { method: "POST" });
      if (!current()) return "";
      await request(`/api/stations/${encodeURIComponent(station)}/start`, { method: "POST" });
      if (!current()) return "";
      return `${stationName()} restarted.`;
    },
    });
  }

  async function loadPanel(panel) {
    if (panel === zonePanel || panel === formatPanel) {
      try {
        const value = await request(ENDPOINT);
        if (panel === zonePanel) {
          // Choices are metadata: load them even when runtime values cannot
          // overwrite the open Settings draft.
          showZones(value.timezones);
          timeDraft.accept(value);
        }
        if (panel === formatPanel) formatDraft.accept(value);
      } catch { /* The fields keep their drafts; Save reports any problem. */ }
    }
    if (panel === stationsPanel && stationDraft) {
      try { stationDraft.accept(await request("/api/station-settings")); } catch { /* Save reports errors. */ }
    }
  }
  const panels = [...new Set([zonePanel, formatPanel, stationsPanel])];
  for (const panel of panels) onCardOpen(panel, () => loadPanel(panel));
  const load = () => Promise.all(panels.map(loadPanel));
  return { load };
}
