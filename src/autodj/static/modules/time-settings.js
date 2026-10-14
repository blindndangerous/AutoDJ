// Settings > Stations, Time for every station (#282, #286): the station
// time zone and the time format.  Loads when the Stations tab is shown;
// Save says the result through #stations-status.
import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";
import { setTimeFormat } from "./time-format.js";

const ENDPOINT = "/api/time-settings";

export function installTimeSettings({ doc = document, request = requestJson } = {}) {
  const panel = doc.getElementById("spanel-stations");
  const zone = doc.getElementById("time-zone");
  const format = doc.getElementById("time-format");
  const save = doc.getElementById("time-settings-save");
  const status = doc.getElementById("stations-status");
  if (!panel || !zone || !format || !save || !status) return { load: async () => {} };
  let running = null;

  function show(value) {
    running = value.running_timezone;
    if (zone.options.length !== value.timezones.length) {
      zone.replaceChildren(...value.timezones.map((name) => {
        const option = doc.createElement("option");
        option.value = name;
        option.textContent = name.replaceAll("_", " ");
        return option;
      }));
    }
    zone.value = value.timezone;
    format.value = value.time_format;
    setTimeFormat(value.time_format);
  }

  async function load() {
    try {
      show(await request(ENDPOINT));
    } catch {
      // The fields keep what they show; Save reports any problem.
    }
  }

  save.addEventListener("click", async () => {
    try {
      const value = await request(ENDPOINT, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timezone: zone.value, time_format: format.value }),
      });
      show(value);
      announceStatus(status, value.timezone === running
        ? "Time settings saved."
        : "Time settings saved. The new time zone applies when AutoDJ restarts.", { force: true });
    } catch (error) {
      announceStatus(status, `Could not save the time settings: ${error.message}`,
        { force: true, tone: "error" });
    }
  });
  panel.addEventListener("toggle", () => { if (panel.open) void load(); });
  return { load };
}
