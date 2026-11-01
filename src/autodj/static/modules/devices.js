import { copyByCommand } from "./clipboard.js";
// Settings > Browser access: native pairing or SSO-owned browser names.
// SSO forgetting removes a name without revoking the provider session.
// Native: "Sign out this browser", the paired devices
// list with Rename and Revoke per device, and "Show pairing code" for
// pairing another device.
//
// Loads when its Settings subtab is shown.  A server that does not ask browsers to
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
  failureText,
} from "./api-client.js";
import { authenticationState } from "./auth.js";
import { confirmAction } from "./confirm-dialog.js";
import { createEditorDialog } from "./editor-dialog.js";
import { clearFieldError, fmtTime, showFieldError } from "./dom-helpers.js";
import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { tableFocusTarget } from "./table-row.js";
import { makeSay } from "./live-region.js";
import { onCardOpen } from "./card-state.js";

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
export function shownCode(code) {
  return `${code.slice(0, 4)} ${code.slice(4)}`;
}

export function spokenCode(code) {
  return `${[...code.slice(0, 4)].join(" ")}, ${[...code.slice(4)].join(" ")}`;
}

export function installAccess(els, { onSignedOut, getAuthState = authenticationState }) {
  const {
    card, note, controls, signOut, refresh, list, status,
    pairToggle, pairPanel, pairCode, pairCountdown, pairCopy, pairRefresh,
  } = els;
  if (!card || !note || !controls || !signOut || !refresh || !list || !status) return;
  const doc = card.ownerDocument;
  let devices = [];
  let renderedDevices = null;
  let sso = false;
  const nativeNote = note.textContent;
  const nativeSignOutLabel = signOut.textContent;
  const signOutDescription = doc.getElementById("access-sign-out-desc");
  const nativeSignOutDescription = signOutDescription?.textContent;
  const pairingSection = doc.getElementById("access-pairing");
  const heading = doc.getElementById("device-list-heading");

  const say = makeSay(status, { dwellMs: 3000, errorDwellMs: 6000 });
  const collection = createCollectionTable({
    host: list, label: "Paired devices", hideCaption: true, status, fallback: refresh,
    emptyText: "No paired devices.", keyOf: (device) => device.device_id, nameOf: who,
    columns: [
      { key: "name", label: "Name", value: who },
      { key: "paired", label: "Paired", value: (device) => pairedDate(device.paired_at) },
      { key: "seen", label: "Last seen", value: (device) => lastSeenWords(device.last_seen_at) },
    ],
    actions: (device) => [
      { id: "rename", label: "Rename", run: (button) => renameDevice(device, button) },
      { id: "revoke", label: sso ? "Forget" : "Revoke access", run: (button) => void revokeDevice(device, button) },
    ],
  });

  function who(device) {
    return device.current ? `${device.name}, this browser` : device.name;
  }

  function render({ announceFocus = true } = {}) {
    const signature = JSON.stringify([sso, devices.map((device) => [device, lastSeenWords(device.last_seen_at)])]);
    if (signature === renderedDevices) return { focusLost: false, target: null };
    renderedDevices = signature;
    collection.table.caption.textContent = sso ? "Your browsers" : "Paired devices";
    collection.table.querySelectorAll("thead th")[1].textContent = sso ? "Registered" : "Paired";
    const result = collection.update(devices, { announceFocus: false });
    if (!devices.length) collection.body.querySelector(".no-results").textContent = sso ? "No saved browsers." : "No paired devices.";
    if (announceFocus && result.focusLost) {
      say(`${sso ? "Your browsers" : "The paired devices list"} changed. Focus moved to ${focusName(result.target)}.`);
    }
    return result;
  }

  function focusName(target) {
    return target?.querySelector("button")?.getAttribute("aria-label")
      || target?.getAttribute("aria-label") || target?.textContent.trim() || "Refresh device list";
  }

  // announce: say the count (the Refresh button).  reportNew: say any
  // device that was not in the list before (the poll while the code is
  // shown).  quiet: a failed poll says nothing.
  async function load({ announce = false, reportNew = false, quiet = false } = {}) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/devices");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      sso = body.mode === "trusted_proxy";
      const enabled = body.pairing === true || sso;
      const logoutUrl = getAuthState().logout_url;
      note.hidden = enabled && !sso;
      note.textContent = sso
        ? `Signed in as ${body.user?.name || body.user?.id || "your SSO account"}. `
          + (logoutUrl ? "Sign-in is managed by your SSO provider."
            : "Sign-in and sign-out are managed externally by your SSO provider.")
        : nativeNote;
      controls.hidden = !enabled;
      signOut.hidden = sso && !logoutUrl;
      signOut.textContent = sso ? "Sign out through SSO" : nativeSignOutLabel;
      if (signOutDescription) {
        signOutDescription.hidden = signOut.hidden;
        signOutDescription.textContent = sso
          ? "Opens your SSO provider's sign-out page. Other open AutoDJ tabs may stay connected."
          : nativeSignOutDescription;
      }
      if (heading) heading.textContent = sso ? "Your browsers" : "Paired devices";
      if (pairingSection) pairingSection.hidden = sso;
      if (pairToggle) pairToggle.hidden = sso;
      if (!enabled || sso) hideCode();
      const known = new Set(devices.map((device) => device.device_id));
      devices = enabled && Array.isArray(body.devices) ? body.devices : [];
      const reconciliation = render({ announceFocus: !announce });
      if (announce) {
        const count = devices.length;
        const moved = reconciliation.focusLost
          ? ` Focus moved to ${focusName(reconciliation.target)}.`
          : "";
        say(sso ? `Browser list refreshed.  ${count} ${count === 1 ? "browser" : "browsers"}.${moved}`
          : `Device list refreshed.  ${count} paired ${count === 1 ? "device" : "devices"}.${moved}`);
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
      if (!quiet) say(failureText(`load ${sso ? "browsers" : "paired devices"}`, errorValue), "error");
      return false;
    }
  }

  async function signOutHere() {
    if (sso && !getAuthState().logout_url) return;
    const confirmed = await confirmAction(doc, {
      title: "Sign out this browser?",
      message: sso
        ? "Opens your SSO provider's sign-out page. Other open AutoDJ tabs may stay connected."
        : "Using AutoDJ here again needs a new pairing code from the server.",
      confirmLabel: "Sign out",
    });
    signOut.focus();
    if (!confirmed) return;
    const epoch = captureAuthenticatedRequestEpoch();
    let reply;
    try {
      reply = await requestJson("/api/logout", { method: "POST" });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(failureText(`sign out`, errorValue), "error");
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    hideCode();
    if (sso) onSignedOut("", reply.logout_url);
    else onSignedOut(SIGNED_OUT_REASON);
  }

  // The revoke runs while the confirmation is still open, so the list is
  // already updated when it closes and focus goes straight to the next
  // row's Actions cell, or Refresh when none is left (see confirmAction).  The
  // result is said after the dialog has closed.
  async function revokeDevice(device, button) {
    const index = devices.findIndex((item) => item.device_id === device.device_id);
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    let signedOut = false;
    const action = sso ? "Forget" : "Revoke";
    const confirmed = await deleteCollectionItem(doc, {
      title: device.current ? `${action} ${device.name}, this browser?` : `${action} ${device.name}?`,
      message: sso
        ? "Forgetting this browser removes its saved name. It does not revoke SSO access or sign it out."
        : device.current
        ? "This browser is signed out and needs a new pairing code to use AutoDJ again."
        : "That device needs a new pairing code to use AutoDJ again.",
      confirmLabel: action,
      onConfirm: async () => {
        let reply;
        try {
          reply = await requestJson(`/api/devices/${encodeURIComponent(device.device_id)}`, {
            method: "DELETE",
          });
        } catch (errorValue) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [failureText(`${action.toLowerCase()} ${device.name}`, errorValue), "error"];
          return tableFocusTarget(button);
        }
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        if (reply && reply.signed_out) {
          signedOut = true;
          return null;
        }
        await load();
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        result = [sso ? `Forgot ${device.name}. SSO access is unchanged.` : `Revoked ${device.name}.`];
        return collection.target(devices[Math.min(index, devices.length - 1)]?.device_id, "options") || refresh;
      },
    });
    if (!confirmed) {
      tableFocusTarget(button).focus();
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

  const renameForm = doc.getElementById("device-rename-form");
  const renameInput = doc.getElementById("device-rename-name");
  const renameError = doc.getElementById("device-rename-error");
  const renameTitle = doc.getElementById("device-rename-title");
  let renaming = null;
  const renameEditor = renameForm && createEditorDialog({
    dialog: doc.getElementById("device-rename-dialog"), form: renameForm, heading: renameTitle,
    cancel: doc.getElementById("device-rename-cancel"), fallback: refresh, pendingText: null,
    save: async (_event, current) => {
      const device = renaming;
      const name = renameInput.value.trim();
      clearFieldError(renameInput, renameError);
      if (!name) {
        showFieldError(renameInput, "Enter a name.", renameError, renameEditor.status);
        return;
      }
      try {
        const reply = await requestJson(`/api/devices/${encodeURIComponent(device.device_id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
        });
        if (!current()) return;
        await load();
        if (!current()) return;
        renameEditor.close({ restore: true });
        const target = collection.target(device.device_id) || refresh;
        if (doc.activeElement !== target) target.focus();
        say(`Renamed to ${reply.name}.`);
      } catch (errorValue) {
        if (!current()) return;
        showFieldError(renameInput, errorValue.status === 422
          ? NAME_RULE_TEXT : failureText(`rename ${device.name}`, errorValue), renameError, renameEditor.status);
      }
    },
  });
  renameInput?.addEventListener("input", () => clearFieldError(renameInput, renameError));

  function renameDevice(device, button) {
    if (!renameEditor || renameEditor.dialog.open) return;
    renaming = device;
    renameTitle.textContent = `Rename ${who(device)}`;
    renameInput.value = device.name;
    clearFieldError(renameInput, renameError);
    renameEditor.open(renameInput, button);
    renameInput.select();
  }

  // ---- Pairing code ------------------------------------------------

  const pairing = { timer: null, code: null, validUntil: 0, nextAt: 0, ticks: 0, busy: false };
  const hasPairingUi = Boolean(pairToggle && pairPanel && pairCode && pairCountdown
    && pairCopy && pairRefresh);

  function showCountdown(now) {
    const left = Math.ceil((pairing.validUntil - now) / 1000);
    pairCountdown.textContent = left > 0
      ? `Valid for ${fmtTime(left)}.`
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
      if (speak) say(failureText(`get a pairing code`, errorValue), "error");
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
      say(`Pairing code ${spokenCode(pairing.code)}, valid for ${fmtTime(body.valid_seconds)}.`);
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
    if (sso) return;
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

  async function copyCode() {
    const code = pairing.code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
    } catch (_) {
      if (!copyByCommand(code, doc)) {
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
  onCardOpen(card, load, { onClose: hideCode });
  if (card.open) void load();
  return { clear() {
    devices = [];
    renderedDevices = null;
    renaming = null;
    hideCode();
    renameEditor?.close();
    collection.clear();
  } };
}
