// DMCA limit and upcoming voice-track conflicts are resolved in the shared
// dialog before the same search action is retried. Nothing is dropped
// silently: the caller says out loud whether the song was queued.
//
// send(confirm) makes the request; for Play now it goes through playNow, so
// a page that has not started playing starts in the same click, the first
// time and again when "Play anyway" is pressed.  The request runs while the
// dialog is still open, and focus then returns to the opener (see
// confirm-dialog.js).

import { confirmAction } from "./confirm-dialog.js";
import { chooseVoiceTrackAction, voiceTrackConflict } from "./voice-track-conflict.js";

// The server's 409 for a DMCA limit names the "confirm" field.
function isDmcaWarning(errorValue) {
  return errorValue?.status === 409 && errorValue.field === "confirm";
}

// Resolves to { value, confirmed } once sent (value is what send resolved
// to), or null when the operator cancels either choice. Other failures are
// thrown for the caller to report.
export async function sendWithDmcaConfirm(doc, send, { now = false, opener = null } = {}) {
  let confirmed = false;
  let voice = {};
  for (;;) {
    try {
      return { value: await send(confirmed, voice), confirmed };
    } catch (errorValue) {
      if (voiceTrackConflict(errorValue)) {
        voice = await chooseVoiceTrackAction(doc, errorValue);
        if (!voice) return null;
        continue;
      }
      if (!isDmcaWarning(errorValue) || confirmed) throw errorValue;

      let value;
      let failure = null;
      let voiceConflict = null;
      const accepted = await confirmAction(doc, {
        title: "Over the DMCA limit",
        message: `${errorValue.message} ${now ? "Play it now anyway?" : "Queue it anyway?"}`,
        confirmLabel: now ? "Play anyway" : "Queue anyway",
        onConfirm: async () => {
          confirmed = true;
          try {
            value = await send(true, voice);
          } catch (retryError) {
            if (voiceTrackConflict(retryError)) voiceConflict = retryError;
            else failure = retryError;
          }
          return opener;
        },
      });
      if (!accepted) return null;
      if (failure) throw failure;
      if (voiceConflict) {
        voice = await chooseVoiceTrackAction(doc, voiceConflict);
        if (!voice) return null;
        continue;
      }
      return { value, confirmed: true };
    }
  }
}
