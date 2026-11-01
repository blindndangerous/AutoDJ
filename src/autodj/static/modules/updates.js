// Settings > Updates: the version and channel this server runs, whether a
// newer AutoDJ is available, the update settings, Update, Go back and
// Restart.
//
// While an update is available the panel links its release notes and, when
// this installation's updater can install it, offers Update; otherwise one
// sentence says to update it the way it was installed, with a link to its
// guide (#552).  On the Stable channel, a checkout the updater moved from a
// release to Development can go back to that release with the backup made
// then.  Commands appear only after a failed update, under Last update.
//
// Loads when its Settings subtab is shown.  The version and result lines
// are plain text (aria-live off), so loading or refreshing them says
// nothing; Check now, Save update settings, Update, Go back and Restart
// say their result once through #updates-status.
//
// Update, Go back and Restart ask first in the shared confirmation dialog,
// which says the stream goes off the air.  While AutoDJ is away the page
// asks /api/version every two seconds, and then says once whether it came
// back; after Update or Go back, it says the updater's own sentence about
// how it went.

import { onCardOpen } from "./card-state.js";
import { failureText, requestJson, withDisabled } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { makeSay } from "./live-region.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const RESTART_POLL_MS = 2000;
export const RESTART_WAIT_MS = 180000;
// An update makes a backup, fetches, runs setup and Doctor, waits for the
// new version and may roll back, restoring the backup: each step has its own
// time limit in the launcher (autodj.checkout_update), which add up to under
// four hours with setup's package downloads.  The page waits that long,
// saying quietly how long so far.
export const UPDATE_WAIT_MS = 4 * 3600000;
const MINUTE_MS = 60000;
// After a confirmation closes, focus moves back to its button; a live-region
// change in the same moment can be lost in NVDA's focus speech.  What is said
// right after the dialog waits this long first.  A refusal needs no wait when
// focus is back on its button: the button describes it (aria-describedby).
const SETTLE_MS = 300;
// After AutoDJ is back, how long the page waits for the updater's result.
const RESULT_WAIT_MS = 60000;
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

// The link text for the release notes: a release's page, or for master the
// commits since this version (GitHub's compare view), or master's commits
// when this version's commit is not known.
export function notesWords(check) {
  if (!check?.notes_url) return "";
  if (check.target && check.target.startsWith("v")) return `Release notes for ${check.target}`;
  return check.notes_url.includes("/compare/")
    ? "Changes on master since this version"
    : "Recent changes on master";
}

// "0.22.0" for a release, "master at 1234567" for the development channel.
function targetWords(check) {
  if (check?.target && check.target.startsWith("v")) return check.target.slice(1);
  return `master at ${check?.latest || "its newest commit"}`;
}

// What the check found: "AutoDJ X is available." or the server's own sentence.
function foundWords(check) {
  return check.status === "available" && check.target
    ? `AutoDJ ${targetWords(check)} is available.`
    : check.message;
}

// The result line: what the check found, and when it was checked.
export function checkWords(view, locale = undefined) {
  const check = view.check;
  if (!check) return "Not checked yet.";
  if (typeof check.checked_at !== "number") return foundWords(check);
  const when = new Date(check.checked_at * 1000).toLocaleTimeString(locale, {
    hour: "2-digit", minute: "2-digit",
  });
  return `${foundWords(check)}  Checked at ${when}.`;
}

// The guide each kind of installation updates with, by its link text.
function guideWords(kind) {
  if (kind === "container") return "Update the container";
  if (kind === "package") return "Update a wheel installation";
  return "Update a source checkout";
}

export function installUpdates({
  doc = document,
  request = requestJson,
  confirm = confirmAction,
  probe = (url) => requestJson(url, { cache: "no-store" }),
  // This no-cors health probe must accept an opaque response; requestJson
  // cannot read it. Fetch still verifies TLS normally.
  // eslint-disable-next-line no-restricted-syntax -- opaque no-cors probe for HTTPS reachability
  probeHttps = (url) => globalThis.fetch(url, {
    mode: "no-cors", credentials: "omit", cache: "no-store", redirect: "error",
  }),
  sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); }),
  now = () => Date.now(),
  reload = () => globalThis.location.reload(),
  navigate = (url) => globalThis.location.assign(url),
  settle = () => new Promise((resolve) => { setTimeout(resolve, SETTLE_MS); }),
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
  const restartNote = get("updates-restart-note");
  const restartButton = get("updates-restart");
  const notes = get("updates-notes");
  const notesLink = get("updates-notes-link");
  const updateButton = get("updates-update");
  const installNote = get("updates-install-note");
  const installWords = get("updates-install-words");
  const guideLink = get("updates-guide-link");
  const goBack = get("updates-go-back");
  const goBackNote = get("updates-go-back-note");
  const goBackButton = get("updates-go-back-button");
  const last = get("updates-last");
  const result = get("updates-result");
  const how = get("updates-how");
  const commands = get("updates-commands");
  const progress = get("updates-progress");
  const reloadButton = get("updates-reload");
  const updateRefused = get("updates-update-refused");
  const goBackRefused = get("updates-go-back-refused");
  const restartRefused = get("updates-restart-refused");
  const parts = [panel, status, current, latest, check, enabled, channel, automatic,
    restartNote, restartButton, notes, notesLink, updateButton, installNote, installWords,
    guideLink, goBack, goBackNote, goBackButton, last, result, how, commands, progress,
    reloadButton, updateRefused, goBackRefused, restartRefused];
  if (parts.some((part) => !part)) {
    return { load: async () => false, canRestart: () => false, restartForHttps: async () => false };
  }

  let view = null;
  let loading = null;

  const say = makeSay(status, { dwellMs: 4000, errorDwellMs: 6000 });

  // Each refusable button and the note that says why it was refused.
  const refusals = [
    [updateButton, updateRefused],
    [goBackButton, goBackRefused],
    [restartButton, restartRefused],
  ];

  // A refusal is written beside its button, and the button points at it,
  // while the dialog is still open: focus coming back to the button then
  // reads it, with no timing involved.  It stays until the next action.
  function showRefusal(button, note, message) {
    note.textContent = message;
    note.hidden = false;
    button.setAttribute("aria-describedby", note.id);
  }

  function clearRefusals() {
    for (const [button, note] of refusals) {
      note.textContent = "";
      note.hidden = true;
      button.removeAttribute("aria-describedby");
    }
  }

  // Also said aloud once focus has settled, even when focus came back to the
  // refused button: someone with descriptions turned off hears only the
  // button's name there.  Hearing it twice is the lesser problem.
  async function sayRefusal(_button, message) {
    await settle();
    say(message, "error");
  }

  function showLast(next) {
    const done = next.last_update;
    last.hidden = !done;
    result.textContent = done ? done.message : "";
    const lines = done && Array.isArray(done.commands) ? done.commands : [];
    how.hidden = lines.length === 0;
    commands.replaceChildren(...lines.map((line) => {
      const item = doc.createElement("li");
      const code = doc.createElement("code");
      code.textContent = line;
      item.append(code);
      return item;
    }));
  }

  function showGoBack(back) {
    goBack.hidden = !back;
    goBackButton.hidden = !back?.available;
    goBackButton.textContent = back ? `Go back to ${back.version}` : "Go back";
    if (!back) goBackNote.textContent = "";
    else if (back.available) {
      goBackNote.textContent = `You can go back to ${back.version} now with the backup AutoDJ `
        + `made on ${back.date}, when it moved this installation to Development.`;
    } else goBackNote.textContent = back.message;
  }

  function show(next) {
    view = next;
    current.textContent = installationWords(next);
    latest.textContent = checkWords(next);
    const settings = next.settings || {};
    settingsDraft?.accept({ check: settings.check !== false, channel: settings.channel || "auto" });
    automatic.textContent = `Automatic: ${channelName(next.channel?.inferred)}`;
    check.hidden = settings.check === false;
    const words = notesWords(next.check);
    notes.hidden = !words;
    notesLink.textContent = words || "Release notes";
    notesLink.href = words ? next.check.notes_url : "#";
    const available = next.check?.status === "available" && Boolean(next.check?.target);
    const update = next.update || {};
    updateButton.hidden = !(available && update.available);
    installNote.hidden = !(available && !update.available);
    installWords.textContent = update.message || "Update it the way you installed it.";
    guideLink.textContent = guideWords(next.installation?.kind);
    // No guide, no link: never a link that goes nowhere.
    if (update.guide_url) {
      guideLink.href = update.guide_url;
      guideLink.hidden = false;
    } else {
      guideLink.removeAttribute("href");
      guideLink.hidden = true;
    }
    showGoBack(next.go_back);
    showLast(next);
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
        say(failureText("load the update status", errorValue), "error");
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
      say(failureText("check for updates", errorValue), "error");
      return;
    }
    say(view.check ? foundWords(view.check) : "Checked.");
  }

  const settingsDraft = registerSettingsGroup({
    panel: panel.closest('[role="tabpanel"]') || panel,
    scope: "server",
    controls: [enabled, channel],
    fields: { check: enabled, channel },
    read: () => ({ check: enabled.checked, channel: channel.value }),
    write: (value) => {
      enabled.checked = value.check !== false;
      channel.value = value.channel || "auto";
    },
    describe: (value) => value.check
      ? `Update settings saved.  Channel: ${value.channel === "auto" ? channelName(view?.channel?.inferred) : channelName(value.channel)}.`
      : "Update settings saved.  AutoDJ no longer checks for updates.",
    commit: async (value) => {
      const next = await request("/api/updates/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(value),
      });
      show(next);
      return next;
    }
  });

  // True once AutoDJ has gone away and answered again, or answered after
  // RESTART_UNSEEN_MS; false when it is still away after *waitMs*.
  // *onAway* hears how long AutoDJ has been away, after each poll.
  async function waitForServer(waitMs = RESTART_WAIT_MS, onAway = () => {}, checkServer = () => probe("/api/version")) {
    const started = now();
    let away = false;
    while (now() - started < waitMs) {
      await sleep(RESTART_POLL_MS);
      let answered = true;
      try {
        await checkServer();
      } catch (_) {
        answered = false;
      }
      if (!answered) away = true;
      else if (away || now() - started >= RESTART_UNSEEN_MS) return true;
      onAway(now() - started);
    }
    return false;
  }

  // The quiet line while an update runs: it is still working, and for how long.
  function showProgress(words, elapsed) {
    const minutes = Math.floor(elapsed / MINUTE_MS);
    progress.hidden = false;
    progress.textContent = minutes < 1
      ? `${words}  It started less than a minute ago.`
      : `${words}  It has been working for ${minutes} ${minutes === 1 ? "minute" : "minutes"}.`;
  }

  // The updater's result for update *id*, once AutoDJ is back and it has
  // been written; null when it does not come within RESULT_WAIT_MS.
  async function resultFor(id) {
    const started = now();
    for (;;) {
      try {
        show(await request("/api/updates"));
        if (id && view.last_update?.id === id) return view.last_update;
      } catch (_) {
        // Still starting; ask again.
      }
      if (now() - started >= RESULT_WAIT_MS) return null;
      await sleep(RESTART_POLL_MS);
    }
  }

  // Focus stays on the button that started it.  When that button is gone
  // (an update that worked has nothing left to offer), it moves to the button
  // that loads the new version's page, else to Check now.
  function steadyFocus(button) {
    const active = doc.activeElement;
    if (active && active !== doc.body && !active.hidden) return;
    const target = [button, reloadButton, check, restartButton].find((each) => !each.hidden);
    target?.focus();
  }

  // Update and Go back: ask, stop AutoDJ for its updater, wait, and say once
  // how it went in the updater's own words.
  async function runUpdater({ url, button, note, dialog, refusedWords, away }) {
    let refused = null;
    let started = null;
    const confirmed = await confirm(doc, {
      ...dialog,
      onConfirm: async () => {
        try {
          started = await request(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
        } catch (errorValue) {
          refused = `${refusedWords}: ${errorValue.message}`;
          showRefusal(button, note, refused);
        }
        return button;
      },
    });
    if (!confirmed) return;
    if (refused) {
      await sayRefusal(button, refused);
      return;
    }
    await settle();
    say(away);
    reloadButton.hidden = true;
    showProgress(away, 0);
    const back = await waitForServer(UPDATE_WAIT_MS, (elapsed) => showProgress(away, elapsed));
    progress.hidden = true;
    if (!back) {
      say("AutoDJ has not come back after 4 hours.  Check the computer it runs on; the "
        + "window AutoDJ runs in says what happened.", "error");
      return;
    }
    const done = await resultFor(started?.id);
    // A new version's page may differ from this one: offer to load it.
    reloadButton.hidden = done?.outcome !== "updated";
    steadyFocus(button);
    if (!done) {
      say("AutoDJ is back, but it did not say how the update went.  Look under Last update, "
        + "or check the computer it runs on.", "error");
      return;
    }
    say(done.message, done.outcome === "updated" ? "info" : "error");
  }

  function update() {
    return runUpdater({
      url: "/api/server/update",
      button: updateButton,
      note: updateRefused,
      dialog: {
        title: "Update AutoDJ?",
        message: `AutoDJ makes a backup, installs ${targetWords(view?.check)} and starts again.  `
          + "Every station's stream goes off the air while it updates, usually for a few "
          + "minutes.  If the new version does not start, AutoDJ goes back to this one.",
        confirmLabel: "Update",
        pendingText: "Updating…",
      },
      refusedWords: "Could not update AutoDJ",
      away: "AutoDJ is updating.  The stream is off the air until it is back.",
    });
  }

  function goBackNow() {
    const back = view?.go_back || {};
    return runUpdater({
      url: "/api/server/go-back",
      button: goBackButton,
      note: goBackRefused,
      dialog: {
        title: `Go back to AutoDJ ${back.version}?`,
        message: `AutoDJ restores the backup it made on ${back.date}, when it moved to `
          + `Development, and switches back to ${back.version}.  Settings, play history and `
          + `library changes made since ${back.date} are lost.  Every station's stream goes `
          + "off the air while it goes back.",
        confirmLabel: "Go back",
        pendingText: "Going back…",
      },
      refusedWords: "Could not go back",
      away: `AutoDJ is going back to ${back.version}.  The stream is off the air until it is back.`,
    });
  }

  async function httpsRestartTarget(url) {
    try {
      const source = new URL(doc.location.href);
      const target = new URL(url, source);
      const port = (address) => address.port || (address.protocol === "http:" ? "80" : "443");
      if (target.protocol !== "https:" || target.username || target.password
        || port(target) !== port(source)) return null;
      if (target.hostname !== source.hostname) {
        // Only the server's active managed certificate can authorize a domain handoff.
        const inventory = await request("/api/server/certificates");
        const active = inventory.certificates?.find((cert) => cert.active);
        if (!inventory.can_manage || active?.type !== "Let's Encrypt"
          || !active.hosts.includes(target.hostname)
          || !inventory.urls?.some((address) => new URL(address).origin === target.origin)) return null;
      }
      return target;
    } catch (_) {
      return null;
    }
  }

  async function restart(target = null) {
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
          refused = failureText("restart AutoDJ", errorValue);
          showRefusal(restartButton, restartRefused, refused);
        }
        return restartButton;
      },
    });
    if (!confirmed) return;
    if (refused) {
      await sayRefusal(restartButton, refused);
      return;
    }
    await settle();
    say("AutoDJ is restarting.  The stream is off the air until it is back.");
    const checkServer = target
      ? () => probeHttps(new URL("/healthz", target).href)
      : undefined;
    if (await waitForServer(RESTART_WAIT_MS, () => {}, checkServer)) {
      if (target) {
        say("AutoDJ is back.");
        navigate(target.href);
        return;
      }
      await load();
      say(`AutoDJ is back.  ${view ? installationWords(view) : ""}`.trim());
    } else {
      say("AutoDJ has not come back after 3 minutes.  Check the computer it runs on.", "error");
    }
  }

  // Every action starts by clearing the last refusal.
  const act = (button, action) => () => {
    clearRefusals();
    void withDisabled(button, action);
  };
  check.addEventListener("click", act(check, checkNow));
  updateButton.addEventListener("click", act(updateButton, update));
  goBackButton.addEventListener("click", act(goBackButton, goBackNow));
  restartButton.addEventListener("click", act(restartButton, restart));
  reloadButton.addEventListener("click", () => reload());
  onCardOpen(panel, load);
  if (panel.open) void load();
  async function restartForHttps(url) {
    const target = await httpsRestartTarget(url);
    if (!target || !view?.restart?.available) return false;
    clearRefusals();
    return withDisabled(restartButton, () => restart(target));
  }

  return { load, canRestart: () => Boolean(view?.restart?.available), restartForHttps };
}
