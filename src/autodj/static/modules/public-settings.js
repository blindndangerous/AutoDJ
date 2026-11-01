// Station configuration for DMCA mode and the public player pages.
import { failureText, isStaleStationReply, requestJson } from "./api-client.js";
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { makeSay } from "./live-region.js";
import { registerSettingsGroup } from "./settings-drafts.js";

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

  const say = makeSay(status, { tone: "success", dwellMs: 4000, errorDwellMs: 6000 });
  const clearError = () => clearFieldError(origins, errorNode);
  const showError = (message) => showFieldError(origins, message, errorNode, status);

  function fillDmca(settings) {
    if ("dmca_enabled" in settings) dmca.checked = settings.dmca_enabled === true;
  }

  function fillPages(settings) {
    if ("player_page_enabled" in settings) page.checked = settings.player_page_enabled === true;
    if ("embed_enabled" in settings) embed.checked = settings.embed_enabled === true;
    if ("embed_origins" in settings) origins.value = (settings.embed_origins || []).join("\n");
  }

  function fill(settings) {
    fillDmca(settings);
    fillPages(settings);
  }

  // Each control's tab owns its Save bar.
  const dmcaDraft = registerSettingsGroup({
    panel: dmca.closest('[role="tabpanel"]') || doc.getElementById("spanel-picking"), group: "public",
    controls: [dmca],
    read: () => ({ dmca_enabled: dmca.checked }),
    write: fillDmca,
  });

  const draft = registerSettingsGroup({
    panel: doc.getElementById("spanel-stream"), group: "public",
    controls: [page, embed, origins],
    fields: { player_page_enabled: page, embed_enabled: embed, embed_origins: origins },
    read: () => ({ player_page_enabled: page.checked,
      embed_enabled: embed.checked,
      embed_origins: origins.value.split(/\r?\n/).map((site) => site.trim()).filter(Boolean) }),
    write: fillPages,
    validate() {
      clearError();
      const sites = origins.value.split(/\r?\n/).map((site) => site.trim()).filter(Boolean);
      const invalid = sites.length > 20 || sites.some((site) => {
        if (site === "*") return false;
        try {
          const url = new URL(site);
          return !["http:", "https:"].includes(url.protocol) || !url.hostname
            || url.username || url.password || !["", "/"].includes(url.pathname)
            || url.search || url.hash;
        } catch { return true; }
      });
      if (!invalid) return true;
      showError("Sites allowed to embed must be HTTP(S) site addresses, or * for any site.");
      return false;
    },
  });

  async function refresh() {
    const mine = ++loadSequence;
    const station = generation;
    try {
      const settings = await request("/api/public-settings");
      if (mine === loadSequence && station === generation) {
        dmcaDraft.accept(settings);
        draft.accept(settings);
      }
    } catch (error) {
      if (mine === loadSequence && station === generation && !isStaleStationReply(error)) {
        say(failureText("load public settings", error, "no answer"), "error");
      }
    }
  }

  function clear() {
    generation += 1;
    loadSequence += 1;
    fill({ dmca_enabled: false, player_page_enabled: false, embed_enabled: false, embed_origins: [] });
    clearError();
  }

  dmca.addEventListener("change", clearError);
  pageForm.addEventListener("input", clearError);
  pageForm.addEventListener("submit", (event) => event.preventDefault());
  return { refresh, clear };
}
