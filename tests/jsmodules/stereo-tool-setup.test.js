import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  installStereoToolSetup,
  installText,
  licenseText,
} from "../../src/autodj/static/modules/stereo-tool-setup.js";

const staticPath = (name) => join(process.cwd(), "src", "autodj", "static", name);
const html = readFileSync(staticPath("index.html"), "utf8");
const template = document.createElement("template");
template.innerHTML = html;
const page = template.content;

beforeEach(() => {
  document.body.innerHTML = `
    ${page.getElementById("spanel-sound").outerHTML}
    <div id="settings-status" role="status" aria-live="polite"></div>
    <div id="status-toast" aria-hidden="true" hidden></div>`;
});

const $ = (id) => document.getElementById(id);
const statusText = () => $("settings-status").textContent;
const KEY = "<SECRET-KEY-1234567890>";

function status(overrides = {}) {
  return {
    installed: false,
    version: null,
    download: { available: true, platform: "Windows", busy: false },
    license: { in_use: null, install: null, station: null, problem: null },
    several_stations: false,
    terms_url: "https://www.thimeo.com/terms-and-conditions/",
    licensing_url: "https://www.thimeo.com/stereo-tool/licenses/",
    ...overrides,
  };
}

function server(initial = status()) {
  const state = { data: initial, calls: [] };
  state.request = vi.fn(async (url, options = {}) => {
    state.calls.push([url, options.method || "GET", options.body]);
    if (url === "/api/stereo-tool") return state.data;
    if (url === "/api/stereo-tool/download") {
      if (state.failDownload) throw new Error(state.failDownload);
      state.data = { ...state.data, installed: true, version: "11.05" };
      return { version: "11.05" };
    }
    if (url === "/api/stereo-tool/license" && options.method === "PUT") {
      if (state.failSave) {
        const error = new Error(state.failSave);
        error.status = state.failStatus;
        throw error;
      }
      const { scope } = JSON.parse(options.body);
      state.data = { ...state.data, license: { ...state.data.license,
        in_use: scope === "station" ? "station" : "install",
        [scope === "station" ? "station" : "install"]: "saved" } };
      return state.data;
    }
    if (url.startsWith("/api/stereo-tool/license?scope=") && options.method === "DELETE") {
      state.data = { ...state.data, license: { in_use: null, install: null, station: null, problem: null } };
      return { ...state.data, cleared: true };
    }
    throw new Error(`Unexpected request ${url}`);
  });
  return state;
}

function answer(accept) {
  return vi.fn(async (_doc, { onConfirm }) => {
    if (!accept) return false;
    const target = await onConfirm();
    target?.focus();
    return true;
  });
}

async function install(state, { confirm = answer(true), stationChanges = () => () => {},
  recheckMs = 60_000 } = {}) {
  const panel = installStereoToolSetup({ document, request: state.request, confirm,
    status: $("settings-status"), stationChanges, recheckMs });
  await panel.refresh();
  return panel;
}

describe("Stereo Tool setup markup", () => {
  const group = page.getElementById("stereo-setup");

  it("is the Stereo Tool group, shown only for Stereo Tool", () => {
    expect(group.tagName).toBe("FIELDSET");
    expect(group.querySelector(":scope > legend").textContent).toBe("Stereo Tool");
    expect(group.dataset.showWhen).toBe("pb-replaygain=stereotool");
  });

  it("takes the key in a labelled password field that browsers do not fill", () => {
    const field = page.getElementById("stereo-license-key");
    expect(field.type).toBe("password");
    // Browsers ignore "off" on password fields; "new-password" keeps saved logins out.
    expect(field.getAttribute("autocomplete")).toBe("new-password");
    expect(page.querySelector('label[for="stereo-license-key"]').textContent)
      .toBe("Stereo Tool licence key");
    expect(field.getAttribute("aria-describedby").split(" "))
      .toEqual(["stereo-license-status", "stereo-license-desc"]);
  });

  it("labels the terms box and says the links open a new tab", () => {
    expect(page.querySelector('label[for="stereo-accept-terms"]').textContent)
      .toBe("I accept Thimeo's terms for Stereo Tool");
    const links = [...page.getElementById("stereo-terms-desc").querySelectorAll("a")];
    expect(links.map((link) => link.href)).toEqual([
      "https://www.thimeo.com/terms-and-conditions/",
      "https://www.thimeo.com/stereo-tool/licenses/",
    ]);
    for (const link of links) {
      expect(link.textContent).toContain("(opens in a new tab)");
      expect(link.rel).toContain("noopener");
    }
  });

  it("uses plain buttons that are never disabled in the markup", () => {
    for (const id of ["stereo-download", "stereo-license-save", "stereo-license-clear"]) {
      const button = page.getElementById(id);
      expect(button.getAttribute("type")).toBe("button");
      expect(button.disabled).toBe(false);
      expect(button.textContent.trim()).not.toBe("");
    }
  });
});

describe("Stereo Tool setup words", () => {
  it("says what is installed and how to get it", () => {
    expect(installText(status())).toBe(
      "Stereo Tool is not installed. Tick that you accept Thimeo's terms, then press Download Stereo Tool.");
    expect(installText(status({ installed: true, version: "11.05" })))
      .toBe("Stereo Tool 11.05 is installed.");
    expect(installText(status({ installed: true })))
      .toBe("Stereo Tool is installed (version unknown).");
    expect(installText(status({ download: { available: false } })))
      .toContain("AutoDJ cannot download Stereo Tool for this computer");
    expect(installText(status({ download: { available: false,
      note: "Thimeo's ARM build of Stereo Tool processes 16-bit audio only." } })))
      .toBe("Stereo Tool is not installed. Thimeo's ARM build of Stereo Tool processes 16-bit audio only.");
  });

  it("says Licence set or No licence and never needs the key", () => {
    expect(licenseText(status())).toMatch(/^No licence\./);
    expect(licenseText(status({ license: { in_use: "install" } }))).toBe("Licence set.");
    expect(licenseText(status({ several_stations: true, license: { in_use: "install" } })))
      .toBe("Licence set for every station.");
    expect(licenseText(status({ license: { in_use: "station" } })))
      .toBe("Licence set for this station only.");
    expect(licenseText(status({ license: { in_use: "environment", problem: "AUTODJ_STEREOTOOL_LICENSE_KEY does not hold a licence key." } })))
      .toMatch(/^No licence\. AUTODJ_STEREOTOOL_LICENSE_KEY/);
  });

  it("adds what the running Stereo Tool said of the licence", () => {
    expect(licenseText(status({ license: { in_use: "install" }, running: { licensed: true } })))
      .toBe("Licence set. Stereo Tool says the licence key is valid.");
    const text = licenseText(status({ license: { in_use: null },
      running: { licensed: false, unlicensed: "Declipper, 5-band AGC" } }));
    expect(text).toContain("Not licensed: Stereo Tool says the licence does not cover this preset. Declipper, 5-band AGC");
    expect(text).toContain("Thimeo's unlicensed reminder");
    expect(licenseText(status({ license: { in_use: "install" }, running: { licensed: null } })))
      .toBe("Licence set.");
  });
});

describe("Stereo Tool setup controls", () => {
  it("loads when the Sound panel is shown", async () => {
    const state = server(status({ installed: true, version: "11.05" }));
    installStereoToolSetup({ document, request: state.request, status: $("settings-status"),
      stationChanges: () => () => {} });
    expect(state.request).not.toHaveBeenCalled();
    $("spanel-sound").open = true;
    $("spanel-sound").dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect($("stereo-install-status").textContent)
      .toBe("Stereo Tool 11.05 is installed."));
    expect($("stereo-download").textContent).toBe("Update Stereo Tool");
  });

  it("refreshes for the station the page switches to", async () => {
    const state = server();
    let changed = null;
    await install(state, { stationChanges: (listener) => { changed = listener; return () => {}; } });
    $("spanel-sound").open = true;
    state.data = status({ license: { in_use: "station" } });
    changed("night", "default");
    await vi.waitFor(() => expect($("stereo-license-status").textContent)
      .toBe("Licence set for this station only."));
  });

  it("refuses to download until the terms are accepted, and focuses the box", async () => {
    const state = server();
    await install(state);
    $("stereo-download").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Tick I accept Thimeo's terms for Stereo Tool first."));
    expect(document.activeElement).toBe($("stereo-accept-terms"));
    expect(state.calls.some(([url]) => url === "/api/stereo-tool/download")).toBe(false);
  });

  it("downloads after the tick, says it is busy, then names the version", async () => {
    const state = server();
    let release;
    const original = state.request;
    state.request = vi.fn(async (url, options) => {
      if (url === "/api/stereo-tool/download") await new Promise((resolve) => { release = resolve; });
      return original(url, options);
    });
    await install(state);
    $("stereo-accept-terms").checked = true;
    $("stereo-download").focus();
    $("stereo-download").click();
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    expect($("stereo-download").getAttribute("aria-busy")).toBe("true");
    expect($("stereo-download").disabled).toBe(false);
    expect($("stereo-install-status").textContent).toContain("Downloading Stereo Tool from thimeo.com");
    $("stereo-download").click();
    release();
    await vi.waitFor(() => expect(statusText()).toBe("Stereo Tool 11.05 installed."));
    expect(state.request.mock.calls.filter(([url]) => url === "/api/stereo-tool/download"))
      .toHaveLength(1);
    const [, options] = state.request.mock.calls.find(([url]) => url === "/api/stereo-tool/download");
    expect(JSON.parse(options.body)).toEqual({ accept_terms: true });
    expect($("stereo-download").hasAttribute("aria-busy")).toBe(false);
    expect($("stereo-download").textContent).toBe("Update Stereo Tool");
    expect(document.activeElement).toBe($("stereo-download"));
  });

  it("says when the installed program names no version", async () => {
    const state = server();
    const original = state.request;
    state.request = vi.fn(async (url, options) => {
      if (url === "/api/stereo-tool/download") {
        state.data = { ...state.data, installed: true, version: null };
        return { version: null };
      }
      return original(url, options);
    });
    await install(state);
    $("stereo-accept-terms").checked = true;
    $("stereo-download").click();
    await vi.waitFor(() => expect(statusText()).toBe("Stereo Tool installed (version unknown)."));
    expect($("stereo-install-status").textContent).toBe("Stereo Tool is installed (version unknown).");
  });

  it("explains why there is nothing to download for this computer", async () => {
    const note = "Thimeo's ARM build of Stereo Tool processes 16-bit audio only, and AutoDJ sends it 32-bit audio, so it is not supported yet.";
    const state = server(status({ download: { available: false, platform: null, note, busy: false } }));
    await install(state);
    $("stereo-accept-terms").checked = true;
    $("stereo-download").click();
    await vi.waitFor(() => expect(statusText()).toBe(note));
    expect(state.calls.some(([url]) => url === "/api/stereo-tool/download")).toBe(false);
  });

  it("says why a download failed", async () => {
    const state = server();
    state.failDownload = "The download is not a Windows program, so it was not installed.";
    await install(state);
    $("stereo-accept-terms").checked = true;
    $("stereo-download").click();
    await vi.waitFor(() => expect(statusText()).toBe(
      "Could not download Stereo Tool: The download is not a Windows program, so it was not installed."));
  });

  it("saves the key for every station, empties the field and never repeats the key", async () => {
    const state = server();
    await install(state);
    $("stereo-license-key").value = `  ${KEY}  `;
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText()).toBe("Licence key saved. Stereo Tool checks the key in a moment."));
    const put = state.calls.find(([url, method]) => url === "/api/stereo-tool/license" && method === "PUT");
    expect(JSON.parse(put[2])).toEqual({ key: KEY, scope: "all" });
    expect($("stereo-license-key").value).toBe("");
    expect($("stereo-license-status").textContent).toBe("Licence set.");
    expect(document.body.innerHTML).not.toContain("SECRET-KEY");
  });

  it("offers For this station only when there are several stations", async () => {
    const state = server(status({ several_stations: true }));
    await install(state);
    expect($("stereo-license-station-row").hidden).toBe(false);
    $("stereo-license-station-only").checked = true;
    $("stereo-license-key").value = KEY;
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Licence key saved for this station. Stereo Tool checks the key in a moment."));
    const put = state.calls.find(([, method]) => method === "PUT");
    expect(JSON.parse(put[2]).scope).toBe("station");
  });

  it("hides For this station only with one station", async () => {
    await install(server());
    expect($("stereo-license-station-row").hidden).toBe(true);
    expect($("stereo-license-station-desc").hidden).toBe(true);
  });

  it("shows a refused key under the field and moves focus there", async () => {
    const state = server();
    state.failSave = "A Stereo Tool licence key is 4 to 2048 letters, digits and symbols, with no spaces or line breaks.";
    state.failStatus = 400;
    await install(state);
    $("stereo-license-key").value = "bad key";
    $("stereo-license-save").click();
    await vi.waitFor(() => expect($("stereo-license-error").hidden).toBe(false));
    expect($("stereo-license-error").textContent).toContain("Could not save the licence key: A Stereo Tool");
    expect($("stereo-license-key").getAttribute("aria-invalid")).toBe("true");
    expect($("stereo-license-key").getAttribute("aria-describedby")).toContain("stereo-license-error");
    expect(document.activeElement).toBe($("stereo-license-key"));
    $("stereo-license-key").value = "better";
    $("stereo-license-key").dispatchEvent(new Event("input"));
    expect($("stereo-license-error").hidden).toBe(true);
    expect($("stereo-license-key").hasAttribute("aria-invalid")).toBe(false);
  });

  it("says when the key was already saved", async () => {
    const state = server();
    const original = state.request;
    state.request = vi.fn(async (url, options = {}) => {
      const reply = await original(url, options);
      return options.method === "PUT" ? { ...reply, changed: false } : reply;
    });
    await install(state);
    $("stereo-license-key").value = KEY;
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText()).toBe("That licence key is already saved."));
  });

  it.each([409, 500])("does not blame the field for a %i answer", async (code) => {
    const state = server();
    const original = state.request;
    state.request = vi.fn(async (url, options = {}) => {
      if (options.method === "PUT") {
        const error = new Error("The licence for every station comes from the server.");
        error.status = code;
        throw error;
      }
      return original(url, options);
    });
    await install(state);
    $("stereo-license-key").value = KEY;
    $("stereo-license-save").focus();
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Could not save the licence key: The licence for every station comes from the server."));
    expect($("stereo-license-key").hasAttribute("aria-invalid")).toBe(false);
    expect($("stereo-license-error").hidden).toBe(true);
    expect(document.activeElement).toBe($("stereo-license-save"));
  });

  it("asks for a key before saving an empty field", async () => {
    const state = server();
    await install(state);
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText()).toBe("Enter the licence key first."));
    expect(state.calls.some(([, method]) => method === "PUT")).toBe(false);
    expect(document.activeElement).toBe($("stereo-license-key"));
  });

  it("clears the saved licence after confirming", async () => {
    const state = server(status({ license: { in_use: "install", install: "saved", station: null, problem: null } }));
    const confirm = answer(true);
    await install(state, { confirm });
    $("stereo-license-clear").click();
    await vi.waitFor(() => expect(statusText()).toMatch(/^Licence cleared\. No licence\./));
    expect(state.calls.some(([url, method]) => url === "/api/stereo-tool/license?scope=all" && method === "DELETE")).toBe(true);
    expect(document.activeElement).toBe($("stereo-license-clear"));
  });

  it("says a licence from the server cannot be cleared here", async () => {
    const state = server(status({ license: { in_use: "environment", install: "environment", station: null, problem: null } }));
    const confirm = answer(true);
    await install(state, { confirm });
    $("stereo-license-clear").click();
    await vi.waitFor(() => expect(statusText()).toContain("cannot be cleared here"));
    expect(confirm).not.toHaveBeenCalled();
  });

  it("looks again a few seconds after a save and speaks Stereo Tool's verdict once", async () => {
    const state = server();
    const original = state.request;
    let saved = false;
    state.request = vi.fn(async (url, options = {}) => {
      const reply = await original(url, options);
      if (options.method === "PUT") saved = true;
      // The PUT's own reply has no verdict yet; the later look does.
      if (url === "/api/stereo-tool" && saved) {
        return { ...reply, running: { licensed: true, unlicensed: null } };
      }
      return reply;
    });
    await install(state, { recheckMs: 20 });
    $("stereo-license-key").value = KEY;
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Licence set. Stereo Tool says the licence key is valid."));
    expect($("stereo-license-status").textContent)
      .toBe("Licence set. Stereo Tool says the licence key is valid.");
  });

  it("says nothing more when the recheck finds the same verdict", async () => {
    const state = server();
    await install(state, { recheckMs: 20 });
    $("stereo-license-key").value = KEY;
    $("stereo-license-save").click();
    await vi.waitFor(() => expect(statusText())
      .toBe("Licence key saved. Stereo Tool checks the key in a moment."));
    await new Promise((resolve) => { setTimeout(resolve, 80); });
    expect(state.calls.filter(([url, method]) => url === "/api/stereo-tool" && method === "GET"))
      .toHaveLength(2);
    expect(statusText()).toBe("Licence key saved. Stereo Tool checks the key in a moment.");
  });
});
