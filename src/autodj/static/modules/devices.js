// Settings > Browser access: "Sign out this browser", the paired devices
// list with Rename and Revoke per device, and "Show pairing code" for
// pairing another device.
//
// Loads when the card is opened.  A server that does not ask browsers to
// pair says so and shows no buttons.  Signing out, or revoking this
// browser's own entry, hands over to onSignedOut(reason), which opens the
// pairing dialog; focus then belongs to that dialog.
//
// Rows are kept by device id: a reload changes their words in place and
// only adds or removes whole rows, so the button that has focus stays in
// the page while the list refreshes under it.
//
// The pairing code is said once through the status region, when it is
// shown and on Refresh code.  The code on screen and its countdown are
// not live: the code changes by itself every five minutes and is swapped
// in silently, and the countdown ticks every second.  While the code is
// shown the list is polled, and a newly paired device is said once.  Each
// code pairs one device, so the code is hidden then.

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { fmtDurationWords, nextRowControl, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

export const SIGNED_OUT_REASON =
  "This browser was signed out.  Enter a new pairing code to use it again.";
export const REVOKED_REASON =
  "This browser's pairing was revoked.  Enter a new pairing code to use it again.";

const NAME_RULE_TEXT = "The name must be 1 to 64 printable characters.";
const POLL_EVERY_SECONDS = 5;

// "28 September 2026", in the order the browser's language uses.
function pairedDate(seconds, locale = undefined) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "on an unknown date";
  return new Date(seconds * 1000).toLocaleDateString(locale, {
    day: "numeric", month: "long", year: "numeric",
  });
}

// "just now", "5 minutes ago", "3 hours ago", "2 days ago".
export function lastSeenWords(seconds, nowMs = Date.now()) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "at an unknown time";
  const ago = Math.max(0, Math.floor(nowMs / 1000 - seconds));
  if (ago < 60) return "just now";
  const [count, unit] = ago < 3600 ? [Math.floor(ago / 60), "minute"]
    : ago < 86400 ? [Math.floor(ago / 3600), "hour"]
      : [Math.floor(ago / 86400), "day"];
  return `${count} ${unit}${count === 1 ? "" : "s"} ago`;
}

// "1234 5678" on screen; "1 2 3 4, 5 6 7 8" for speech, so a screen
// reader says each digit instead of "one thousand two hundred ...".
function shownCode(code) {
  return `${code.slice(0, 4)} ${code.slice(4)}`;
}

export function spokenCode(code) {
  return `${[...code.slice(0, 4)].join(" ")}, ${[...code.slice(4)].join(" ")}`;
}

function validForWords(seconds) {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return fmtDurationWords(seconds);
  return `${minutes} minute${minutes === 1 ? "" : "s"}`;
}

export function installAccess(els, { onSignedOut }) {
  const {
    card, note, controls, signOut, refresh, list, status,
    pairToggle, pairPanel, pairCode, pairCountdown, pairCopy, pairRefresh,
  } = els;
  if (!card || !note || !controls || !signOut || !refresh || !list || !status) return;
  const doc = card.ownerDocument;
  let devices = [];
  const rows = new Map();

  const say = (message, tone = "info") => {
    announceStatus(status, message, { dwellMs: tone === "error" ? 6000 : 3000, force: true, tone });
  };

  function who(device) {
    return device.current ? `${device.name}, this browser` : device.name;
  }

  function newRow(device) {
    const row = { device };
    row.li = doc.createElement("li");
    row.text = doc.createElement("span");
    row.text.className = "device-text";
    row.rename = rowButton(doc, "Rename", "", () => void renameDevice(row));
    row.revoke = rowButton(doc, "Revoke", "", (button) => void revokeDevice(row.device, button));
    for (const [button, action] of [[row.rename, "rename"], [row.revoke, "revoke"]]) {
      button.dataset.deviceId = device.device_id;
      button.dataset.action = action;
    }
    row.li.append(row.text, " ", row.rename, " ", row.revoke);
    return row;
  }

  function render() {
    const ids = new Set(devices.map((device) => device.device_id));
    for (const [id, row] of rows) {
      if (ids.has(id)) continue;
      row.li.remove();
      rows.delete(id);
    }
    const now = Date.now();
    for (const device of devices) {
      let row = rows.get(device.device_id);
      if (!row) {
        row = newRow(device);
        rows.set(device.device_id, row);
        list.append(row.li);
      }
      row.device = device;
      const name = who(device);
      row.text.textContent = `${name}.  Paired ${pairedDate(device.paired_at)}, `
        + `last seen ${lastSeenWords(device.last_seen_at, now)}.`;
      row.rename.setAttribute("aria-label", `Rename ${name}`);
      row.revoke.setAttribute("aria-label", `Revoke ${name}`);
    }
  }

  // announce: say the count (the Refresh button).  reportNew: say any
  // device that was not in the list before (the poll while the code is
  // shown).  quiet: a failed poll says nothing.
  async function load({ announce = false, reportNew = false, quiet = false } = {}) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/devices");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      const enabled = body.pairing === true;
      note.hidden = enabled;
      controls.hidden = !enabled;
      if (!enabled) hideCode();
      const known = new Set(devices.map((device) => device.device_id));
      devices = enabled && Array.isArray(body.devices) ? body.devices : [];
      render();
      if (announce) {
        const count = devices.length;
        say(`Device list refreshed.  ${count} paired ${count === 1 ? "device" : "devices"}.`);
      } else if (reportNew) {
        const added = devices.filter((device) => !known.has(device.device_id));
        if (added.length > 0) {
          const names = added.map((device) => device.name).join(", ");
          const paired = added.length === 1
            ? `New device paired: ${names}.`
            : `New devices paired: ${names}.`;
          // The code on screen is used up; keep focus off the hidden panel.
          const focusInPanel = Boolean(pairPanel && pairPanel.contains(doc.activeElement));
          hideCode();
          if (focusInPanel && pairToggle) pairToggle.focus();
          say(`${paired}  That code is used up.  Choose Show pairing code to pair another device.`);
        }
      }
      return true;
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      if (errorValue.status === 401) hideCode();
      if (!quiet) say(`Could not load paired devices: ${errorValue.message}`, "error");
      return false;
    }
  }

  async function signOutHere() {
    const confirmed = await confirmAction(doc, {
      title: "Sign out this browser?",
      message: "Using AutoDJ here again needs a new pairing code from the server.",
      confirmLabel: "Sign out",
    });
    signOut.focus();
    if (!confirmed) return;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson("/api/logout", { method: "POST" });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not sign out: ${errorValue.message}`, "error");
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    hideCode();
    onSignedOut(SIGNED_OUT_REASON);
  }

  // The revoke runs while the confirmation is still open, so the list is
  // already updated when it closes and focus goes straight to the next
  // row's Revoke, or Refresh when none is left (see confirmAction).  The
  // result is said after the dialog has closed.
  async function revokeDevice(device, button) {
    const index = devices.indexOf(device);
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    let signedOut = false;
    const confirmed = await confirmAction(doc, {
      title: device.current ? `Revoke ${device.name}, this browser?` : `Revoke ${device.name}?`,
      message: device.current
        ? "This browser is signed out and needs a new pairing code to use AutoDJ again."
        : "That device needs a new pairing code to use AutoDJ again.",
      confirmLabel: "Revoke",
      onConfirm: async () => {
        let reply;
        try {
          reply = await requestJson(`/api/devices/${encodeURIComponent(device.device_id)}`, {
            method: "DELETE",
          });
        } catch (errorValue) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [`Could not revoke ${device.name}: ${errorValue.message}`, "error"];
          return button;
        }
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        if (reply && reply.signed_out) {
          signedOut = true;
          return null;
        }
        await load();
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        result = [`Revoked ${device.name}.`];
        return nextRowControl(list, 'button[data-action="revoke"]', index, refresh);
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (signedOut) {
      hideCode();
      onSignedOut(REVOKED_REASON);
      return;
    }
    if (result) say(...result);
  }

  // A native modal dialog with one labelled field.  Save keeps the dialog
  // open until the server answers: a refusal is said inside the dialog
  // (the page behind it is inert) and focus stays in the field.  After a
  // save the row's words change in place, the dialog closes onto the same
  // Rename button that opened it, and the new name is said once.
  function renameDevice(row) {
    const dialog = doc.getElementById("device-rename-dialog");
    const form = doc.getElementById("device-rename-form");
    const title = doc.getElementById("device-rename-title");
    const input = doc.getElementById("device-rename-name");
    const error = doc.getElementById("device-rename-error");
    const cancel = doc.getElementById("device-rename-cancel");
    if (!dialog || !form || !title || !input || !error || !cancel || dialog.open) return;
    const device = row.device;
    title.textContent = `Rename ${who(device)}`;
    input.value = device.name;
    input.removeAttribute("aria-invalid");
    error.textContent = "";
    let pending = false;
    let saved = null;

    const showError = (message) => {
      input.setAttribute("aria-invalid", "true");
      announceStatus(error, message, { force: true, mirror: false });
      input.focus();
    };

    async function submit(event) {
      event.preventDefault();
      if (pending) return;
      const name = input.value.trim();
      if (!name) {
        showError("Enter a name.");
        return;
      }
      pending = true;
      const epoch = captureAuthenticatedRequestEpoch();
      try {
        const reply = await requestJson(`/api/devices/${encodeURIComponent(device.device_id)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (isAuthenticatedRequestCurrent(epoch)) {
          await load();
          saved = reply.name;
        }
      } catch (errorValue) {
        pending = false;
        if (!isAuthenticatedRequestCurrent(epoch)) {
          dialog.close("cancel");
          return;
        }
        showError(errorValue.status === 422
          ? NAME_RULE_TEXT : `Could not rename ${device.name}: ${errorValue.message}`);
        return;
      }
      pending = false;
      dialog.close("save");
    }
    function cancelClick() {
      if (!pending) dialog.close("cancel");
    }
    function holdWhilePending(event) {
      if (pending) event.preventDefault();
    }
    function closed() {
      form.removeEventListener("submit", submit);
      cancel.removeEventListener("click", cancelClick);
      dialog.removeEventListener("cancel", holdWhilePending);
      dialog.removeEventListener("close", closed);
      error.textContent = "";
      (row.rename.isConnected ? row.rename : refresh).focus();
      if (saved !== null) say(`Renamed to ${saved}.`);
    }
    form.addEventListener("submit", submit);
    cancel.addEventListener("click", cancelClick);
    dialog.addEventListener("cancel", holdWhilePending);
    dialog.addEventListener("close", closed);
    dialog.showModal();
    input.focus();
    input.select();
  }

  // ---- Pairing code ------------------------------------------------

  const pairing = { timer: null, code: null, validUntil: 0, nextAt: 0, ticks: 0, busy: false };
  const hasPairingUi = Boolean(pairToggle && pairPanel && pairCode && pairCountdown
    && pairCopy && pairRefresh);

  function showCountdown(now) {
    const left = Math.ceil((pairing.validUntil - now) / 1000);
    pairCountdown.textContent = left > 0
      ? `Valid for ${fmtDurationWords(left)}.`
      : "This code has expired.  Press Refresh code.";
  }

  function hideCode() {
    if (!hasPairingUi) return;
    clearInterval(pairing.timer);
    pairing.timer = null;
    pairing.code = null;
    pairPanel.hidden = true;
    pairToggle.setAttribute("aria-expanded", "false");
    pairCode.replaceChildren();
    pairCountdown.textContent = "";
  }

  // Fetches the current code and shows it.  speak: say it once (Show
  // pairing code and Refresh code); a rotation while shown is silent.
  async function fetchCode({ speak }) {
    const epoch = captureAuthenticatedRequestEpoch();
    let body;
    try {
      body = await requestJson("/api/pairing-code");
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      if (errorValue.status === 401) hideCode();
      if (speak) say(`Could not get a pairing code: ${errorValue.message}`, "error");
      return false;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    if (!speak && pairing.timer === null) return false;
    const now = Date.now();
    pairing.code = String(body.code);
    pairing.validUntil = now + body.valid_seconds * 1000;
    pairing.nextAt = now + body.next_code_seconds * 1000;
    const shown = doc.createElement("span");
    shown.setAttribute("aria-hidden", "true");
    shown.textContent = shownCode(pairing.code);
    const heard = doc.createElement("span");
    heard.className = "visually-hidden";
    heard.textContent = spokenCode(pairing.code);
    pairCode.replaceChildren(shown, heard);
    showCountdown(now);
    if (speak) {
      say(`Pairing code ${spokenCode(pairing.code)}, valid for ${validForWords(body.valid_seconds)}.`);
    }
    return true;
  }

  function tick() {
    const now = Date.now();
    showCountdown(now);
    if (now >= pairing.nextAt) {
      // A failed fetch tries again in five seconds, not every tick.
      pairing.nextAt = now + POLL_EVERY_SECONDS * 1000;
      void fetchCode({ speak: false });
    }
    pairing.ticks += 1;
    if (pairing.ticks % POLL_EVERY_SECONDS === 0) void load({ reportNew: true, quiet: true });
  }

  async function showCode() {
    if (pairing.busy) return;
    pairing.busy = true;
    try {
      if (!await fetchCode({ speak: true })) return;
      pairPanel.hidden = false;
      pairToggle.setAttribute("aria-expanded", "true");
      pairing.ticks = 0;
      clearInterval(pairing.timer);
      pairing.timer = setInterval(tick, 1000);
    } finally {
      pairing.busy = false;
    }
  }

  // Plain http is not a secure context, so it has no clipboard API; the
  // older copy command still works there through a one-off copy listener.
  function copyByCommand(text) {
    let wrote = false;
    const onCopy = (event) => {
      if (!event.clipboardData) return;
      event.clipboardData.setData("text/plain", text);
      event.preventDefault();
      wrote = true;
    };
    doc.addEventListener("copy", onCopy);
    try {
      return typeof doc.execCommand === "function" && doc.execCommand("copy") === true && wrote;
    } catch (_) {
      return false;
    } finally {
      doc.removeEventListener("copy", onCopy);
    }
  }

  async function copyCode() {
    const code = pairing.code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
    } catch (_) {
      if (!copyByCommand(code)) {
        say(`Could not copy.  The code is ${spokenCode(code)}.`, "error");
        return;
      }
    }
    say("Pairing code copied.");
  }

  signOut.addEventListener("click", () => void signOutHere());
  refresh.addEventListener("click", () => void load({ announce: true }));
  if (hasPairingUi) {
    pairToggle.addEventListener("click", () => {
      if (pairing.timer !== null) hideCode();
      else void showCode();
    });
    pairRefresh.addEventListener("click", () => void fetchCode({ speak: true }));
    pairCopy.addEventListener("click", () => void copyCode());
  }
  card.addEventListener("toggle", () => {
    if (card.open) void load();
    else hideCode();
  });
  if (card.open) void load();
}
