// Settings > Sound > Stereo Tool program and licence: download Thimeo's
// command-line Stereo Tool onto the server and set its licence key.
//
// AutoDJ cannot include Stereo Tool, so Download Stereo Tool asks the
// server to fetch Thimeo's own build from thimeo.com.  The page never
// uploads a program.  It needs the "I accept Thimeo's terms" box ticked;
// pressing it without the tick says so and moves focus to the box.  Once
// Stereo Tool is installed the same button reads Update Stereo Tool.
//
// The licence key is write-only: the field is a password field, the page
// is told only whether a licence is set and where it comes from ("Licence
// set" or "No licence"), and the field is emptied once the key is saved.
// With several stations, "For this station only" saves a key that only
// this station uses; otherwise the key is for every station.  A key set
// in the server's configuration or environment is reported but cannot be
// changed here.
//
// Buttons are never disabled while they send (NVDA would say
// "unavailable", #331): a second press is ignored, and aria-busy with the
// .is-pending look says the button is busy.  Results are spoken through
// the settings status region; a refused key is also shown under the
// field, linked to it, and focus goes to the field.

import { isStaleStationReply, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { focusRevealed } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { onStationChange } from "./stations.js";

export function installText(data) {
  if (!data) return "Checking whether Stereo Tool is installed.";
  const version = data.version ? ` ${data.version}` : "";
  const busy = data.download?.busy ? " A download is running." : "";
  if (data.location === "configured") {
    return data.installed
      ? `This station uses the Stereo Tool${version} program set in the server configuration. Download Stereo Tool installs a copy for the stations without that setting.${busy}`
      : `This station is set to use a Stereo Tool program named in the server configuration, and it is not there. Check stereotool_path in the configuration.${busy}`;
  }
  if (data.installed) {
    return data.version
      ? `Stereo Tool ${data.version} is installed.${busy}`
      : `Stereo Tool is installed (version unknown).${busy}`;
  }
  if (data.download && !data.download.available) {
    return `Stereo Tool is not installed. ${data.download.note
      || "Thimeo offers no command-line Stereo Tool that AutoDJ can download for this computer."}`;
  }
  return `Stereo Tool is not installed. Tick that you accept Thimeo's terms, then press Download Stereo Tool.${busy}`;
}

export function licenseText(data) {
  const license = data?.license;
  if (!license) return "Checking the licence.";
  const every = data.several_stations ? " for every station" : "";
  const unlicensed = "Stereo Tool runs unlicensed, with the limits of its free version.";
  if (license.problem) return `No licence. ${license.problem} ${unlicensed}`;
  switch (license.in_use) {
    case "station-config":
      return "Licence set for this station, from a key file named in the server configuration.";
    case "station":
      return "Licence set for this station only.";
    case "environment":
      return `Licence set${every} by the server's environment.`;
    case "install":
      return `Licence set${every}.`;
    default:
      return `No licence. ${unlicensed}`;
  }
}

export function installStereoToolSetup({ document: doc = document, request = requestJson,
  confirm = confirmAction, status, stationChanges = onStationChange }) {
  const panel = doc.getElementById("spanel-sound");
  const installStatus = doc.getElementById("stereo-install-status");
  const accept = doc.getElementById("stereo-accept-terms");
  const download = doc.getElementById("stereo-download");
  const licenseStatus = doc.getElementById("stereo-license-status");
  const keyField = doc.getElementById("stereo-license-key");
  const stationRow = doc.getElementById("stereo-license-station-row");
  const stationDesc = doc.getElementById("stereo-license-station-desc");
  const stationOnly = doc.getElementById("stereo-license-station-only");
  const keyError = doc.getElementById("stereo-license-error");
  const save = doc.getElementById("stereo-license-save");
  const clear = doc.getElementById("stereo-license-clear");
  if (!panel || !installStatus || !accept || !download || !licenseStatus || !keyField
    || !stationOnly || !keyError || !save || !clear) return null;
  const speak = (message, error = false) => announceStatus(status, message,
    { force: true, dwellMs: 8000, tone: error ? "error" : "info" });
  let current = null;
  let downloading = false;
  let saving = false;

  function busy(button, on) {
    button.classList.toggle("is-pending", on);
    if (on) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  }

  function describe(field, on) {
    const ids = (field.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((ref) => ref && ref !== keyError.id);
    if (on) ids.push(keyError.id);
    field.setAttribute("aria-describedby", ids.join(" "));
  }

  function clearKeyError() {
    keyField.removeAttribute("aria-invalid");
    describe(keyField, false);
    keyError.textContent = "";
    keyError.hidden = true;
  }

  function keyFailed(message) {
    keyField.setAttribute("aria-invalid", "true");
    keyError.textContent = message;
    keyError.hidden = false;
    describe(keyField, true);
    focusRevealed(keyField);
    speak(message, true);
  }

  function render(data) {
    current = data;
    installStatus.textContent = installText(data);
    download.textContent = data.installed && data.location !== "configured"
      ? "Update Stereo Tool" : "Download Stereo Tool";
    licenseStatus.textContent = licenseText(data);
    const several = Boolean(data.several_stations);
    if (stationRow) stationRow.hidden = !several;
    if (stationDesc) stationDesc.hidden = !several;
    if (!several) stationOnly.checked = false;
  }

  async function refresh() {
    try {
      render(await request("/api/stereo-tool"));
    } catch (error) {
      if (isStaleStationReply(error)) return;
      speak(`Could not check Stereo Tool: ${error.message}`, true);
    }
  }

  panel.addEventListener("toggle", () => {
    if (panel.open) void refresh();
  });
  if (panel.open) void refresh();
  // Another station's licence and program setting differ.
  stationChanges(() => {
    current = null;
    if (panel.open) void refresh();
  });

  download.addEventListener("click", async () => {
    if (downloading) return;
    if (!accept.checked) {
      speak("Tick I accept Thimeo's terms for Stereo Tool first.", true);
      focusRevealed(accept);
      return;
    }
    if (current?.download && !current.download.available) {
      speak(current.download.note || installText(current), true);
      return;
    }
    downloading = true;
    busy(download, true);
    const working = "Downloading Stereo Tool from thimeo.com. This can take a few minutes.";
    installStatus.textContent = working;
    speak(working);
    try {
      const result = await request("/api/stereo-tool/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accept_terms: true }),
      });
      await refresh();
      speak(result.version ? `Stereo Tool ${result.version} installed.`
        : "Stereo Tool installed (version unknown).");
    } catch (error) {
      await refresh();
      speak(`Could not download Stereo Tool: ${error.message}`, true);
    } finally {
      downloading = false;
      busy(download, false);
    }
  });

  keyField.addEventListener("input", () => {
    if (keyField.getAttribute("aria-invalid") === "true") clearKeyError();
  });

  save.addEventListener("click", async () => {
    if (saving) return;
    const key = keyField.value.trim();
    if (!key) {
      keyFailed("Enter the licence key first.");
      return;
    }
    const scope = current?.several_stations && stationOnly.checked ? "station" : "all";
    saving = true;
    busy(save, true);
    try {
      const data = await request("/api/stereo-tool/license", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, scope }),
      });
      keyField.value = "";
      clearKeyError();
      render(data);
      const where = scope === "station" ? " for this station"
        : data.several_stations ? " for every station" : "";
      speak(data.changed === false
        ? `That licence key is already saved${where}.`
        : `Licence key saved${where}. Stereo Tool restarts with it.`);
    } catch (error) {
      if (isStaleStationReply(error)) return;
      const message = `Could not save the licence key: ${error.message}`;
      // Only a refused key is the field's fault; a conflict or a server
      // error is said without marking the field invalid.
      if (error.status === 400 || error.status === 422) keyFailed(message);
      else speak(message, true);
    } finally {
      saving = false;
      busy(save, false);
    }
  });

  clear.addEventListener("click", async () => {
    if (saving) return;
    const license = current?.license || {};
    const inUse = license.in_use;
    // The key in use if it is a saved one; otherwise a saved key for
    // every station, even while the configuration's key is used instead.
    let scope = null;
    if (inUse === "station") scope = "station";
    else if (license.install === "saved") scope = "all";
    if (!scope) {
      if (inUse === "station-config" || inUse === "environment") {
        speak("This licence is set on the server, in its configuration or environment, so it cannot be cleared here.", true);
      } else {
        speak("There is no saved licence to clear.");
      }
      return;
    }
    let result = null;
    const confirmed = await confirm(doc, {
      title: scope === "station" ? "Clear this station's Stereo Tool licence?"
        : "Clear the Stereo Tool licence?",
      message: "The saved key is deleted. Stereo Tool restarts without it, and the sound bypasses it for a moment.",
      confirmLabel: "Clear licence",
      station: scope === "station",
      onConfirm: async () => {
        saving = true;
        busy(clear, true);
        try {
          const data = await request(`/api/stereo-tool/license?scope=${scope}`, { method: "DELETE" });
          render(data);
          result = [`Licence cleared. ${licenseText(data)}`, false];
        } catch (error) {
          result = [`Could not clear the licence: ${error.message}`, true];
        } finally {
          saving = false;
          busy(clear, false);
        }
        return clear;
      },
    });
    if (confirmed && result) speak(...result);
  });

  return { refresh };
}
