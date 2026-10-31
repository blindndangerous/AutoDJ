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
