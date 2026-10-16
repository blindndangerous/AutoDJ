// Station configuration for DMCA mode and the public player pages.
import { isStaleStationReply, requestJson, staleActionText } from "./api-client.js";
import { focusRevealed } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

export function installPublicSettings({ doc = document, request = requestJson } = {}) {
  const get = (id) => doc.getElementById(`public-${id}`);
  const dmcaForm = get("dmca-form");
  const pageForm = get("page-form");
  const dmca = get("dmca");
  const page = get("page");
  const embed = get("embed");
  const origins = get("origins");
  const errorNode = get("origins-error");
  const status = doc.getElementById("settings-status");
  if (!dmcaForm || !pageForm || !dmca || !page || !embed || !origins || !status) {
    return { refresh: async () => {}, clear: () => {} };
  }
  let generation = 0;
  let loadSequence = 0;
  let dirtyDmca = false;
  let dirtyPage = false;
  let savingDmca = false;
  let savingPage = false;
  let savedDmca = false;

  function say(message, tone = "success") {
    announceStatus(status, message, { tone, force: true, dwellMs: tone === "error" ? 6000 : 4000 });
  }

  function clearError() {
    origins.removeAttribute("aria-invalid");
    origins.setAttribute("aria-describedby", "public-origins-help");
    if (errorNode) {
      errorNode.textContent = "";
      errorNode.hidden = true;
    }
  }

  function showError(message) {
    origins.setAttribute("aria-invalid", "true");
    origins.setAttribute("aria-describedby", "public-origins-help public-origins-error");
    if (errorNode) {
      errorNode.textContent = message;
      errorNode.hidden = false;
    }
    focusRevealed(origins);
    say(message, "error");
  }

  function fill(settings, { forcePage = false } = {}) {
    if (!savingDmca) savedDmca = settings.dmca_enabled === true;
    if (!dirtyDmca && !savingDmca && !dmcaForm.contains(doc.activeElement)) {
      dmca.checked = savedDmca;
    }
    if (forcePage || (!dirtyPage && !savingPage && !pageForm.contains(doc.activeElement))) {
      page.checked = settings.player_page_enabled === true;
      embed.checked = settings.embed_enabled === true;
      origins.value = (settings.embed_origins || []).join("\n");
    }
  }

  async function refresh() {
    const mine = ++loadSequence;
    const station = generation;
    try {
      const settings = await request("/api/public-settings");
      if (mine === loadSequence && station === generation) fill(settings);
    } catch (error) {
      if (mine === loadSequence && station === generation && !isStaleStationReply(error)) {
        say(`Could not load public settings: ${error?.message || "no answer"}`, "error");
      }
    }
  }

  function clear() {
    generation += 1;
    loadSequence += 1;
    dirtyDmca = false;
    dirtyPage = false;
    savedDmca = false;
    dmca.checked = false;
    page.checked = false;
    embed.checked = false;
    origins.value = "";
    clearError();
  }

  async function save(button, body, success, onFailure, done) {
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-pending");
    const mine = generation;
    try {
      const settings = await request("/api/public-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (mine !== generation) return;
      done?.(settings);
      fill(settings, { forcePage: "embed_origins" in body && !dirtyPage });
      say(success);
    } catch (error) {
      if (mine !== generation) return;
      const left = staleActionText(error);
      if (left) say(left, error?.actionDone ? "info" : "error");
      else onFailure(error);
    } finally {
      button.removeAttribute("aria-busy");
      button.classList.remove("is-pending");
    }
  }

  function saveDmca() {
    if (savingDmca) return;
    savingDmca = true;
    dirtyDmca = false;
    const enabled = dmca.checked;
    void save(dmca, { dmca_enabled: enabled },
      `DMCA mode ${enabled ? "on" : "off"}.`,
      (error) => {
        if (!dirtyDmca) dmca.checked = savedDmca;
        say(`Could not save DMCA mode: ${error?.message || "no answer"}`, "error");
      },
      (settings) => { savedDmca = settings.dmca_enabled === true; })
      .finally(() => {
        savingDmca = false;
        if (dirtyDmca) saveDmca();
      });
  }
  dmca.addEventListener("change", () => { dirtyDmca = true; saveDmca(); });
  pageForm.addEventListener("input", () => { dirtyPage = true; clearError(); });
  pageForm.addEventListener("change", () => { dirtyPage = true; });
  pageForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (savingPage) return;
    clearError();
    savingPage = true;
    dirtyPage = false;
    const body = {
      player_page_enabled: page.checked,
      embed_enabled: embed.checked,
      embed_origins: origins.value.split(/\r?\n/).map((site) => site.trim()).filter(Boolean),
    };
    void save(get("page-save"), body, "Public player pages saved.",
      (error) => {
        dirtyPage = true;
        const message = error?.message || "Could not save player pages: no answer";
        if (error?.field === "embed_origins") showError(message);
        else say(`Could not save player pages: ${message}`, "error");
      }).finally(() => { savingPage = false; });
  });
  return { refresh, clear };
}
