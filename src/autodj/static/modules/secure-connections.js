import { requestJson, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { installTableNavigation } from "./table-navigation.js";
import { openDialog, closeDialog } from "./dialog-guard.js";
import { announceStatus } from "./live-region.js";
import { approximateDurationText } from "./time-format.js";

const BASE = "/api/server/certificates";
const installed = new WeakMap();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const exactDate = (value) => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(undefined, { dateStyle: "long" }).format(date) : "Unavailable";
};
const expiry = (value) => {
  if (!Number.isFinite(new Date(value).getTime())) return "Expiry unavailable";
  return new Date(value).getTime() <= Date.now() ? `Expired ${exactDate(value)}`
    : `Expires ${exactDate(value)} (${approximateDurationText(value)})`;
};
const instructions = {
  windows: '<ol><li>Open the certificate and choose <strong>Install Certificate → Current User</strong>.</li><li>Select <strong>Trusted Root Certification Authorities</strong>.</li><li>Finish and approve the Windows prompt.</li></ol>',
  mac: '<ol><li>Import the certificate into <strong>Keychain Access</strong>.</li><li>Open it, expand <strong>Trust</strong>, and set <strong>Secure Sockets Layer (SSL)</strong> to <strong>Always Trust</strong>.</li><li>Close the window and approve the change.</li></ol>',
  linux: '<p>In your browser’s certificate settings, import the certificate and trust it for websites.</p><details><summary>Chrome / Chromium instructions</summary><p>Open <code>chrome://settings/certificates</code> to manage certificates.</p><a href="https://chromium.googlesource.com/chromium/src/+/main/docs/linux/cert_management.md">Chromium’s Linux certificate guide</a></details>',
  other: '<p>Use your device’s certificate settings to install and trust the certificate. For a managed device, ask your administrator.</p>',
};

export function installSecureConnections({ panel = document.getElementById("secure-connections"), status = document.getElementById("secure-connections-status"), request = requestJson } = {}) {
  if (!panel) return { refresh() {} };
  if (installed.has(panel)) { const controller = installed.get(panel); void controller.refresh(); return controller; }
  const doc = panel.ownerDocument;
  const win = doc.defaultView;
  let inventory = null, wizard = null, opener = null, busy = false, refreshSequence = 0;
  let resumePending = new URL(win.location.href).searchParams.get("https-setup") === "trust";
  const ready = () => win.isSecureContext && win.location.protocol === "https:";
  const say = (message) => announceStatus(status, message, { force: true });
  const api = (path, method = "GET", body) => request(path, {
    method, ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
  });
  const dialog = doc.createElement("dialog");
  dialog.className = "editor-dialog secure-connection-dialog";
  dialog.setAttribute("aria-labelledby", "sc-dialog-title");
  dialog.innerHTML = '<h2 id="sc-dialog-title">Set up HTTPS</h2><div id="sc-dialog-content"></div><p id="sc-error" role="status" aria-live="polite" aria-atomic="true"></p><div class="dialog-actions"><button type="button" id="sc-back"></button><button type="button" id="sc-next"></button><button type="button" id="sc-cancel">Cancel</button></div>';
  doc.body.append(dialog);
  const content = dialog.querySelector("#sc-dialog-content");
  const next = dialog.querySelector("#sc-next");
  const back = dialog.querySelector("#sc-back");
  const cancel = dialog.querySelector("#sc-cancel");
  const errorRegion = dialog.querySelector("#sc-error");
  const active = () => inventory?.certificates.find((cert) => cert.active);
  const selected = () => inventory?.certificates.find((cert) => cert.id === wizard?.id) || active();
  const download = (cert) => `<a href="${BASE}/${encodeURIComponent(cert.id)}/download" download="autodj.crt">Save public certificate</a>`;
  function closeWizard() {
    if (busy) return;
    const returnTo = opener?.isConnected ? opener : doc.getElementById(opener?.id) || panel.querySelector("#sc-remote");
    closeDialog(dialog, { opener: returnTo });
    wizard = null;
    const url = new URL(win.location.href);
    url.searchParams.delete("https-setup");
    win.history.replaceState(null, "", url.href);
  }
  function setBusy(value) {
    busy = value;
    for (const button of dialog.querySelectorAll("button")) button.setAttribute("aria-disabled", String(value));
  }
  const dialogError = (error) => announceStatus(errorRegion, error.message || String(error), { force: true, tone: "error" });
  function openWizard(kind, trigger, step = 1) {
    if (busy || dialog.open) return;
    opener = trigger || doc.activeElement;
    wizard = { kind, step, id: kind === "remote" ? active()?.id : null, platform: "windows" };
    renderWizard();
    openDialog(dialog, { home: cancel, onEscape: closeWizard });
    cancel.focus();
  }
  function httpsLinks() {
    return (inventory?.urls || []).map((address) => {
      const url = new URL(address);
      url.searchParams.set("https-setup", "trust");
      url.hash = "settings";
      return `<li><a href="${esc(url.href)}">${esc(address)}</a></li>`;
    }).join("");
  }
  function certificateStep() {
    const cert = selected();
    if (wizard.kind === "add" && !wizard.id) {
      return '<p>Create a certificate for this host, or import an existing pair.</p>'
        + '<label for="sc-name">Certificate name</label><input id="sc-name" maxlength="120" required value="AutoDJ certificate">'
        + `<label for="sc-hosts">Host names and addresses</label><input id="sc-hosts" aria-describedby="sc-hosts-help" value="${esc((inventory.hosts || []).join(", "))}"><p id="sc-hosts-help" class="setting-desc">Separate addresses with commas.</p>`
        + '<details><summary>Import a certificate</summary><label for="sc-cert">Certificate (PEM)</label><textarea id="sc-cert" rows="4" autocomplete="off" spellcheck="false"></textarea><label for="sc-key">Private key (PEM)</label><textarea id="sc-key" rows="4" autocomplete="off" spellcheck="false"></textarea></details>';
    }
    if (!cert) return `<p>On the AutoDJ host, open <code>http://localhost:${esc(win.location.port || "8080")}</code>, then go to <strong>Settings → Server → Secure connections (HTTPS)</strong> and choose <strong>Add certificate</strong>.</p><details><summary>Using a NAS or a server without a browser?</summary><p>Set up the certificate through a trusted terminal or admin connection. Follow <a href="https://github.com/blindndangerous/AutoDJ/blob/master/docs/guides/operations/network.md#https-on-your-home-network">the host setup guide</a>.</p></details>`;
    let text = `<p><strong>${esc(cert.name)}</strong></p>`;
    text += inventory.local || inventory.https ? `<p>${download(cert)}</p>`
      : '<p>Save the public certificate on the AutoDJ host and transfer it to this recording device through a trusted connection.</p>';
    if (!cert.active && inventory.can_manage) {
      if (!inventory.https) text += '<p>Activating this certificate requires restarting AutoDJ to start serving HTTPS.</p>';
      text += '<button type="button" id="sc-activate">Activate certificate</button>';
    }
    if (inventory.restart_required) text += '<p>Restart AutoDJ to start serving HTTPS. Use <strong>Settings → Update and restart</strong>, or restart AutoDJ on the host.</p>';
    return text;
  }
  function trustGuide() {
    const cert = selected();
    return (cert && (inventory.local || inventory.https) ? `<p>${download(cert)}</p>` : '')
      + `<label for="sc-platform">Recording device</label><select id="sc-platform">${[["windows", "Windows"], ["mac", "macOS"], ["linux", "Linux"], ["other", "Other / managed device"]].map(([value, label]) => `<option value="${value}"${wizard.platform === value ? " selected" : ""}>${label}</option>`).join("")}</select><div id="sc-trust-instructions">${instructions[wizard.platform]}</div>`
      + (inventory.restart_required ? '<p>Restart AutoDJ on the host, then open an HTTPS address below.</p>' : '<p>Open AutoDJ over HTTPS on the recording device.</p>')
      + `<ul>${httpsLinks()}</ul>`;
  }
  function renderWizard() {
    if (!wizard) return;
    const complete = wizard.step === 3;
    dialog.querySelector("#sc-dialog-title").textContent = wizard.kind === "add" ? "Add certificate" : "Set up a remote device";
    const trust = ready() && !inventory.restart_required ? '<p>This browser is ready to record.</p><details><summary>Set up another recording device</summary>' + trustGuide() + '</details>' : trustGuide();
    content.innerHTML = complete ? '<h3 id="sc-step-heading" tabindex="-1">You’re all set</h3><p>Go to Voice tracks to start recording. Allow microphone access when your browser asks.</p>'
      : `<h3 id="sc-step-heading" tabindex="-1">Step ${wizard.step} of 2</h3><ol class="secure-steps"><li${wizard.step === 1 ? ' aria-current="step"' : ''}>Certificate</li><li${wizard.step === 2 ? ' aria-current="step"' : ''}>Trust</li></ol><h3>${wizard.step === 1 ? 'Certificate' : 'Trust on the recording device'}</h3>${wizard.step === 1 ? certificateStep() : trust}`;
    back.hidden = wizard.step !== 2;
    back.textContent = "Previous: certificate";
    next.hidden = complete || (wizard.step === 1 && !selected() && wizard.kind !== "add");
    next.textContent = wizard.step === 1 ? (wizard.kind === "add" && !wizard.id ? "Save certificate" : "Next: trust the certificate") : "Finish setup";
    next.setAttribute("aria-disabled", String(busy || (wizard.step === 2 && (!ready() || inventory.restart_required))));
    cancel.textContent = complete ? "Close" : "Cancel";
    content.querySelector("#sc-platform")?.addEventListener("change", (event) => {
      wizard.platform = event.target.value;
      content.querySelector("#sc-trust-instructions").innerHTML = instructions[wizard.platform];
    });
    content.querySelector("#sc-activate")?.addEventListener("click", () => void activate(selected()));
  }
  async function advance() {
    if (busy || !wizard) return;
    if (wizard.step === 2) {
      if (!ready() || inventory.restart_required) { dialogError("Open AutoDJ over HTTPS on the recording device to finish setup."); return; }
      wizard.step = 3;
    } else if (wizard.kind === "add" && !wizard.id) {
      const name = content.querySelector("#sc-name");
      if (!name.reportValidity()) return;
      const certificate = content.querySelector("#sc-cert").value.trim();
      const private_key = content.querySelector("#sc-key").value.trim();
      const hosts = content.querySelector("#sc-hosts").value.split(",").map((s) => s.trim()).filter(Boolean);
      const opening = wizard;
      const epoch = captureAuthenticatedRequestEpoch();
      setBusy(true);
      try {
        const result = await api(certificate || private_key ? BASE + "/import" : BASE, "POST", certificate || private_key ? { name: name.value.trim(), certificate, private_key } : { name: name.value.trim(), hosts });
        if (!isAuthenticatedRequestCurrent(epoch) || wizard !== opening) return;
        wizard.id = result.id;
        await refresh();
      } catch (error) { if (isAuthenticatedRequestCurrent(epoch)) dialogError(error); return; }
      finally { setBusy(false); }
    } else {
      if (wizard.id && !selected()?.active) { dialogError("Activate this certificate before continuing."); return; }
      wizard.step = 2;
    }
    renderWizard();
    content.querySelector("#sc-step-heading").focus();
  }
  async function activate(cert) {
    if (busy || !cert) return;
    if (!await confirmAction(doc, { title: `Activate ${cert.name}?`, message: "AutoDJ will use this certificate for new connections.", confirmLabel: "Activate" })) return;
    const epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    try {
      await api(`${BASE}/${encodeURIComponent(cert.id)}/activate`, "POST");
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await refresh();
      (dialog.open ? dialogError : say)(inventory.restart_required ? "Certificate selected. Restart AutoDJ to serve HTTPS." : "Certificate activated.");
    } catch (error) { if (isAuthenticatedRequestCurrent(epoch)) (dialog.open ? dialogError : say)(error.message); }
    finally {
      setBusy(false);
      if (wizard) { renderWizard(); content.querySelector("#sc-step-heading").focus(); }
      else panel.querySelector(`[data-row="${cert.id}"] td`)?.focus();
    }
  }
  async function remove(cert) {
    if (busy) return;
    if (!await confirmAction(doc, { title: `Delete ${cert.name}?`, message: "Deletes this saved certificate and its private key from AutoDJ.", confirmLabel: "Delete" })) return;
    const epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    try {
      await api(`${BASE}/${encodeURIComponent(cert.id)}`, "DELETE");
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await refresh();
      panel.querySelector("#sc-add")?.focus();
      say("Certificate deleted.");
    } catch (error) { if (isAuthenticatedRequestCurrent(epoch)) say(error.message); }
    finally { setBusy(false); }
  }
  function render() {
    const data = inventory;
    const rows = data.certificates.map((cert) => {
      const expired = new Date(cert.expires).getTime() <= Date.now();
      const manageable = data.can_manage && cert.managed !== false && !cert.active;
      return `<tr data-row="${esc(cert.id)}"><td role="rowheader"><strong>${esc(cert.name)}</strong><small>${cert.hosts.map(esc).join(" · ")}</small><small>Issued ${exactDate(cert.issued)} · ${esc(cert.type)}</small></td><td role="gridcell">${cert.active ? "Active" : expired ? "Expired" : cert.status === "invalid" ? "Unavailable" : "Saved"}</td><td role="gridcell">${expiry(cert.expires)}</td><td role="gridcell">${manageable && !expired && cert.status !== "invalid" ? `<button type="button" data-activate="${esc(cert.id)}">Activate</button>` : "—"}</td><td role="gridcell">${manageable ? `<button type="button" data-delete="${esc(cert.id)}">Delete</button>` : "—"}</td></tr>`;
    }).join("");
    panel.innerHTML = `<h4 id="secure-connections-heading" tabindex="-1">Secure connections (HTTPS)</h4><h5 id="sc-table-heading">Server certificate</h5>${rows ? `<div class="data-table-scroll"><table role="grid" aria-labelledby="sc-table-heading"><thead><tr><th scope="col">Certificate</th><th scope="col">Status</th><th scope="col">Expiry</th><th scope="col">Activate</th><th scope="col">Delete</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p>Create a certificate on the AutoDJ host to get started.</p>'}${data.can_manage ? '<button type="button" id="sc-add">Add certificate</button>' : '<p>Manage certificates at localhost on the AutoDJ host.</p>'} <button type="button" id="sc-remote">Set up a remote device</button>${!data.https && data.can_manage ? '<p>Activating a certificate requires restarting AutoDJ to start serving HTTPS.</p>' : ''}${data.restart_required ? '<p>Restart AutoDJ from Settings → Update and restart, then open an HTTPS address.</p>' : ''}${ready() ? '<p>This browser is ready to record.</p>' : ''}<details><summary>How to remove trust on this device</summary><p>If you installed the certificate on this device, open its certificate settings, select the AutoDJ certificate, and remove trust.</p></details>`;
    panel.querySelector("#sc-add")?.addEventListener("click", (event) => openWizard("add", event.currentTarget));
    panel.querySelector("#sc-remote").addEventListener("click", (event) => openWizard("remote", event.currentTarget));
    for (const button of panel.querySelectorAll("[data-activate]")) button.addEventListener("click", () => void activate(data.certificates.find((cert) => cert.id === button.dataset.activate)));
    for (const button of panel.querySelectorAll("[data-delete]")) button.addEventListener("click", () => void remove(data.certificates.find((cert) => cert.id === button.dataset.delete)));
    installTableNavigation(panel.querySelector("tbody"));
  }
  async function refresh() {
    const sequence = ++refreshSequence;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const data = await api(BASE);
      if (!isAuthenticatedRequestCurrent(epoch) || sequence !== refreshSequence) return;
      inventory = data;
      render();
      if (data.management_note) say(data.management_note);
      if (resumePending) {
        resumePending = false;
        doc.getElementById("stab-server")?.click();
        openWizard("remote", panel.querySelector("#sc-remote"), 2);
      }
    } catch (error) { if (isAuthenticatedRequestCurrent(epoch)) say(error.message); }
  }
  cancel.addEventListener("click", closeWizard);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeWizard(); });
  next.addEventListener("click", () => void advance());
  back.addEventListener("click", () => { if (!busy && wizard) { wizard.step = 1; renderWizard(); content.querySelector("#sc-step-heading").focus(); } });
  const serverPanel = doc.getElementById("spanel-server");
  serverPanel?.addEventListener("toggle", () => { if (serverPanel.open) void refresh(); });
  const voiceHelp = doc.getElementById("voice-https-help");
  if (voiceHelp) voiceHelp.hidden = Boolean(win.isSecureContext);
  doc.getElementById("voice-https-setup")?.addEventListener("click", () => {
    doc.getElementById("tab-settings")?.click();
    doc.getElementById("stab-server")?.click();
    panel.querySelector("#secure-connections-heading")?.focus();
  });
  const controller = { refresh };
  installed.set(panel, controller);
  void refresh();
  return controller;
}
