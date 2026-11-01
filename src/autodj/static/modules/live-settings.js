// Settings > Live DJ (#48): who may take over this station from BUTT,
// Mixxx or another Icecast source program, where those programs connect,
// and how the music ducks under browser talkover.
//
// The tab shows only while the station can go live (stream mode with
// server playout); GET /api/live says so.  DJ passwords are write-only:
// the list says whether one is saved, the form never shows it, and a
// blank password on an edit keeps the saved one.  Backups leave passwords
// out, so the help says to set them again after a restore.
//
// The source address and the connection facts are plain text, filled from
// the page's own address, so they match what the DJ can reach.  Results go
// to the settings status region; a refused save shows its error in words
// under the form, linked to the field it is about, and focus moves there.

import { failureText, staleActionText, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { clearFieldError, clearFormErrors, showFieldError } from "./dom-helpers.js";
import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const USERNAME = /^[\x21-\x39\x3b-\x7e]{1,64}$/;

// What a DJ program needs: host, port, mount, and whether to use TLS.
export function sourceFacts(sourcePath, loc = globalThis.location) {
  const secure = loc?.protocol === "https:";
  const host = loc?.hostname || "";
  const port = loc?.port || (secure ? "443" : "80");
  const path = typeof sourcePath === "string" && sourcePath.startsWith("/") ? sourcePath : "/live";
  return {
    url: `${secure ? "https" : "http"}://${loc?.host || host}${path}`,
    host, port, mount: path.slice(1), secure,
  };
}

// The words about plain HTTP, from the server's LAN setting.
export function transportNote(secure, plainHttpLan) {
  if (secure) {
    return "This address uses HTTPS: turn on SSL/TLS (\"Use SSL/TLS\" in BUTT) in the DJ program.";
  }
  return plainHttpLan
    ? "Without HTTPS, AutoDJ accepts a DJ program on this computer or on your local network only, and its password crosses the network unencrypted. From anywhere else, open AutoDJ over HTTPS and use that address with SSL/TLS turned on."
    : "Without HTTPS, AutoDJ accepts a DJ program on this computer only. For a DJ on another computer, open AutoDJ over HTTPS and use that address with SSL/TLS turned on.";
}

// The field a refused save is about: the one the server's words name first.
function fieldFor(message) {
  const text = String(message || "").toLowerCase();
  const named = [["username", "username"], ["user name", "username"], ["display", "name"], ["password", "password"]]
    .map(([word, field]) => [text.indexOf(word), field])
    .filter(([at]) => at >= 0)
    .sort((a, b) => a[0] - b[0]);
  return named[0]?.[1] || "username";
}

export function installLiveSettings({
  doc = globalThis.document,
  request,
  announce,
  onInfo = () => {},
  location: loc = globalThis.location,
} = {}) {
  const id = (name) => doc.getElementById(`live-${name}`);
  const rows = id("dj-rows");
  const form = id("dj-form");
  if (!rows || !form) return null;
  const table = installTableNavigation(rows);
  const formError = id("dj-error");
  const talkError = id("talkover-error");
  const rowByName = new Map();
  let djs = [];
  let editing = null;
  let returnFocus = null;
  let generation = 0;
  // The Save buttons with a request on the go: another press waits.  No
  // aria-disabled, or NVDA says "unavailable" on the pressed button (#331);
  // aria-busy says it is busy instead, and a slow reply says "Saving…"
  // (#384).
  const saving = new Set();
  const returnSeconds = id("return-seconds");
  const duckDb = id("duck-db");
  const draft = registerSettingsGroup({
    panel: doc.getElementById("spanel-live"), group: "live",
    controls: [returnSeconds, duckDb],
    fields: { mic_return_seconds: returnSeconds, mic_duck_db: duckDb },
    read: () => ({ mic_return_seconds: Number(returnSeconds.value), mic_duck_db: Number(duckDb.value) }),
    write: renderSettings,
    validate() {
      clearFormErrors(id("talkover"), talkError);
      const seconds = Number(returnSeconds.value);
      const duck = Number(duckDb.value);
      if (returnSeconds.value === "" || !(seconds >= 0.1 && seconds <= 10)) {
        fail(returnSeconds, talkError, "Return time: enter a number of seconds from 0.1 to 10.");
        return false;
      }
      if (duckDb.value === "" || !(duck >= 0 && duck <= 30)) {
        fail(duckDb, talkError, "Duck amount: enter a number of decibels from 0 to 30.");
        return false;
      }
      return true;
    },
  });

  function fail(field, errorNode, message) {
    showFieldError(field, message, errorNode, null);
    announce(message, "error");
  }

  // ---- The connection facts ----

  function renderSource(info) {
    const facts = sourceFacts(info?.source_path, loc);
    id("source-url").value = facts.url;
    id("source-host").textContent = facts.host;
    id("source-port").textContent = facts.port;
    id("source-mount").textContent = facts.mount;
    id("source-tls").textContent = facts.secure ? "SSL/TLS on" : "SSL/TLS off";
    id("transport-note").textContent = transportNote(facts.secure, Boolean(info?.plain_http_lan));
  }

  function renderSettings(settings) {
    if (!settings) return;
    if (Number.isFinite(settings.mic_return_seconds)) id("return-seconds").value = String(settings.mic_return_seconds);
    if (Number.isFinite(settings.mic_duck_db)) id("duck-db").value = String(settings.mic_duck_db);
  }

  // ---- The DJ list ----

  function render() {
    const names = new Set(djs.map((dj) => dj.username));
    let changed = false;
    for (const [name, record] of rowByName) {
      if (names.has(name)) continue;
      if (record.row.contains(doc.activeElement)) id("dj-add").focus();
      record.row.remove();
      rowByName.delete(name);
      changed = true;
    }
    for (const dj of djs) {
      let record = rowByName.get(dj.username);
      if (!record) {
        const button = (label, action) => {
          const item = doc.createElement("button");
          item.type = "button";
          item.textContent = label;
          item.addEventListener("click", action);
          return item;
        };
        const edit = button("Edit", () => showForm(record.dj));
        const removeButton = button("Remove", () => removeDj(record.dj));
        const row = tableRow(doc, ["", "", edit, removeButton]);
        record = { row, edit, removeButton, dj };
        rowByName.set(dj.username, record);
        changed = true;
      }
      record.dj = dj;
      // One cell for both names keeps the table inside a 320 px window.
      record.row.cells[0].textContent = `${dj.display_name}, username ${dj.username}`;
      record.row.cells[1].textContent = dj.password_set ? "Saved" : "Not set: this DJ cannot connect";
      record.edit.setAttribute("aria-label", `Edit DJ ${dj.username}`);
      record.removeButton.setAttribute("aria-label", `Remove DJ ${dj.username}`);
    }
    let cursor = rows.firstElementChild;
    for (const dj of djs) {
      const row = rowByName.get(dj.username).row;
      if (row !== cursor) rows.insertBefore(row, cursor);
      cursor = row.nextElementSibling;
    }
    id("dj-none").hidden = djs.length > 0;
    id("dj-table").hidden = djs.length === 0;
    if (changed) table.refresh();
  }

  function showForm(dj = null) {
    editing = dj?.username ?? null;
    clearFormErrors(form, formError);
    id("dj-form-title").textContent = dj ? `Edit DJ ${dj.username}` : "Add DJ";
    id("dj-username").value = dj?.username || "";
    id("dj-username").readOnly = Boolean(dj);
    id("dj-name").value = dj?.display_name || "";
    id("dj-password").value = "";
    id("dj-password-help").textContent = dj?.password_set
      ? "A password is saved. Leave blank to keep it, or enter a new one of 12 to 256 characters."
      : "12 to 256 characters. The DJ types it as the password in BUTT or Mixxx.";
    form.hidden = false;
    returnFocus = dj ? rowByName.get(dj.username)?.edit || id("dj-add") : id("dj-add");
    (dj ? id("dj-name") : id("dj-username")).focus();
  }

  function hideForm() {
    form.hidden = true;
    id("dj-password").value = "";
    clearFormErrors(form, formError);
  }

  // Checked here first, so each mistake is named against its own field.
  function formProblem(payload) {
    if (!USERNAME.test(payload.username)) {
      return ["dj-username", "Username: use 1 to 64 letters, digits or punctuation, with no spaces and no colon."];
    }
    if (!payload.display_name || payload.display_name.length > 80) {
      return ["dj-name", "Display name: enter 1 to 80 characters."];
    }
    const password = payload.password;
    if (password === null && editing === null) {
      return ["dj-password", "Password: a new DJ needs a password of 12 to 256 characters."];
    }
    if (password !== null && (password.length < 12 || password.length > 256)) {
      return ["dj-password", "Password: use 12 to 256 characters."];
    }
    return null;
  }

  async function saveDj(control) {
    clearFormErrors(form, formError);
    const password = id("dj-password").value;
    const payload = {
      username: editing ?? id("dj-username").value.trim(),
      display_name: id("dj-name").value.trim(),
      password: password ? password : null,
    };
    const problem = formProblem(payload);
    if (problem) {
      fail(id(problem[0]), formError, problem[1]);
      return false;
    }
    try {
      saving.add(control);
      await withDisabled(control, () => request("/api/live/djs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }), { pendingText: `Saving DJ ${payload.username}\u2026`, say: announce });
    } catch (err) {
      // Answered after a station switch: the form is the new station's (#70).
      const left = staleActionText(err);
      if (left) announce(left, err.actionDone ? "success" : "error");
      else fail(id(`dj-${fieldFor(err.message)}`), formError, failureText("save the DJ.", err));
      return false;
    } finally {
      saving.delete(control);
    }
    hideForm();
    await refresh();
    (rowByName.get(payload.username)?.edit || returnFocus || id("dj-add")).focus();
    announce(`DJ ${payload.username} saved.`);
    return true;
  }

  async function removeDj(dj) {
    await confirmAction(doc, {
      title: "Remove DJ?",
      message: `Remove ${dj.display_name} (${dj.username})? If this DJ is on the air, they are disconnected and the music comes back.`,
      confirmLabel: "Remove DJ",
      pendingText: `Removing DJ ${dj.username}…`,
      onConfirm: async () => {
        try {
          await request(`/api/live/djs/${encodeURIComponent(dj.username)}`, { method: "DELETE" });
        } catch (err) {
          const left = staleActionText(err);
          announce(left || failureText(`remove DJ ${dj.username}`, err), err.actionDone ? "success" : "error");
          return null;
        }
        await refresh();
        announce(`DJ ${dj.username} removed.`);
        return id("dj-add");
      },
    });
  }

  // ---- Talkover settings ----

  // ---- Loading ----

  async function refresh() {
    const mine = ++generation;
    let info = null;
    try {
      info = await request("/api/live");
    } catch (_) {
      info = null;
    }
    if (mine !== generation) return null;
    const available = Boolean(info?.available);
    id("unavailable").hidden = available || !info?.reason;
    id("unavailable").textContent = available ? "" : (info?.reason || "");
    id("settings").hidden = !info;
    if (info) {
      djs = Array.isArray(info.djs) ? info.djs.filter((dj) => typeof dj?.username === "string") : [];
      renderSource(info);
      draft.accept(info.settings);
      render();
    }
    onInfo(info);
    return info;
  }

  id("dj-add").addEventListener("click", () => showForm());
  id("dj-cancel").addEventListener("click", () => {
    hideForm();
    (returnFocus || id("dj-add")).focus();
  });
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid") === "true") {
      clearFieldError(event.target, formError);
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const control = id("dj-save");
    if (saving.has(control)) return;
    void saveDj(control);
  });
  id("talkover")?.addEventListener("submit", (event) => event.preventDefault());
  id("source-copy").addEventListener("click", async () => {
    const text = id("source-url").value;
    try {
      await globalThis.navigator.clipboard.writeText(text);
      announce("Source address copied.");
    } catch (_) {
      id("source-url").select();
      announce("Could not copy. The address is selected: press Control+C or Command+C to copy it.", "error");
    }
  });

  return {
    refresh,
    reset() {
      generation += 1;
      djs = [];
      render();
      hideForm();
      clearFormErrors(id("talkover"), talkError);
    },
  };
}
