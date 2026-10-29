// Settings > Station > Profiles: create and edit saved overrides, activate profiles,
// and reset or delete them.
//
// The list loads when the Profiles subtab is shown. Every result is said
// once through #profiles-status.  Focus stays on the control that was
// pressed, except after Delete, where that row is gone and focus moves to
// the neighbouring row (or the name field when the list is empty).

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { listRow, nextRowControl, reconcileRows, rowButton } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { confirmSettingsNavigation, loadSettingsDrafts, registerSettingsGroup, setSettingsProfile } from "./settings-drafts.js";

// profiles.validate_name on the server.
const NAME_PATTERN = /^[A-Za-z0-9 _-]{1,64}$/;

export function installProfiles(els) {
  const { card, nameInput, saveButton, list, status } = els;
  if (!card || !nameInput || !saveButton || !list || !status) return;
  const doc = card.ownerDocument;
  const listNavigation = installActionListNavigation(list);
  let names = [];
  let active = null;
  let clockwheels = [];
  let cartPages = [];
  let renderedNames = null;

  const say = (message, tone = "info") => {
    announceStatus(status, message, { dwellMs: tone === "error" ? 6000 : 3000, force: true, tone });
  };

  function choice(label, value) {
    const option = doc.createElement("option");
    option.textContent = label;
    option.value = value;
    return option;
  }

  function render() {
    const signature = JSON.stringify([names, active]);
    if (signature === renderedNames) { listNavigation.refresh(); return; }
    renderedNames = signature;
    if (names.length === 0) {
      const empty = doc.createElement("li");
      empty.className = "no-results";
      empty.textContent = "No saved profiles yet.";
      reconcileRows(list, [], (name) => name, () => null, () => {}, {
        fallback: nameInput,
        onFocusLost: ({ target }) => say(`The saved profiles list changed. Focus moved to ${target?.getAttribute("aria-label") || target?.textContent.trim() || "profile name"}.`),
      });
      list.append(empty);
      listNavigation.refresh();
      return;
    }
    reconcileRows(list, names, (name) => name, (name) => {
      const activeLabel = active === name ? " (active)" : "";
      const apply = rowButton(doc, "Activate", `Activate profile ${name}`, () => void applyProfile(name));
      const remove = rowButton(doc, "Delete", `Delete profile ${name}`,
        (button) => void deleteProfile(name, button));
      const reset = rowButton(doc, "Reset", `Reset profile ${name} to defaults`,
        () => void resetProfile(name));
      remove.dataset.profile = name;
      return listRow(doc, `${name}${activeLabel}`, "profile-name", apply, reset, remove);
    }, (row, name) => {
      const label = row.querySelector(".profile-name");
      if (label) label.textContent = `${name}${active === name ? " (active)" : ""}`;
      const apply = row.querySelector("button");
      if (apply) {
        apply.textContent = "Activate";
        apply.setAttribute("aria-label", `Activate profile ${name}`);
      }
    }, { fallback: nameInput, onFocusLost: ({ target }) => say(`The saved profiles list changed. Focus moved to ${target?.getAttribute("aria-label") || target?.textContent.trim() || "profile name"}.`) });
    listNavigation.refresh();
  }

  function updateActive(name) {
    active = name || null;
    const select = els.profileSelect;
    if (select) {
      // Another browser may create and activate a profile before this list
      // refreshes. Add its name without changing the editing selection.
      if (active && ![...select.options].some((option) => option.value === active)) {
        select.add(choice(active, active));
      }
      for (const option of select.options) {
        const label = `${option.value || "Station defaults"}${(option.value || null) === active ? " (active)" : ""}`;
        if (option.textContent !== label) option.textContent = label;
      }
    }
    render();
  }

  async function load() {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/profiles");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      names = Array.isArray(body.profiles) ? body.profiles : [];
      active = body.active ?? null;
      if (els.applyDefaultsButton) {
        els.applyDefaultsButton.textContent = "Activate station defaults";
      }
      clockwheels = body.clockwheels || [];
      cartPages = body.cart_pages || [];
      updateChoices(els.clockSelect, clockwheels, "No clockwheel");
      updateChoices(els.cartSelect, cartPages, null);
      if (els.profileSelect) {
        const selected = els.profileSelect.value;
        const wanted = ["", ...names];
        if (JSON.stringify([...els.profileSelect.options].map((option) => option.value)) !== JSON.stringify(wanted)) {
          els.profileSelect.replaceChildren(...wanted.map((profile) => choice(profile || "Station defaults", profile)));
        }
        els.profileSelect.value = names.includes(selected) ? selected : "";
      }
      updateActive(active);
      return true;
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      say(`Could not load profiles: ${errorValue.message}`, "error");
      return false;
    }
  }

  function updateChoices(select, choices, placeholder) {
    if (!select) return;
    const selected = select.value;
    select.replaceChildren(...(placeholder === null ? [] : [choice(placeholder, "")]));
    for (const value of choices) select.add(choice(value, value));
    select.value = choices.includes(selected) ? selected : placeholder === null ? (choices[0] || "") : "";
  }

  async function save() {
    const name = nameInput.value.trim();
    if (!NAME_PATTERN.test(name)) {
      nameInput.setAttribute("aria-invalid", "true");
      say("Could not save: a profile name needs 1 to 64 letters, digits, spaces, dashes or underscores.", "error");
      return;
    }
    nameInput.removeAttribute("aria-invalid");
    const exists = names.some((other) => other.toLowerCase() === name.toLowerCase());
    if (exists) {
      nameInput.setAttribute("aria-invalid", "true");
      say(`Profile ${name} already exists. Choose it in Profile to edit to change its overrides.`, "error");
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await load();
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Created profile ${name}.`);
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not save profile ${name}: ${errorValue.message}`, "error");
    }
  }

  async function resetProfile(name) {
    const confirmed = await confirmAction(doc, {
      title: `Reset profile ${name}?`,
      message: "This removes the profile's saved overrides. The settings on air stay as they are until you activate the profile.",
      confirmLabel: "Reset profile",
    });
    if (!confirmed) return;
    const editing = els.profileSelect?.value === name;
    if (editing && !await confirmSettingsNavigation(doc)) return;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson(`/api/profiles/${encodeURIComponent(name)}/reset`, { method: "POST" });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      if (editing) await setSettingsProfile(name, doc);
      await load();
      say(`Reset profile ${name} to station defaults.`);
    } catch (errorValue) {
      if (isAuthenticatedRequestCurrent(epoch)) say(`Could not reset profile ${name}: ${errorValue.message}`, "error");
    }
  }

  async function applyProfile(name) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson(`/api/profiles/${encodeURIComponent(name)}/apply`, { method: "POST" });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await load();
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Activated profile ${name}.`);
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not activate profile ${name}: ${errorValue.message}`, "error");
    }
  }

  async function applyDefaults() {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson("/api/profiles/apply-defaults", { method: "POST" });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await load();
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say("Activated station defaults.");
    } catch (errorValue) {
      if (isAuthenticatedRequestCurrent(epoch)) say(`Could not activate station defaults: ${errorValue.message}`, "error");
    }
  }

  // The delete runs while the confirmation is still open, so the list is
  // already updated when it closes and focus goes straight to the next
  // row's Delete, or the name field when none is left (see confirmAction).
  // The result is said after the dialog has closed: the status region is
  // behind it until then.
  async function deleteProfile(name, button) {
    if (els.profileSelect?.value === name) {
      say(`Choose another editing target before deleting profile ${name}.`, "error");
      return;
    }
    const index = names.indexOf(name);
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    const confirmed = await confirmAction(doc, {
      title: `Delete profile ${name}?`,
      message: "The saved settings are removed.  The settings in use now do not change.",
      confirmLabel: "Delete profile",
      onConfirm: async () => {
        try {
          await requestJson(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          await load();
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [`Deleted profile ${name}.`];
          return nextRowControl(list, "button[data-profile]", index, nameInput);
        } catch (errorValue) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [`Could not delete profile ${name}: ${errorValue.message}`, "error"];
          return button;
        }
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (result && isAuthenticatedRequestCurrent(epoch)) say(...result);
  }

  saveButton.addEventListener("click", () => void save());
  els.applyDefaultsButton?.addEventListener("click", () => void applyDefaults());
  nameInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void save();
  });
  card.addEventListener("toggle", () => {
    if (card.open) void load().then(() => loadSettingsDrafts(doc));
  });
  if (card.open) void load().then(() => loadSettingsDrafts(doc));
  const settingsPanel = doc.getElementById("panel-settings");
  const Observer = doc.defaultView?.MutationObserver;
  if (settingsPanel && Observer) {
    new Observer(() => {
      if (!settingsPanel.hidden) void load();
    }).observe(settingsPanel, { attributes: true, attributeFilter: ["hidden"] });
    if (!settingsPanel.hidden) void load();
  }
  function showChoice(select, value) {
    if (value && ![...select.options].some((option) => option.value === value)) {
      select.add(choice(value, value));
    }
    select.value = value || "";
  }
  const clock = els.clockSelect;
  const cart = els.cartSelect;
  if (clock) registerSettingsGroup({
    panel: clock.closest('[role="tabpanel"]') || card, group: "clock", controls: [clock], loadSettings: false, read: () => ({ name: clock.value || null }),
    write: (value) => { if ("name" in value) showChoice(clock, value.name); },
  });
  if (cart) registerSettingsGroup({
    panel: cart.closest('[role="tabpanel"]') || card, group: "cart", controls: [cart], loadSettings: false, read: () => ({ page: cart.value || null }),
    write: (value) => { if ("page" in value) showChoice(cart, value.page); },
  });
  // After a station switch (#70): the open list shows the new station's.
  return { refresh: load, updateActive };
}
