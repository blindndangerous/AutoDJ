
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import {
  installAnalysisWorkers, ONCE_WARNING, pairCommand, pairTransportOk, queueWords, refusalWords,
  settingsWords, tokenWords, workerWords,
} from "../../src/autodj/static/modules/analysis-workers.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const said = () => vi.waitFor(() => {
  const text = byId("workers-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});
const NOW = Date.now() / 1000;

const WORKER = {
  token_id: "a".repeat(32), name: "Studio PC", device: "NVIDIA RTX 4060 (CUDA GPU)",
  last_seen: NOW, connected: true, songs_done: 120, songs_failed: 2, songs_stale: 0,
  rate_per_hour: 341.6, leased: 4,
};
const TOKEN = {
  token_id: "a".repeat(32), name: "Studio PC", scopes: ["analysis-worker"],
  created_at: NOW - 86400, last_used_at: NOW,
};

function server({ workers = [WORKER], tokens = [TOKEN], refusals = [] } = {}) {
  const state = { workers, tokens, refusals };
  const request = vi.fn(async (url, options = {}) => {
    if (url === "/api/analysis/workers") {
      return { available: true, workers: state.workers, waiting: 125, analysing: 4,
        server_paused: state.workers.some((w) => w.connected), refusals: state.refusals };
    }
    if (url.endsWith("/settings") && options.method === "PUT") {
      const body = JSON.parse(options.body);
      const id = url.split("/")[4];
      const worker = state.workers.find((w) => w.token_id === id);
      const settings = { ...body, revision: (worker.settings?.revision || 0) + 1 };
      state.workers = state.workers.map((w) => (w.token_id === id
        ? { ...w, settings, settings_applied: false } : w));
      return settings;
    }
    if (url.endsWith("/refusal") && options.method === "DELETE") {
      const id = url.split("/")[4];
      state.refusals = state.refusals.filter((r) => r.token_id !== id);
      return { dismissed: id };
    }
    if (url === "/api/machine-tokens/pairing-code") {
      const body = JSON.parse(options.body);
      return { code: "12345678", valid_seconds: 600, name: body.name || null };
    }
    if (url === "/api/machine-tokens" && options.method === "POST") {
      const body = JSON.parse(options.body);
      const token = { ...TOKEN, token_id: "b".repeat(32), name: body.name, last_used_at: null,
        scopes: body.scopes, station: body.station ?? null };
      state.tokens = [...state.tokens, token];
      return { token, secret: "adj1.bbbb.secret-text" };
    }
    if (url === "/api/machine-tokens") {
      return {
        scopes: [
          { id: "analysis-worker", label: "Analysis worker", station: false },
          { id: "announcer", label: "Announcer", station: true },
        ],
        stations: [{ slug: "default", name: "Example FM" }, { slug: "nightowl", name: "Night Owl" }],
        tokens: state.tokens,
      };
    }
    if (url.startsWith("/api/machine-tokens/") && options.method === "DELETE") {
      const id = url.split("/").pop();
      state.tokens = state.tokens.filter((t) => t.token_id !== id);
      return { revoked: id };
    }
    throw new Error(`unexpected ${url}`);
  });
  return { request, state };
}

// The confirmation as the real dialog runs it: onConfirm while open, then
// focus to what it returned.
const confirmYes = vi.fn(async (_doc, { onConfirm }) => {
  const target = await onConfirm();
  target?.focus();
  return true;
});

function open() {
  const panel = byId("spanel-workers");
  panel.hidden = false;
  panel.open = true;
  panel.dispatchEvent(new Event("toggle"));
}

function install(overrides = {}) {
  const { request, state } = server(overrides);
  const timers = { set: vi.fn(() => 7), clear: vi.fn() };
  const clipboard = { writeText: vi.fn(async () => {}) };
  installAnalysisWorkers({
    request, confirm: confirmYes, setTimer: timers.set, clearTimer: timers.clear,
    clipboard: () => clipboard,
  });
  return { request, state, timers, clipboard };
}

beforeEach(installIndex);
afterEach(() => vi.restoreAllMocks());

describe("Analysis workers markup", () => {
  it("follows Profiles and access, with no aria-controls, and a labelled panel", () => {
    const tab = byId("stab-workers");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(tab.previousElementSibling.id).toBe("stab-access");
    const panel = byId(tab.dataset.panel);
    expect(panel.getAttribute("role")).toBe("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe("stab-workers");
    expect(panel.querySelector(":scope > h3").textContent).toBe("Analysis workers");
  });

  it("uses native, labelled controls and one polite status region", () => {
    const status = byId("workers-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    for (const id of ["token-name", "token-scope", "token-station", "token-secret"]) {
      expect(document.querySelector(`label[for="${id}"]`), id).not.toBeNull();
      const described = byId(id).getAttribute("aria-describedby").split(/\s+/);
      for (const ref of described) expect(byId(ref), ref).not.toBeNull();
    }
    expect(byId("token-secret").readOnly).toBe(true);
    expect(byId("token-secret-warning").textContent).toMatch(/shown only now/);
    for (const id of ["workers-refresh", "token-create", "token-copy", "token-done"]) {
      expect(byId(id).tagName).toBe("BUTTON");
      expect(byId(id).type).toBe("button");
    }
    expect(byId("workers-list").getAttribute("aria-labelledby")).toBe("workers-heading");
    expect(byId("token-list").getAttribute("aria-labelledby")).toBe("token-list-heading");
    // The queue line and the lists update every ten seconds; none is live.
    for (const id of ["workers-queue", "workers-list", "token-list"]) {
      expect(byId(id).hasAttribute("aria-live"), id).toBe(false);
    }
  });
});

describe("Analysis workers panel", () => {
  it("loads when shown and polls only while shown", async () => {
    const { request, timers } = install();
    expect(request).not.toHaveBeenCalled();
    open();
    await vi.waitFor(() => expect(byId("workers-list").children).toHaveLength(1));
    expect(byId("workers-queue").textContent).toBe(
      "125 songs waiting for analysis, 4 being analysed now.  "
      + "This server's own analysis is paused while a worker is connected.",
    );
    expect(byId("workers-list").textContent).toContain("Studio PC, connected");
    await vi.waitFor(() => expect(byId("token-list").children).toHaveLength(1));
    expect(byId("token-list-empty").hidden).toBe(true);
    expect(byId("token-list").querySelector("button").getAttribute("aria-label"))
      .toBe("Revoke token Studio PC");
    expect(timers.set).toHaveBeenCalledWith(expect.any(Function), 10000);
    const panel = byId("spanel-workers");
    panel.open = false;
    panel.dispatchEvent(new Event("toggle"));
    expect(timers.clear).toHaveBeenCalledWith(7);
  });

  it("keeps rows in place on refresh and says the result once", async () => {
    install();
    open();
    await vi.waitFor(() => expect(byId("workers-list").children).toHaveLength(1));
    const row = byId("workers-list").firstElementChild;
    byId("workers-refresh").click();
    expect(await said()).toBe("Workers refreshed.  1 worker connected.  125 songs waiting.");
    expect(byId("workers-list").firstElementChild).toBe(row);
  });

  it("says when no worker has connected and there are no tokens", async () => {
    install({ workers: [], tokens: [] });
    open();
    await vi.waitFor(() => expect(byId("workers-list").textContent)
      .toBe("No worker has connected yet."));
    expect(byId("token-list-empty").hidden).toBe(false);
  });

  it("asks for a name before creating a token", async () => {
    const { request } = install();
    open();
    byId("token-create").click();
    expect(await said()).toMatch(/Enter a name/);
    expect(byId("token-name").getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(byId("token-name"));
    expect(request).not.toHaveBeenCalledWith("/api/machine-tokens", expect.objectContaining({
      method: "POST" }));
  });

  it("shows a new token once, focused, with a spoken warning, then hides it on Done", async () => {
    const { request, clipboard } = install();
    open();
    byId("token-name").value = "  Studio PC  ";
    byId("token-create").click();
    await vi.waitFor(() => expect(byId("token-new").hidden).toBe(false));
    const post = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(JSON.parse(post[1].body)).toEqual({ name: "Studio PC", scopes: ["analysis-worker"] });
    expect(byId("token-secret").value).toBe("adj1.bbbb.secret-text");
    expect(byId("token-new-name").textContent).toBe("Studio PC");
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("token-secret")));
    expect(await said()).toBe(`Token for Studio PC created.  ${ONCE_WARNING}`);
    expect(byId("token-name").value).toBe("");

    byId("workers-status").textContent = "";
    byId("token-copy").click();
    expect(await said()).toMatch(/^Token copied\./);
    expect(clipboard.writeText).toHaveBeenCalledWith("adj1.bbbb.secret-text");

    // Switching tabs keeps it until it is copied and Done is chosen.
    const panel = byId("spanel-workers");
    panel.open = false;
    panel.dispatchEvent(new Event("toggle"));
    expect(byId("token-secret").value).toBe("adj1.bbbb.secret-text");

    byId("workers-status").textContent = "";
    byId("token-done").click();
    expect(byId("token-new").hidden).toBe(true);
    expect(byId("token-secret").value).toBe("");
    expect(document.activeElement).toBe(byId("token-create"));
    expect(await said()).toBe("Token hidden.");
  });

  it("tells the user how to copy when the clipboard is unavailable", async () => {
    const { clipboard } = install();
    clipboard.writeText.mockRejectedValue(new Error("not secure"));
    document.execCommand = vi.fn(() => false);
    open();
    byId("token-name").value = "Box";
    byId("token-create").click();
    await vi.waitFor(() => expect(byId("token-new").hidden).toBe(false));
    byId("workers-status").textContent = "";
    byId("token-copy").click();
    expect(await said()).toMatch(/press Control\+C/);
    expect(document.activeElement).toBe(byId("token-secret"));
  });

  it("shows the station choice only for an announcer token and sends it", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("token-station").options).toHaveLength(3));
    expect(byId("token-station-row").hidden).toBe(true);
    expect(byId("token-station-desc").hidden).toBe(true);
    const scope = byId("token-scope");
    scope.value = "announcer";
    scope.dispatchEvent(new Event("change"));
    expect(byId("token-station-row").hidden).toBe(false);
    expect(byId("token-station-desc").hidden).toBe(false);
    expect([...byId("token-station").options].map((o) => o.textContent))
      .toEqual(["Choose a station", "Example FM (default)", "Night Owl (nightowl)"]);
    // Nothing is chosen for the user.
    expect(byId("token-station").value).toBe("");
    byId("token-station").value = "nightowl";
    byId("token-name").value = "news-bot";
    byId("token-create").click();
    await vi.waitFor(() => expect(byId("token-new").hidden).toBe(false));
    const post = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(JSON.parse(post[1].body))
      .toEqual({ name: "news-bot", scopes: ["announcer"], station: "nightowl" });
    await vi.waitFor(() => expect(byId("token-list").textContent)
      .toContain("news-bot.  Announcer, for Night Owl."));
    scope.value = "analysis-worker";
    scope.dispatchEvent(new Event("change"));
    expect(byId("token-station-row").hidden).toBe(true);
  });

  it("asks for a station until one is chosen, and keeps the choice across reloads", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("token-station").options).toHaveLength(3));
    byId("token-scope").value = "announcer";
    byId("token-name").value = "news-bot";
    byId("token-create").click();
    expect(await said()).toMatch(/Choose the station/);
    expect(document.activeElement).toBe(byId("token-station"));
    expect(request).not.toHaveBeenCalledWith("/api/machine-tokens", expect.objectContaining({
      method: "POST" }));
    // A reload of the same stations leaves the options and the choice alone.
    byId("token-station").value = "nightowl";
    const first = byId("token-station").options[1];
    byId("token-done").click();
    byId("workers-refresh").click();
    const panel = byId("spanel-workers");
    panel.open = false;
    panel.dispatchEvent(new Event("toggle"));
    open();
    await vi.waitFor(() => expect(request.mock.calls.filter(([url]) => url === "/api/machine-tokens"))
      .toHaveLength(2));
    expect(byId("token-station").options[1]).toBe(first);
    expect(byId("token-station").value).toBe("nightowl");
  });

  it("revokes a token after confirmation and moves focus sensibly", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("token-list").children).toHaveLength(1));
    byId("token-list").querySelector("button").click();
    expect(await said()).toBe("Revoked token Studio PC.");
    expect(request).toHaveBeenCalledWith(`/api/machine-tokens/${"a".repeat(32)}`,
      { method: "DELETE" });
    expect(byId("token-list").children).toHaveLength(0);
    expect(document.activeElement).toBe(byId("token-create"));
  });

  it("says a failure as an error", async () => {
    const request = vi.fn(async () => {
      throw Object.assign(new Error("Server unavailable"), { status: 503 });
    });
    installAnalysisWorkers({ request, setTimer: vi.fn(), clearTimer: vi.fn() });
    open();
    byId("workers-refresh").click();
    await vi.waitFor(() => expect(byId("workers-status").textContent)
      .toMatch(/Could not load the workers: Server unavailable/));
  });
});

describe("worker words", () => {
  it("names the worker, its device, counts and rate", () => {
    expect(workerWords(WORKER, NOW * 1000)).toBe(
      "Studio PC, connected, on NVIDIA RTX 4060 (CUDA GPU).  Last seen just now.  "
      + "120 songs analysed, 2 failed, about 342 an hour, 4 in progress.",
    );
    expect(workerWords({ ...WORKER, connected: false, device: "", songs_failed: 0,
      songs_stale: 1, songs_done: 1, last_seen: NOW - 7200 }, NOW * 1000)).toBe(
      "Studio PC, not connected.  Last seen 2 hours ago.  "
      + "1 song analysed, 1 skipped because the file changed.",
    );
  });

  it("names a token's use and its station", () => {
    const labels = new Map([["announcer", "Announcer"]]);
    const token = { ...TOKEN, name: "news-bot", scopes: ["announcer"], station: "nightowl",
      last_used_at: null };
    expect(tokenWords(token, labels, new Map([["nightowl", "Night Owl"]])))
      .toMatch(/^news-bot\. {2}Announcer, for Night Owl\. {2}Created .*, never used\.$/);
    expect(tokenWords(token, labels)).toMatch(/Announcer, for nightowl\./);
    expect(tokenWords({ ...TOKEN, station: null }, new Map()))
      .toMatch(/^Studio PC\. {2}analysis-worker\. {2}Created/);
  });

  it("describes the queue and who analyses it", () => {
    expect(queueWords({ available: false })).toMatch(/no music library/);
    expect(queueWords({ available: true, waiting: 1, analysing: 0, server_paused: false }))
      .toBe("1 song waiting for analysis.  "
        + "With no worker connected, this server analyses them itself.");
  });
});

const REFUSAL = {
  token_id: "c".repeat(32), name: "Old PC", kind: "revoked", detail: "", refused_at: NOW - 300,
  revoked: true, message: "Its token was revoked.  Pair the worker again, or give it a new "
    + "token, and start it again.",
};

describe("managing workers from Settings (#554)", () => {
  // ADR-0008-30
  it("gives each worker labelled native settings, filled from the server", async () => {
    install({ workers: [{ ...WORKER, settings: { paused: true, batch: 2,
      idle_release_minutes: null, revision: 3 }, settings_applied: true }] });
    open();
    const id = "a".repeat(32);
    await vi.waitFor(() => expect(byId(`worker-${id}-paused`)).not.toBeNull());
    const fieldset = byId(`worker-${id}-paused`).closest("fieldset");
    expect(fieldset.querySelector("legend").textContent).toBe("Settings for Studio PC");
    for (const [kind, type, label] of [["paused", "checkbox", "Paused"],
      ["batch", "number", "Songs per claim"], ["idle", "number", "Idle release minutes"]]) {
      const input = byId(`worker-${id}-${kind}`);
      expect(input.type).toBe(type);
      expect(document.querySelector(`label[for="${input.id}"]`).textContent).toBe(label);
      if (kind === "paused") expect(input.hasAttribute("aria-describedby")).toBe(false);
      else expect(byId(input.getAttribute("aria-describedby")).textContent)
        .toBe("Empty uses the worker's own setting; its command line wins.");
    }
    expect(byId(`worker-${id}-paused`).checked).toBe(true);
    expect(byId(`worker-${id}-batch`).value).toBe("2");
    expect(byId(`worker-${id}-idle`).value).toBe("");
    const save = fieldset.querySelector("button");
    expect(save.type).toBe("button");
    expect(save.textContent).toBe("Save");
    expect(save.getAttribute("aria-label")).toBe("Save settings for Studio PC");
    expect(byId("workers-list").textContent).toContain(
      "Settings: paused, 2 songs per claim, the worker's own idle release.  "
      + "The worker has applied them.");
  });

  // ADR-0008-30
  it("saves a row, says so, and a refresh keeps changes not yet saved", async () => {
    const { request } = install({ workers: [{ ...WORKER, settings_applied: true, settings: {
      paused: false, batch: null, idle_release_minutes: null, revision: 1 } }] });
    open();
    const id = "a".repeat(32);
    await vi.waitFor(() => expect(byId(`worker-${id}-batch`)).not.toBeNull());
    byId(`worker-${id}-paused`).checked = true;
    byId(`worker-${id}-paused`).dispatchEvent(new Event("change"));
    byId(`worker-${id}-idle`).value = "5";
    byId(`worker-${id}-idle`).dispatchEvent(new Event("input"));
    // A refresh before Save leaves what was typed.
    byId("workers-refresh").click();
    await said();
    expect(byId(`worker-${id}-idle`).value).toBe("5");
    byId("workers-status").textContent = "";
    byId(`worker-${id}-paused`).closest("fieldset").querySelector("button").click();
    expect(await said()).toMatch(/^Settings for Studio PC saved: paused\./);
    const put = request.mock.calls.find(([, options]) => options?.method === "PUT");
    expect(put[0]).toBe(`/api/analysis/workers/${id}/settings`);
    expect(JSON.parse(put[1].body)).toEqual({ paused: true, batch: null, idle_release_minutes: 5 });
    expect(byId("workers-list").textContent).toContain("Waiting for the worker to apply them.");
  });

  // ADR-0008-30
  it("marks a value out of range, says why and moves focus to it", async () => {
    const { request } = install();
    open();
    const id = "a".repeat(32);
    await vi.waitFor(() => expect(byId(`worker-${id}-batch`)).not.toBeNull());
    const batch = byId(`worker-${id}-batch`);
    batch.value = "17";
    batch.dispatchEvent(new Event("input"));
    batch.closest("fieldset").querySelector("button").click();
    expect(await said()).toMatch(/enter songs per claim from 1 to 16/);
    expect(batch.getAttribute("aria-invalid")).toBe("true");
    const [help, errorId] = batch.getAttribute("aria-describedby").split(" ");
    expect(help).toBe(`worker-${id}-help`);
    const error = byId(errorId);
    expect(error.hidden).toBe(false);
    expect(error.textContent).toMatch(/from 1 to 16/);
    expect(document.activeElement).toBe(batch);
    expect(request.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
    // A corrected value clears the error but keeps the help linked.
    batch.value = "3";
    batch.closest("fieldset").querySelector("button").click();
    await vi.waitFor(() => expect(error.hidden).toBe(true));
    expect(batch.getAttribute("aria-describedby")).toBe(`worker-${id}-help`);
    expect(batch.hasAttribute("aria-invalid")).toBe(false);
  });

  // ADR-0008-30
  it("shows refusal notices in words, says a new one once, and dismisses them", async () => {
    const { state } = install();
    open();
    await vi.waitFor(() => expect(byId("workers-list").children).toHaveLength(1));
    expect(byId("workers-refusals").hidden).toBe(true);
    expect(byId("workers-refusals-heading").hidden).toBe(true);
    // A refresh on the timer finds a new refusal and says it once.
    state.refusals = [REFUSAL];
    byId("workers-status").textContent = "";
    byId("spanel-workers").dispatchEvent(new Event("toggle"));
    expect(await said()).toMatch(/^Refused: Old PC \(revoked token\), 5 minutes ago\./);
    expect(byId("workers-refusals").hidden).toBe(false);
    expect(byId("workers-refusals").textContent).toMatch(
      /^Refused: Old PC \(revoked token\), 5 minutes ago\. {2}Its token was revoked\./);
    byId("workers-status").textContent = "";
    byId("workers-refresh").click();
    expect(await said()).toMatch(/1 refusal notice\.$/);
    const dismiss = byId("workers-refusals").querySelector("button");
    expect(dismiss.getAttribute("aria-label")).toBe("Dismiss the notice for Old PC");
    byId("workers-status").textContent = "";
    dismiss.click();
    expect(await said()).toBe("Notice for Old PC dismissed.");
    expect(byId("workers-refusals").hidden).toBe(true);
    expect(document.activeElement).toBe(byId("workers-refresh"));
  });

  // ADR-0008-30
  it("pairs a worker with a spoken code, a selected command and Copy buttons", async () => {
    const { request, clipboard } = install();
    open();
    expect(document.querySelector('label[for="pair-worker-name"]').textContent)
      .toBe("Worker name");
    expect(byId("pair-worker-command").readOnly).toBe(true);
    expect(byId(byId("pair-worker-name").getAttribute("aria-describedby")).textContent)
      .toMatch(/^Optional\. {2}Empty uses the worker computer's own name\.$/);
    byId("pair-worker-name").value = " Studio PC ";
    byId("pair-worker-create").click();
    await vi.waitFor(() => expect(byId("pair-worker-new").hidden).toBe(false));
    const post = request.mock.calls.find(([url]) => url === "/api/machine-tokens/pairing-code");
    expect(JSON.parse(post[1].body)).toEqual({ name: "Studio PC" });
    expect(await said()).toMatch(/^Pairing code 1 2 3 4, 5 6 7 8\. {2}It works once, until /);
    expect(byId("pair-worker-code").querySelector("[aria-hidden=true]").textContent)
      .toBe("1234 5678");
    expect(byId("pair-worker-command").value)
      .toBe(`autodj worker --pair 12345678 --server ${location.origin}`);
    expect(document.activeElement).toBe(byId("pair-worker-command"));
    expect(byId("pair-worker-expiry").textContent).toMatch(/for a token named Studio PC\.$/);
    byId("workers-status").textContent = "";
    byId("pair-worker-copy-code").click();
    expect(await said()).toBe("Pairing code copied.");
    expect(clipboard.writeText).toHaveBeenCalledWith("12345678");
    byId("workers-status").textContent = "";
    byId("pair-worker-copy-command").click();
    expect(await said()).toBe("Command copied.");
    byId("workers-status").textContent = "";
    byId("pair-worker-done").click();
    expect(byId("pair-worker-new").hidden).toBe(true);
    expect(byId("pair-worker-command").value).toBe("");
    expect(document.activeElement).toBe(byId("pair-worker-create"));
  });

  it("words settings, refusals and the pairing command", () => {
    expect(settingsWords({ settings: { paused: false, batch: null, idle_release_minutes: 1,
      revision: 0 } })).toBe("Settings: running, the worker's own batch size, "
      + "idle release after 1 minute.");
    expect(settingsWords({ settings: { paused: true, batch: 1, idle_release_minutes: null,
      revision: 2 }, settings_applied: null })).toMatch(/an older worker cannot, but Paused/);
    expect(refusalWords({ ...REFUSAL, revoked: false, refused_at: NOW }, NOW * 1000))
      .toBe(`Refused: Old PC, just now.  ${REFUSAL.message}`);
    expect(pairCommand("12345678", "https://nas.local:8765"))
      .toBe("autodj worker --pair 12345678 --server https://nas.local:8765");
    expect(pairTransportOk({ protocol: "http:", hostname: "192.168.1.5" })).toBe(false);
    expect(pairTransportOk({ protocol: "http:", hostname: "localhost" })).toBe(true);
    expect(pairTransportOk({ protocol: "https:", hostname: "nas.local" })).toBe(true);
  });
});

describe("worker rows across refreshes (#554 review)", () => {
  const second = { ...WORKER, token_id: "b".repeat(32), name: "GPU box", last_seen: NOW - 60,
    settings: { paused: false, batch: null, idle_release_minutes: null, revision: 0 } };

  // ADR-0008-30
  it("keeps focus on a field in any row while polls reorder last-seen times", async () => {
    const { request, state, timers } = install({ workers: [WORKER, second] });
    const loads = () => request.mock.calls.filter(([url]) => url === "/api/analysis/workers").length;
    open();
    const poll = timers.set.mock.calls[0][0];
    await vi.waitFor(() => expect(byId("workers-list").children).toHaveLength(2));
    // A fixed order by name, whatever the server's order.
    expect([...byId("workers-list").children].map((li) => li.querySelector("legend").textContent))
      .toEqual(["Settings for GPU box", "Settings for Studio PC"]);
    for (const id of ["a".repeat(32), "b".repeat(32)]) {
      const input = byId(`worker-${id}-idle`);
      input.focus();
      input.value = "7";
      input.dispatchEvent(new Event("input"));
      for (const order of [[second, WORKER], [WORKER, second], [second, WORKER]]) {
        state.workers = order.map((worker) => ({ ...worker, last_seen: NOW }));
        const before = loads();
        poll();
        await vi.waitFor(() => expect(loads()).toBeGreaterThan(before));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(document.activeElement).toBe(input);
      }
      expect(input.value).toBe("7");
    }
  });

  // ADR-0008-30
  it("says an ongoing refusal once, though its time keeps moving", async () => {
    const { state, timers } = install({ refusals: [] });
    open();
    const poll = timers.set.mock.calls[0][0];
    await vi.waitFor(() => expect(byId("workers-list").children).toHaveLength(1));
    state.refusals = [{ ...REFUSAL, refused_at: NOW - 120 }];
    poll();
    expect(await said()).toMatch(/^Refused: Old PC/);
    for (const refusedAt of [NOW - 60, NOW]) {
      byId("workers-status").textContent = "";
      state.refusals = [{ ...REFUSAL, refused_at: refusedAt }];
      poll();
      await vi.waitFor(() => expect(byId("workers-refusals").textContent)
        .toMatch(/just now|1 minute ago/));
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(byId("workers-status").textContent).toBe("");
    }
    // The same worker refused for another reason is news again.
    state.refusals = [{ ...REFUSAL, kind: "invalid",
      message: "It sent a token whose secret is wrong." }];
    poll();
    expect(await said()).toMatch(/secret is wrong/);
  });
});
