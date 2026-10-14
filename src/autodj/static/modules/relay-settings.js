import { tableRow } from "./table-row.js";
import { installTableNavigation } from "./table-navigation.js";
import { confirmAction } from "./confirm-dialog.js";

const root = (slug) => slug === "default" ? "/api" : `/api/stations/${encodeURIComponent(slug)}`;

export function installRelaySettings({ doc, request, post, remove, announce, slug = "default" }) {
  const id = (name) => doc.getElementById(`stream-${name}`);
  const rows = id("relay-rows");
  const form = id("relay-form");
  const table = installTableNavigation(rows);
  let relays = [];
  let statuses = new Map();
  let editing = null;
  let returnFocus = null;
  let available = false;
  let generation = 0;
  const rowByName = new Map();
  function validationError(error) {
    form.querySelectorAll("[aria-invalid='true']").forEach((field) => field.removeAttribute("aria-invalid"));
    const key = error.message.toLowerCase().includes("password") ? "password"
      : error.message.toLowerCase().includes("host") ? "host"
      : error.message.toLowerCase().includes("port") ? "port"
      : error.message.toLowerCase().includes("mount") ? "mount"
      : error.message.toLowerCase().includes("user") ? "user"
      : error.message.toLowerCase().includes("stream_id") ? "stream-id"
      : error.message.toLowerCase().includes("bitrate") ? "bitrate"
      : error.message.toLowerCase().includes("type") ? "type" : "name";
    const field = id(`relay-${key}`);
    field.setAttribute("aria-invalid", "true");
    field.focus();
    announce(error.message, true);
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
    const onError = fromForm ? validationError : (error) => announce(error.message, true);
    const ok = await post(`${root(slug)}/stream/relays`, payload, form, { onError });
    if (!ok) return false;
    form.hidden = true;
    id("relay-password").value = "";
    await refresh();
    if (fromForm) {
      (rowByName.get(payload.name)?.row.querySelector("button") || returnFocus || id("relay-add")).focus();
    }
    announce("Relay settings saved.");
    return true;
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
    form.hidden = true;
    (returnFocus || id("relay-add")).focus();
  });
  id("relay-type").addEventListener("change", updateType);
  form.addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = values();
    payload.is_edit = editing !== null;
    if (editing && !payload.password) payload.password = null;
    await save(payload, true);
  });
  id("metadata-save").addEventListener("click", async (event) => {
    const body = {
      station_name: id("station-name").value,
      genre: id("station-genre").value,
      description: id("station-description").value,
      url: id("station-url").value,
    };
    if (await post(`${root(slug)}/stream/settings`, body, event.currentTarget)) {
      announce("Station metadata saved. Enabled relays are reconnecting.");
    }
  });

  return {
    setAvailable(value) {
      available = Boolean(value);
      if (available) void refresh();
      else generation++;
    },
    applyStatuses(value) {
      statuses = new Map((Array.isArray(value) ? value : []).map((item) => [item.name, item]));
      render();
    },
    refresh,
    reset() {
      available = false; generation++; statuses.clear(); relays = []; render(); form.hidden = true;
    },
  };
}
