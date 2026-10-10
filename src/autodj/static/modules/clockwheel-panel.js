// Clockwheel configuration, with PAL treated only as uploaded text.
import { requestJson } from "./api-client.js";

const FIELDS = ["folder", "recursive", "selection", "enforce_rules", "kind"];

export function parseResyncMinutes(text) {
  if (!String(text).trim()) return [];
  const parts = String(text).split(",").map((part) => part.trim());
  if (parts.some((part) => !/^\d+$/.test(part))) {
    throw new Error("Enter comma-separated whole minutes from 0 to 59.");
  }
  const values = parts.map(Number);
  if (values.some((value) => value > 59)) {
    throw new Error("Each resync minute must be from 0 to 59.");
  }
  return [...new Set(values)].sort((a, b) => a - b);
}

export function installClockwheelPanel({ document: doc = document, request = requestJson } = {}) {
  const panel = doc.getElementById("panel-library");
  const status = doc.getElementById("clock-status");
  const slotsList = doc.getElementById("clock-slots");
  const nameInput = doc.getElementById("clock-name");
  const resyncInput = doc.getElementById("clock-resync-minutes");
  const next = doc.getElementById("clock-next");
  let config = { name: "Clock", slots: [], resync_minutes: [0], cursor: 0 };
  let timer = null;
  let slotsSignature = null;

  function say(message, error = false) {
    if (status.textContent !== message) status.textContent = message;
    status.dataset.tone = error ? "error" : "info";
  }
  function render(data) {
    const previousName = config.name || "Clock";
    const previousResync = (config.resync_minutes || [0]).join(", ");
    config = { ...data, slots: data.slots || [] };
    if (doc.activeElement !== nameInput && nameInput.value === previousName) nameInput.value = config.name || "Clock";
    if (doc.activeElement !== resyncInput && resyncInput.value === previousResync) resyncInput.value = (config.resync_minutes || [0]).join(", ");
    const signature = JSON.stringify(config.slots.map(({ folder, recursive, selection, enforce_rules, kind, ready_count }) => ({ folder, recursive, selection, enforce_rules, kind, ready_count })));
    if (signature !== slotsSignature) {
      slotsSignature = signature;
      slotsList.replaceChildren();
      if (!config.slots.length) {
        const item = doc.createElement("li");
        item.textContent = "No clock imported yet.";
        slotsList.append(item);
      } else config.slots.forEach((slot, index) => {
        const item = doc.createElement("li");
        const kind = slot.kind === "liner" ? "Voice liner" : "Music";
        const mode = slot.selection === "random" ? "random" : "least recently played";
        const scope = slot.recursive ? "including subfolders" : "top level only";
        const rules = slot.enforce_rules ? "with repeat rules" : "without repeat rules";
        const count = Number.isInteger(slot.ready_count) ? `; ${slot.ready_count} ready` : "";
        item.textContent = `${index + 1}. ${kind}: ${slot.folder} (${scope}, ${mode}, ${rules}${count})`;
        slotsList.append(item);
      });
    }
    if (!config.slots.length) {
      next.textContent = "No clock slots are configured.";
    } else {
      const cursor = Number.isInteger(config.cursor) ? config.cursor % config.slots.length : 0;
      next.textContent = `Next slot: ${cursor + 1} of ${config.slots.length} — ${config.slots[cursor].folder}`;
    }
    if (config.last_status) say(config.last_status);
  }
  async function refresh() {
    if (!panel || panel.hidden || doc.hidden) return;
    try { render(await request("/api/library/clock")); }
    catch (error) { say(`Could not load clock: ${error.message}`, true); }
  }
  function updatePolling() {
    if (!panel.hidden && !doc.hidden && timer == null) {
      void refresh();
      timer = setInterval(refresh, 5000);
    } else if ((panel.hidden || doc.hidden) && timer != null) {
      clearInterval(timer);
      timer = null;
    }
  }
  async function postConfig(url, body) {
    return request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  doc.getElementById("clock-save").addEventListener("click", async () => {
    try {
      const resync_minutes = parseResyncMinutes(resyncInput.value);
      const slots = config.slots.map((slot) => Object.fromEntries(FIELDS.map((field) => [field, slot[field]])));
      render(await postConfig("/api/library/clock", { name: nameInput.value.trim(), slots, resync_minutes }));
      say("Clock saved.");
    } catch (error) { say(`Could not save clock: ${error.message}`, true); }
  });
  doc.getElementById("clock-import").addEventListener("click", async () => {
    const file = doc.getElementById("clock-pal-file").files?.[0];
    if (!file) { say("Choose a .pal text file to import.", true); return; }
    if (file.size > 256 * 1024) { say("PAL files must be 256 KB or smaller.", true); return; }
    try {
      const text = await file.text();
      const resync_minutes = parseResyncMinutes(resyncInput.value);
      render(await postConfig("/api/library/clock/import", {
        text, name: nameInput.value.trim(), resync_minutes,
      }));
      say("PAL clock imported and saved.");
    } catch (error) { say(`Could not import clock: ${error.message}`, true); }
  });
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  updatePolling();
  return { refresh, destroy() {
    if (timer != null) clearInterval(timer);
    window.removeEventListener("hashchange", updatePolling);
    doc.removeEventListener("visibilitychange", updatePolling);
  } };
}
