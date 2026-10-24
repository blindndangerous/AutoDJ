import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { confirmAction } from "./confirm-dialog.js";
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { clearFieldError, clearFormErrors, showFieldError } from "./dom-helpers.js";
import { sayIfStillPending } from "./live-region.js";
import { stationEpoch } from "./stations.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const root = (slug) => slug === "default" ? "/api" : `/api/stations/${encodeURIComponent(slug)}`;

export function installRelaySettings({ doc, request, post, remove, announce, slug = "default" }) {
  const id = (name) => doc.getElementById(`stream-${name}`);
  const rows = id("relay-rows");
  const form = id("relay-form");
  const formError = id("relay-form-error");
  const metadata = [id("station-name"), id("station-genre"), id("station-description"), id("station-url")];
  const metadataKeys = ["station_name", "genre", "description", "url"];
  metadata.forEach((control, index) => {
    const key = metadataKeys[index];
    registerSettingsGroup({
      panel: doc.getElementById("spanel-stream"),
      group: "stream",
      controls: [control],
      read: () => ({ [key]: control.value }),
      write: (body) => { if (body[key] != null) control.value = body[key]; },
    });
  });
  const table = installTableNavigation(rows);
  let relays = [];
  let statuses = new Map();
  let editing = null;
  let returnFocus = null;
  let available = false;
  let generation = 0;
  let saving = false;
  let formSaveId = 0;
  const rowByName = new Map();
  function validationError(error) {
    const named = ["name", "type", "host", "port", "password", "mount", "user", "stream_id", "bitrate"].includes(error.field)
      ? error.field.replaceAll("_", "-") : null;
    const key = named || (error.message.toLowerCase().includes("password") ? "password"
      : error.message.toLowerCase().includes("host") ? "host"
      : error.message.toLowerCase().includes("port") ? "port"
      : error.message.toLowerCase().includes("mount") ? "mount"
      : error.message.toLowerCase().includes("user") ? "user"
      : error.message.toLowerCase().includes("stream_id") ? "stream-id"
      : error.message.toLowerCase().includes("bitrate") ? "bitrate"
      : error.message.toLowerCase().includes("type") ? "type" : "name");
    clearFormErrors(form, formError);
    showFieldError(id(`relay-${key}`), error.message, formError, doc.getElementById("settings-status"));
  }

  function values() {
    return {
      name: id("relay-name").value.trim(), type: id("relay-type").value,
      host: id("relay-host").value.trim(), port: Number(id("relay-port").value),
      password: id("relay-password").value || null,
      mount: id("relay-type").value === "icecast" ? id("relay-mount").value.trim() : null,
      user: id("relay-type").value === "icecast" ? id("relay-user").value.trim() : null,
      stream_id: id("relay-type").value === "shoutcast2" ? Number(id("relay-stream-id").value) : null,
      bitrate: id("relay-bitrate").value ? Number(id("relay-bitrate").value) : null,
      public: id("relay-public").checked, enabled: id("relay-enabled").checked,
    };
  }

  function showForm(relay = null) {
    if (saving) return;
    clearFormErrors(form, formError);
    editing = relay?.name ?? null;
    id("relay-form-title").textContent = relay ? `Edit ${relay.name}` : "Add relay";
    id("relay-name").value = relay?.name || "";
    id("relay-name").readOnly = Boolean(relay);
    id("relay-type").value = relay?.type || "icecast";
    id("relay-host").value = relay?.host || "";
    id("relay-port").value = relay?.port || 8000;
    id("relay-password").value = "";
    id("relay-password").readOnly = Boolean(relay?.password_managed);
    id("relay-password-help").textContent = relay?.password_managed
      ? "This password is managed by the server environment and cannot be changed here."
      : relay?.password_configured ? "A password is saved. Leave blank to keep it." : "Enter the relay source password.";
    id("relay-mount").value = relay?.mount || "/live";
    id("relay-user").value = relay?.user || "source";
    id("relay-stream-id").value = relay?.stream_id || 1;
    id("relay-bitrate").value = relay?.bitrate == null ? "" : String(relay.bitrate);
    id("relay-public").checked = Boolean(relay?.public);
    id("relay-enabled").checked = relay ? Boolean(relay.enabled) : true;
    updateType();
    form.hidden = false;
    returnFocus = relay
      ? rowByName.get(relay.name)?.row.querySelector("button") || id("relay-add")
      : id("relay-add");
    id("relay-name").focus();
  }

  function updateType() {
    const type = id("relay-type").value;
    id("relay-icecast").hidden = type !== "icecast";
    id("relay-shoutcast2").hidden = type !== "shoutcast2";
    id("relay-port").max = type === "icecast" ? "65535" : "65534";
  }

  function render() {
    const names = new Set(relays.map((relay) => relay.name));
    let structuralChange = false;
    for (const [name, record] of rowByName) {
      if (!names.has(name)) {
        if (record.row.contains(doc.activeElement)) id("relay-add").focus();
        record.row.remove();
        rowByName.delete(name);
        structuralChange = true;
      }
    }
    for (const relay of relays) {
      let record = rowByName.get(relay.name);
      if (!record) {
        const button = (label, action) => {
          const item = doc.createElement("button");
          item.type = "button"; item.textContent = label;
          item.addEventListener("click", action);
          return item;
        };
        const edit = button("Edit", () => showForm(record.relay));
        const toggle = button("Disable", () => save({
          name: record.relay.name, type: record.relay.type, host: record.relay.host,
          is_edit: true,
          port: record.relay.port, password: null, mount: record.relay.mount,
          user: record.relay.user, stream_id: record.relay.stream_id,
          bitrate: record.relay.bitrate, public: record.relay.public,
          enabled: !record.relay.enabled,
        }));
        const removeButton = button("Remove", () => removeRelay(record.relay));
        const row = tableRow(doc, ["", "", edit, toggle, removeButton]);
        record = { row, nameCell: row.cells[0], statusCell: row.cells[1], toggle, relay };
        rowByName.set(relay.name, record);
        structuralChange = true;
      }
      record.relay = relay;
      record.row.querySelectorAll("button")[0].setAttribute("aria-label", `Edit relay ${relay.name}`);
      record.toggle.setAttribute("aria-label", `${relay.enabled ? "Disable" : "Enable"} relay ${relay.name}`);
      record.row.querySelectorAll("button")[2].setAttribute("aria-label", `Remove relay ${relay.name}`);
      const status = statuses.get(relay.name) || relay;
      const text = [status.state || "off", status.error, status.title_error]
        .filter(Boolean).join(" — ");
      record.nameCell.textContent = relay.name;
      record.statusCell.textContent = text;
      record.toggle.textContent = relay.enabled ? "Disable" : "Enable";
    }
    let cursor = rows.firstElementChild;
    for (const relay of relays) {
      const row = rowByName.get(relay.name).row;
      if (row !== cursor) rows.insertBefore(row, cursor);
      cursor = row.nextElementSibling;
    }
    if (structuralChange) table.refresh();
  }

  async function refresh() {
    if (!available) return;
    const mine = ++generation;
    try {
      const result = await request(`${root(slug)}/stream/relays`);
      if (!available || mine !== generation) return;
      relays = Array.isArray(result.relays) ? result.relays : [];
      render();
    } catch (err) {
      if (available && mine === generation) announce(`Could not load relays: ${err.message}`, true);
    }
  }

  // *fromForm*: the form's own Save; a row's Enable or Disable keeps focus
  // on that button.
  async function save(payload, fromForm = false) {
    if (fromForm && saving) return false;
    const saveId = fromForm ? ++formSaveId : null;
    const auth = captureAuthenticatedRequestEpoch();
    const station = stationEpoch();
    const current = () => isAuthenticatedRequestCurrent(auth) && stationEpoch() === station
      && (!fromForm || saveId === formSaveId);
    if (fromForm) saving = true;
    const onError = fromForm
      ? (error) => { if (current()) validationError(error); }
      : (error) => { if (current()) announce(error.message, true); };
    const submitButtons = fromForm ? [...form.querySelectorAll('button[type="submit"]')] : [];
    if (fromForm) {
      form.setAttribute("aria-busy", "true");
      submitButtons.forEach((button) => button.setAttribute("aria-busy", "true"));
    }
    const stopPending = fromForm
      ? sayIfStillPending((message) => { if (current()) announce(message); }, "Saving relay settings…")
      : () => {};
    try {
      const ok = await post(`${root(slug)}/stream/relays`, payload, form, { onError });
      if (!current()) return false;
      if (!ok) return false;
      form.hidden = true;
      id("relay-password").value = "";
      await refresh();
      if (!current()) return false;
      if (fromForm) {
        (rowByName.get(payload.name)?.row.querySelector("button") || returnFocus || id("relay-add")).focus();
      }
      announce("Relay settings saved.");
      return true;
    } finally {
      stopPending();
      if (fromForm && saveId === formSaveId) {
        saving = false;
        form.removeAttribute("aria-busy");
        submitButtons.forEach((button) => button.removeAttribute("aria-busy"));
      }
    }
  }

  async function removeRelay(relay) {
    await confirmAction(doc, {
      title: "Remove relay?", message: `Remove ${relay.name} and disconnect it?`,
      confirmLabel: "Remove relay",
      onConfirm: async () => {
        if (!await remove(`${root(slug)}/stream/relays/${encodeURIComponent(relay.name)}`)) return null;
        await refresh();
        announce(`Relay ${relay.name} removed.`);
        return id("relay-add");
      },
    });
  }

  id("relay-add").addEventListener("click", () => showForm());
  id("relay-cancel").addEventListener("click", () => {
    if (saving) return;
    form.hidden = true;
    (returnFocus || id("relay-add")).focus();
  });
  id("relay-type").addEventListener("change", updateType);
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute("aria-invalid") === "true") clearFieldError(event.target, formError);
  });
  // The form is novalidate (index.html says why), so its required fields
  // are checked here, the first missing one said and focused.
  function missingField(payload) {
    if (!payload.name) return { field: "name", message: "Enter a name for the relay." };
    if (!payload.host) return { field: "host", message: "Enter the relay server's host." };
    if (!Number.isInteger(payload.port) || payload.port < 1 || payload.port > 65535) {
      return { field: "port", message: "Enter a listener port from 1 to 65535." };
    }
    return null;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    const payload = values();
    const missing = missingField(payload);
    if (missing) { validationError(missing); return; }
    payload.is_edit = editing !== null;
    if (editing && !payload.password) payload.password = null;
    await save(payload, true);
  });

  return {
    setAvailable(value) {
      available = Boolean(value);
      if (available) void refresh();
      else {
        generation++;
        formSaveId++;
        saving = false;
        form.removeAttribute("aria-busy");
        form.querySelectorAll('[aria-busy="true"]').forEach((button) => button.removeAttribute("aria-busy"));
      }
    },
    applyStatuses(value) {
      statuses = new Map((Array.isArray(value) ? value : []).map((item) => [item.name, item]));
      render();
    },
    refresh,
    reset() {
      available = false; generation++; formSaveId++; saving = false; form.removeAttribute("aria-busy");
      form.querySelectorAll('[aria-busy="true"]').forEach((button) => button.removeAttribute("aria-busy"));
      clearFormErrors(form, formError); statuses.clear(); relays = []; render(); form.hidden = true;
    },
  };
}
