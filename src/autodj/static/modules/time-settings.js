// Settings > Stations, Time for every station (#282, #286): the station
// time zone and the time format, with the station's output and log label.
// Loads when the Stations tab is shown.
import { requestJson } from "./api-client.js";
import { currentStation, onStationChange } from "./stations.js";
import { setTimeFormat } from "./time-format.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const ENDPOINT = "/api/time-settings";

export function installTimeSettings({ doc = document, request = requestJson } = {}) {
  const panel = doc.getElementById("spanel-stations");
  const zone = doc.getElementById("time-zone");
  const format = doc.getElementById("time-format");
  if (!panel || !zone || !format) return { load: async () => {} };
  const output = doc.getElementById("station-output");
  const monitor = doc.getElementById("station-monitor");
  const cap = doc.getElementById("station-upload-cap");
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

  function show(value) {
    if (Array.isArray(value.timezones) && zone.options.length !== value.timezones.length) {
      zone.replaceChildren(...value.timezones.map((name) => {
        const option = doc.createElement("option");
        option.value = name;
        option.textContent = name.replaceAll("_", " ");
        return option;
      }));
    }
    if (value.timezone != null) zone.value = value.timezone;
    if (value.time_format != null) {
      format.value = value.time_format;
      setTimeFormat(value.time_format);
    }
  }

  const timeDraft = registerSettingsGroup({
    panel, group: "time", controls: [zone, format],
    read: () => ({ timezone: zone.value, time_format: format.value }),
    write: show,
  });
  const stationDraft = output && monitor && cap ? registerSettingsGroup({
    panel, group: "station", controls: [output, monitor, cap, logLabel].filter(Boolean),
    read: () => ({ output: output.value, server_audio_too: monitor.checked,
      liner_upload_max_mib: Number(cap.value),
      ...(logLabel ? { log_label: logLabel.value.trim() } : {}) }),
    write: showOptions,
  }) : null;

  async function load() {
    try {
      timeDraft.accept(await request(ENDPOINT));
      if (stationDraft) stationDraft.accept(await request("/api/station-settings"));
    } catch {
      // The fields keep what they show; Save reports any problem.
    }
  }

  panel.addEventListener("toggle", () => { if (panel.open) void load(); });
  return { load };
}
