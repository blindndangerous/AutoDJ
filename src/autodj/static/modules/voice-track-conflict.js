import { chooseAction, confirmAction } from "./confirm-dialog.js";

export function voiceTrackConflict(errorValue) {
  const detail = errorValue?.detail;
  return errorValue?.status === 409
    && detail?.code === "voice_track_conflict"
    && typeof detail.revision === "string";
}

export async function chooseVoiceTrackAction(doc, errorValue) {
  if (!voiceTrackConflict(errorValue)) return null;
  const detail = errorValue.detail;
  const pairs = (detail.affected || []).map((item) => {
    const next = item.proposed_pair?.join(" into ");
    return `${item.label || "Voice track"}: ${item.old_pair?.join(" into ") || "previous song change"}. ${next ? `Proposed: ${next}.` : "No replacement pair."}`;
  });
  const message = [detail.message, ...pairs, detail.can_keep === false ? detail.keep_reason : "Keep the voice track with this change?"]
    .filter((part) => typeof part === "string" && part.trim())
    .join(" ");
  if (detail.can_keep === false) {
    const discard = await confirmAction(doc, {
      title: "Upcoming voice track", message, confirmLabel: "Discard voice track",
    });
    return discard ? { voice_action: "discard", voice_revision: detail.revision } : null;
  }
  const choice = await chooseAction(doc, {
    title: "Upcoming voice track",
    message,
    choices: ["Keep voice track", "Discard voice track", "Cancel change"],
  });
  if (choice === "accept") return { voice_action: "keep", voice_revision: detail.revision };
  if (choice === "alternate") return { voice_action: "discard", voice_revision: detail.revision };
  return null;
}

// Each conflict opens a new prompt and replaces the revision on the next
// retry; a previous choice never authorizes a later server revision.
export async function sendWithVoiceTrackConflict(doc, send, initialError = null) {
  let voice = {};
  let conflict = initialError;
  for (;;) {
    if (conflict) {
      voice = await chooseVoiceTrackAction(doc, conflict);
      if (!voice) return null;
      conflict = null;
    }
    try {
      return { value: await send(voice) };
    } catch (errorValue) {
      if (!voiceTrackConflict(errorValue)) throw errorValue;
      conflict = errorValue;
    }
  }
}
