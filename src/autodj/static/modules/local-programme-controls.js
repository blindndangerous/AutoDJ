import { requestJson } from "./api-client.js";
import { localProgrammeUrl } from "./local-programme.js";

export function installLocalProgrammeControls({ wrap, take, returnButton, status, announce, owner }) {
  let state = null;
  let visible = false;
  let observed = null;
  let returnPending = false;
  let lastReason = null;
  const labels = { automation: "Automation", preparing: "Preparing local control",
    local: "Local control", returning: "Returning to automation", recovering: "Recovering automation" };
  const takeUnavailable = {
    preparing: "Local control is being prepared. Wait or select Return to automation to cancel.",
    local: "Return to automation before taking local control.",
    returning: "Wait for the return to automation to finish.",
    recovering: "Wait for automation to recover before taking local control.",
  };
  function render(next, reason) {
    if (next === state && !reason) return;
    reason ||= labels[next];
    const changed = state !== null && next !== state;
    const newReason = reason !== lastReason;
    state = next;
    lastReason = reason;
    status.textContent = reason;
    take.setAttribute("aria-disabled", String(next !== "automation"));
    returnButton.setAttribute("aria-disabled", String(next === "automation"));
    const intermediate = next === "preparing" || next === "returning" || next === "recovering";
    if (changed && !intermediate) announce(reason);
    else if (next === "recovering" && newReason && reason !== labels[next]
      && !(returnPending && ["Returned to automation", "Local control ended; automation is recovering"].includes(reason))) announce(reason);
  }
  owner.change = (next, reason) => render(next, reason);
  take.addEventListener("click", async () => {
    if (!visible) return;
    if (state !== "automation") { announce(takeUnavailable[state]); return; }
    try { await owner.take(); }
    catch (error) { owner.stop(undefined, { notify: false }); render("automation", error.message); }
  });
  returnButton.addEventListener("click", async () => {
    if (!visible) return;
    if (state === "automation") { announce("Automation already controls this station."); return; }
    if (returnPending) return;
    returnPending = true;
    try {
      if (owner.owned || owner.taking || owner.socket) {
        await owner.returnToAutomation();
        render("automation");
        return;
      }
      render("recovering");
      const result = await requestJson(localProgrammeUrl("/return"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ epoch: observed.epoch, generation: observed.generation }),
      });
      render(result.state);
    } catch (error) {
      status.textContent = error.message;
      announce(error.message);
      try {
        observed = await requestJson(localProgrammeUrl());
        render(observed.state);
      } catch (_) { /* Keep the Return failure visible until status is available. */ }
    }
    finally { returnPending = false; }
  });
  return {
    apply(snapshot) {
      visible = Boolean(snapshot.stream_mode && snapshot.local_programme);
      wrap.hidden = !visible;
      if (!visible) return;
      observed = snapshot.local_programme;
      const next = snapshot.local_programme.state;
      if (owner.owned && next === "automation") return;
      if ((state === "recovering" || state === "returning") && next === "local") return;
      render(next);
    },
  };
}
