// History tab: save the play log between two dates for royalty reports,
// as a CSV with one row per play or as a SoundExchange Report of Use.
// "Include liners" adds the liners and other non-music items to the CSV
// (#601).  A Report of Use lists songs only, so the choice is hidden while
// SoundExchange is chosen and never sent with it.
// The server builds the file (GET /api/history/export); the page fetches
// it rather than following a link, so a failure can be said out loud,
// then hands the browser the file to save.

import { failureText, requestFile, withDisabled } from "./api-client.js";
import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

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

export function exportMessage(plays, missing, what = "Play log", unmatchable = 0) {
  const count = Number(plays) || 0;
  let text = count
    ? `${what} saved: ${count} ${count === 1 ? "play" : "plays"}.`
    : `${what} saved.  No plays in those dates, so the file is empty apart from any column names.`;
  const lacking = Number(missing) || 0;
  if (lacking) {
    text += `  ${lacking} ${lacking === 1 ? "song lacks" : "songs lack"} ISRC, label, listener count or other reporting fields.`;
  }
  const unmatched = Number(unmatchable) || 0;
  if (unmatched) {
    text += `  Warning: ${unmatched} ${unmatched === 1 ? "song has" : "songs have"} neither an ISRC nor both an album and a label, so SoundExchange may not be able to match ${unmatched === 1 ? "it" : "them"}.`;
  }
  return text;
}

export function saveFile(doc, blob, filename) {
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
  return readStorage(storage, SERVICE_KEY) || "";
}

function rememberService(storage, value) {
  writeStorage(storage, SERVICE_KEY, value);
}

// announce(message, tone) reports the outcome once; tone is "info" or "error".
export function installHistoryExport({
  document: doc = document,
  request = requestFile,
  save = saveFile,
  announce,
  now = () => new Date(),
  storage = defaultStorage(),
}) {
  const form = doc.getElementById("hist-export");
  const from = doc.getElementById("hist-export-from");
  const to = doc.getElementById("hist-export-to");
  if (!form || !from || !to) return;
  const format = doc.getElementById("hist-export-format");
  const soundexchange = doc.getElementById("hist-export-soundexchange");
  const service = doc.getElementById("hist-export-service");
  const category = doc.getElementById("hist-export-category");
  const liners = doc.getElementById("hist-export-liners");
  const csvOptions = doc.getElementById("hist-export-csv");
  const button = doc.getElementById("hist-export-button");
  // Default to this month so far.
  const today = now();
  if (!from.value) from.value = localDate(new Date(today.getFullYear(), today.getMonth(), 1));
  if (!to.value) to.value = localDate(today);
  if (service && !service.value) service.value = rememberedService(storage);
  const isSoundExchange = () => format?.value === "soundexchange";
  // Each format's own fields come next after Format in reading order: the
  // CSV's Include liners, or the SoundExchange fields, the other hidden.
  // Neither is announced: a select's change can fire while its options are
  // still being read, and an announcement then spoke over the option (#601).
  const showFormatFields = () => {
    if (soundexchange) soundexchange.hidden = !isSoundExchange();
    if (csvOptions) csvOptions.hidden = isSoundExchange();
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
    } else if (liners?.checked) {
      query.set("liners", "true");
    }
    busy = true;
    announce("Exporting play log", "info");
    try {
      await withDisabled(button, () => withDisabled(form, async () => {
        const response = await request(`/api/history/export?${query}`, ["text/csv", "text/plain"]);
        const blob = await response.blob();
        save(doc, blob, exportFilename(response.headers.get("Content-Disposition")));
        announce(exportMessage(
          response.headers.get("X-Play-Count"),
          response.headers.get("X-Songs-Missing-Fields"),
          what,
          response.headers.get("X-Songs-Unmatchable"),
        ), "info");
      }));
    } catch (err) {
      announce(failureText("export the play log", err), "error");
    } finally {
      busy = false;
    }
  });
}
