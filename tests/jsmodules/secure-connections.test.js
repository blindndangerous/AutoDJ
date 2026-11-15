// @vitest-environment-options {"url":"https://autodj.example.test/"}
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({
  confirmAction: vi.fn(async () => true),
}));

import { confirmAction } from "../../src/autodj/static/modules/confirm-dialog.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";
import { installSecureConnections } from "../../src/autodj/static/modules/secure-connections.js";

const cert = (id, props = {}) => ({
  id, name: `Certificate ${id}`, hosts: [`${id}.autodj.local`], issued: "2026-10-01T00:00:00Z",
  expires: "2027-10-01T00:00:00Z", type: "Self-signed", active: false, managed: true, status: "valid", ...props,
});
const inventory = (certificates = [], props = {}) => ({
  certificates, can_manage: true, local: true, https: true, urls: ["https://autodj.local:8080"],
  hosts: ["autodj.local", "192.168.1.20"], restart_required: false, ...props,
});
function mount() {
  document.body.innerHTML = `
    <button id="tab-settings">Settings</button><button id="stab-server">Server</button>
    <section id="spanel-server"><section id="secure-connections"></section></section>
    <p id="secure-connections-status"></p><p id="voice-https-help"></p><button id="voice-https-setup">Set up HTTPS</button>`;
  const nativeCreate = document.createElement.bind(document);
  const dialog = nativeCreate("dialog");
  dialog.show = () => dialog.setAttribute("open", "");
  dialog.showModal = dialog.show;
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.removeAttribute("open");
    setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
  };
  return { panel: document.getElementById("secure-connections"), dialog };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

describe("secure connections", () => {
  beforeEach(() => {
    confirmAction.mockClear();
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
  });
  afterEach(() => { document.body.innerHTML = ""; vi.restoreAllMocks(); });

  it("offers Restart now only when supported and hands off to the shared restart flow", async () => {
    const { panel } = mount();
    const updates = { load: vi.fn(async () => true), canRestart: vi.fn(() => true), restartForHttps: vi.fn() };
    const request = vi.fn(async () => inventory([cert("active", { active: true })], { restart_required: true }));
    const controller = installSecureConnections({ panel, request, updates });
    await vi.waitFor(() => expect(panel.querySelector("[data-https-restart]")).toBeTruthy());
    panel.querySelector("[data-https-restart]").click();
    expect(updates.restartForHttps).toHaveBeenCalledWith("https://autodj.example.test/?https-setup=trust#settings");
    updates.canRestart.mockReturnValue(false);
    await controller.refresh();
    expect(panel.querySelector("[data-https-restart]")).toBeNull();
  });

  it("renders active, expired, and external rows with status and exact plus relative expiry", async () => {
    const { panel } = mount();
    const request = vi.fn(async () => inventory([
      cert("active", { active: true, managed: false }),
      cert("expired", { expires: "2020-01-01T00:00:00Z" }),
      cert("external", { managed: false }),
    ]));
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelectorAll("tbody tr")).toHaveLength(3));
    const rows = [...panel.querySelectorAll("tbody tr")];
    expect(rows[0].textContent).toContain("Active");
    expect(rows[1].textContent).toContain("Expired");
    expect(rows[1].querySelector("[data-activate]")).toBeNull();
    expect(rows[2].querySelector("[data-activate]")).toBeNull();
    expect(rows[2].querySelector("[data-delete]")).toBeNull();
    expect(rows[0].textContent).toContain("Issued");
    expect(rows[0].textContent).toMatch(/Expires .*\(.*about/);
  });

  it.each(["expired", "not yet valid"])("warns about an active %s certificate while keeping renewal controls available", async (reason) => {
    const { panel } = mount();
    const warning = `The HTTPS certificate is ${reason}. HTTPS remains enabled; renew the certificate.`;
    const request = vi.fn(async () => inventory([cert("active", { active: true, warning })]));
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.textContent).toContain(warning));
    expect(panel.textContent).toContain("Active");
    expect(panel.querySelector("#sc-add")).toBeTruthy();
  });

  it("creates, activates, and finishes only after this browser is secure", async () => {
    const { panel } = mount();
    let saved = false, activated = false;
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/api/server/certificates" && options.method === "POST") { saved = true; return { id: "new" }; }
      if (path.endsWith("/new/activate")) { activated = true; return {}; }
      if (path === "/api/server/certificates") return inventory(saved ? [cert("new", { active: activated })] : []);
      throw new Error(`Unexpected ${options.method || "GET"} ${path}`);
    });
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-add")).toBeTruthy());
    panel.querySelector("#sc-add").click();
    const dialog = document.querySelector(".secure-connection-dialog");
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain("Step 1 of 2");
    expect(dialog.querySelector("[aria-current='step']").textContent).toContain("Certificate");
    dialog.querySelector("#sc-next").click();
    await vi.waitFor(() => expect(saved).toBe(true));
    await vi.waitFor(() => expect(dialog.querySelector("#sc-activate")).toBeTruthy());
    expect(dialog.querySelector("a[download]")).toBeTruthy();
    dialog.querySelector("#sc-activate").click();
    await vi.waitFor(() => expect(activated).toBe(true));
    await vi.waitFor(() => expect(dialog.querySelector("#sc-activate")).toBeNull());
    dialog.querySelector("#sc-next").click();
    expect(dialog.textContent).toContain("Step 2 of 2");
    expect(dialog.querySelector("[aria-current='step']").textContent).toContain("Trust");

    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    dialog.querySelector("#sc-next").click();
    expect(dialog.textContent).toContain("Open AutoDJ over HTTPS");
    expect(dialog.textContent).not.toContain("You’re all set");
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    dialog.querySelector("#sc-next").click();
    expect(dialog.textContent).toContain("You’re all set");
    expect(dialog.textContent).toContain("Go to Voice tracks");
    dialog.querySelector("#sc-cancel").click();
    expect(document.activeElement).toBe(panel.querySelector("#sc-add"));
  });


  it("validates the certificate name on Next without native missing-value state", async () => {
    const { panel } = mount();
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/api/server/certificates" && options.method === "POST") return { id: "new" };
      return inventory([]);
    });
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-add")).toBeTruthy());
    panel.querySelector("#sc-add").click();
    const dialog = document.querySelector(".secure-connection-dialog");
    const name = dialog.querySelector("#sc-name");
    name.value = "";
    expect(name.hasAttribute("required")).toBe(false);
    expect(name.validity.valueMissing).toBe(false);
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(name);
    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    name.value = "AutoDJ certificate";
    name.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.querySelector("#sc-next").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([path, options]) => path === "/api/server/certificates" && options?.method === "POST")).toBe(true));
  });

  it("validates import fields and ACME consent when Next is pressed", async () => {
    const { panel } = mount();
    const termsUrl = "https://letsencrypt.org/documents/LE-SA-v1.5-February-24-2025.pdf";
    const request = vi.fn(async (path) => {
      if (path === "/api/server/certificates/import") return { id: "imported" };
      if (path.endsWith("/acme/preflight")) return {
        id: "operation", phase: "ready", name: "AutoDJ certificate", domain: "radio.example",
        environment: "staging", terms_url: termsUrl, confirmed: false,
      };
      if (path.endsWith("/acme/operation/issue")) return {
        id: "operation", phase: "queued", name: "AutoDJ certificate", domain: "radio.example",
        environment: "staging", terms_url: termsUrl, confirmed: false,
      };
      return inventory([], { acme_available: true });
    });
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-add")).toBeTruthy());
    panel.querySelector("#sc-add").click();
    let dialog = document.querySelector(".secure-connection-dialog");
    const method = dialog.querySelector("#sc-method");
    method.value = "import";
    method.dispatchEvent(new Event("change", { bubbles: true }));
    const certificate = dialog.querySelector("#sc-cert");
    expect(certificate.getAttribute("aria-required")).toBe("true");
    expect(dialog.querySelector("#sc-key").getAttribute("aria-required")).toBe("true");
    expect(certificate.hasAttribute("required")).toBe(false);
    expect(certificate.validity.valueMissing).toBe(false);
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(certificate);
    expect(certificate.getAttribute("aria-invalid")).toBe("true");
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    certificate.value = "certificate";
    certificate.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.querySelector("#sc-key").value = "private key";
    dialog.querySelector("#sc-next").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([path, options]) => path === "/api/server/certificates/import" && options?.method === "POST")).toBe(true));
    await vi.waitFor(() => expect(dialog.querySelector("#sc-cancel").getAttribute("aria-disabled")).toBe("false"));
    dialog.querySelector("#sc-cancel").click();

    panel.querySelector("#sc-add").click();
    dialog = document.querySelector(".secure-connection-dialog");
    dialog.querySelector("#sc-method").value = "acme";
    dialog.querySelector("#sc-method").dispatchEvent(new Event("change", { bubbles: true }));
    const domain = dialog.querySelector("#sc-domain");
    const zone = dialog.querySelector("#sc-zone");
    const token = dialog.querySelector("#sc-token");
    for (const field of [domain, zone, token]) {
      expect(field.hasAttribute("required")).toBe(false);
      expect(field.validity.valueMissing).toBe(false);
    }
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(domain);
    expect(domain.getAttribute("aria-invalid")).toBe("true");
    domain.value = "radio.example";
    domain.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(zone);
    expect(zone.getAttribute("aria-invalid")).toBe("true");
    zone.value = "0123456789abcdef0123456789abcdef";
    zone.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(token);
    expect(token.getAttribute("aria-invalid")).toBe("true");
    token.value = "secret";
    token.dispatchEvent(new Event("input", { bubbles: true }));
    dialog.querySelector("#sc-next").click();
    await vi.waitFor(() => expect(dialog.querySelector("#sc-terms")).toBeTruthy());
    const terms = dialog.querySelector("#sc-terms");
    expect(terms.hasAttribute("required")).toBe(false);
    expect(terms.validity.valueMissing).toBe(false);
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(terms);
    expect(terms.getAttribute("aria-invalid")).toBe("true");
    expect(request.mock.calls.some(([path]) => path.endsWith("/acme/operation/issue"))).toBe(false);
    terms.click();
    const host = dialog.querySelector("#sc-host-confirm");
    expect(host.validity.valueMissing).toBe(false);
    dialog.querySelector("#sc-next").click();
    expect(document.activeElement).toBe(host);
    expect(host.getAttribute("aria-invalid")).toBe("true");
    host.click();
    dialog.querySelector("#sc-next").click();
    await vi.waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("/acme/operation/issue"))).toBe(true));
  });

  it("does not repeat a create request while the first save is pending", async () => {
    const { panel } = mount();
    let resolveSave;
    const request = vi.fn((path, options = {}) => {
      if (path === "/api/server/certificates" && options.method === "POST") return new Promise((resolve) => { resolveSave = resolve; });
      return Promise.resolve(inventory([]));
    });
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-add")).toBeTruthy());
    panel.querySelector("#sc-add").click();
    const dialog = document.querySelector(".secure-connection-dialog");
    dialog.querySelector("#sc-next").click();
    dialog.querySelector("#sc-next").click();
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    resolveSave({ id: "new" });
    await flush();
  });

  it("keeps the remote guide free of an untrusted public-certificate download", async () => {
    const { panel } = mount();
    const request = vi.fn(async () => inventory([cert("remote", { active: true })], { local: false, https: false, urls: [] }));
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-remote")).toBeTruthy());
    panel.querySelector("#sc-remote").click();
    const dialog = document.querySelector(".secure-connection-dialog");
    expect(dialog.querySelector("a[download]")).toBeNull();
    expect(dialog.textContent).toContain("trusted connection");
  });

  it("updates platform guidance without moving focus or announcing unsolicited status", async () => {
    const { panel } = mount();
    const request = vi.fn(async () => inventory([cert("remote", { active: true })], { local: false, https: false, urls: [] }));
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-remote")).toBeTruthy());
    panel.querySelector("#sc-remote").click();
    const dialog = document.querySelector(".secure-connection-dialog");
    dialog.querySelector("#sc-next").click();
    const select = dialog.querySelector("#sc-platform");
    select.focus();
    select.value = "mac";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    expect(document.activeElement).toBe(select);
    expect(dialog.querySelector("#sc-trust-instructions").textContent).toContain("Keychain Access");
    expect(document.getElementById("secure-connections-status").textContent).toBe("");
  });

  it("restores focus to the opener on Escape and keeps voice setup warning hidden in a secure context", async () => {
    const { panel } = mount();
    const request = vi.fn(async () => inventory([]));
    installSecureConnections({ panel, request });
    await vi.waitFor(() => expect(panel.querySelector("#sc-remote")).toBeTruthy());
    expect(document.getElementById("voice-https-help").hidden).toBe(true);
    const opener = panel.querySelector("#sc-remote");
    opener.focus(); opener.click();
    const dialog = document.querySelector(".secure-connection-dialog");
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(document.activeElement).toBe(opener);
  });

  it("drops inventory replies from a previous authenticated session", async () => {
    const { panel } = mount();
    let resolve;
    const request = vi.fn(() => new Promise((r) => { resolve = r; }));
    installSecureConnections({ panel, request });
    invalidateAuthenticatedRequestEpoch();
    resolve(inventory([cert("stale", { active: true })]));
    await flush();
    expect(panel.querySelector("tbody")).toBeNull();
  });
});
