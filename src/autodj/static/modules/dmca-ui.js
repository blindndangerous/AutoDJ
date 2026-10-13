// DMCA mode: a song the server says would break a performance-complement
// limit is queued only after the operator chooses "Queue anyway" (or "Play
// anyway" for Play now) in the shared confirm dialog, which shows the
// server's sentence naming the limit.  Nothing is dropped silently: the
// caller says out loud whether the song was queued.
//
// send(confirm) makes the request; for Play now it goes through playNow, so
// a page that has not started playing starts in the same click, the first
// time and again when "Play anyway" is pressed.  The request runs while the
// dialog is still open, and focus then returns to the opener (see
// confirm-dialog.js).

import { confirmAction } from "./confirm-dialog.js";

// The server's 409 for a DMCA limit names the "confirm" field.
function isDmcaWarning(errorValue) {
  return errorValue?.status === 409 && errorValue.field === "confirm";
}

// Resolves to { value, confirmed } once the request is sent (value is what
// send resolved to), or null when the operator chose Cancel.  A failed
// request, first or confirmed, is thrown for the caller to report.
export async function sendWithDmcaConfirm(doc, send, { now = false, opener = null } = {}) {
  let warning;
  try {
    return { value: await send(false), confirmed: false };
  } catch (errorValue) {
    if (!isDmcaWarning(errorValue)) throw errorValue;
    warning = errorValue.message;
  }
  let value;
  let failure = null;
  const accepted = await confirmAction(doc, {
    station: true,
    title: "Over the DMCA limit",
    message: `${warning} ${now ? "Play it now anyway?" : "Queue it anyway?"}`,
    confirmLabel: now ? "Play anyway" : "Queue anyway",
    onConfirm: async () => {
      try {
        value = await send(true);
      } catch (errorValue) {
        failure = errorValue;
      }
      return opener;
    },
  });
  if (failure) throw failure;
  return accepted ? { value, confirmed: true } : null;
}
