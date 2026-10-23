import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import {
  checkWords, installationWords, installUpdates, RESTART_WAIT_MS,
} from "../../src/autodj/static/modules/updates.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const said = () => vi.waitFor(() => {
  const text = byId("updates-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});

function view(overrides = {}) {
  return {
    installation: {
      kind: "git", version: "0.21.0", commit: "0db0a92", branch: "master", tag: null,
    },
    settings: { check: true, channel: null },
    channel: { name: "development", source: "installation", inferred: "development" },
    check: {
      status: "available", channel: "development", channel_source: "installation",
      message: "master has new commits, up to 1234567.", latest: "1234567", target: "master",
      checked_at: null,
      notes_url: "https://github.com/blindndangerous/AutoDJ/blob/master/CHANGELOG.md",
    },
    commands: ["git fetch origin", "git switch master", "git merge --ff-only origin/master",
      "./autodj setup", "./autodj doctor --no-fix"],
    restart: { available: true, mode: "launcher", message: "The AutoDJ launcher starts AutoDJ again." },
    ...overrides,
  };
}

function server(initial = view()) {
  const state = { view: initial, posted: [] };
  const request = vi.fn(async (url, options = {}) => {
    if (url === "/api/updates") return state.view;
    if (url === "/api/updates/check" && options.method === "POST") return state.view;
    if (url === "/api/updates/settings" && options.method === "PUT") {
      const body = JSON.parse(options.body);
      state.view = {
        ...state.view,
        settings: { check: body.check, channel: body.channel === "auto" ? null : body.channel },
        channel: { ...state.view.channel, name: body.channel === "auto" ? "development" : body.channel,
          source: body.channel === "auto" ? "installation" : "setting" },
      };
      return state.view;
    }
    if (url === "/api/server/restart" && options.method === "POST") {
      state.posted.push(url);
      if (state.refuse) {
        const error = new Error(state.refuse);
        error.status = 429;
        throw error;
      }
      return { restarting: true };
    }
    throw new Error(`unexpected ${url}`);
  });
  return { request, state };
}

const confirmYes = vi.fn(async (_doc, { onConfirm }) => {
  const target = await onConfirm();
  target?.focus();
  return true;
});

function open() {
  const panel = byId("spanel-updates");
  panel.hidden = false;
  panel.open = true;
  panel.dispatchEvent(new Event("toggle"));
}

function install({ initial, confirm = confirmYes, probeAnswers = [false, true] } = {}) {
  const { request, state } = server(initial);
  let clock = 0;
  const answers = [...probeAnswers];
  const probe = vi.fn(async () => {
    if (answers.length && !answers.shift()) throw new Error("Could not reach AutoDJ.");
    return { version: "0.21.0" };
  });
  installUpdates({
    request, confirm, probe,
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
  });
  return { request, state, probe };
}

beforeEach(installIndex);
afterEach(() => vi.restoreAllMocks());

describe("Updates markup", () => {
  it("is the last Settings subtab, after Analysis workers, with a labelled panel", () => {
    const tab = byId("stab-updates");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(tab.previousElementSibling.id).toBe("stab-workers");
    expect(tab.nextElementSibling).toBeNull();
    const panel = byId(tab.dataset.panel);
    expect(panel.getAttribute("role")).toBe("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe("stab-updates");
    expect(panel.querySelector(":scope > h3").textContent).toBe("Updates");
  });

  // ADR-0016-07
  it("uses native labelled controls, quiet result lines and one polite status region", () => {
    const status = byId("updates-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    for (const id of ["updates-enabled", "updates-channel"]) {
      expect(document.querySelector(`label[for="${id}"]`), id).not.toBeNull();
    }
    for (const id of ["updates-check", "updates-save", "updates-restart"]) {
      expect(byId(id).tagName).toBe("BUTTON");
      expect(byId(id).type).toBe("button");
    }
    for (const id of ["updates-current", "updates-latest"]) {
      expect(byId(id).getAttribute("aria-live"), id).toBe("off");
    }
    expect(byId("updates-restart").hidden).toBe(true);
  });
});

describe("Updates words", () => {
  it("names the version, commit, branch and where the channel comes from", () => {
    expect(installationWords(view())).toBe(
      "AutoDJ 0.21.0, commit 0db0a92, on master.  Channel: Development, from this installation.",
    );
    expect(installationWords(view({
      installation: { kind: "git", version: "0.21.0", commit: "abcdef1", branch: null, tag: "v0.21.0" },
      channel: { name: "stable", source: "setting" },
    }))).toBe(
      "AutoDJ 0.21.0, commit abcdef1, release tag v0.21.0.  Channel: Stable, chosen in Update settings.",
    );
    expect(checkWords(view({ check: null }))).toBe("Not checked yet.");
  });
});

describe("Updates panel", () => {
  it("loads when shown, quietly, and shows Restart only when it is possible", async () => {
    const { request } = install({
      initial: view({ restart: { available: false, mode: null, message: "Restart it yourself." } }),
    });
    expect(request).not.toHaveBeenCalled();
    open();
    await vi.waitFor(() => expect(byId("updates-current").textContent).toMatch(/^AutoDJ 0\.21\.0/));
    expect(byId("updates-latest").textContent).toBe("master has new commits, up to 1234567.");
    expect(byId("updates-restart").hidden).toBe(true);
    expect(byId("updates-restart-note").textContent).toBe("Restart it yourself.");
    expect(byId("updates-channel-auto").textContent).toBe("Automatic: Development");
    expect(byId("updates-status").textContent).toBe("");
  });

  // ADR-0016-09
  it("links the release notes and lists the update commands while an update is available", async () => {
    install({
      initial: view({
        check: {
          status: "available", channel: "stable", channel_source: "installation",
          message: "Release v0.22.0 is available.", latest: "v0.22.0", target: "v0.22.0",
          checked_at: null,
          notes_url: "https://github.com/blindndangerous/AutoDJ/releases/tag/v0.22.0",
        },
      }),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-notes").hidden).toBe(false));
    const link = byId("updates-notes-link");
    expect(link.textContent).toBe("Release notes for v0.22.0");
    expect(link.getAttribute("href")).toBe(
      "https://github.com/blindndangerous/AutoDJ/releases/tag/v0.22.0",
    );
    expect(byId("updates-how").hidden).toBe(false);
    const items = [...byId("updates-commands").querySelectorAll("li > code")];
    expect(items.map((code) => code.textContent)[0]).toBe("git fetch origin");
    expect(byId("updates-how-note").textContent).toContain("on Windows use .\\autodj.cmd in place of ./autodj.");
    expect(byId("updates-commands").getAttribute("aria-labelledby")).toBe("updates-how-heading");
  });

  it("hides the notes and commands when nothing is available", async () => {
    install({ initial: view({ check: { status: "current", message: "This is the latest." },
      commands: [] }) });
    open();
    await vi.waitFor(() => expect(byId("updates-latest").textContent).toBe("This is the latest."));
    expect(byId("updates-notes").hidden).toBe(true);
    expect(byId("updates-how").hidden).toBe(true);
    expect(byId("updates-commands").children).toHaveLength(0);
  });

  it("says the result of Check now once", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-check").click();
    expect(await said()).toBe("master has new commits, up to 1234567.");
    expect(request).toHaveBeenCalledWith("/api/updates/check", expect.objectContaining({ method: "POST" }));
  });

  it("saves the check switch and channel and says so", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-channel").value = "stable";
    byId("updates-save").click();
    expect(await said()).toBe("Update settings saved.  Channel: Stable.");
    const call = request.mock.calls.find(([url]) => url === "/api/updates/settings");
    expect(JSON.parse(call[1].body)).toEqual({ check: true, channel: "stable" });
  });

  // ADR-0016-07
  it("asks before Restart, says it is going off the air, and says when it is back", async () => {
    const confirm = vi.fn(confirmYes);
    const { state, probe } = install({ confirm });
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").focus();
    byId("updates-restart").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toMatch(/^AutoDJ is back/));
    const options = confirm.mock.calls[0][1];
    expect(options.title).toBe("Restart AutoDJ?");
    expect(options.message).toMatch(/off the air/);
    expect(options.confirmLabel).toBe("Restart");
    expect(state.posted).toEqual(["/api/server/restart"]);
    expect(probe).toHaveBeenCalledWith("/api/version");
    expect(document.activeElement).toBe(byId("updates-restart"));
  });

  it("does nothing when Restart is cancelled", async () => {
    const { state } = install({ confirm: vi.fn(async () => false) });
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").click();
    await Promise.resolve();
    expect(state.posted).toEqual([]);
    expect(byId("updates-status").textContent).toBe("");
  });

  it("says why a refused restart did not happen", async () => {
    const { state } = install();
    state.refuse = "AutoDJ restarted a moment ago. Try again in 40 seconds.";
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").click();
    expect(await said()).toBe(
      "Could not restart AutoDJ: AutoDJ restarted a moment ago. Try again in 40 seconds.",
    );
  });

  it("says so when AutoDJ does not come back", async () => {
    install({ probeAnswers: Array(RESTART_WAIT_MS).fill(false) });
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toMatch(/has not come back/));
  });
});
