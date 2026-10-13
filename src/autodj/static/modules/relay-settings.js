// Settings > Stream > Relays (#185): add, edit, turn on or off and remove
// the station's relays to Icecast and Shoutcast servers while it plays.
//
// Speech rules (docs/accessibility-testing.md, Announcement contract):
//   - The table is a grid with the shared keyboard navigation: one tab
//     stop, arrows between cells.  Its status cells are plain text, never
//     live, rewritten only when their words change, so a relay retrying
//     in the background says nothing.
//   - Save, On and Remove are user actions: each result is said once in
//     the settings status region, failures with the error tone.  After
//     the user saves or turns on a relay, the first state it reaches
//     (live, or not connected with the error) is said once too; later
//     changes are not.
//   - The form is a native modal dialog.  A refused field is marked
//     aria-invalid, its error is linked to it with aria-describedby and it
//     takes focus, which reads the error.  Any other refusal is said
//     inside the dialog, as the page behind it is inert.
//   - The password field is never filled in and its value never leaves
//     the form except in the save request; the dialog says whether a
//     password is set and where it comes from.
//   - Focus never falls to the page: closing the dialog puts it on the
//     row's Edit button (or the button that opened it), and a removed
//     row hands it to the next row's Remove button or to Add relay.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";
import { installTableNavigation } from "./table-navigation.js";
import { tableRow } from "./table-row.js";

const RELAY_TYPE_LABELS = {
  icecast: "Icecast",
  shoutcast1: "Shoutcast 1",
  shoutcast2: "Shoutcast 2",
};
const API = "/api/stream/relays";
const NAME_RULE = /^[A-Za-z0-9_-]{1,32}$/;
// The server's field names, with the id of the page's control.
const FIELD_IDS = {
  name: "relay-name",
  type: "relay-type",
  host: "relay-host",
  port: "relay-port",
  mount: "relay-mount",
  user: "relay-user",
  stream_id: "relay-stream-id",
  password: "relay-password",
  bitrate: "relay-bitrate",
};
const COLUMNS = 6;

// Where the password comes from, in words.
export function passwordWords(relay) {
  if (!relay.password_set) return "not set";
  if (relay.password_source === "environment") return `set by ${relay.password_variable}`;
  if (relay.password_source === "saved") return "set here";
  return "set in the config files";
}

// The Server cell: type, where it sends, and the password's state.
export function relayServerText(relay) {
  const type = RELAY_TYPE_LABELS[relay.type] || String(relay.type || "Unknown server");
  const where = relay.target ? ` at ${relay.target}` : "";
  const quality = relay.bitrate ? `${relay.bitrate} kbps` : "the station's quality";
  return `${type}${where}, ${quality}. Password ${passwordWords(relay)}.`;
}

// The Status cell, in one sentence.
export function relayStatusText(relay) {
  let text;
  if (relay.state === "live") text = "Live.";
  else if (relay.state === "connecting") text = "Connecting.";
  else if (relay.state === "retrying") {
    text = `Not connected, trying again. Last error: ${relay.error || "unknown"}.`;
  } else if (relay.state === "off") text = relay.error ? `Off: ${relay.error}` : "Off.";
  else text = "Stopped.";
  if (relay.title_error) text += ` Song titles are not updating: ${relay.title_error}.`;
  return text;
}

// The first error in the form's values, as [field, message], or null.  The
// server checks the same rules again and names the field it refuses.
export function relayFormError(values, { creating = true, passwordSet = false } = {}) {
  if (creating && !NAME_RULE.test(values.name)) {
    return ["name", "Enter a name of 1 to 32 letters, digits, hyphens or underscores."];
  }
  const host = values.host.trim();
  if (!host || /[\s/]/.test(host)) {
    return ["host", "Enter the server's host name or IP address, without http://, a port or a path."];
  }
  const top = values.type === "icecast" ? 65535 : 65534;
  if (!/^\d{1,5}$/.test(values.port.trim()) || Number(values.port) < 1 || Number(values.port) > top) {
    return ["port", `Enter the listener port, a whole number from 1 to ${top}.`];
  }
  if (values.type === "icecast" && !/^\/[A-Za-z0-9._~/-]{1,200}$/.test(values.mount.trim())) {
    return ["mount", "Enter the mount point: a / followed by letters, digits and . _ ~ - /, for example /live."];
  }
  if (values.type === "icecast" && values.user.includes(":")) {
    return ["user", "Enter the source user name without a colon."];
  }
  if (values.type === "shoutcast2" && (!/^\d{1,10}$/.test(values.streamId.trim()) || Number(values.streamId) < 1)) {
    return ["stream_id", "Enter the stream ID, a whole number of 1 or more."];
  }
  if (values.passwordRequired && !values.password && !passwordSet) {
    return ["password", "Enter the source password."];
  }
  return null;
}

// The request body for the form's values; the password only when typed.
export function relayBody(values) {
  const body = {
    name: values.name.trim(),
    type: values.type,
    host: values.host.trim(),
    port: Number(values.port.trim()),
    bitrate: values.bitrate ? Number(values.bitrate) : null,
    public: values.public,
    enabled: values.enabled,
  };
  if (values.type === "icecast") {
    body.mount = values.mount.trim();
    body.user = values.user.trim() || null;
  }
  if (values.type === "shoutcast2") body.stream_id = Number(values.streamId.trim());
  if (values.password) body.password = values.password;
  return body;
}

export function installRelaySettings({
  document: doc = document,
  request = requestJson,
  settingsStatus = null,
} = {}) {
  const el = (id) => doc.getElementById(id);
  const section = el("stream-relays");
  const list = el("stream-relays-list");
  const addButton = el("stream-relay-add");
  const problem = el("stream-relays-problem");
  const dialog = el("relay-dialog");
  const form = el("relay-form");
  if (!section || !list || !addButton || !dialog || !form) return { apply() {}, reset() {} };
  const navigation = installTableNavigation(list);
  const title = el("relay-dialog-title");
  const formError = el("relay-form-error");
  const inputs = Object.fromEntries(Object.entries(FIELD_IDS).map(([field, id]) => [field, el(id)]));
  const enabledBox = el("relay-enabled");
  const publicBox = el("relay-public");
  const passwordField = el("relay-password-field");
  const passwordState = el("relay-password-state");
  const passwordEnv = el("relay-password-env");
  const typeFields = {
    mount: el("relay-mount-field"),
    user: el("relay-user-field"),
    stream_id: el("relay-stream-id-field"),
  };
  let relays = [];
  let loaded = false;
  let loading = null;
  let active = false;
  // The state last pushed for each relay, by name.
  let pushed = new Map();
  // Relays the user just saved or turned on: their first state is said once.
  const watched = new Set();
  let editing = null;
  let opener = null;
  let pending = false;

  function say(message, tone = "info") {
    announceStatus(settingsStatus, message, { dwellMs: 6000, force: true, tone });
  }

  function current(relay) {
    const live = pushed.get(relay.name);
    if (!live || (relay.state === "off" && relay.error)) return relay;
    return { ...relay, state: live.state, error: live.error, title_error: live.title_error };
  }

  function rowsOf() {
    return [...list.querySelectorAll("tr")];
  }

  function cellControl(cell) {
    return cell?.querySelector("button, input") || cell;
  }

  function render() {
    const focused = list.contains(doc.activeElement) ? {
      name: doc.activeElement.closest("tr")?.dataset.name,
      column: doc.activeElement.closest("td")?.cellIndex,
    } : null;
    const rows = relays.map((relay) => {
      const name = doc.createElement("span");
      name.textContent = relay.name;
      const status = doc.createElement("span");
      status.className = "relay-status";
      const on = doc.createElement("input");
      on.type = "checkbox";
      on.dataset.action = "enabled";
      on.checked = Boolean(relay.enabled);
      const onLabel = doc.createElement("label");
      // Named "On, <relay>": the visible word first, then which relay.
      const whose = doc.createElement("span");
      whose.className = "visually-hidden";
      whose.textContent = `, ${relay.name}`;
      onLabel.append(on, " On", whose);
      const edit = doc.createElement("button");
      edit.type = "button";
      edit.dataset.action = "edit";
      edit.textContent = "Edit";
      edit.setAttribute("aria-label", `Edit ${relay.name}`);
      let remove = "Set in config.toml";
      if (relay.removable || relay.revertable) {
        remove = doc.createElement("button");
        remove.type = "button";
        remove.dataset.action = "remove";
        remove.textContent = relay.removable ? "Remove" : "Revert";
        remove.setAttribute("aria-label", relay.removable
          ? `Remove ${relay.name}` : `Revert ${relay.name} to config.toml`);
      }
      const row = tableRow(doc, [name, relayServerText(relay), status, onLabel, edit, remove]);
      row.dataset.name = relay.name;
      return row;
    });
    list.replaceChildren(...(rows.length ? rows
      : [tableRow(doc, ["No relays yet. Add relay sends this station to an Icecast or Shoutcast server."], { colspan: [COLUMNS] })]));
    showStatuses();
    navigation.refresh();
    if (focused) {
      const row = rowsOf().find((item) => item.dataset.name === focused.name);
      const target = row ? cellControl(row.cells[focused.column]) : addButton;
      target?.focus();
    }
  }

  // Status words change in place, never by rebuilding the row.
  function showStatuses() {
    for (const row of rowsOf()) {
      const relay = relays.find((item) => item.name === row.dataset.name);
      const cell = row.querySelector(".relay-status");
      if (!relay || !cell) continue;
      const shown = current(relay);
      const text = relayStatusText(shown);
      if (cell.textContent !== text) cell.textContent = text;
      const failing = shown.state === "retrying" || Boolean(shown.title_error)
        || (shown.state === "off" && Boolean(shown.error));
      cell.classList.toggle("relay-error", failing);
    }
  }

  function showProblem(text) {
    if (!problem) return;
    if (problem.textContent !== (text || "")) problem.textContent = text || "";
    problem.hidden = !text;
  }

  // A load the user's change waits for starts after any in flight, which
  // may have read the relays before the change.
  async function load({ fresh = false } = {}) {
    if (loading && !fresh) return loading;
    if (loading) await loading;
    const epoch = captureAuthenticatedRequestEpoch();
    loading = (async () => {
      try {
        const data = await request(API);
        if (!active || !isAuthenticatedRequestCurrent(epoch)) return;
        relays = Array.isArray(data.relays) ? data.relays : [];
        loaded = true;
        showProblem(data.problem);
        render();
      } catch (error) {
        if (!active || !isAuthenticatedRequestCurrent(epoch) || error?.name === "AuthenticationRequiredError") return;
        if (!loaded) {
          list.replaceChildren(tableRow(doc, [`Could not load the relays: ${error.message}`], { colspan: [COLUMNS] }));
          navigation.refresh();
        }
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  function announceFirstState() {
    for (const name of [...watched]) {
      const live = pushed.get(name);
      if (!live) continue;
      if (live.state === "live") {
        watched.delete(name);
        say(`${name} is live.`);
      } else if (live.state === "retrying") {
        watched.delete(name);
        say(`${name} could not connect: ${live.error || "unknown error"}. It keeps trying.`, "error");
      }
    }
  }

  function apply(state) {
    if (!state?.stream_mode) {
      active = false;
      return;
    }
    const arriving = !active;
    active = true;
    pushed = new Map((state.stream_relays || []).map((relay) => [relay.name, relay]));
    const names = new Set(relays.map((relay) => relay.name));
    const unknown = [...pushed.keys()].some((name) => !names.has(name));
    if (arriving || !loaded || (unknown && !pending)) void load();
    else showStatuses();
    announceFirstState();
  }

  function reset() {
    active = false;
    loaded = false;
    relays = [];
    pushed = new Map();
    watched.clear();
    if (dialog.open) dialog.close("cancel");
    list.replaceChildren(tableRow(doc, ["Relays not loaded."], { colspan: [COLUMNS] }));
    showProblem("");
  }

  // -- the dialog --------------------------------------------------------------

  function errorOf(field) { return el(`${FIELD_IDS[field]}-error`); }
  function linkError(field, linked) {
    const input = inputs[field];
    const error = errorOf(field);
    if (!input || !error) return;
    const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== error.id);
    if (linked) ids.push(error.id);
    if (ids.length) input.setAttribute("aria-describedby", ids.join(" "));
    else input.removeAttribute("aria-describedby");
  }
  function clearError(field) {
    const error = errorOf(field);
    inputs[field]?.removeAttribute("aria-invalid");
    if (error) { error.textContent = ""; error.hidden = true; }
    linkError(field, false);
  }
  function clearErrors() {
    for (const field of Object.keys(FIELD_IDS)) clearError(field);
    formError.textContent = "";
  }
  function showError(field, message) {
    const input = inputs[field];
    const error = errorOf(field);
    if (!input || !error || input.closest("[hidden]")) {
      announceStatus(formError, message, { force: true, mirror: false });
      return;
    }
    input.setAttribute("aria-invalid", "true");
    error.textContent = message;
    error.hidden = false;
    linkError(field, true);
    input.focus();
  }

  function showTypeFields() {
    const type = inputs.type.value;
    typeFields.mount.hidden = type !== "icecast";
    typeFields.user.hidden = type !== "icecast";
    typeFields.stream_id.hidden = type !== "shoutcast2";
  }

  function showPasswordState(relay) {
    const fromEnvironment = relay?.password_source === "environment";
    passwordField.hidden = fromEnvironment;
    passwordEnv.hidden = !fromEnvironment;
    passwordEnv.textContent = fromEnvironment
      ? `The environment variable ${relay.password_variable} sets this relay's password, and it wins over one saved here. Change the password there.`
      : "";
    if (!relay) passwordState.textContent = "Not set. Enter the source password the server gave you.";
    else if (relay.password_set) {
      passwordState.textContent = `A password is ${passwordWords(relay)}. Leave this empty to keep it.`;
    } else passwordState.textContent = "Not set. Enter the source password the server gave you.";
  }

  function fill(relay) {
    inputs.name.value = relay?.name || "";
    inputs.name.readOnly = Boolean(relay);
    enabledBox.checked = relay ? Boolean(relay.enabled) : true;
    inputs.type.value = relay?.type || "icecast";
    inputs.host.value = relay?.host || "";
    inputs.port.value = String(relay?.port ?? 8000);
    inputs.mount.value = relay?.mount || "/live";
    inputs.user.value = relay?.user && relay.user !== "source" ? relay.user : "";
    inputs.stream_id.value = String(relay?.stream_id ?? 1);
    inputs.password.value = "";
    inputs.bitrate.value = relay?.bitrate ? String(relay.bitrate) : "";
    publicBox.checked = Boolean(relay?.public);
    showTypeFields();
    showPasswordState(relay);
  }

  function open(relay, from) {
    if (dialog.open) return;
    editing = relay;
    opener = from;
    clearErrors();
    fill(relay);
    title.textContent = relay ? `Edit ${relay.name}` : "Add relay";
    dialog.showModal();
    (relay ? inputs.type : inputs.name).focus();
  }

  function focusAfter(name) {
    const row = rowsOf().find((item) => item.dataset.name === name);
    const target = row?.querySelector("button[data-action=edit]")
      || (opener?.isConnected ? opener : addButton);
    target.focus();
  }

  async function submit(event) {
    event.preventDefault();
    if (pending) return;
    clearErrors();
    const values = {
      name: inputs.name.value,
      type: inputs.type.value,
      host: inputs.host.value,
      port: inputs.port.value,
      mount: inputs.mount.value,
      user: inputs.user.value,
      streamId: inputs.stream_id.value,
      password: passwordField.hidden ? "" : inputs.password.value,
      bitrate: inputs.bitrate.value,
      public: publicBox.checked,
      enabled: enabledBox.checked,
      passwordRequired: !passwordField.hidden,
    };
    const creating = !editing;
    const refused = relayFormError(values, { creating, passwordSet: Boolean(editing?.password_set) });
    if (refused) { showError(...refused); return; }
    const body = relayBody(values);
    pending = true;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const reply = await request(creating ? API : `${API}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!isAuthenticatedRequestCurrent(epoch)) { dialog.close("cancel"); return; }
      inputs.password.value = "";
      await load({ fresh: true });
      pending = false;
      dialog.close("save");
      focusAfter(body.name);
      // Saved either way: a warning says why it is not running yet.
      if (reply?.warning) say(reply.warning, "error");
      else {
        if (body.enabled) watched.add(body.name);
        say(`${creating ? "Added" : "Saved"} ${body.name}. ${body.enabled ? "It is connecting." : "It is off."}`);
      }
    } catch (error) {
      pending = false;
      if (!isAuthenticatedRequestCurrent(epoch)) { dialog.close("cancel"); return; }
      if (error?.field && FIELD_IDS[error.field]) showError(error.field, error.message);
      else {
        announceStatus(formError, `Could not save ${body.name}: ${error.message}`, { force: true, mirror: false });
        // The table shows what the server has, whatever happened.
        void load({ fresh: true });
      }
    }
  }

  function cancel() {
    if (pending) return;
    dialog.close("cancel");
  }

  dialog.addEventListener("cancel", (event) => { if (pending) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    inputs.password.value = "";
    if (dialog.returnValue !== "save") (opener?.isConnected ? opener : addButton).focus();
    editing = null;
  });
  form.addEventListener("submit", submit);
  el("relay-cancel")?.addEventListener("click", cancel);
  inputs.type.addEventListener("change", showTypeFields);
  form.addEventListener("input", (event) => {
    const field = Object.keys(FIELD_IDS).find((name) => inputs[name] === event.target);
    if (field && inputs[field].getAttribute("aria-invalid") === "true") clearError(field);
  });
  addButton.addEventListener("click", () => open(null, addButton));

  // -- the row actions -----------------------------------------------------------

  async function setEnabled(relay, box) {
    if (box.dataset.busy) { box.checked = !box.checked; return; }
    const enabled = box.checked;
    box.dataset.busy = "1";
    box.setAttribute("aria-disabled", "true");
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const reply = await request(`${API}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: relay.name, enabled }),
      });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      if (enabled && !reply?.warning) watched.add(relay.name);
      else watched.delete(relay.name);
      await load({ fresh: true });
      if (reply?.warning) say(reply.warning, "error");
      else say(`${relay.name} turned ${enabled ? "on. It is connecting." : "off."}`);
    } catch (error) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      box.checked = !enabled;
      say(`Could not turn ${relay.name} ${enabled ? "on" : "off"}: ${error.message}`, "error");
      void load({ fresh: true });
    } finally {
      delete box.dataset.busy;
      box.removeAttribute("aria-disabled");
    }
  }

  async function remove(relay, button) {
    const reverting = !relay.removable;
    const index = rowsOf().findIndex((row) => row.dataset.name === relay.name);
    const done = await confirmAction(doc, {
      title: reverting ? `Revert ${relay.name}?` : `Remove ${relay.name}?`,
      message: reverting
        ? "The relay goes back to its settings and password in config.toml and config.local.toml and logs in again. The stream and the other relays keep playing."
        : "The relay disconnects from its server, and its settings and saved password are deleted. The stream and the other relays keep playing.",
      confirmLabel: reverting ? "Revert relay" : "Remove relay",
      onConfirm: async () => {
        const epoch = captureAuthenticatedRequestEpoch();
        try {
          const reply = await request(`${API}/remove`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: relay.name }),
          });
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          watched.delete(relay.name);
          await load({ fresh: true });
          if (reply?.warning) say(reply.warning, "error");
          else say(reverting ? `${relay.name} uses config.toml again.` : `Removed ${relay.name}.`);
          if (reverting) return rowsOf().find((row) => row.dataset.name === relay.name)
            ?.querySelector("button[data-action=edit]") || addButton;
          const rows = rowsOf().filter((row) => row.dataset.name);
          const next = rows[Math.min(index, rows.length - 1)];
          return next?.querySelector("button[data-action=remove]")
            || next?.querySelector("button[data-action=edit]") || addButton;
        } catch (error) {
          if (isAuthenticatedRequestCurrent(epoch)) {
            say(`Could not ${reverting ? "revert" : "remove"} ${relay.name}: ${error.message}`, "error");
            void load({ fresh: true });
          }
          return button;
        }
      },
    });
    return done;
  }

  list.addEventListener("click", (event) => {
    const control = event.target.closest?.("button[data-action]");
    const name = event.target.closest?.("tr")?.dataset.name;
    const relay = relays.find((item) => item.name === name);
    if (!control || !relay) return;
    if (control.dataset.action === "edit") open(relay, control);
    else if (control.dataset.action === "remove") void remove(relay, control);
  });
  list.addEventListener("change", (event) => {
    const box = event.target;
    if (box?.dataset?.action !== "enabled") return;
    const relay = relays.find((item) => item.name === box.closest("tr")?.dataset.name);
    if (relay) void setEnabled(relay, box);
  });

  return { apply, reset, load };
}
