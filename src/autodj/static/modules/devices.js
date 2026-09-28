// Settings > Browser access: "Sign out this browser" and the paired
// devices list with a Revoke button per device.
//
// Loads when the card is opened.  A server that does not ask browsers to
// pair says so and shows no buttons.  Signing out, or revoking this
// browser's own entry, hands over to onSignedOut(reason), which opens the
// pairing dialog; focus then belongs to that dialog.

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { replaceRows } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

export const SIGNED_OUT_REASON =
  "This browser was signed out.  Enter a new pairing code to use it again.";
export const REVOKED_REASON =
  "This browser's pairing was revoked.  Enter a new pairing code to use it again.";

function formatSeen(seconds, locale = undefined) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "unknown";
  return new Date(seconds * 1000).toLocaleString(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function installAccess(els, { onSignedOut }) {
  const { card, note, controls, signOut, refresh, list, status } = els;
  if (!card || !note || !controls || !signOut || !refresh || !list || !status) return;
  const doc = card.ownerDocument;
  let devices = [];

  const say = (message, tone = "info") => {
    announceStatus(status, message, { dwellMs: tone === "error" ? 6000 : 3000, force: true, tone });
  };

  // `focus` comes from a revoke: see replaceRows.
  function render(focus = null) {
    const rows = [];
    for (const device of devices) {
      const row = doc.createElement("li");
      const text = doc.createElement("span");
      text.className = "device-text";
      const who = device.current ? `${device.name}, this browser` : device.name;
      text.textContent = `${who}.  Last used ${formatSeen(device.last_seen_at)}.  `
        + `Paired ${formatSeen(device.paired_at)}.`;
      const revoke = doc.createElement("button");
      revoke.type = "button";
      revoke.textContent = "Revoke";
      revoke.dataset.deviceId = device.device_id;
      revoke.setAttribute("aria-label", `Revoke ${who}`);
      revoke.addEventListener("click", () => void revokeDevice(device, revoke));
      row.append(text, " ", revoke);
      rows.push(row);
    }
    replaceRows(list, rows, focus);
  }

  async function load({ announce = false, focus = null } = {}) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/devices");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      const pairing = body.pairing === true;
      note.hidden = pairing;
      controls.hidden = !pairing;
      devices = pairing && Array.isArray(body.devices) ? body.devices : [];
      render(focus);
      if (announce) {
        const count = devices.length;
        say(`Device list refreshed.  ${count} paired ${count === 1 ? "device" : "devices"}.`);
      }
      return true;
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      say(`Could not load paired devices: ${errorValue.message}`, "error");
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
    onSignedOut(SIGNED_OUT_REASON);
  }

  async function revokeDevice(device, button) {
    const confirmed = await confirmAction(doc, {
      title: device.current ? `Revoke ${device.name}, this browser?` : `Revoke ${device.name}?`,
      message: device.current
        ? "This browser is signed out and needs a new pairing code to use AutoDJ again."
        : "That device needs a new pairing code to use AutoDJ again.",
      confirmLabel: "Revoke",
    });
    button.focus();
    if (!confirmed) return;
    const index = devices.indexOf(device);
    const epoch = captureAuthenticatedRequestEpoch();
    let result;
    try {
      result = await requestJson(`/api/devices/${encodeURIComponent(device.device_id)}`, {
        method: "DELETE",
      });
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not revoke ${device.name}: ${errorValue.message}`, "error");
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (result && result.signed_out) {
      onSignedOut(REVOKED_REASON);
      return;
    }
    await load({ focus: { from: button, selector: "button[data-device-id]", index, fallback: refresh } });
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    say(`Revoked ${device.name}.`);
  }

  signOut.addEventListener("click", () => void signOutHere());
  refresh.addEventListener("click", () => void load({ announce: true }));
  card.addEventListener("toggle", () => {
    if (card.open) void load();
  });
  if (card.open) void load();
}
