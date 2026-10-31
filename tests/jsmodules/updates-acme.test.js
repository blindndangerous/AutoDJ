import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installUpdates } from "../../src/autodj/static/modules/updates.js";

beforeEach(() => installIndexMarkup());
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

function setup(inventory) {
  let clock = 0;
  const navigate = vi.fn();
  const request = vi.fn(async (url) => {
    if (url === "/api/updates") return {
      installation: { kind: "git", version: "0.22.0", branch: "master" },
      settings: { check: false, channel: null },
      channel: { name: "stable", source: "installation" },
      check: { status: "current", message: "Current" },
      restart: { available: true }, update: { available: false },
    };
    if (url === "/api/server/certificates") return inventory;
    if (url === "/api/server/restart") return { restarting: true };
    throw new Error(`Unexpected request ${url}`);
  });
  const controls = installUpdates({
    request, navigate, settle: async () => {},
    confirm: async (_doc, { onConfirm }) => { await onConfirm(); return true; },
    sleep: async (ms) => { clock += ms; },
    now: () => clock,
    probe: vi.fn(async () => ({})),
    probeHttps: vi.fn(async () => true),
  });
  return { controls, request, navigate };
}

function destination() {
  const url = new URL(document.location.href);
  const port = url.port || (url.protocol === "http:" ? "80" : "443");
  url.protocol = "https:";
  url.port = port;
  url.hostname = "radio.example.com";
  url.search = "?https-setup=trust";
  url.hash = "settings";
  return url;
}

it("validates a public-domain restart against the active managed certificate", async () => {
  const target = destination();
  const { controls, request, navigate } = setup({
    can_manage: true, urls: [target.origin],
    certificates: [{ active: true, type: "Let's Encrypt", hosts: [target.hostname] }],
  });
  await controls.load();
  await controls.restartForHttps(target.href);
  expect(request).toHaveBeenCalledWith("/api/server/certificates");
  expect(navigate).toHaveBeenCalledWith(target.href);
});

it.each([
  { can_manage: false, certificate: true, advertised: true },
  { can_manage: true, certificate: false, advertised: true },
  { can_manage: true, certificate: true, advertised: false },
])("refuses an unverified domain handoff: %j", async ({ can_manage, certificate, advertised }) => {
  const target = destination();
  const { controls, request, navigate } = setup({
    can_manage, urls: advertised ? [target.origin] : [],
    certificates: certificate ? [{ active: true, type: "Let's Encrypt", hosts: [target.hostname] }] : [],
  });
  await controls.load();
  await controls.restartForHttps(target.href);
  expect(request.mock.calls.some(([url]) => url === "/api/server/restart")).toBe(false);
  expect(navigate).not.toHaveBeenCalled();
});
