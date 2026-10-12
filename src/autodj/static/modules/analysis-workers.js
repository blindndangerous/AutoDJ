// Settings > Analysis workers: the computers analysing songs for this
// server (autodj worker), how many songs are waiting, and the tokens
// those computers use.
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

import { requestJson, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { copyByCommand, lastSeenWords } from "./devices.js";
import { nextRowControl, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

const POLL_MS = 10000;
export const ONCE_WARNING =
  "This token is shown only now and cannot be shown again.  Copy it before you leave this page.";

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function shownDate(seconds, locale = undefined) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "on an unknown date";
  return new Date(seconds * 1000).toLocaleDateString(locale, {
    day: "numeric", month: "long", year: "numeric",
  });
}

// "Alienware, connected, on NVIDIA GeForce RTX 3070 (CUDA GPU).  Last seen
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

function tokenWords(token, scopeLabels) {
  const scopes = token.scopes.map((scope) => scopeLabels.get(scope) || scope).join(", ");
  const used = token.last_used_at == null
    ? "never used"
    : `last used ${lastSeenWords(token.last_used_at)}`;
  return `${token.name}.  ${scopes}.  Created ${shownDate(token.created_at)}, ${used}.`;
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
  const create = get("token-create");
  const fresh = get("token-new");
  const freshName = get("token-new-name");
  const secretField = get("token-secret");
  const copy = get("token-copy");
  const done = get("token-done");
  const tokenList = get("token-list");
  const tokensEmpty = get("token-list-empty");
  if (!panel || !status || !queue || !workerList || !refresh || !nameInput || !create
      || !fresh || !freshName || !secretField || !copy || !done || !tokenList || !tokensEmpty) {
    return { load: async () => false };
  }

  const workerRows = new Map();
  const tokenRows = new Map();
  let tokens = [];
  let scopeLabels = new Map([["analysis-worker", "Analysis worker"]]);
  let timer = null;

  const say = (message, tone = "info") => {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  };

  function showWorkers(view) {
    queue.textContent = queueWords(view);
    const workers = Array.isArray(view.workers) ? view.workers : [];
    const ids = new Set(workers.map((worker) => worker.token_id));
    for (const [id, li] of workerRows) {
      if (!ids.has(id)) {
        li.remove();
        workerRows.delete(id);
      }
    }
    for (const worker of workers) {
      let li = workerRows.get(worker.token_id);
      if (!li) {
        li = doc.createElement("li");
        workerRows.set(worker.token_id, li);
      }
      workerList.append(li);
      li.textContent = workerWords(worker);
    }
    if (workers.length === 0) {
      const none = doc.createElement("li");
      none.textContent = "No worker has connected yet.";
      workerRows.set("", none);
      workerList.append(none);
    }
    return workers;
  }

  async function loadWorkers({ announce = false } = {}) {
    try {
      const view = await request("/api/analysis/workers");
      const workers = showWorkers(view);
      if (announce) {
        const connected = workers.filter((worker) => worker.connected).length;
        say(`Workers refreshed.  ${plural(connected, "worker")} connected.  `
          + `${plural(view.waiting || 0, "song")} waiting.`);
      }
      return true;
    } catch (errorValue) {
      if (announce) say(`Could not load the workers: ${errorValue.message}`, "error");
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
      li.text.textContent = tokenWords(token, scopeLabels);
      li.revoke.setAttribute("aria-label", `Revoke token ${token.name}`);
    }
    tokensEmpty.hidden = tokens.length > 0;
  }

  async function loadTokens() {
    try {
      const body = await request("/api/machine-tokens");
      if (Array.isArray(body.scopes)) {
        scopeLabels = new Map(body.scopes.map((scope) => [scope.id, scope.label]));
      }
      tokens = Array.isArray(body.tokens) ? body.tokens : [];
      showTokens();
      return true;
    } catch (errorValue) {
      say(`Could not load the tokens: ${errorValue.message}`, "error");
      return false;
    }
  }

  function hideSecret() {
    secretField.value = "";
    freshName.textContent = "";
    fresh.hidden = true;
  }

  async function createToken() {
    const name = nameInput.value.trim();
    if (!name) {
      nameInput.setAttribute("aria-invalid", "true");
      say("Enter a name for the token, such as the computer's name.", "error");
      nameInput.focus();
      return;
    }
    nameInput.removeAttribute("aria-invalid");
    let body;
    try {
      body = await withDisabled(create, () => request("/api/machine-tokens", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, scopes: ["analysis-worker"] }),
      }));
    } catch (errorValue) {
      if (errorValue.status === 422) nameInput.setAttribute("aria-invalid", "true");
      say(`Could not create the token: ${errorValue.message}`, "error");
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
      message: "A worker using it stops at its next request and gives its songs back.  "
        + "That computer needs a new token to work again.",
      confirmLabel: "Revoke",
      onConfirm: async () => {
        try {
          await request(`/api/machine-tokens/${encodeURIComponent(token.token_id)}`, {
            method: "DELETE",
          });
        } catch (errorValue) {
          result = [`Could not revoke ${token.name}: ${errorValue.message}`, "error"];
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
  nameInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void createToken();
  });
  copy.addEventListener("click", () => void copySecret());
  done.addEventListener("click", finishSecret);
  panel.addEventListener("toggle", () => {
    if (panel.open) {
      void load();
      startPolling();
    } else {
      stopPolling();
    }
  });
  if (panel.open) {
    void load();
    startPolling();
  }
  return { load };
}
