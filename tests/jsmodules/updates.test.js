import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import {
  checkWords, installationWords, installUpdates, notesWords, RESTART_WAIT_MS, UPDATE_WAIT_MS,
} from "../../src/autodj/static/modules/updates.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const said = () => vi.waitFor(() => {
  const text = byId("updates-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});
const LAUNCHER = {
  available: true, mechanism: "launcher", message: "The AutoDJ launcher updates this checkout.",
  guide_url: "https://github.com/blindndangerous/AutoDJ/blob/master/docs/guides/backups-and-updates.md#update-a-source-checkout",
};
const RELEASE = {
  status: "available", channel: "stable", channel_source: "installation",
  message: "Release v0.22.0 is available.", latest: "v0.22.0", target: "v0.22.0",
  checked_at: null,
  notes_url: "https://github.com/blindndangerous/AutoDJ/releases/tag/v0.22.0",
};
const CURRENT = { status: "current", message: "This is the latest release, v0.22.0." };

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
      notes_url: "https://github.com/blindndangerous/AutoDJ/compare/0db0a92...master",
    },
    restart: { available: true, mode: "launcher", message: "The AutoDJ launcher starts AutoDJ again." },
    update: LAUNCHER,
    go_back: null,
    last_update: null,
    ...overrides,
  };
}

function server(initial = view()) {
  const state = { view: initial, posted: [], after: null };
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
    if (url.startsWith("/api/server/") && options.method === "POST") {
      state.posted.push(url);
      if (state.refuse) {
        const error = new Error(state.refuse);
        error.status = 409;
        throw error;
      }
      if (state.after) state.view = state.after;
      return url === "/api/server/restart" ? { restarting: true } : { updating: true, id: "abcdef12" };
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

function install({
  initial, confirm = confirmYes, probeAnswers = [false, true], stepMs = 0,
} = {}) {
  const { request, state } = server(initial);
  let clock = 0;
  const answers = [...probeAnswers];
  const progressSeen = [];
  const probe = vi.fn(async () => {
    progressSeen.push(byId("updates-progress").hidden ? "" : byId("updates-progress").textContent);
    if (answers.length && !answers.shift()) throw new Error("Could not reach AutoDJ.");
    return { version: "0.21.0" };
  });
  const reload = vi.fn();
  installUpdates({
    request, confirm, probe, reload, settle: async () => {},
    sleep: async (ms) => { clock += ms + stepMs; },
    now: () => clock,
  });
  return { request, state, probe, reload, progressSeen };
}

// The result the updater leaves for the page, as GET /api/updates returns it.
function finished(outcome, message, commands = []) {
  return {
    id: "abcdef12", action: "update", outcome, message, commands, finished_at: 1_793_000_000,
  };
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
    expect(document.querySelectorAll("#spanel-updates [aria-live='polite']")).toHaveLength(1);
    for (const id of ["updates-enabled", "updates-channel"]) {
      expect(document.querySelector(`label[for="${id}"]`), id).not.toBeNull();
    }
    for (const id of ["updates-check", "updates-save", "updates-restart", "updates-update",
      "updates-go-back-button"]) {
      expect(byId(id).tagName).toBe("BUTTON");
      expect(byId(id).type).toBe("button");
    }
    for (const id of ["updates-current", "updates-latest", "updates-result"]) {
      expect(byId(id).getAttribute("aria-live"), id).toBe("off");
    }
    for (const id of ["updates-restart", "updates-update", "updates-go-back", "updates-last"]) {
      expect(byId(id).hidden, id).toBe(true);
    }
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
    expect(checkWords(view({ check: RELEASE }))).toBe("AutoDJ 0.22.0 is available.");
    expect(checkWords(view())).toBe("AutoDJ master at 1234567 is available.");
  });

  // ADR-0016-09
  it("names the release notes of a tag and the changes on master since this version", () => {
    expect(notesWords({ target: "v0.22.0",
      notes_url: "https://github.com/blindndangerous/AutoDJ/releases/tag/v0.22.0" }))
      .toBe("Release notes for v0.22.0");
    expect(notesWords(view().check)).toBe("Changes on master since this version");
    expect(notesWords({ target: "master",
      notes_url: "https://github.com/blindndangerous/AutoDJ/commits/master" }))
      .toBe("Recent changes on master");
    expect(notesWords({ target: null, notes_url: null })).toBe("");
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
    expect(byId("updates-latest").textContent).toBe("AutoDJ master at 1234567 is available.");
    expect(byId("updates-restart").hidden).toBe(true);
    expect(byId("updates-restart-note").textContent).toBe("Restart it yourself.");
    expect(byId("updates-channel-auto").textContent).toBe("Automatic: Development");
    expect(byId("updates-status").textContent).toBe("");
  });

  // ADR-0016-09
  it("links the release notes and offers Update, with no command list, while an update is available", async () => {
    install({ initial: view({ check: RELEASE }) });
    open();
    await vi.waitFor(() => expect(byId("updates-notes").hidden).toBe(false));
    const link = byId("updates-notes-link");
    expect(link.textContent).toBe("Release notes for v0.22.0");
    expect(link.getAttribute("href")).toBe(
      "https://github.com/blindndangerous/AutoDJ/releases/tag/v0.22.0",
    );
    expect(byId("updates-update").hidden).toBe(false);
    expect(byId("updates-update").textContent).toBe("Update AutoDJ");
    expect(byId("updates-install-note").hidden).toBe(true);
    expect(byId("updates-how").hidden).toBe(true);
    expect(byId("updates-commands").children).toHaveLength(0);
  });

  // ADR-0016-21
  it("tells an installation that cannot update to update it the way it was installed", async () => {
    const guide = "https://github.com/blindndangerous/AutoDJ/blob/master/docs/guides/operations/containers-and-windows.md#update-the-container";
    install({
      initial: view({
        installation: { kind: "container", version: "0.21.0", commit: "0db0a92" },
        check: RELEASE,
        update: { available: false, mechanism: null, message: "Update it the way you installed it.",
          guide_url: guide },
      }),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-install-note").hidden).toBe(false));
    expect(byId("updates-update").hidden).toBe(true);
    expect(byId("updates-install-words").textContent).toBe("Update it the way you installed it.");
    const link = byId("updates-guide-link");
    expect(link.textContent).toBe("Update the container");
    expect(link.getAttribute("href")).toBe(guide);
    expect(byId("updates-how").hidden).toBe(true);
  });

  // ADR-0016-21
  it("leaves the guide link out when there is no guide", async () => {
    install({
      initial: view({
        installation: { kind: "package", version: "0.21.0" },
        check: RELEASE,
        update: { available: false, mechanism: null, message: "Update it the way you installed it.",
          guide_url: null },
      }),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-install-note").hidden).toBe(false));
    expect(byId("updates-guide-link").hidden).toBe(true);
    expect(byId("updates-guide-link").hasAttribute("href")).toBe(false);
  });

  // ADR-0016-21
  it("says quietly that a long update is still working, and waits up to 3 hours", async () => {
    const { state, progressSeen } = install({
      initial: view({ check: RELEASE }),
      probeAnswers: Array(60).fill(false),
      stepMs: 58000,
    });
    state.after = view({ check: CURRENT, last_update: finished("updated", "Updated to 0.22.0.") });
    open();
    await vi.waitFor(() => expect(byId("updates-update").hidden).toBe(false));
    byId("updates-update").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toBe("Updated to 0.22.0."));
    expect(byId("updates-progress").getAttribute("aria-live")).toBe("off");
    expect(progressSeen[0]).toBe(
      "AutoDJ is updating.  The stream is off the air until it is back.  "
      + "It started less than a minute ago.",
    );
    expect(progressSeen).toContain(
      "AutoDJ is updating.  The stream is off the air until it is back.  "
      + "It has been working for 30 minutes.",
    );
    expect(UPDATE_WAIT_MS).toBe(3 * 3600000);
  });

  it("hides the notes, Update and commands when nothing is available", async () => {
    install({ initial: view({ check: { status: "current", message: "This is the latest." } }) });
    open();
    await vi.waitFor(() => expect(byId("updates-latest").textContent).toBe("This is the latest."));
    expect(byId("updates-notes").hidden).toBe(true);
    expect(byId("updates-update").hidden).toBe(true);
    expect(byId("updates-install-note").hidden).toBe(true);
    expect(byId("updates-how").hidden).toBe(true);
    expect(byId("updates-commands").children).toHaveLength(0);
  });

  // ADR-0016-19
  it("says development waits for the next release on the Stable channel, with no Update", async () => {
    const waiting = "You'll move to Stable when the next release includes your version. "
      + "Installed: 0.22.0, commit 0db0a92, on master. Latest release: v0.21.0.";
    install({
      initial: view({
        channel: { name: "stable", source: "setting", inferred: "development" },
        check: { status: "waiting-for-release", message: waiting, latest: "v0.21.0", target: null },
      }),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-latest").textContent).toBe(waiting));
    expect(byId("updates-update").hidden).toBe(true);
    expect(byId("updates-install-note").hidden).toBe(true);
  });

  it("says the result of Check now once", async () => {
    const { request } = install();
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-check").click();
    expect(await said()).toBe("AutoDJ master at 1234567 is available.");
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

  // ADR-0016-21
  it("describes a refused restart on its button before the dialog closes", async () => {
    const seen = {};
    const confirm = vi.fn(async (_doc, { onConfirm }) => {
      const target = await onConfirm();
      seen.note = byId("updates-restart-refused").textContent;
      seen.describedBy = target.getAttribute("aria-describedby");
      target.focus();
      return true;
    });
    const { state } = install({ confirm });
    state.refuse = "AutoDJ restarted less than a minute ago. Try again in 40 seconds.";
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").focus();  // a press focuses the button, as in a browser
    byId("updates-restart").click();
    const words = "Could not restart AutoDJ: AutoDJ restarted less than a minute ago. "
      + "Try again in 40 seconds.";
    await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
    await vi.waitFor(() => expect(seen.note).toBe(words));
    expect(seen.describedBy).toBe("updates-restart-refused");
    expect(document.activeElement).toBe(byId("updates-restart"));
    expect(byId("updates-restart-refused").hidden).toBe(false);
    expect(byId("updates-restart-refused").getAttribute("aria-live")).toBe("off");
    // Focus back on the button reads it: no second, live announcement.
    await Promise.resolve();
    expect(byId("updates-status").textContent).toBe("");
    byId("updates-check").click();
    expect(byId("updates-restart-refused").hidden).toBe(true);
    expect(byId("updates-restart").hasAttribute("aria-describedby")).toBe(false);
  });

  it("says so when AutoDJ does not come back", async () => {
    install({ probeAnswers: Array(RESTART_WAIT_MS).fill(false) });
    open();
    await vi.waitFor(() => expect(byId("updates-restart").hidden).toBe(false));
    byId("updates-restart").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toMatch(/has not come back/));
  });
});

describe("Update and Go back", () => {
  // ADR-0016-21
  it("asks before Update, says the stream goes off the air, and says once how it went", async () => {
    const confirm = vi.fn(confirmYes);
    const { state, reload } = install({ initial: view({ check: RELEASE }), confirm });
    state.after = view({
      installation: { kind: "git", version: "0.22.0", commit: "2222222", tag: "v0.22.0" },
      check: CURRENT,
      last_update: finished("updated", "Updated to 0.22.0."),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-update").hidden).toBe(false));
    byId("updates-update").focus();
    byId("updates-update").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toBe("Updated to 0.22.0."));
    const options = confirm.mock.calls[0][1];
    expect(options.title).toBe("Update AutoDJ?");
    expect(options.message).toMatch(/installs 0\.22\.0/);
    expect(options.message).toMatch(/off the air/);
    expect(options.confirmLabel).toBe("Update");
    expect(state.posted).toEqual(["/api/server/update"]);
    // Nothing left to update: focus moves to the button that loads the new
    // version's page, rather than a hidden button.
    expect(byId("updates-update").hidden).toBe(true);
    expect(byId("updates-reload").hidden).toBe(false);
    expect(document.activeElement).toBe(byId("updates-reload"));
    expect(byId("updates-progress").hidden).toBe(true);
    byId("updates-reload").click();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(byId("updates-last").hidden).toBe(false);
    expect(byId("updates-result").textContent).toBe("Updated to 0.22.0.");
    expect(byId("updates-how").hidden).toBe(true);
  });

  // ADR-0016-21
  it("says the update failed, why, and only then lists the commands for that failure", async () => {
    const { state } = install({ initial: view({ check: RELEASE }) });
    const message = "The update failed, so AutoDJ went back to 0.21.0. The new version needs a "
      + "library migration, which AutoDJ does not run by itself.";
    const commands = ["git switch --detach v0.22.0", "./autodj setup", "./autodj migrate-index"];
    state.after = view({ check: RELEASE, last_update: finished("rolled-back", message, commands) });
    open();
    await vi.waitFor(() => expect(byId("updates-update").hidden).toBe(false));
    byId("updates-update").click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toBe(message));
    // The old version is back: there is no new page to load.
    expect(byId("updates-reload").hidden).toBe(true);
    expect(byId("updates-how").hidden).toBe(false);
    const heading = byId("updates-how-heading");
    expect(heading.tagName).toBe("H5");
    expect(heading.closest("#updates-last").querySelector("h4").textContent).toBe("Last update");
    expect([...byId("updates-commands").querySelectorAll("li > code")].map((code) => code.textContent))
      .toEqual(commands);
    expect(byId("updates-commands").getAttribute("aria-labelledby")).toBe("updates-how-heading");
  });

  // ADR-0016-21
  it("describes a refused update on its button before the dialog closes, until the next action", async () => {
    const seen = {};
    const confirm = vi.fn(async (_doc, { onConfirm }) => {
      const target = await onConfirm();
      seen.note = byId("updates-update-refused").textContent;
      seen.hidden = byId("updates-update-refused").hidden;
      seen.describedBy = target.getAttribute("aria-describedby");
      target.focus();
      return true;
    });
    const { state } = install({ initial: view({ check: RELEASE }), confirm });
    state.refuse = "AutoDJ was already updated 6 times in the last hour. "
      + "Try again in about 32 minutes.";
    open();
    await vi.waitFor(() => expect(byId("updates-update").hidden).toBe(false));
    byId("updates-update").focus();  // a press focuses the button, as in a browser
    byId("updates-update").click();
    const words = "Could not update AutoDJ: AutoDJ was already updated 6 times in the last hour. "
      + "Try again in about 32 minutes.";
    await vi.waitFor(() => expect(seen.note).toBe(words));
    expect(seen.hidden).toBe(false);
    expect(seen.describedBy).toBe("updates-update-refused");
    const note = byId("updates-update-refused");
    expect(note.getAttribute("aria-live")).toBe("off");
    await Promise.resolve();
    expect(byId("updates-status").textContent).toBe("");
    byId("updates-check").click();
    expect(note.hidden).toBe(true);
    expect(note.textContent).toBe("");
    expect(byId("updates-update").hasAttribute("aria-describedby")).toBe(false);
  });

  // ADR-0016-21
  it("says a refusal aloud when focus did not come back to its button", async () => {
    const confirm = vi.fn(async (_doc, { onConfirm }) => {
      await onConfirm();
      byId("updates-check").focus();  // the button was gone, say
      return true;
    });
    const { state } = install({ initial: view({ check: RELEASE }), confirm });
    state.refuse = "AutoDJ was updated less than a minute ago. Try again in 40 seconds.";
    open();
    await vi.waitFor(() => expect(byId("updates-update").hidden).toBe(false));
    byId("updates-update").click();
    expect(await said()).toBe(
      "Could not update AutoDJ: AutoDJ was updated less than a minute ago. Try again in 40 seconds.",
    );
  });

  // ADR-0016-21
  it("keeps a refused Go back's note beside Go back, not Update", async () => {
    const seen = {};
    const confirm = vi.fn(async (_doc, { onConfirm }) => {
      const target = await onConfirm();
      seen.describedBy = target.getAttribute("aria-describedby");
      target.focus();
      return true;
    });
    const back = { available: true, tag: "v0.21.0", version: "0.21.0", date: "2026-10-27", message: "" };
    const { state } = install({ initial: view({ go_back: back }), confirm });
    state.refuse = "AutoDJ was updated less than a minute ago. Try again in 40 seconds.";
    open();
    await vi.waitFor(() => expect(byId("updates-go-back-button").hidden).toBe(false));
    byId("updates-go-back-button").focus();
    byId("updates-go-back-button").click();
    const note = byId("updates-go-back-refused");
    await vi.waitFor(() => expect(note.hidden).toBe(false));
    expect(note.previousElementSibling).toBe(byId("updates-go-back-button"));
    expect(note.textContent).toBe(
      "Could not go back: AutoDJ was updated less than a minute ago. Try again in 40 seconds.",
    );
    expect(seen.describedBy).toBe("updates-go-back-refused");
    expect(byId("updates-update-refused").hidden).toBe(true);
    byId("updates-check").click();
    expect(note.hidden).toBe(true);
    expect(byId("updates-go-back-button").hasAttribute("aria-describedby")).toBe(false);
  });

  // ADR-0016-21
  it("offers Go back with its version and date, and says what is lost before it starts", async () => {
    const confirm = vi.fn(confirmYes);
    const back = { available: true, tag: "v0.21.0", version: "0.21.0", date: "2026-10-27", message: "" };
    const { state } = install({
      initial: view({
        channel: { name: "stable", source: "setting", inferred: "development" },
        check: { status: "waiting-for-release", message: "You'll move to Stable later.", target: null },
        go_back: back,
      }),
      confirm,
    });
    state.after = view({
      check: CURRENT,
      last_update: { ...finished("updated", "Went back to 0.21.0."), action: "go-back" },
    });
    open();
    await vi.waitFor(() => expect(byId("updates-go-back").hidden).toBe(false));
    const button = byId("updates-go-back-button");
    expect(button.textContent).toBe("Go back to 0.21.0");
    expect(byId("updates-go-back-note").textContent).toBe(
      "You can go back to 0.21.0 now with the backup AutoDJ made on 2026-10-27, when it moved "
      + "this installation to Development.",
    );
    button.click();
    await vi.waitFor(() => expect(byId("updates-status").textContent).toBe("Went back to 0.21.0."));
    const options = confirm.mock.calls[0][1];
    expect(options.title).toBe("Go back to AutoDJ 0.21.0?");
    expect(options.message).toContain("made on 2026-10-27");
    expect(options.message).toContain(
      "Settings, play history and library changes made since 2026-10-27 are lost.",
    );
    expect(options.message).toMatch(/off the air/);
    expect(options.confirmLabel).toBe("Go back");
    expect(state.posted).toEqual(["/api/server/go-back"]);
  });

  // ADR-0016-21
  it("says in one sentence why Go back is not offered", async () => {
    const reason = "Going back to 0.21.0 is not offered, because the backup AutoDJ made on "
      + "2026-10-27 is missing.";
    install({
      initial: view({
        go_back: { available: false, tag: "v0.21.0", version: "0.21.0", date: "2026-10-27",
          message: reason },
      }),
    });
    open();
    await vi.waitFor(() => expect(byId("updates-go-back").hidden).toBe(false));
    expect(byId("updates-go-back-note").textContent).toBe(reason);
    expect(byId("updates-go-back-button").hidden).toBe(true);
  });
});
