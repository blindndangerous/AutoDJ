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
import { listRow, nextRowControl, rowButton } from "./dom-helpers.js";
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

  function render() {
    list.replaceChildren(...devices.map((device) => {
      const who = device.current ? `${device.name}, this browser` : device.name;
      const revoke = rowButton(doc, "Revoke", `Revoke ${who}`,
        (button) => void revokeDevice(device, button));
      revoke.dataset.deviceId = device.device_id;
      return listRow(doc, `${who}.  Last used ${formatSeen(device.last_seen_at)}.  `
        + `Paired ${formatSeen(device.paired_at)}.`, "device-text", revoke);
    }));
  }

  async function load({ announce = false } = {}) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/devices");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      const pairing = body.pairing === true;
      note.hidden = pairing;
      controls.hidden = !pairing;
      devices = pairing && Array.isArray(body.devices) ? body.devices : [];
      render();
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
        return nextRowControl(list, "button[data-device-id]", index, refresh);
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (signedOut) {
      onSignedOut(REVOKED_REASON);
      return;
    }
    if (result) say(...result);
  }

  signOut.addEventListener("click", () => void signOutHere());
  refresh.addEventListener("click", () => void load({ announce: true }));
  card.addEventListener("toggle", () => {
    if (card.open) void load();
  });
  if (card.open) void load();
}
