// History tab: save the play log between two dates for royalty reports,
// as a CSV with one row per play or as a SoundExchange Report of Use.
// The server builds the file (GET /api/history/export); the page fetches
// it rather than following a link, so a failure can be said out loud,
// then hands the browser the file to save.

import { requestFile } from "./api-client.js";

const FALLBACK_NAME = "autodj-plays.csv";
const SERVICE_KEY = "autodj.historyExport.service";

// YYYY-MM-DD in local time, the value a date input holds.
export function localDate(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function exportFilename(contentDisposition) {
  const match = /filename="([^"/\\]+)"/.exec(contentDisposition || "");
  return match ? match[1] : FALLBACK_NAME;
}

export function exportMessage(plays, missing, what = "Play log") {
  const count = Number(plays) || 0;
  let text = count
    ? `${what} saved: ${count} ${count === 1 ? "play" : "plays"}.`
    : `${what} saved.  No plays in those dates, so the file is empty apart from any column names.`;
  const lacking = Number(missing) || 0;
  if (lacking) {
    text += `  ${lacking} ${lacking === 1 ? "song lacks" : "songs lack"} ISRC, label, listener count or other reporting fields.`;
  }
  return text;
}

function saveFile(doc, blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = doc.createElement("a");
  link.href = url;
  link.download = filename;
  link.hidden = true;
  doc.body.append(link);
  link.click();
  link.remove();
  // Some browsers read the blob after click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// The service name is remembered in this browser only, as a convenience.
function rememberedService(storage) {
  try {
    return storage?.getItem(SERVICE_KEY) || "";
  } catch (_) {
    return "";
  }
}

function rememberService(storage, value) {
  try {
    storage?.setItem(SERVICE_KEY, value);
  } catch (_) {
    // Private windows may refuse storage; the export still works.
  }
}

// announce(message, tone) reports the outcome once; tone is "info" or "error".
export function installHistoryExport({
  document: doc = document,
  request = requestFile,
  save = saveFile,
  announce,
  now = () => new Date(),
  storage = globalThis.localStorage,
}) {
  const form = doc.getElementById("hist-export");
  const from = doc.getElementById("hist-export-from");
  const to = doc.getElementById("hist-export-to");
  if (!form || !from || !to) return;
  const format = doc.getElementById("hist-export-format");
  const soundexchange = doc.getElementById("hist-export-soundexchange");
  const service = doc.getElementById("hist-export-service");
  const category = doc.getElementById("hist-export-category");
  // Default to this month so far.
  const today = now();
  if (!from.value) from.value = localDate(new Date(today.getFullYear(), today.getMonth(), 1));
  if (!to.value) to.value = localDate(today);
  if (service && !service.value) service.value = rememberedService(storage);
  const isSoundExchange = () => format?.value === "soundexchange";
  const showFormatFields = () => {
    if (soundexchange) soundexchange.hidden = !isSoundExchange();
  };
  format?.addEventListener("change", showFormatFields);
  showFormatFields();
  let busy = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    if (!from.value || !to.value) {
      announce("Choose both a From date and a To date.", "error");
      (from.value ? to : from).focus();
      return;
    }
    if (from.value > to.value) {
      announce("The From date is after the To date.", "error");
      from.focus();
      return;
    }
    const query = new URLSearchParams({ from: from.value, to: to.value });
    let what = "Play log";
    if (isSoundExchange()) {
      const name = service.value.trim();
      if (!name) {
        announce("Enter the name of the service for the SoundExchange report.", "error");
        service.focus();
        return;
      }
      rememberService(storage, name);
      query.set("format", "soundexchange");
      query.set("service", name);
      query.set("category", category.value);
      what = "SoundExchange report";
    }
    busy = true;
    try {
      const response = await request(`/api/history/export?${query}`, ["text/csv", "text/plain"]);
      const blob = await response.blob();
      save(doc, blob, exportFilename(response.headers.get("Content-Disposition")));
      announce(exportMessage(
        response.headers.get("X-Play-Count"),
        response.headers.get("X-Songs-Missing-Fields"),
        what,
      ), "info");
    } catch (err) {
      announce(`Could not export the play log: ${err.message}`, "error");
    } finally {
      busy = false;
    }
  });
}
