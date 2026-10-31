import { afterEach, expect, it, vi } from "vitest";
import { chooseVoiceTrackAction } from "../../src/autodj/static/modules/voice-track-conflict.js";
import { installIndexMarkup } from "./helpers.js";

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

function setup(canKeep) {
  installIndexMarkup();
  const opener = document.createElement("button");
  opener.textContent = "Stop show";
  document.body.append(opener);
  opener.focus();
  const dialog = document.getElementById("confirm-dialog");
  dialog.show = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event("close")); };
  const result = chooseVoiceTrackAction(document, { status: 409, detail: {
    code: "voice_track_conflict", revision: "rev-1", can_keep: canKeep,
    message: "This change affects a voice track.", keep_reason: "No eligible replacement pair.",
    affected: [{ label: "Take one", old_pair: ["A", "B"], proposed_pair: canKeep ? ["A", "X"] : null }],
  } });
  return { opener, dialog, result };
}

it("names A-to-X before Keep and returns focus directly after keyboard cancellation", async () => {
  const { opener, dialog, result } = setup(true);
  expect(document.getElementById("confirm-message").textContent).toContain("A into X");
  expect(document.activeElement).toBe(document.getElementById("confirm-cancel"));
  dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
  expect(await result).toBeNull();
  expect(document.activeElement).toBe(opener);
});

it("offers Discard and Cancel when Keep has no eligible pair", async () => {
  const { opener, result } = setup(false);
  const accept = document.getElementById("confirm-accept");
  expect(accept.textContent).toBe("Discard voice track");
  expect(document.getElementById("confirm-message").textContent).toContain("No eligible replacement pair.");
  expect(document.activeElement).toBe(document.getElementById("confirm-cancel"));
  accept.focus();
  accept.click();
  expect(await result).toEqual({ voice_action: "discard", voice_revision: "rev-1" });
  expect(document.activeElement).toBe(opener);
});
