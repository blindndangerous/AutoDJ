// Settings > Updates: the version and channel this server runs, whether a
// newer AutoDJ is available, the update settings, and Restart.
//
// While an update is available the panel links its release notes and
// lists the commands that update this installation: AutoDJ does not
// update itself (#552).
//
// Loads when its Settings subtab is shown.  The version and result lines
// are plain text (aria-live off), so loading or refreshing them says
// nothing; Check now, Save update settings and Restart say their result
// once through #updates-status.
//
// Restart asks first in the shared confirmation dialog, which says the
// stream goes off the air.  While AutoDJ is away the page asks
// /api/version every two seconds, for up to three minutes, and then says
// once whether it came back.  Restart shows only when the server says
// something would start it again; otherwise the note beside it says why.

import { requestJson, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const RESTART_POLL_MS = 2000;
export const RESTART_WAIT_MS = 180000;
// A server that answers this long after Restart without ever having been
// seen away has restarted between two polls.
const RESTART_UNSEEN_MS = 20000;

function channelName(channel) {
  return channel === "development" ? "Development" : "Stable";
}

// "AutoDJ 0.21.0, commit 0db0a92, on master.  Channel: Development, from
// this installation."
export function installationWords(view) {
  const inst = view.installation || {};
  const parts = [`AutoDJ ${inst.version || "unknown"}`];
  if (inst.commit) parts.push(`commit ${inst.commit}`);
  if (inst.kind === "git") {
    if (inst.tag) parts.push(`release tag ${inst.tag}`);
    else if (inst.branch) parts.push(`on ${inst.branch}`);
    else parts.push("not on a branch");
  } else if (inst.kind === "container") {
    parts.push("in a container");
  } else if (inst.kind === "package") {
    parts.push("installed as a package");
  }
  const channel = view.channel || {};
  const source = channel.source === "setting" ? "chosen in Update settings" : "from this installation";
  return `${parts.join(", ")}.  Channel: ${channelName(channel.name)}, ${source}.`;
}

// The line above the update commands: where to run them.
function commandsNote(view) {
  const kind = view.installation?.kind;
  if (kind === "container") {
    return "Make a backup first.  On the computer that runs the container, in the folder with "
      + "compose.yaml, run these in order, using the docker or the podman line.";
  }
  if (kind === "package") {
    return "Make a backup first.  Run this in the environment AutoDJ is installed in, then "
      + "restart AutoDJ.";
  }
  return "Stop AutoDJ and make a backup first.  Run these in order in the AutoDJ folder; "
    + "on Windows use .\\autodj.cmd in place of ./autodj.";
}

// The link text for the release notes: a release's page, or master's changelog.
function notesWords(check) {
  if (!check?.notes_url) return "";
  return check.target && check.target.startsWith("v")
    ? `Release notes for ${check.target}`
    : "Changes on master, in the changelog";
}

// The result line: the server's own sentence, and when it was checked.
export function checkWords(view, locale = undefined) {
  const check = view.check;
  if (!check) return "Not checked yet.";
  if (typeof check.checked_at !== "number") return check.message;
  const when = new Date(check.checked_at * 1000).toLocaleTimeString(locale, {
    hour: "2-digit", minute: "2-digit",
  });
  return `${check.message}  Checked at ${when}.`;
}

export function installUpdates({
  doc = document,
  request = requestJson,
  confirm = confirmAction,
  probe = (url) => requestJson(url, { cache: "no-store" }),
  sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); }),
  now = () => Date.now(),
} = {}) {
  const get = (id) => doc.getElementById(id);
  const panel = get("spanel-updates");
  const status = get("updates-status");
  const current = get("updates-current");
  const latest = get("updates-latest");
  const check = get("updates-check");
  const enabled = get("updates-enabled");
  const channel = get("updates-channel");
  const automatic = get("updates-channel-auto");
  const save = get("updates-save");
  const restartNote = get("updates-restart-note");
  const restartButton = get("updates-restart");
  const notes = get("updates-notes");
  const notesLink = get("updates-notes-link");
  const how = get("updates-how");
  const howNote = get("updates-how-note");
  const commands = get("updates-commands");
  if (!notes || !notesLink || !how || !howNote || !commands) return { load: async () => false };
  if (!panel || !status || !current || !latest || !check || !enabled || !channel
      || !automatic || !save || !restartNote || !restartButton) {
    return { load: async () => false };
  }

  let view = null;
  let loading = null;

  const say = (message, tone = "info") => {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  };

  function show(next) {
    view = next;
    current.textContent = installationWords(next);
    latest.textContent = checkWords(next);
    const settings = next.settings || {};
    enabled.checked = settings.check !== false;
    channel.value = settings.channel || "auto";
    automatic.textContent = `Automatic: ${channelName(next.channel?.inferred)}`;
    check.hidden = settings.check === false;
    const words = notesWords(next.check);
    notes.hidden = !words;
    notesLink.textContent = words || "Release notes";
    notesLink.href = words ? next.check.notes_url : "#";
    const lines = Array.isArray(next.commands) ? next.commands : [];
    how.hidden = lines.length === 0;
    howNote.textContent = lines.length ? commandsNote(next) : "";
    commands.replaceChildren(...lines.map((line) => {
      const item = doc.createElement("li");
      const code = doc.createElement("code");
      code.textContent = line;
      item.append(code);
      return item;
    }));
    const restart = next.restart || {};
    restartButton.hidden = !restart.available;
    restartNote.textContent = restart.message || "";
  }

  async function load() {
    if (loading) return loading;
    latest.textContent = view ? latest.textContent : "Checking for updates…";
    loading = (async () => {
      try {
        show(await request("/api/updates"));
        return true;
      } catch (errorValue) {
        if (!view) latest.textContent = "Could not load the update status.";
        say(`Could not load the update status: ${errorValue.message}`, "error");
        return false;
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  async function checkNow() {
    latest.textContent = "Checking for updates…";
    try {
      show(await request("/api/updates/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }));
    } catch (errorValue) {
      if (view) latest.textContent = checkWords(view);
      say(`Could not check for updates: ${errorValue.message}`, "error");
      return;
    }
    say(view.check ? view.check.message : "Checked.");
  }

  async function saveSettings() {
    try {
      show(await request("/api/updates/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ check: enabled.checked, channel: channel.value }),
      }));
    } catch (errorValue) {
      say(`Could not save the update settings: ${errorValue.message}`, "error");
      return;
    }
    say(enabled.checked
      ? `Update settings saved.  Channel: ${channelName(view.channel?.name)}.`
      : "Update settings saved.  AutoDJ no longer checks for updates.");
  }

  // True once AutoDJ has gone away and answered again, or answered after
  // RESTART_UNSEEN_MS; false when it is still away after RESTART_WAIT_MS.
  async function waitForServer() {
    const started = now();
    let away = false;
    while (now() - started < RESTART_WAIT_MS) {
      await sleep(RESTART_POLL_MS);
      let answered = true;
      try {
        await probe("/api/version");
      } catch (_) {
        answered = false;
      }
      if (!answered) away = true;
      else if (away || now() - started >= RESTART_UNSEEN_MS) return true;
    }
    return false;
  }

  async function restart() {
    let refused = null;
    const confirmed = await confirm(doc, {
      title: "Restart AutoDJ?",
      message: "Every station's stream goes off the air for a moment while AutoDJ restarts.  "
        + "This page reconnects when AutoDJ is back.",
      confirmLabel: "Restart",
      pendingText: "Restarting…",
      onConfirm: async () => {
        try {
          await request("/api/server/restart", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
        } catch (errorValue) {
          refused = `Could not restart AutoDJ: ${errorValue.message}`;
        }
        return restartButton;
      },
    });
    if (!confirmed) return;
    if (refused) {
      say(refused, "error");
      return;
    }
    say("AutoDJ is restarting.  The stream is off the air until it is back.");
    if (await waitForServer()) {
      await load();
      say(`AutoDJ is back.  ${view ? installationWords(view) : ""}`.trim());
    } else {
      say("AutoDJ has not come back after 3 minutes.  Check the computer it runs on.", "error");
    }
  }

  check.addEventListener("click", () => void withDisabled(check, checkNow));
  save.addEventListener("click", () => void withDisabled(save, saveSettings));
  restartButton.addEventListener("click", () => void withDisabled(restartButton, restart));
  panel.addEventListener("toggle", () => {
    if (panel.open) void load();
  });
  if (panel.open) void load();
  return { load };
}
