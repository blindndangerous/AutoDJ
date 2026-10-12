import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  installAnalysisWorkers, ONCE_WARNING, queueWords, workerWords,
} from "../../src/autodj/static/modules/analysis-workers.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.
function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
}

const byId = (id) => document.getElementById(id);
const said = () => vi.waitFor(() => {
  const text = byId("workers-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});
const NOW = Date.now() / 1000;

const WORKER = {
  token_id: "a".repeat(32), name: "Alienware", device: "NVIDIA RTX 3070 (CUDA GPU)",
  last_seen: NOW, connected: true, songs_done: 120, songs_failed: 2, songs_stale: 0,
  rate_per_hour: 341.6, leased: 4,
};
const TOKEN = {
  token_id: "a".repeat(32), name: "Alienware", scopes: ["analysis-worker"],
  created_at: NOW - 86400, last_used_at: NOW,
};

function server({ workers = [WORKER], tokens = [TOKEN] } = {}) {
  const state = { workers, tokens };
  const request = vi.fn(async (url, options = {}) => {
    if (url === "/api/analysis/workers") {
      return { available: true, workers: state.workers, waiting: 125, analysing: 4,
        server_paused: state.workers.some((w) => w.connected) };
    }
    if (url === "/api/machine-tokens" && options.method === "POST") {
      const body = JSON.parse(options.body);
      const token = { ...TOKEN, token_id: "b".repeat(32), name: body.name, last_used_at: null };
      state.tokens = [...state.tokens, token];
      return { token, secret: "adj1.bbbb.secret-text" };
    }
    if (url === "/api/machine-tokens") {
      return { scopes: [{ id: "analysis-worker", label: "Analysis worker" }],
        tokens: state.tokens };
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
  it("is the last Settings subtab, with no aria-controls, and a labelled panel", () => {
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
    for (const id of ["token-name", "token-secret"]) {
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
    expect(byId("workers-list").textContent).toContain("Alienware, connected");
    await vi.waitFor(() => expect(byId("token-list").children).toHaveLength(1));
    expect(byId("token-list-empty").hidden).toBe(true);
    expect(byId("token-list").querySelector("button").getAttribute("aria-label"))
      .toBe("Revoke token Alienware");
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
    byId("token-name").value = "  Alienware  ";
    byId("token-create").click();
    await vi.waitFor(() => expect(byId("token-new").hidden).toBe(false));
    const post = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(JSON.parse(post[1].body)).toEqual({ name: "Alienware", scopes: ["analysis-worker"] });
    expect(byId("token-secret").value).toBe("adj1.bbbb.secret-text");
    expect(byId("token-new-name").textContent).toBe("Alienware");
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("token-secret")));
    expect(await said()).toBe(`Token for Alienware created.  ${ONCE_WARNING}`);
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

  it("revokes a token after confirmation and moves focus sensibly", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("token-list").children).toHaveLength(1));
    byId("token-list").querySelector("button").click();
    expect(await said()).toBe("Revoked token Alienware.");
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
      "Alienware, connected, on NVIDIA RTX 3070 (CUDA GPU).  Last seen just now.  "
      + "120 songs analysed, 2 failed, about 342 an hour, 4 in progress.",
    );
    expect(workerWords({ ...WORKER, connected: false, device: "", songs_failed: 0,
      songs_stale: 1, songs_done: 1, last_seen: NOW - 7200 }, NOW * 1000)).toBe(
      "Alienware, not connected.  Last seen 2 hours ago.  "
      + "1 song analysed, 1 skipped because the file changed.",
    );
  });

  it("describes the queue and who analyses it", () => {
    expect(queueWords({ available: false })).toMatch(/no music library/);
    expect(queueWords({ available: true, waiting: 1, analysing: 0, server_paused: false }))
      .toBe("1 song waiting for analysis.  "
        + "With no worker connected, this server analyses them itself.");
  });
});
