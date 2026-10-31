// @vitest-environment-options {"url":"https://autodj.example.test/"}
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({ confirmAction: vi.fn(async () => true) }));
import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";
import * as liveRegion from "../../src/autodj/static/modules/live-region.js";
import { installSecureConnections } from "../../src/autodj/static/modules/secure-connections.js";

const BASE = "/api/server/certificates";
const task = (props = {}) => ({ id: "a".repeat(32), name: "Radio", domain: "radio.example.org", environment: "staging", phase: "ready", addresses: ["203.0.113.8"], confirmed: false, terms_url: "https://letsencrypt.org/terms", ...props });
const inventory = (props = {}) => ({ certificates: [], can_manage: true, acme_available: true, local: true, https: true, hosts: ["localhost", "radio.example.org"], urls: [], ...props });
const button = (id) => document.getElementById(id);
async function mount(handler = async () => inventory(), updates = null) {
  document.body.innerHTML = '<section id="secure-connections"></section><p id="secure-connections-status"></p>';
  const request = vi.fn(handler);
  installSecureConnections({ request, updates });
  await vi.waitFor(() => expect(button("sc-add")).toBeTruthy());
  button("sc-add").click();
  return request;
}
function chooseAcme() {
  const control = button("sc-method");
  control.value = "acme";
  control.dispatchEvent(new Event("change"));
}
function fill() {
  button("sc-domain").value = "radio.example.org";
  button("sc-zone").value = "b".repeat(32);
  button("sc-token").value = "secret-dns-token";
}
async function preflight(requestHandler = null) {
  const request = await mount(requestHandler || (async (path) => path === BASE ? inventory() : task()));
  chooseAcme(); fill(); button("sc-next").click();
  await vi.waitFor(() => expect(button("sc-terms")).toBeTruthy());
  return request;
}
describe("ACME secure connections", () => {
  beforeEach(() => { Object.defineProperty(window, "isSecureContext", { configurable: true, value: true }); confirmAction.mockClear(); });
  afterEach(() => { button("sc-cancel")?.click(); document.body.innerHTML = ""; vi.useRealTimers(); vi.restoreAllMocks(); });

  // ADR-0019-16
  it("offers ACME third in the existing wizard with staging first and private native inputs", async () => {
    await mount();
    expect([...button("sc-method").options].map((o) => o.value)).toEqual(["create", "import", "acme"]);
    chooseAcme();
    expect(document.querySelector("dialog").textContent).toContain("Step 1 of 2");
    expect(button("sc-environment").value).toBe("staging");
    expect(button("sc-environment").options[0].textContent).toBe("Let's Encrypt staging test — not trusted");
    expect(button("sc-token").type).toBe("password");
    expect(button("sc-domain").labels[0].textContent).toBe("Domain");
    expect(document.activeElement).toBe(button("sc-method"));
  });

  it("focuses an invalid field and prevents preflight until its required inputs are valid", async () => {
    const request = await mount(); chooseAcme(); button("sc-next").click();
    expect(button("sc-domain").getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(button("sc-domain"));
    expect(request.mock.calls.filter(([, o]) => o.method === "POST")).toHaveLength(0);
  });

  // ADR-0019-16.
  it("associates zone format and token permissions with their inputs", async () => {
    await mount(); chooseAcme();
    for (const [id, text] of [["sc-zone", "32"], ["sc-token", "DNS Edit and Zone Read"]]) {
      const descriptions = (button(id).getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      expect(descriptions.length).toBeGreaterThan(0);
      expect(descriptions.map((description) => button(description)?.textContent).join(" ")).toContain(text);
    }
  });

  it("associates the production disclosure with the domain and action only while applicable", async () => {
    await mount(); chooseAcme();
    const describedBy = (id) => (button(id).getAttribute("aria-describedby") || "").split(/\s+/);
    expect(describedBy("sc-domain")).not.toContain("sc-ct-warning");
    expect(describedBy("sc-next")).not.toContain("sc-ct-warning");
    button("sc-environment").value = "production";
    button("sc-environment").dispatchEvent(new Event("change"));
    expect(button("sc-ct-warning").hidden).toBe(false);
    expect(describedBy("sc-domain")).toContain("sc-domain-help");
    expect(describedBy("sc-domain")).toContain("sc-ct-warning");
    expect(describedBy("sc-next")).toContain("sc-ct-warning");
    button("sc-environment").value = "staging";
    button("sc-environment").dispatchEvent(new Event("change"));
    expect(button("sc-ct-warning").hidden).toBe(true);
    expect(describedBy("sc-domain")).not.toContain("sc-ct-warning");
    expect(describedBy("sc-next")).not.toContain("sc-ct-warning");
  });

  it("keeps the production disclosure associated with the issuance action after preflight", async () => {
    await preflight(async (path) => path === BASE ? inventory() : task({ environment: "production", confirmed: true }));
    expect(button("sc-next").textContent).toBe("Get production certificate");
    expect((button("sc-next").getAttribute("aria-describedby") || "").split(/\s+/)).toContain("sc-ct-warning");
    expect(button("sc-ct-warning")?.textContent).toContain("public Certificate Transparency logs");
  });

  // ADR-0019-16.
  it("announces successful preflight exactly once while retaining heading focus", async () => {
    const announce = vi.spyOn(liveRegion, "announceStatus");
    await preflight();
    const message = "Preflight complete. Review the addresses and terms before continuing.";
    await vi.waitFor(() => expect(button("sc-error").textContent).toBe(message));
    expect(announce.mock.calls.filter(([, text]) => text === message)).toHaveLength(1);
    expect(document.activeElement).toBe(button("sc-step-heading"));
  });

  it.each([
    ["staging", "queued", "Checking DNS and certificate service. You can close this dialog and return to the saved setup."],
    ["staging", "complete", "Staging test complete. No trusted certificate was saved."],
    ["production", "complete", "Certificate saved. Activate it to use HTTPS."],
  ])("announces %s issuance %s exactly once while retaining heading focus", async (environment, phase, message) => {
    await preflight(async (path) => path === BASE ? inventory() : task({ environment, confirmed: true, ...(path.endsWith("/issue") ? { phase } : {}) }));
    const announce = vi.spyOn(liveRegion, "announceStatus");
    button("sc-terms").checked = true;
    button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-error").textContent).toBe(message));
    expect(announce.mock.calls.filter(([, text]) => text === message)).toHaveLength(1);
    expect(document.activeElement).toBe(button("sc-step-heading"));
  });

  it("shows observed addresses and requires fresh terms and uncertain host consent before issuance", async () => {
    const request = await preflight();
    expect(document.querySelector("dialog").textContent).toContain("203.0.113.8");
    expect(button("sc-token")).toBeNull();
    expect(button("sc-terms").checked).toBe(false);
    expect(button("sc-host-confirm").checked).toBe(false);
    button("sc-next").click();
    expect(document.activeElement).toBe(button("sc-terms"));
    button("sc-terms").checked = true; button("sc-next").click();
    expect(document.activeElement).toBe(button("sc-host-confirm"));
    button("sc-host-confirm").checked = true; button("sc-next").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("/issue"))).toBe(true));
    const [, options] = request.mock.calls.find(([path]) => path.endsWith("/issue"));
    expect(JSON.parse(options.body)).toEqual({ terms_url: task().terms_url, accept_terms: true, host_confirmed: true });
    expect(document.querySelector("dialog").textContent).not.toContain("secret-dns-token");
  });

  it("warns about public Certificate Transparency for production and omits unnecessary host consent", async () => {
    await mount(async (path) => path === BASE ? inventory() : task({ environment: "production", confirmed: true }));
    chooseAcme(); fill();
    button("sc-environment").value = "production";
    button("sc-environment").dispatchEvent(new Event("change"));
    expect(button("sc-ct-warning").hidden).toBe(false);
    button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-terms")).toBeTruthy());
    expect(button("sc-host-confirm")).toBeNull();
  });

  it("drops stale preflight responses and never leaves a credential displayed", async () => {
    let resolve;
    await mount((path) => path === BASE ? Promise.resolve(inventory()) : new Promise((r) => { resolve = r; }));
    chooseAcme(); fill(); button("sc-next").click();
    expect(button("sc-token").value).toBe("");
    invalidateAuthenticatedRequestEpoch(); resolve(task());
    await vi.waitFor(() => expect(document.querySelector("dialog").open).toBe(false));
    expect(button("sc-terms")).toBeNull();
  });

  it("resumes saved cleanup and requires confirmation before releasing it with manual removal instructions", async () => {
    const operation = task({ phase: "error", cleanup_pending: true, cleanup_name: "_acme-challenge.radio.example.org", cleanup_record_ids: ["record-1"], error: "DNS cleanup failed." });
    const request = await mount(async (path) => path === BASE ? inventory({ acme_operations: [operation] }) : path.endsWith("release-cleanup") ? { ...operation, cleanup_pending: false, cleanup_released: true } : operation);
    button("sc-cancel").click(); document.querySelector("[data-acme-operation]").click();
    await vi.waitFor(() => expect(button("sc-release-cleanup")).toBeTruthy());
    button("sc-release-cleanup").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([p]) => p.endsWith("release-cleanup"))).toBe(true));
    expect(confirmAction).toHaveBeenCalled();
    expect(document.querySelector("dialog").textContent).toContain("record-1");
    expect(document.querySelector("dialog").textContent).toContain("Remove the TXT record");
  });

  it("activates production with an explicit query hostname and explains trusted-domain handoff", async () => {
    const row = { id: "saved", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid", active: false };
    const request = await mount(async () => inventory({ certificates: [row] }));
    button("sc-cancel").click(); document.querySelector("[data-activate]").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([p]) => p === `${BASE}/saved/activate?hostname=radio.example.org`)).toBe(true));
    const [, options] = request.mock.calls.find(([p]) => p.includes("/activate"));
    expect(options.body).toBeUndefined();
    expect(confirmAction.mock.calls[0][1].message).toContain("radio.example.org");
  });

  it("polls without repeating unchanged status, preserves focus, and stops after close", async () => {
    let operation = task({ phase: "queued" });
    const request = await mount(async (path) => path === BASE ? inventory() : operation);
    vi.useFakeTimers(); chooseAcme(); fill(); button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-check-operation")).toBeTruthy());
    const announce = vi.spyOn(liveRegion, "announceStatus");
    button("sc-cancel").focus();
    await vi.advanceTimersByTimeAsync(3000);
    expect(document.activeElement).toBe(button("sc-cancel"));
    expect(button("sc-error").textContent).toBe("Checking DNS and certificate service. You can close this dialog and return to the saved setup.");
    expect(announce).not.toHaveBeenCalled();
    operation = task();
    await vi.advanceTimersByTimeAsync(3000);
    expect(button("sc-error").textContent).toContain("Preflight complete");
    expect(document.activeElement).toBe(button("sc-cancel"));
    button("sc-cancel").click();
    const calls = request.mock.calls.length;
    await vi.advanceTimersByTimeAsync(15000);
    expect(request).toHaveBeenCalledTimes(calls);
  });

  it("keeps staging completion out of activation and respects the saved retry time", async () => {
    let operation = task({ phase: "error", next_attempt: Date.now() / 1000 + 900, error: "Try later." });
    const request = await preflight(async (path) => path === BASE ? inventory() : operation);
    button("sc-terms").checked = true; button("sc-host-confirm").checked = true;
    button("sc-next").click();
    expect(request.mock.calls.some(([path]) => path.endsWith("/issue"))).toBe(false);
    operation = task({ phase: "complete" });
    button("sc-check-operation").click();
    await vi.waitFor(() => expect(document.querySelector("dialog").textContent).toContain("No trusted certificate was saved"));
    expect(button("sc-next").hidden).toBe(true);
    expect(button("sc-activate")).toBeNull();
  });

  it("requires the certificate domain before finishing production trust and never offers root installation", async () => {
    const row = { id: "saved", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid", active: true };
    await mount(async () => inventory({ certificates: [row], urls: ["https://radio.example.org:8080"] }));
    button("sc-cancel").click(); button("sc-remote").click(); button("sc-next").click();
    expect(document.querySelector("dialog").textContent).toContain("do not install this certificate as a trusted root");
    expect(button("sc-platform")).toBeNull();
    button("sc-next").click();
    expect(document.querySelector("dialog").textContent).not.toContain("You’re all set");
    await vi.waitFor(() => expect(button("sc-error").textContent).toContain("radio.example.org"));
  });

  it("issues production without activating it and restarts into its explicit domain", async () => {
    let saved = false, activated = false;
    const row = { id: "saved", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid" };
    const updates = { load: vi.fn(async () => true), canRestart: vi.fn(() => true), restartForHttps: vi.fn() };
    const request = await mount(async (path) => {
      if (path === BASE) return inventory({ certificates: saved ? [{ ...row, active: activated }] : [], restart_required: activated });
      if (path.includes("/activate")) { activated = true; return {}; }
      if (path.endsWith("/issue")) { saved = true; return task({ phase: "complete", environment: "production", confirmed: true, certificate_id: row.id }); }
      return task({ environment: "production", confirmed: true });
    }, updates);
    chooseAcme(); fill(); button("sc-environment").value = "production"; button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-terms")).toBeTruthy());
    button("sc-terms").checked = true; button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-activate")).toBeTruthy());
    expect(activated).toBe(false);
    expect(request.mock.calls.find(([path]) => path.endsWith("/issue"))[1].body).not.toContain("api_token");
    button("sc-activate").click();
    await vi.waitFor(() => expect(document.querySelector("dialog [data-https-restart]")).toBeTruthy());
    document.querySelector("dialog [data-https-restart]").click();
    expect(updates.restartForHttps).toHaveBeenCalledWith("https://radio.example.org/?https-setup=trust#settings");
  });

  it("corrects a failed setup with non-secret fields retained and its error associated to the field", async () => {
    const operation = task({ phase: "error", terms_url: undefined, error: "The domain is outside the selected Cloudflare zone." });
    const request = await mount(async (path) => path === BASE ? inventory() : operation);
    chooseAcme(); fill(); button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-correct-setup")).toBeTruthy());
    button("sc-correct-setup").click();
    await vi.waitFor(() => expect(button("sc-domain")).toBeTruthy());
    expect(button("sc-domain").value).toBe("radio.example.org");
    expect(button("sc-zone").value).toBe("b".repeat(32));
    expect(button("sc-token").value).toBe("");
    expect(button("sc-terms")).toBeNull();
    expect(button("sc-domain").getAttribute("aria-describedby")).toBe("sc-domain-help sc-error");
    expect(document.activeElement).toBe(button("sc-domain"));
    button("sc-domain").dispatchEvent(new Event("input"));
    expect(button("sc-domain").getAttribute("aria-describedby")).toBe("sc-domain-help");
    expect(request.mock.calls.some(([, options]) => options.method === "DELETE")).toBe(true);
  });

  it("does not create a self-signed certificate when Import is selected without PEM data", async () => {
    const request = await mount();
    button("sc-method").value = "import";
    button("sc-method").dispatchEvent(new Event("change"));
    button("sc-next").click();
    expect(request.mock.calls.some(([, options]) => options.method === "POST")).toBe(false);
    expect(button("sc-cert").getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(button("sc-cert"));
  });

  it("associates a rejected preflight with its domain field while retaining its format hint", async () => {
    await mount(async (path) => {
      if (path === BASE) return inventory();
      throw new Error("Use one ordinary public DNS name; no wildcards.");
    });
    chooseAcme(); fill(); button("sc-domain").value = "station.local"; button("sc-next").click();
    await vi.waitFor(() => expect(button("sc-domain").getAttribute("aria-invalid")).toBe("true"));
    expect(button("sc-domain").getAttribute("aria-describedby")).toBe("sc-domain-help sc-error");
    expect(button("sc-token").value).toBe("");
    expect(document.activeElement).toBe(button("sc-domain"));
  });

  it("shows renewal failures beside the saved pause note and restores the renewed operation opener", async () => {
    const operation = task({ phase: "error", certificate_id: "saved", renewal_note: "Saved lineage paused.", error: "DNS provider refused the request." });
    const row = { id: "saved", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid", renewal: operation };
    await mount(async (path) => path === BASE ? inventory({ certificates: [row] }) : operation);
    button("sc-cancel").click();
    expect(document.querySelector("[data-row]").textContent).toContain(operation.error);
    document.querySelector("[data-acme-operation]").click();
    await vi.waitFor(() => expect(button("sc-activate")).toBeTruthy());
    button("sc-cancel").click();
    expect(document.activeElement).toBe(document.querySelector("[data-acme-operation]").closest("td"));
  });

  it("uses activation response for the hostname handoff without requiring another localhost request", async () => {
    const row = { id: "saved", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid", active: false };
    const request = await mount(async (path) => inventory({ certificates: [{ ...row, active: path.includes("/activate") }], urls: ["https://radio.example.org:8080"] }));
    button("sc-cancel").click(); document.querySelector("[data-activate]").click();
    await vi.waitFor(() => expect(document.querySelector("[data-activate]")).toBeNull());
    expect(request.mock.calls.filter(([path]) => path === BASE)).toHaveLength(1);
    expect(document.querySelector('a[href="https://radio.example.org:8080/?https-setup=trust#settings"]')).toBeTruthy();
  });

  it("accepts changed terms and retries renewal while the existing certificate stays active", async () => {
    const operation = task({ environment: "production", phase: "error", certificate_id: "active", confirmed: true, error: "Review and accept the current ACME terms before issuance." });
    const row = { id: "active", name: "Radio", hosts: ["radio.example.org"], type: "Let's Encrypt", issued: "2026-10-01", expires: "2030-01-01", status: "valid", active: true, renewal: operation };
    const request = await mount(async (path) => path === BASE ? inventory({ certificates: [row] }) : operation);
    button("sc-cancel").click();
    document.querySelector("[data-acme-operation]").click();
    await vi.waitFor(() => expect(button("sc-next").textContent).toBe("Retry issuance"));
    expect(button("sc-terms").checked).toBe(false);
    button("sc-terms").checked = true;
    button("sc-next").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("/issue"))).toBe(true));
    const [, options] = request.mock.calls.find(([path]) => path.endsWith("/issue"));
    expect(JSON.parse(options.body)).toEqual({ terms_url: operation.terms_url, accept_terms: true, host_confirmed: true });
  });
});
