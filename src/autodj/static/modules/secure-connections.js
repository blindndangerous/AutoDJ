import { withDisabled, requestJson, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { installTableNavigation } from "./table-navigation.js";
import { openDialog, closeDialog } from "./dialog-guard.js";
import { makeSay, clearAnnouncedStatus } from "./live-region.js";
import { clearFieldError, clearFormErrors, showFieldError } from "./dom-helpers.js";
import { onCardOpen } from "./card-state.js";
import { approximateDurationText } from "./time-format.js";

const BASE = "/api/server/certificates";
const STAGING = "Let's Encrypt staging test — not trusted";
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
  windows: '<ol><li>Open the certificate and choose <strong>Install Certificate → Current User</strong>.</li><li>Select <strong>Trusted Root Certification Authorities</strong> as the store.</li><li>Finish and approve the Windows prompt.</li></ol>',
  mac: '<ol><li>Import the certificate into <strong>Keychain Access</strong>.</li><li>Open the certificate, expand <strong>Trust</strong>, and set <strong>Secure Sockets Layer (SSL)</strong> to <strong>Always Trust</strong>.</li><li>Close the window and approve the change.</li></ol><a href="https://support.apple.com/guide/keychain-access/kyca11871/mac">Apple’s certificate instructions</a>',
  linux: '<p>In your browser’s certificate settings, import the certificate and trust it for websites.</p><details><summary>Chrome / Chromium instructions</summary><p>Open <code>chrome://settings/certificates</code> to manage certificates.</p><a href="https://chromium.googlesource.com/chromium/src/+/main/docs/linux/cert_management.md">Chromium’s Linux certificate guide</a></details>',
  other: '<p>Use your device’s certificate settings to install and trust the certificate. For a managed device, ask your administrator.</p>',
};

export function installSecureConnections({ panel = document.getElementById("secure-connections"), status = document.getElementById("secure-connections-status"), request = requestJson, updates = null } = {}) {
  if (!panel) return { refresh() {} };
  if (installed.has(panel)) { const controller = installed.get(panel); void controller.refresh(); return controller; }
  const doc = panel.ownerDocument;
  const win = doc.defaultView;
  let inventory = null, wizard = null, opener = null, busy = false, refreshSequence = 0;
  let restartAvailable = false;
  let pollTimer = null, pollDeadline = 0;
  const cleanupNotes = new Set();
  let resumePending = new URL(win.location.href).searchParams.get("https-setup") === "trust";
  const ready = () => win.isSecureContext && win.location.protocol === "https:";
  const say = makeSay(status);
  const api = (path, method = "GET", body) => {
    const operation = () => request(path, {
      method, ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    });
    const activeControl = panel.contains(doc.activeElement)
      ? doc.activeElement.closest("button") || doc.activeElement.closest("td")?.querySelector("button") : null;
    return method === "GET" ? operation() : withDisabled(dialog.open ? next : activeControl, operation);
  };
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
  const fieldErrorNode = doc.createElement("p");
  fieldErrorNode.id = "sc-field-error";
  fieldErrorNode.className = "field-error";
  fieldErrorNode.hidden = true;
  errorRegion.before(fieldErrorNode);
  const sayDialog = makeSay(errorRegion);
  const active = () => inventory?.certificates.find((cert) => cert.active);
  const selected = () => inventory?.certificates.find((cert) => cert.id === wizard?.id) || active();
  const trustedBrowser = () => ready() && (selected()?.type !== "Let's Encrypt" || selected().hosts.includes(win.location.hostname));
  const download = (cert) => `<a href="${BASE}/${encodeURIComponent(cert.id)}/download" download="autodj.crt">Save public certificate</a>`;
  function closeWizard() {
    if (busy) return;
    win.clearTimeout(pollTimer);
    clearAnnouncedStatus(errorRegion);
    const returnTo = opener?.isConnected ? opener : (opener?.id && doc.getElementById(opener.id)) || [...panel.querySelectorAll("[data-acme-operation]")].find((button) => button.dataset.acmeOperation === opener?.dataset.acmeOperation) || panel.querySelector("#sc-add") || panel.querySelector("#sc-remote");
    closeDialog(dialog, { opener: returnTo });
    wizard = null;
    content.replaceChildren();
    errorRegion.textContent = "";
    const url = new URL(win.location.href);
    url.searchParams.delete("https-setup");
    win.history.replaceState(null, "", url.href);
  }
  function setBusy(value) {
    busy = value;
    for (const button of dialog.querySelectorAll("button")) button.setAttribute("aria-disabled", String(value));
  }
  const dialogError = (error) => sayDialog(error.message || String(error), "error");
  function openWizard(kind, trigger, step = 1) {
    if (busy || dialog.open) return;
    opener = trigger || doc.activeElement;
    wizard = { kind, step, id: kind === "remote" ? active()?.id : null, platform: "windows", method: "create" };
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
  function restartControl() {
    return inventory?.restart_required && restartAvailable ? '<button type="button" data-https-restart>Restart now</button>' : '';
  }
  function restartNow() {
    if (busy || !restartAvailable) return;
    const destination = new URL(win.location.href);
    const port = destination.port || (destination.protocol === "https:" ? "443" : "80");
    destination.protocol = "https:";
    const cert = selected();
    if (cert?.type === "Let's Encrypt") destination.hostname = cert.hosts[0];
    destination.port = port;
    destination.searchParams.set("https-setup", "trust");
    destination.hash = "settings";
    if (dialog.open) closeWizard();
    doc.getElementById("tab-settings")?.click();
    doc.getElementById("stab-updates")?.click();
    void updates.restartForHttps(destination.href);
  }
  function acmeForm() {
    return `<label for="sc-domain">Domain</label><input id="sc-domain" required maxlength="253" autocomplete="off" spellcheck="false" aria-describedby="sc-domain-help" value="${esc(wizard.draft?.domain || "")}"><p id="sc-domain-help" class="setting-desc">One public DNS name; no IP addresses or wildcards.</p>`
      + `<label for="sc-zone">Cloudflare zone ID</label><input id="sc-zone" required pattern="[0-9a-f]{32}" maxlength="32" autocomplete="off" spellcheck="false" aria-describedby="sc-zone-help" value="${esc(wizard.draft?.zone_id || "")}"><p id="sc-zone-help" class="setting-desc">32 lowercase hexadecimal characters from the Cloudflare zone overview.</p>`
      + '<label for="sc-token">Cloudflare API token</label><input id="sc-token" type="password" required maxlength="1024" autocomplete="new-password" aria-describedby="sc-token-help"><p id="sc-token-help" class="setting-desc">Use a token limited to this zone, with DNS Edit and Zone Read permissions.</p>'
      + `<label for="sc-environment">Certificate service</label><select id="sc-environment"><option value="staging">${STAGING}</option><option value="production"${wizard.draft?.environment === "production" ? " selected" : ""}>Let's Encrypt production certificate</option></select>`
      + `<p id="sc-ct-warning" class="setting-desc"${wizard.draft?.environment === "production" ? "" : " hidden"}>Production certificate names are published in public Certificate Transparency logs.</p>`;
  }
  function updateProductionWarning() {
    const warning = content.querySelector("#sc-ct-warning");
    const production = (content.querySelector("#sc-environment")?.value || wizard.operation?.environment) === "production";
    if (warning) warning.hidden = !production;
    for (const field of [content.querySelector("#sc-domain"), next]) {
      if (!field) continue;
      const descriptions = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== "sc-ct-warning");
      if (warning && production) descriptions.push("sc-ct-warning");
      if (descriptions.length) field.setAttribute("aria-describedby", descriptions.join(" "));
      else field.removeAttribute("aria-describedby");
    }
  }
  const running = (operation) => ["queued", "running", "issued"].includes(operation?.phase);
  function operationText(operation) {
    if (operation.cleanup_pending) return `DNS cleanup is pending. AutoDJ will retry.${operation.error ? ` ${operation.error}` : ""}`;
    if (operation.error) return operation.error;
    if (operation.phase === "ready") return "Preflight complete. Review the addresses and terms before continuing.";
    if (operation.phase === "complete") return operation.environment === "staging" ? "Staging test complete. No trusted certificate was saved." : "Certificate saved. Activate it to use HTTPS.";
    return "Checking DNS and certificate service. You can close this dialog and return to the saved setup.";
  }
  function rememberCleanup(operation) {
    if (!operation.cleanup_released) return "";
    const note = operation.cleanup_note || `Remove the TXT record at ${operation.cleanup_name || `_acme-challenge.${operation.domain}`} manually. AutoDJ record IDs: ${(operation.cleanup_record_ids || []).join(", ") || "unavailable; check its value in Cloudflare"}.`;
    cleanupNotes.add(note);
    return note;
  }
  function acmeOperation() {
    const operation = wizard.operation;
    const terms = (() => { try { const url = new URL(operation.terms_url); return url.protocol === "https:" ? url.href : null; } catch { return null; } })();
    const canIssue = ["ready", "error"].includes(operation.phase) && !operation.cleanup_pending && terms;
    return `<p><strong>${esc(operation.environment === "staging" ? STAGING : "Let's Encrypt production certificate")}</strong></p><p>${esc(operation.domain)}</p><p>${esc(operationText(operation))}</p>`
      + (operation.addresses ? `<p>Addresses observed by this host: ${operation.addresses.length ? operation.addresses.map(esc).join(", ") : "None"}.</p>` : "")
      + (operation.environment === "production" ? '<p id="sc-ct-warning">Production certificate names are published in public Certificate Transparency logs.</p>' : "")
      + (operation.next_attempt && operation.phase === "error" ? `<p>Next attempt no earlier than ${esc(new Date(operation.next_attempt * 1000).toLocaleString())}.</p>` : "")
      + (canIssue ? `<p><a href="${esc(terms)}">Read the current Let's Encrypt terms</a></p><label for="sc-terms"><input id="sc-terms" type="checkbox" required> I accept these terms</label>`
        + (!operation.confirmed ? `<p>The observed addresses could not all be matched to this host. NAT or container networking may explain this.</p><label for="sc-host-confirm"><input id="sc-host-confirm" type="checkbox" required> I have checked that devices on my network reach this host at ${esc(operation.domain)}</label>` : "") : "")
      + (operation.cleanup_pending || operation.cleanup_released ? `<p>Remove the TXT record at <code>${esc(operation.cleanup_name || `_acme-challenge.${operation.domain}`)}</code> manually if automatic cleanup is stopped. Only remove AutoDJ's record${operation.cleanup_record_ids?.length ? ` IDs: ${operation.cleanup_record_ids.map(esc).join(", ")}` : "; check its value in Cloudflare before deleting"}.</p>` : "")
      + (operation.cleanup_note ? `<p>${esc(operation.cleanup_note)}</p>` : "")
      + (operation.renewal_note ? `<p>${esc(operation.renewal_note)}</p>` : "")
      + (operation.cleanup_pending ? '<button type="button" id="sc-release-cleanup">Stop retrying DNS cleanup</button>' : "")
      + (operation.phase === "error" && !operation.certificate_id && !operation.cleanup_pending ? '<button type="button" id="sc-correct-setup">Correct setup</button>' : "")
      + '<button type="button" id="sc-check-operation">Check status</button>'
      + (!running(operation) ? '<button type="button" id="sc-remove-operation">Remove saved setup</button>' : "");
  }
  function fieldError(field, message) {
    clearFormErrors(dialog, fieldErrorNode);
    showFieldError(field, message, fieldErrorNode, null);
    field.addEventListener("input", () => {
      clearFieldError(field, fieldErrorNode);
      clearAnnouncedStatus(errorRegion);
    }, { once: true });
  }
  function validateField(field, message) {
    if (field.checkValidity()) return true;
    fieldError(field, message);
    return false;
  }
  function preflightError(error) {
    const message = error.message || String(error);
    const field = content.querySelector(/token|credential|permission/i.test(message) ? "#sc-token" : /zone/i.test(message) && !/outside|out.of/i.test(message) ? "#sc-zone" : /domain|hostname|DNS name|public|wildcard/i.test(message) ? "#sc-domain" : /name/i.test(message) ? "#sc-name" : "#sc-token");
    if (field) fieldError(field, message); else dialogError(error);
  }
  function schedulePoll() {
    win.clearTimeout(pollTimer);
    if (!wizard?.operation || !running(wizard.operation) || Date.now() >= pollDeadline) return;
    const opening = wizard;
    pollTimer = win.setTimeout(() => {
      if (wizard === opening && panel.isConnected && dialog.open) void checkOperation(false);
    }, 3000);
  }
  async function acceptOperation(operation, { focus = false, announce = false } = {}) {
    const opening = wizard;
    const epoch = captureAuthenticatedRequestEpoch();
    const focused = doc.activeElement?.id;
    const changed = JSON.stringify(wizard.operation) !== JSON.stringify(operation)
      || (operation.certificate_id && !operation.cleanup_pending && operation.certificate_id !== wizard.id);
    wizard.operation = operation;
    if (operation.certificate_id && !operation.cleanup_pending) {
      wizard.id = operation.certificate_id;
      await refresh();
      if (wizard !== opening || !isAuthenticatedRequestCurrent(epoch)) return;
    }
    if (changed || focus) {
      renderWizard();
      if (focus) content.querySelector("#sc-step-heading")?.focus();
      else if (focused) (dialog.querySelector(`#${focused}`) || content.querySelector("#sc-step-heading"))?.focus();
    }
    if (announce || focus) sayDialog(operationText(operation), operation.error || operation.cleanup_pending ? "error" : "info");
    schedulePoll();
  }
  async function checkOperation(manual = true) {
    if (!wizard?.operation) return;
    if (busy) { if (!manual) schedulePoll(); return; }
    const opening = wizard, previous = wizard.operation, epoch = captureAuthenticatedRequestEpoch();
    if (manual) pollDeadline = Date.now() + 15 * 60 * 1000;
    try {
      const result = await api(`${BASE}/acme/${encodeURIComponent(opening.operation.id)}`);
      if (!isAuthenticatedRequestCurrent(epoch)) { closeWizard(); return; }
      if (wizard !== opening || opening.operation !== previous || !panel.isConnected) return;
      await acceptOperation(result, { announce: manual || result.phase !== opening.operation.phase });
    } catch (error) { if (wizard === opening && isAuthenticatedRequestCurrent(epoch)) dialogError(error); }
  }
  async function submitAcme() {
    const operation = wizard.operation;
    let path, body;
    if (operation) {
      if (running(operation) || operation.cleanup_pending || operation.phase === "complete") return;
      if (operation.next_attempt * 1000 > Date.now()) { dialogError("Wait until the saved retry time before trying again."); return; }
      const terms = content.querySelector("#sc-terms"), host = content.querySelector("#sc-host-confirm");
      if (!terms || !validateField(terms, "Accept the current terms before continuing.") || (host && !validateField(host, "Confirm that devices reach this host at this domain."))) return;
      path = `${BASE}/acme/${encodeURIComponent(operation.id)}/issue`;
      body = { terms_url: operation.terms_url, accept_terms: terms.checked, host_confirmed: host?.checked || Boolean(operation.confirmed) };
    } else {
      for (const [id, message] of [["sc-name", "Enter a certificate name."], ["sc-domain", "Enter one public DNS name."], ["sc-zone", "Enter the 32-character Cloudflare zone ID."], ["sc-token", "Enter the Cloudflare API token."]]) {
        if (!validateField(content.querySelector(`#${id}`), message)) return;
      }
      path = BASE + "/acme/preflight";
      body = { name: content.querySelector("#sc-name").value.trim(), domain: content.querySelector("#sc-domain").value.trim(), zone_id: content.querySelector("#sc-zone").value.trim(), api_token: content.querySelector("#sc-token").value, environment: content.querySelector("#sc-environment").value };
      wizard.draft = { name: body.name, domain: body.domain, zone_id: body.zone_id, environment: body.environment };
      content.querySelector("#sc-token").value = "";
    }
    const opening = wizard, epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    clearAnnouncedStatus(errorRegion);
    try {
      const result = await api(path, "POST", body);
      if (!isAuthenticatedRequestCurrent(epoch) || wizard !== opening) return;
      pollDeadline = Date.now() + 15 * 60 * 1000;
      await acceptOperation(result, { focus: true });
      await refresh();
    } catch (error) {
      if (wizard === opening && isAuthenticatedRequestCurrent(epoch)) {
        if (!operation) preflightError(error); else dialogError(error);
      }
    }
    finally { setBusy(false); if (!isAuthenticatedRequestCurrent(epoch)) closeWizard(); }
  }
  async function correctSetup() {
    if (busy || !wizard?.operation) return;
    const opening = wizard, operation = wizard.operation;
    if (running(operation) || operation.certificate_id || operation.cleanup_pending) return;
    if (!await confirmAction(doc, { title: "Correct this setup?", message: "Remove this failed setup and edit its details. Enter the DNS token again; it is never shown from saved settings.", confirmLabel: "Correct setup" })) return;
    if (wizard !== opening) return;
    const epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    try {
      await api(`${BASE}/acme/${encodeURIComponent(operation.id)}`, "DELETE");
      if (!isAuthenticatedRequestCurrent(epoch) || wizard !== opening) return;
      wizard.draft = { ...wizard.draft, name: operation.name, domain: operation.domain, environment: operation.environment };
      wizard.cleanupNote = rememberCleanup(operation);
      wizard.operation = null;
      renderWizard();
      preflightError(operation.error || "Correct the setup details and check again.");
      await refresh();
    } catch (error) { if (wizard === opening && isAuthenticatedRequestCurrent(epoch)) dialogError(error); }
    finally { setBusy(false); if (!isAuthenticatedRequestCurrent(epoch)) closeWizard(); }
  }
  async function changeOperation(release) {
    if (busy || !wizard?.operation) return;
    const opening = wizard, operation = wizard.operation;
    if (!await confirmAction(doc, { title: release ? "Stop retrying DNS cleanup?" : "Remove saved setup?", message: release ? "AutoDJ will stop cleanup retries. Remove its TXT record manually in Cloudflare. This does not delete unrelated DNS records." : "Remove this setup and its unneeded saved credentials. Existing certificates stay in the inventory.", confirmLabel: release ? "Stop retrying" : "Remove" })) return;
    if (wizard !== opening) return;
    const epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    try {
      const result = await api(`${BASE}/acme/${encodeURIComponent(operation.id)}${release ? "/release-cleanup" : ""}`, release ? "POST" : "DELETE", release ? { confirm: true } : undefined);
      if (!isAuthenticatedRequestCurrent(epoch) || wizard !== opening) return;
      if (release) await acceptOperation(result, { focus: true });
      else { rememberCleanup(operation); setBusy(false); closeWizard(); }
      await refresh();
      if (!release) {
        panel.querySelector("#sc-add")?.focus(); say("Saved setup removed.");
      }
    } catch (error) { if (wizard === opening && isAuthenticatedRequestCurrent(epoch)) dialogError(error); }
    finally { setBusy(false); if (!isAuthenticatedRequestCurrent(epoch)) closeWizard(); }
  }
  function certificateStep() {
    const cert = selected();
    if (wizard.kind === "add" && !wizard.id) {
      if (wizard.operation) return acmeOperation();
      return '<p>Create a certificate for this host. Save the public copy for your recording device.</p>'
        + `<label for="sc-method">Certificate source</label><select id="sc-method"><option value="create"${wizard.method === "create" ? " selected" : ""}>Create a self-signed certificate</option><option value="import"${wizard.method === "import" ? " selected" : ""}>Import a certificate</option>${inventory.acme_available ? `<option value="acme"${wizard.method === "acme" ? " selected" : ""}>Get a certificate from Let’s Encrypt</option>` : ""}</select>`
        + `<label for="sc-name">Certificate name</label><input id="sc-name" maxlength="120" required value="${esc(wizard.draft?.name || "AutoDJ certificate")}">`
        + (wizard.method === "acme" ? acmeForm() + (wizard.cleanupNote ? `<p>${esc(wizard.cleanupNote)}</p>` : "") :
        `<label for="sc-hosts">Host names and addresses</label><input id="sc-hosts" aria-describedby="sc-hosts-help" value="${esc((inventory.hosts || []).join(", "))}"><p id="sc-hosts-help" class="setting-desc">Separate addresses with commas.</p>`
        + `<details${wizard.method === "import" ? " open" : ""}><summary>Import a certificate</summary><label for="sc-cert">Certificate (PEM)</label><textarea id="sc-cert" rows="4" autocomplete="off" spellcheck="false"></textarea><label for="sc-key">Private key (PEM)</label><textarea id="sc-key" rows="4" autocomplete="off" spellcheck="false"></textarea></details>`);
    }
    if (!cert) return `<p>Start on the computer or server running AutoDJ.</p><ol><li>On the host, open <code>http://127.0.0.1:${esc(win.location.port || "8080")}</code>.</li><li>Go to <strong>Settings → Server → Secure connections (HTTPS)</strong>.</li><li>Create the certificate and save the public copy.</li></ol><details><summary>Using a NAS or a server without a browser?</summary><p>Set up AutoDJ’s certificate through a trusted terminal or admin connection. Use that connection to copy the public certificate to your recording device.</p><a href="https://github.com/blindndangerous/AutoDJ/blob/master/docs/guides/operations/network.md#https-on-your-home-network">Host setup guide</a></details>`;
    let text = `<p><strong>${esc(cert.name)}</strong></p>`;
    if (cert.type === "Let's Encrypt") text += `<p>Use HTTPS at <strong>${esc(cert.hosts[0])}</strong> on your recording devices. This certificate does not cover localhost. No certificate installation is needed on those devices.</p>`;
    text += inventory.local || inventory.https ? `<p>${download(cert)}</p>`
      : '<p>Get the public certificate from the AutoDJ host.</p><p>Transfer the public certificate using a USB drive or a trusted connection to the host. Keep the private key on the host.</p>';
    if (!cert.active && inventory.can_manage) {
      if (!inventory.https) text += '<p>Restart AutoDJ to start serving HTTPS.</p>';
      text += '<button type="button" id="sc-activate">Activate certificate</button>';
    }
    if (inventory.restart_required) text += '<p>Restart AutoDJ to start serving HTTPS.</p>';
    return text + restartControl() + (wizard.operation ? acmeOperation() : "");
  }
  function trustGuide() {
    const cert = selected();
    if (cert?.type === "Let's Encrypt") return '<p>Open AutoDJ using its certificate domain over HTTPS. Your browser checks trust automatically; do not install this certificate as a trusted root.</p>' + (inventory.restart_required ? '<p>Restart AutoDJ to start serving HTTPS.</p>' : '') + restartControl() + `<ul>${httpsLinks()}</ul>`;
    return (cert && (inventory.local || inventory.https) ? `<p>${download(cert)}</p>` : '')
      + `<label for="sc-platform">Recording device</label><select id="sc-platform">${[["windows", "Windows"], ["mac", "macOS"], ["linux", "Linux"], ["other", "Other / managed device"]].map(([value, label]) => `<option value="${value}"${wizard.platform === value ? " selected" : ""}>${label}</option>`).join("")}</select><div id="sc-trust-instructions">${instructions[wizard.platform]}</div>`
      + (inventory.restart_required ? '<p>Restart AutoDJ to start serving HTTPS.</p>' : '<p>Open AutoDJ over HTTPS on the recording device to finish setup.</p>')
      + restartControl() + `<ul>${httpsLinks()}</ul>`;
  }
  function renderWizard() {
    clearFormErrors(dialog, fieldErrorNode);
    if (!wizard) return;
    const complete = wizard.step === 3;
    const stepTitle = wizard.step === 1 ? (wizard.kind === "remote" ? "Get the server certificate" : "Create the certificate") : "Trust the certificate";
    dialog.querySelector("#sc-dialog-title").textContent = wizard.kind === "add" ? "Set up HTTPS" : "Set up a remote device";
    const trust = trustedBrowser() && !inventory.restart_required ? '<p>This browser is ready to record.</p><details><summary>Set up another recording device</summary>' + trustGuide() + '</details>' : trustGuide();
    content.innerHTML = complete ? '<h3 id="sc-step-heading" tabindex="-1">You’re all set</h3><p>Go to Voice tracks to start recording. Allow microphone access when your browser asks.</p>'
      : `<h3 id="sc-step-heading" tabindex="-1">Step ${wizard.step} of 2</h3><ol class="secure-steps"><li${wizard.step === 1 ? ' aria-current="step"' : ''}>Certificate</li><li${wizard.step === 2 ? ' aria-current="step"' : ''}>Trust</li></ol><h3>${stepTitle}</h3>${wizard.step === 1 ? certificateStep() : trust}`;
    back.hidden = wizard.step !== 2;
    back.textContent = wizard.kind === "remote" ? "Previous: get the certificate" : "Previous: create the certificate";
    next.hidden = complete || (wizard.step === 1 && !selected() && wizard.kind !== "add");
    next.textContent = wizard.step === 1 ? (wizard.kind === "add" && !wizard.id ? "Save certificate" : "Next: trust the certificate") : "Finish setup";
    if (wizard.step === 1 && wizard.method === "acme" && (!wizard.id || wizard.operation?.phase === "error")) {
      next.textContent = wizard.operation ? (wizard.operation.phase === "error" ? "Retry issuance" : wizard.operation.environment === "staging" ? "Run staging test" : "Get production certificate") : "Check domain and terms";
      next.hidden = Boolean(wizard.operation && (running(wizard.operation) || wizard.operation.phase === "complete" || wizard.operation.cleanup_pending || !wizard.operation.terms_url));
    }
    next.setAttribute("aria-disabled", String(busy || (wizard.step === 2 && (!trustedBrowser() || inventory.restart_required))));
    cancel.textContent = complete ? "Close" : "Cancel";
    content.querySelector("#sc-method")?.addEventListener("change", (event) => {
      if (busy) { event.target.value = wizard.method; return; }
      wizard.method = event.target.value;
      const name = content.querySelector("#sc-name").value;
      renderWizard();
      content.querySelector("#sc-name").value = name;
      content.querySelector("#sc-method").focus();
    });
    updateProductionWarning();
    content.querySelector("#sc-environment")?.addEventListener("change", updateProductionWarning);
    content.querySelector("#sc-check-operation")?.addEventListener("click", () => void checkOperation());
    content.querySelector("#sc-correct-setup")?.addEventListener("click", () => void correctSetup());
    content.querySelector("#sc-release-cleanup")?.addEventListener("click", () => void changeOperation(true));
    content.querySelector("#sc-remove-operation")?.addEventListener("click", () => void changeOperation(false));
    content.querySelector("#sc-platform")?.addEventListener("change", (event) => {
      wizard.platform = event.target.value;
      content.querySelector("#sc-trust-instructions").innerHTML = instructions[wizard.platform];
    });
    content.querySelector("#sc-activate")?.addEventListener("click", () => void activate(selected()));
    content.querySelector("[data-https-restart]")?.addEventListener("click", restartNow);
  }
  async function advance() {
    if (busy || !wizard) return;
    if (wizard.step === 1 && wizard.method === "acme" && (!wizard.id || wizard.operation?.phase === "error")) { await submitAcme(); return; }
    if (wizard.step === 2) {
      if (!trustedBrowser() || inventory.restart_required) { dialogError(selected()?.type === "Let's Encrypt" ? `Open AutoDJ over HTTPS at ${selected().hosts[0]} on the recording device to finish setup.` : "Open AutoDJ over HTTPS on the recording device to finish setup."); return; }
      wizard.step = 3;
    } else if (wizard.kind === "add" && !wizard.id) {
      const name = content.querySelector("#sc-name");
      if (!name.reportValidity()) return;
      if (wizard.method === "import") {
        for (const [id, message] of [["sc-cert", "Enter the certificate in PEM format."], ["sc-key", "Enter the matching private key in PEM format."]]) {
          const field = content.querySelector(`#${id}`);
          field.required = true;
          if (!validateField(field, message)) return;
        }
      }
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
      finally { setBusy(false); if (!isAuthenticatedRequestCurrent(epoch)) closeWizard(); }
    } else {
      if (wizard.id && !selected()?.active) { dialogError("Activate this certificate before continuing."); return; }
      wizard.step = 2;
    }
    renderWizard();
    content.querySelector("#sc-step-heading").focus();
  }
  async function activate(cert) {
    if (busy || !cert) return;
    const hostname = cert.type === "Let's Encrypt" ? cert.hosts[0] : null;
    if (!await confirmAction(doc, { title: `Activate ${cert.name}?`, message: hostname ? `AutoDJ will use this certificate for new connections. Open HTTPS at ${hostname}; localhost is not covered. ${inventory.https ? "" : "Restart AutoDJ to start serving HTTPS."}` : "AutoDJ will use this certificate for new connections.", confirmLabel: "Activate" })) return;
    const epoch = captureAuthenticatedRequestEpoch();
    setBusy(true);
    try {
      const result = await api(`${BASE}/${encodeURIComponent(cert.id)}/activate${hostname ? `?hostname=${encodeURIComponent(hostname)}` : ""}`, "POST");
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      if (hostname && Array.isArray(result?.certificates)) {
        ++refreshSequence;
        inventory = result;
        restartAvailable = Boolean(result.restart_required && updates && await updates.load() && updates.canRestart());
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        render();
      } else await refresh();
      (dialog.open ? dialogError : say)(inventory.restart_required ? "Restart AutoDJ to start serving HTTPS." : "Certificate activated.");
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
      return `<tr data-row="${esc(cert.id)}"><td role="rowheader"><strong>${esc(cert.name)}</strong><small>${cert.hosts.map(esc).join(" · ")}</small><small>Issued ${exactDate(cert.issued)} · ${esc(cert.type)}</small>${cert.warning ? `<small>${esc(cert.warning)}</small>` : ""}</td><td role="gridcell">${cert.active ? "Active" : expired ? "Expired" : cert.status === "invalid" ? "Unavailable" : "Saved"}</td><td role="gridcell">${expiry(cert.expires)}</td><td role="gridcell">${manageable && !expired && cert.status !== "invalid" ? `<button type="button" data-activate="${esc(cert.id)}">Activate</button>` : "—"}</td><td role="gridcell">${manageable ? `<button type="button" data-delete="${esc(cert.id)}">Delete</button>` : "—"}</td></tr>`;
    }).join("");
    panel.innerHTML = `<h4 id="secure-connections-heading" tabindex="-1">Secure connections (HTTPS)</h4><h5 id="sc-table-heading">Server certificate</h5>${rows ? `<div class="data-table-scroll"><table role="grid" aria-labelledby="sc-table-heading"><thead><tr><th scope="col">Certificate</th><th scope="col">Status</th><th scope="col">Expiry</th><th scope="col">Activate</th><th scope="col">Delete</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p>Create a certificate on the AutoDJ host, then trust it on your recording device.</p>'}${data.can_manage ? '<button type="button" id="sc-add">Add certificate</button>' : '<p>Manage certificates at localhost on the AutoDJ host.</p>'} <button type="button" id="sc-remote">Set up a remote device</button>${(!data.https && data.can_manage) || data.restart_required ? '<p>Restart AutoDJ to start serving HTTPS.</p>' : ''}${ready() ? '<p>This browser is ready to record.</p>' : ''}<details><summary>How to remove trust on this device</summary><p>If you installed the certificate on this device, open its certificate settings, select the AutoDJ certificate, and remove trust.</p></details>`;
    panel.querySelector("#sc-add")?.addEventListener("click", (event) => openWizard("add", event.currentTarget));
    for (const cert of data.certificates) {
      if (!cert.renewal) continue;
      const cell = [...panel.querySelectorAll("[data-row]")].find((row) => row.dataset.row === cert.id)?.children[1];
      if (!cell) continue;
      const note = doc.createElement("small");
      note.textContent = [cert.renewal.renewal_note || (cert.active ? "Renews automatically while active." : "Automatic renewal starts when activated."), cert.renewal.error, cert.renewal.cleanup_pending ? "DNS cleanup pending." : cert.renewal.cleanup_note].filter(Boolean).join(" ");
      cell.append(note);
      if (data.can_manage && cert.renewal.id) {
        const button = doc.createElement("button");
        button.type = "button";
        button.textContent = "Renewal details";
        button.dataset.acmeOperation = cert.renewal.id;
        button.addEventListener("click", () => resumeOperation(cert.renewal, button));
        cell.append(button);
      }
    }
    if (data.can_manage) {
      const operations = (data.acme_operations || []).filter((operation) => !data.certificates.some((cert) => cert.renewal?.id === operation.id));
      if (operations.length) {
        const list = doc.createElement("ul");
        list.setAttribute("aria-label", "Saved certificate setups");
        for (const operation of operations) {
          const item = doc.createElement("li"), button = doc.createElement("button");
          button.type = "button";
          button.dataset.acmeOperation = operation.id;
          button.textContent = `${operation.name}: ${operation.environment === "staging" ? STAGING : "Let's Encrypt"} — ${operationText(operation)}`;
          button.addEventListener("click", () => resumeOperation(operation, button));
          item.append(button); list.append(item);
        }
        panel.append(list);
      }
    }
    panel.querySelector("#sc-remote").addEventListener("click", (event) => openWizard("remote", event.currentTarget));
    const restartMarkup = restartControl();
    if (restartMarkup) {
      panel.querySelector("#sc-remote").insertAdjacentHTML("afterend", restartMarkup);
      panel.querySelector("[data-https-restart]").addEventListener("click", restartNow);
    }
    for (const button of panel.querySelectorAll("[data-activate]")) button.addEventListener("click", () => void activate(data.certificates.find((cert) => cert.id === button.dataset.activate)));
    for (const button of panel.querySelectorAll("[data-delete]")) button.addEventListener("click", () => void remove(data.certificates.find((cert) => cert.id === button.dataset.delete)));
    installTableNavigation(panel.querySelector("tbody"));
    if (active()?.type === "Let's Encrypt") panel.insertAdjacentHTML("beforeend", `<p>Open AutoDJ using its certificate domain:</p><ul>${httpsLinks()}</ul>`);
    for (const text of cleanupNotes) {
      const note = doc.createElement("p");
      note.textContent = text;
      panel.append(note);
    }
  }
  function resumeOperation(operation, trigger) {
    if (busy || dialog.open) return;
    openWizard("add", trigger);
    wizard.method = "acme";
    wizard.operation = operation;
    renderWizard();
    cancel.focus();
    pollDeadline = Date.now() + 15 * 60 * 1000;
    void checkOperation(false);
  }
  async function refresh() {
    const sequence = ++refreshSequence;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const data = await api(BASE);
      if (!isAuthenticatedRequestCurrent(epoch) || sequence !== refreshSequence) return;
      restartAvailable = Boolean(data.restart_required && updates && await updates.load() && updates.canRestart());
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
  onCardOpen(serverPanel, refresh);
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
