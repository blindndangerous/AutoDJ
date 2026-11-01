import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, withDisabled } from "./api-client.js";

// Explicit saves outside the Settings draft bar keep their endpoint and
// validation, but share submission, pending-state and late-reply ownership.
export function createSettingsSave({ button, save, isCurrent = () => true, say, pendingText }) {
  let generation = 0;
  let active = null;

  function clear() {
    generation += 1;
    active?.abort();
    active = null;
  }

  function run(event) {
    event?.preventDefault();
    if (active || !isCurrent()) return;
    const mine = ++generation;
    const epoch = captureAuthenticatedRequestEpoch();
    const controller = new AbortController();
    active = controller;
    const current = () => generation === mine && isCurrent() && isAuthenticatedRequestCurrent(epoch);
    const release = () => {
      controller.abort();
      if (active === controller) active = null;
    };
    // keepEnabled: Enter in a field, or a save confirmed elsewhere, leaves
    // the button unfocused, and a disabled Save is reported unavailable
    // (#331).  *active* already refuses a second save.
    return withDisabled(button, () => {
      try {
        const result = save(current);
        if (result?.then) return Promise.resolve(result).finally(release);
        release();
        return result;
      } catch (error) {
        release();
        throw error;
      }
    }, { signal: controller.signal, keepEnabled: true, pendingText, say: (message) => { if (current()) say?.(message); } });
  }

  return { run, clear, get pending() { return active !== null; } };
}
