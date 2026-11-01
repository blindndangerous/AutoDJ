// Settings > Analysis workers: the computers analysing songs for this
// server (autodj worker), how many songs are waiting, and the machine
// tokens those computers and an announcer program (#88) use.
// An announcer token is bound to one station, chosen in #token-station,
// which shows only while Token use is an announcer.
//
// Loads when its Settings subtab is shown and refreshes the workers every
// ten seconds while it stays shown.  Refreshing changes each row's words
// in place, keyed by token id, so the control that has focus stays put;
// only Refresh workers says the result.
//
// A new token is shown once, in a read-only field with a Copy button.
// Creating it moves focus to that field and says, through
// #workers-status, that it will not be shown again.  Done empties the
// field; switching to another tab does not, so the token is not lost
// before it is copied.  Every button result is said once through
// #workers-status.
//
// Each worker row has its settings from this server (#554): Paused, Songs
// per claim and Idle release minutes, saved with the row's own Save
// button.  A refresh leaves a row whose fields were changed and not saved
// as the user left it.  A refused save says why under the row's fields,
// marks the field and moves focus there.  Pair a worker shows a single-use
// code and the command to run on the worker, each with a Copy button.  A
// refusal notice newly seen by a refresh is said once; Dismiss hides it.

import { failureText, requestJson, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { lastSeenWords, shownCode, spokenCode } from "./devices.js";
import { copyByCommand } from "./clipboard.js";
import { clearFieldError, showFieldError, nextRowControl, rowButton } from "./dom-helpers.js";
import { onCardOpen } from "./card-state.js";
import { makeSay } from "./live-region.js";

const POLL_MS = 10000;
export const ONCE_WARNING =
  "This token is shown only now and cannot be shown again.  Copy it before you leave this page.";
const MAX_BATCH = 16;
const MAX_IDLE_RELEASE = 10080;

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function shownDate(seconds, locale = undefined) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "on an unknown date";
  return new Date(seconds * 1000).toLocaleDateString(locale, {
    day: "numeric", month: "long", year: "numeric",
  });
}

// "Studio PC, connected, on NVIDIA GeForce RTX 4060 (CUDA GPU).  Last seen
// just now.  120 songs analysed, about 340 an hour, 4 in progress."
export function workerWords(worker, nowMs = Date.now()) {
  let text = `${worker.name}, ${worker.connected ? "connected" : "not connected"}`;
  if (worker.device) text += `, on ${worker.device}`;
  text += `.  Last seen ${lastSeenWords(worker.last_seen, nowMs)}.  `;
  const counts = [`${plural(worker.songs_done, "song")} analysed`];
  if (worker.songs_failed) counts.push(`${worker.songs_failed} failed`);
  if (worker.songs_stale) counts.push(`${worker.songs_stale} skipped because the file changed`);
  if (worker.connected && worker.rate_per_hour > 0) {
    counts.push(`about ${Math.round(worker.rate_per_hour)} an hour`);
  }
  if (worker.connected && worker.leased > 0) counts.push(`${worker.leased} in progress`);
  return `${text}${counts.join(", ")}.`;
}

export function queueWords(view) {
  if (!view.available) {
    return "This server has no music library, so there is nothing for workers to analyse.";
  }
  const waiting = `${plural(view.waiting, "song")} waiting for analysis`;
  const now = view.analysing ? `, ${view.analysing} being analysed now` : "";
  const server = view.server_paused
    ? "This server's own analysis is paused while a worker is connected."
    : "With no worker connected, this server analyses them itself.";
  return `${waiting}${now}.  ${server}`;
}

// "Settings: paused, 2 songs per claim, the worker's own idle release.
// The worker has applied them."
// "paused, 2 songs per claim, the worker's own idle release"
function settingsValues(settings = {}) {
  const batch = settings.batch == null
    ? "the worker's own batch size" : `${plural(settings.batch, "song")} per claim`;
  const idle = settings.idle_release_minutes == null
    ? "the worker's own idle release"
    : `idle release after ${plural(settings.idle_release_minutes, "minute")}`;
  return `${settings.paused ? "paused" : "running"}, ${batch}, ${idle}`;
}

export function settingsWords(worker) {
  const settings = worker.settings || {};
  const text = `Settings: ${settingsValues(settings)}.`;
  if (!settings.revision) return text;
  if (worker.settings_applied === true) return `${text}  The worker has applied them.`;
  if (worker.settings_applied === false) return `${text}  Waiting for the worker to apply them.`;
  return `${text}  The worker has not said it applied them; an older worker cannot, `
    + "but Paused still stops this server handing it songs.";
}

// "Refused: Studio PC, 5 minutes ago.  The worker's copy of ... "
export function refusalWords(refusal, nowMs = Date.now()) {
  const revoked = refusal.revoked ? " (revoked token)" : "";
  return `Refused: ${refusal.name}${revoked}, ${lastSeenWords(refusal.refused_at, nowMs)}.  `
    + `${refusal.message}`;
}

// The command a worker runs to pair with this page's server.
export function pairCommand(code, origin = globalThis.location?.origin || "") {
  return `autodj worker --pair ${code} --server ${origin}`;
}

// Whether a worker can pair from here: HTTPS, or this computer.
export function pairTransportOk(loc = globalThis.location) {
  if (!loc || loc.protocol === "https:") return true;
  return ["localhost", "127.0.0.1", "[::1]"].includes(loc.hostname);
}

// Worker rows in a fixed order, by name then token id, so a refresh never
// shuffles them as their last-seen times change.
function byName(a, b) {
  return String(a.name).localeCompare(String(b.name)) || String(a.token_id)
    .localeCompare(String(b.token_id));
}

// A notice is the same one again when its worker, kind and words match; a
// later time alone is not news.
function refusalKey(refusal) {
  return `${refusal.token_id}\n${refusal.kind}\n${refusal.detail}`;
}

// Put *items* in *list* in order, moving only an item out of place, and
// never the one holding focus: moving it would drop focus to the page.
function placeInOrder(list, items, doc) {
  let previous = null;
  for (const item of items) {
    const wanted = previous ? previous.nextElementSibling : list.firstElementChild;
    if (item !== wanted && !item.contains(doc.activeElement)) list.insertBefore(item, wanted);
    previous = item;
  }
}

// The number in *text* from 1 to *max*, null for empty, or NaN when it is not one.
function wholeOrEmpty(text, max) {
  const trimmed = String(text).trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return Number.NaN;
  const value = Number(trimmed);
  return value >= 1 && value <= max ? value : Number.NaN;
}

function clockTime(ms, locale = undefined) {
  return new Date(ms).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

// "news-bot.  Announcer, for Night Owl.
// Created 3 May 2026, never used."
export function tokenWords(token, scopeLabels, stationNames = new Map()) {
  const scopes = token.scopes.map((scope) => scopeLabels.get(scope) || scope).join(", ");
  const station = token.station
    ? `, for ${stationNames.get(token.station) || token.station}`
    : "";
  const used = token.last_used_at == null
    ? "never used"
    : `last used ${lastSeenWords(token.last_used_at)}`;
  return `${token.name}.  ${scopes}${station}.  Created ${shownDate(token.created_at)}, ${used}.`;
}

export function installAnalysisWorkers({
  doc = document,
  request = requestJson,
  confirm = confirmAction,
  setTimer = setInterval,
  clearTimer = clearInterval,
  clipboard = () => globalThis.navigator?.clipboard,
} = {}) {
  const get = (id) => doc.getElementById(id);
  const panel = get("spanel-workers");
  const status = get("workers-status");
  const queue = get("workers-queue");
  const workerList = get("workers-list");
  const refresh = get("workers-refresh");
  const nameInput = get("token-name");
  const scopeSelect = get("token-scope");
  const stationRow = get("token-station-row");
  const stationSelect = get("token-station");
  const stationDesc = get("token-station-desc");
  const create = get("token-create");
  const fresh = get("token-new");
  const freshName = get("token-new-name");
  const secretField = get("token-secret");
  const copy = get("token-copy");
  const done = get("token-done");
  const tokenList = get("token-list");
  const tokensEmpty = get("token-list-empty");
  const refusalHeading = get("workers-refusals-heading");
  const refusalList = get("workers-refusals");
  const pairName = get("pair-worker-name");
  const pairCreate = get("pair-worker-create");
  const pairNew = get("pair-worker-new");
  const pairCode = get("pair-worker-code");
  const pairExpiry = get("pair-worker-expiry");
  const pairCopyCode = get("pair-worker-copy-code");
  const pairCommandField = get("pair-worker-command");
  const pairCopyCommand = get("pair-worker-copy-command");
  const pairDone = get("pair-worker-done");
  const pairHttp = get("pair-worker-http");
  if (!panel || !status || !queue || !workerList || !refresh || !nameInput || !create
      || !fresh || !freshName || !secretField || !copy || !done || !tokenList || !tokensEmpty
      || !scopeSelect || !stationRow || !stationSelect || !stationDesc || !refusalHeading
      || !refusalList || !pairName || !pairCreate || !pairNew || !pairCode || !pairExpiry
      || !pairCopyCode || !pairCommandField || !pairCopyCommand || !pairDone || !pairHttp) {
    return { load: async () => false };
  }

  const workerRows = new Map();
  const refusalRows = new Map();
  // Refusals seen so far, by token id, time and kind; null until the first
  // load, which says nothing about the ones already there.
  let seenRefusals = null;
  const pairing = { code: null, until: 0 };
  const tokenRows = new Map();
  let tokens = [];
  let scopeLabels = new Map([...scopeSelect.options].map((option) => [option.value,
    option.textContent]));
  // Scopes that bind a token to one station (the announcer's).
  let stationScopes = new Set(["announcer"]);
  let stationNames = new Map();
  let timer = null;

  const say = makeSay(status, { dwellMs: 4000, errorDwellMs: 6000 });

  function field(row, kind, type, labelText) {
    const wrap = doc.createElement("div");
    wrap.className = type === "checkbox" ? "settings-check" : "settings-row";
    const input = doc.createElement("input");
    input.type = type;
    input.id = `worker-${row.id}-${kind}`;
    const label = doc.createElement("label");
    label.htmlFor = input.id;
    label.textContent = labelText;
    if (type === "checkbox") wrap.append(input, label);
    else wrap.append(label, input);
    for (const event of ["input", "change"]) {
      input.addEventListener(event, () => { row.dirty = true; });
    }
    return [wrap, input];
  }

  function newWorkerRow(worker) {
    const li = doc.createElement("li");
    const row = { id: worker.token_id, li, worker, dirty: false };
    row.summary = doc.createElement("p");
    row.state = doc.createElement("p");
    const fieldset = doc.createElement("fieldset");
    row.legend = doc.createElement("legend");
    const [pausedRow, paused] = field(row, "paused", "checkbox", "Paused");
    const [batchRow, batch] = field(row, "batch", "number", "Songs per claim");
    batch.min = "1";
    batch.max = String(MAX_BATCH);
    batch.step = "1";
    const [idleRow, idle] = field(row, "idle", "number", "Idle release minutes");
    idle.min = "1";
    idle.max = String(MAX_IDLE_RELEASE);
    idle.step = "1";
    const help = doc.createElement("span");
    help.className = "setting-desc";
    help.id = `worker-${row.id}-help`;
    help.textContent = "Empty uses the worker's own setting; its command line wins.";
    for (const input of [batch, idle]) input.setAttribute("aria-describedby", help.id);
    row.error = doc.createElement("p");
    row.error.className = "field-error";
    row.error.id = `worker-${row.id}-error`;
    row.error.hidden = true;
    row.save = rowButton(doc, "Save", "", (button) => void saveWorker(row, button));
    row.save.dataset.action = "save-worker";
    fieldset.append(row.legend, pausedRow, batchRow, idleRow, help, row.error, row.save);
    li.append(row.summary, row.state, fieldset);
    Object.assign(row, { paused, batch, idle });
    return row;
  }

  function fillWorkerRow(row, worker) {
    row.worker = worker;
    row.summary.textContent = workerWords(worker);
    row.state.textContent = settingsWords(worker);
    row.legend.textContent = `Settings for ${worker.name}`;
    row.save.setAttribute("aria-label", `Save settings for ${worker.name}`);
    if (row.dirty || row.saving) return;
    const settings = worker.settings || {};
    row.paused.checked = Boolean(settings.paused);
    row.batch.value = settings.batch == null ? "" : String(settings.batch);
    row.idle.value = settings.idle_release_minutes == null
      ? "" : String(settings.idle_release_minutes);
  }

  function clearRowError(row) {
    clearFieldError([row.batch, row.idle], row.error);
  }

  function rowError(row, input, message) {
    showFieldError(input, message, row.error, null);
    say(message, "error");
  }

  async function saveWorker(row, button) {
    if (row.saving) return;
    clearRowError(row);
    const name = row.worker.name;
    const batch = wholeOrEmpty(row.batch.value, MAX_BATCH);
    if (Number.isNaN(batch)) {
      rowError(row, row.batch, `Could not save settings for ${name}: enter songs per claim `
        + `from 1 to ${MAX_BATCH}, or leave it empty.`);
      return;
    }
    const idle = wholeOrEmpty(row.idle.value, MAX_IDLE_RELEASE);
    if (Number.isNaN(idle)) {
      rowError(row, row.idle, `Could not save settings for ${name}: enter idle release `
        + `minutes from 1 to ${MAX_IDLE_RELEASE}, or leave it empty.`);
      return;
    }
    const payload = { paused: row.paused.checked, batch, idle_release_minutes: idle };
    row.saving = true;
    let saved;
    try {
      saved = await withDisabled(button, () => request(
        `/api/analysis/workers/${encodeURIComponent(row.id)}/settings`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }));
    } catch (errorValue) {
      row.saving = false;
      const text = String(errorValue.message || "").toLowerCase();
      const input = text.includes("batch") || text.includes("per claim") ? row.batch
        : text.includes("idle") ? row.idle : null;
      rowError(row, input, `${failureText(`save settings for ${name}`, errorValue)}  `
        + "Your changes are still in the form.");
      return;
    }
    row.saving = false;
    row.dirty = false;
    fillWorkerRow(row, { ...row.worker, settings: saved,
      settings_applied: row.worker.settings_applied == null ? null : false });
    say(`Settings for ${name} saved: ${settingsValues(saved)}.  `
      + "The worker applies them at its next heartbeat, within a minute.");
  }

  function showWorkers(view) {
    queue.textContent = queueWords(view);
    const workers = Array.isArray(view.workers) ? view.workers : [];
    const ids = new Set(workers.map((worker) => worker.token_id));
    for (const [id, row] of workerRows) {
      if (!ids.has(id)) {
        row.li.remove();
        workerRows.delete(id);
      }
    }
    const rows = [...workers].sort(byName).map((worker) => {
      let row = workerRows.get(worker.token_id);
      if (!row) {
        row = newWorkerRow(worker);
        workerRows.set(worker.token_id, row);
      }
      fillWorkerRow(row, worker);
      return row.li;
    });
    placeInOrder(workerList, rows, doc);
    if (workers.length === 0) {
      const none = doc.createElement("li");
      none.textContent = "No worker has connected yet.";
      workerRows.set("", { li: none });
      workerList.append(none);
    }
    return workers;
  }

  function showRefusals(view) {
    const refusals = Array.isArray(view.refusals) ? view.refusals : [];
    const ids = new Set(refusals.map((refusal) => refusal.token_id));
    for (const [id, row] of refusalRows) {
      if (!ids.has(id)) {
        row.li.remove();
        refusalRows.delete(id);
      }
    }
    const fresh = [];
    const items = [];
    for (const refusal of [...refusals].sort(byName)) {
      if (seenRefusals !== null && !seenRefusals.has(refusalKey(refusal))) fresh.push(refusal);
      let row = refusalRows.get(refusal.token_id);
      if (!row) {
        const li = doc.createElement("li");
        const text = doc.createElement("p");
        const dismiss = rowButton(doc, "Dismiss", "", (button) => void dismissRefusal(row, button));
        dismiss.dataset.action = "dismiss-refusal";
        li.append(text, dismiss);
        row = { li, text, dismiss };
        refusalRows.set(refusal.token_id, row);
      }
      row.refusal = refusal;
      items.push(row.li);
      // The time ("4 minutes ago") updates silently.
      row.text.textContent = refusalWords(refusal);
      row.dismiss.setAttribute("aria-label", `Dismiss the notice for ${refusal.name}`);
    }
    placeInOrder(refusalList, items, doc);
    seenRefusals = new Set(refusals.map(refusalKey));
    refusalHeading.hidden = refusals.length === 0;
    refusalList.hidden = refusals.length === 0;
    return fresh;
  }

  async function dismissRefusal(row, button) {
    const { refusal } = row;
    try {
      await withDisabled(button, () => request(
        `/api/analysis/workers/${encodeURIComponent(refusal.token_id)}/refusal`,
        { method: "DELETE" }));
    } catch (errorValue) {
      say(failureText(`dismiss the notice for ${refusal.name}`, errorValue), "error");
      return;
    }
    // Focus moves first: removing the focused button would drop it to the page.
    // The next notice's Dismiss, else the one before, else Refresh workers
    // (the heading hides with the last notice).
    const buttons = [...refusalList.querySelectorAll('button[data-action="dismiss-refusal"]')];
    const index = buttons.indexOf(button);
    (buttons[index + 1] || buttons[index - 1] || refresh).focus();
    row.li.remove();
    refusalRows.delete(refusal.token_id);
    if (!refusalRows.size) {
      refusalHeading.hidden = true;
      refusalList.hidden = true;
    }
    await loadWorkers();
    say(`Notice for ${refusal.name} dismissed.`);
  }

  async function loadWorkers({ announce = false } = {}) {
    try {
      const view = await request("/api/analysis/workers");
      const workers = showWorkers(view);
      const fresh = showRefusals(view);
      if (announce) {
        const connected = workers.filter((worker) => worker.connected).length;
        const refused = refusalRows.size
          ? `  ${plural(refusalRows.size, "refusal notice")}.` : "";
        say(`Workers refreshed.  ${plural(connected, "worker")} connected.  `
          + `${plural(view.waiting || 0, "song")} waiting.${refused}`);
      } else if (fresh.length) {
        // A worker newly refused is said once, even by a quiet refresh.
        say(refusalWords(fresh[0]), "error");
      }
      return true;
    } catch (errorValue) {
      if (announce) say(failureText("load the workers", errorValue), "error");
      return false;
    }
  }

  function newTokenRow(token) {
    const li = doc.createElement("li");
    const text = doc.createElement("span");
    const revoke = rowButton(doc, "Revoke", "", (button) => void revokeToken(li.token, button));
    revoke.dataset.action = "revoke";
    li.append(text, " ", revoke);
    li.token = token;
    li.text = text;
    li.revoke = revoke;
    return li;
  }

  function showTokens() {
    const ids = new Set(tokens.map((token) => token.token_id));
    for (const [id, li] of tokenRows) {
      if (!ids.has(id)) {
        li.remove();
        tokenRows.delete(id);
      }
    }
    for (const token of tokens) {
      let li = tokenRows.get(token.token_id);
      if (!li) {
        li = newTokenRow(token);
        tokenRows.set(token.token_id, li);
      }
      tokenList.append(li);
      li.token = token;
      li.text.textContent = tokenWords(token, scopeLabels, stationNames);
      li.revoke.setAttribute("aria-label", `Revoke token ${token.name}`);
    }
    tokensEmpty.hidden = tokens.length > 0;
  }

  async function loadTokens() {
    try {
      const body = await request("/api/machine-tokens");
      if (Array.isArray(body.scopes)) {
        scopeLabels = new Map(body.scopes.map((scope) => [scope.id, scope.label]));
        stationScopes = new Set(body.scopes.filter((scope) => scope.station)
          .map((scope) => scope.id));
        showScopes(body.scopes);
      }
      if (Array.isArray(body.stations)) showStations(body.stations);
      tokens = Array.isArray(body.tokens) ? body.tokens : [];
      showTokens();
      return true;
    } catch (errorValue) {
      say(failureText("load the tokens", errorValue), "error");
      return false;
    }
  }

  function option(label, value) {
    const made = doc.createElement("option");
    made.value = value;
    made.textContent = label;
    return made;
  }

  // The scope choices, keeping the one chosen; rebuilt only when they change.
  function showScopes(scopes) {
    const wanted = scopes.map((scope) => `${scope.id}\n${scope.label}`).join("\n");
    const shown = [...scopeSelect.options].map((option) => `${option.value}\n${option.textContent}`)
      .join("\n");
    if (wanted === shown) return;
    const chosen = scopeSelect.value;
    scopeSelect.replaceChildren(...scopes.map((scope) => option(scope.label, scope.id)));
    if (scopes.some((scope) => scope.id === chosen)) scopeSelect.value = chosen;
    showStationChoice();
  }

  // The station choices after a "Choose a station" placeholder, so nothing
  // is chosen silently; kept as they are when nothing changed, so a refresh
  // never resets the choice under the user.
  function showStations(stations) {
    stationNames = new Map(stations.map((station) => [station.slug, station.name]));
    const labelled = stations.map((station) => [station.slug,
      station.name && station.name !== station.slug
        ? `${station.name} (${station.slug})` : station.slug]);
    const wanted = ["\nChoose a station", ...labelled.map(([slug, label]) => `${slug}\n${label}`)]
      .join("\n");
    const shown = [...stationSelect.options].map((item) => `${item.value}\n${item.textContent}`)
      .join("\n");
    if (wanted === shown) return;
    const chosen = stationSelect.value;
    stationSelect.replaceChildren(option("Choose a station", ""),
      ...labelled.map(([slug, label]) => option(label, slug)));
    stationSelect.value = stations.some((station) => station.slug === chosen) ? chosen : "";
  }

  // The Station choice shows only for a token that works for one station.
  function showStationChoice() {
    const bound = stationScopes.has(scopeSelect.value);
    stationRow.hidden = !bound;
    stationDesc.hidden = !bound;
  }

  function hideSecret() {
    secretField.value = "";
    freshName.textContent = "";
    fresh.hidden = true;
  }

  async function createToken() {
    const name = nameInput.value.trim();
    if (!name) {
      showFieldError(nameInput, "Enter a name for the token, such as the computer's name.", get("token-error"), null);
      say("Enter a name for the token, such as the computer's name.", "error");
      return;
    }
    clearFieldError(nameInput, get("token-error"));
    const scope = scopeSelect.value;
    const payload = { name, scopes: [scope] };
    if (stationScopes.has(scope)) {
      if (!stationSelect.value) {
        say("Choose the station this token sends breaks to.", "error");
        stationSelect.focus();
        return;
      }
      payload.station = stationSelect.value;
    }
    let body;
    try {
      body = await withDisabled(create, () => request("/api/machine-tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }));
    } catch (errorValue) {
      const message = failureText("create the token", errorValue);
      if (errorValue.status === 422) showFieldError(nameInput, message, get("token-error"), null, { focus: false });
      say(message, "error");
      return;
    }
    nameInput.value = "";
    freshName.textContent = body.token.name;
    secretField.value = body.secret;
    fresh.hidden = false;
    await loadTokens();
    secretField.focus();
    secretField.select();
    say(`Token for ${body.token.name} created.  ${ONCE_WARNING}`);
  }

  async function copySecret() {
    const text = secretField.value;
    if (!text) return;
    try {
      await clipboard().writeText(text);
    } catch (_) {
      secretField.focus();
      secretField.select();
      if (!copyByCommand(text, doc)) {
        say("Could not copy.  The token is selected in the field: press Control+C to copy it.",
          "error");
        return;
      }
    }
    say("Token copied.  It will not be shown again once you choose Done.");
  }

  function finishSecret() {
    hideSecret();
    create.focus();
    say("Token hidden.");
  }

  async function revokeToken(token, button) {
    const index = tokens.indexOf(token);
    let result = null;
    const confirmed = await confirm(doc, {
      title: `Revoke token ${token.name}?`,
      message: "A program using it stops at its next request; a worker gives its songs back.  "
        + "That computer or program needs a new token to work again.",
      confirmLabel: "Revoke",
      onConfirm: async () => {
        try {
          await request(`/api/machine-tokens/${encodeURIComponent(token.token_id)}`, {
            method: "DELETE",
          });
        } catch (errorValue) {
          result = [failureText(`revoke ${token.name}`, errorValue), "error"];
          return button;
        }
        await loadTokens();
        void loadWorkers();
        result = [`Revoked token ${token.name}.`];
        return nextRowControl(tokenList, 'button[data-action="revoke"]', index, create);
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (result) say(...result);
  }

  function hidePairing() {
    pairing.code = null;
    pairing.until = 0;
    pairNew.hidden = true;
    pairCode.replaceChildren();
    pairExpiry.textContent = "";
    pairCommandField.value = "";
  }

  async function createPairing() {
    const name = pairName.value.trim();
    let body;
    try {
      body = await withDisabled(pairCreate, () => request("/api/machine-tokens/pairing-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }));
    } catch (errorValue) {
      const message = failureText("get a pairing code", errorValue);
      if (errorValue.status === 422) showFieldError(pairName, message, get("pair-worker-error"), null);
      say(message, "error");
      return;
    }
    clearFieldError(pairName, get("pair-worker-error"));
    pairing.code = String(body.code);
    pairing.until = Date.now() + body.valid_seconds * 1000;
    const shown = doc.createElement("span");
    shown.setAttribute("aria-hidden", "true");
    shown.textContent = shownCode(pairing.code);
    const heard = doc.createElement("span");
    heard.className = "visually-hidden";
    heard.textContent = spokenCode(pairing.code);
    pairCode.replaceChildren(shown, heard);
    const until = clockTime(pairing.until);
    const named = body.name ? `, for a token named ${body.name}` : "";
    pairExpiry.textContent = `Works once, until ${until}${named}.`;
    pairCommandField.value = pairCommand(pairing.code);
    pairHttp.hidden = pairTransportOk();
    pairNew.hidden = false;
    pairCommandField.focus();
    pairCommandField.select();
    say(`Pairing code ${spokenCode(pairing.code)}.  It works once, until ${until}.  `
      + "The command to run on the worker is selected.");
  }

  async function copyPairing(text, what) {
    if (!text) return;
    if (Date.now() > pairing.until) {
      say("This pairing code has expired.  Choose Pair a worker for a new one.", "error");
      return;
    }
    try {
      await clipboard().writeText(text);
    } catch (_) {
      if (!copyByCommand(text, doc)) {
        const fallback = what === "Pairing code"
          ? `The code is ${spokenCode(pairing.code)}.`
          : "The command is selected in its field: press Control+C to copy it.";
        if (what !== "Pairing code") {
          pairCommandField.focus();
          pairCommandField.select();
        }
        say(`Could not copy.  ${fallback}`, "error");
        return;
      }
    }
    say(`${what} copied.`);
  }

  function finishPairing() {
    hidePairing();
    pairCreate.focus();
    say("Pairing code hidden.");
  }

  function startPolling() {
    if (timer !== null) return;
    timer = setTimer(() => void loadWorkers(), POLL_MS);
  }

  function stopPolling() {
    if (timer === null) return;
    clearTimer(timer);
    timer = null;
  }

  async function load() {
    const [ok] = await Promise.all([loadWorkers(), loadTokens()]);
    return ok;
  }

  refresh.addEventListener("click", () => void withDisabled(refresh,
    () => loadWorkers({ announce: true })));
  create.addEventListener("click", () => void createToken());
  scopeSelect.addEventListener("change", showStationChoice);
  nameInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void createToken();
  });
  copy.addEventListener("click", () => void copySecret());
  done.addEventListener("click", finishSecret);
  pairCreate.addEventListener("click", () => void createPairing());
  pairName.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void createPairing();
  });
  pairCopyCode.addEventListener("click", () => void copyPairing(pairing.code, "Pairing code"));
  pairCopyCommand.addEventListener("click",
    () => void copyPairing(pairCommandField.value, "Command"));
  pairDone.addEventListener("click", finishPairing);
  onCardOpen(panel, () => { void load(); startPolling(); }, { onClose: stopPolling });
  if (panel.open) {
    void load();
    startPolling();
  }
  return { load };
}
