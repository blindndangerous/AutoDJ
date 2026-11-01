
import { afterEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import { installLiveSettings, sourceFacts, transportNote } from "../../src/autodj/static/modules/live-settings.js";
const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    groups.push(spec);
    return { accept: (body) => spec.write(body), dirty: () => false };
  },
}));

const byId = (id) => document.getElementById(id);

function installIndex() {
  installIndexMarkup();
  byId("spanel-live").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => dialog.setAttribute("open", "");
  dialog.close = () => {
    dialog.removeAttribute("open");
    dialog.dispatchEvent(new Event("close"));
  };
}

const INFO = {
  available: true, reason: null, live: null, talkover: false,
  source_path: "/stations/night/live", plain_http_lan: false,
  settings: { mic_return_seconds: 0.5, mic_duck_db: 12 },
  djs: [{ username: "alice", display_name: "DJ Alice", password_set: true }],
};

function install(answers = {}) {
  installIndex();
  const calls = [];
  const request = vi.fn(async (url, options = {}) => {
    calls.push([url, options]);
    const method = options.method || "GET";
    const answer = answers[`${method} ${url}`];
    if (answer instanceof Error) throw answer;
    if (typeof answer === "function") return answer(options);
    if (answer !== undefined) return answer;
    if (url === "/api/live") return JSON.parse(JSON.stringify(INFO));
    return { ok: true };
  });
  const announce = vi.fn();
  const onInfo = vi.fn();
  const ui = installLiveSettings({
    doc: document, request, announce, onInfo,
    location: { protocol: "http:", host: "192.168.1.5:8080", hostname: "192.168.1.5", port: "8080" },
  });
  return { ui, request, calls, announce, onInfo };
}

afterEach(() => { groups.length = 0; document.body.replaceChildren(); });

describe("source address", () => {
  it("gives a DJ program its host, port, mount and TLS from the page's own address", () => {
    expect(sourceFacts("/live", { protocol: "https:", host: "radio.example", hostname: "radio.example", port: "" }))
      .toEqual({ url: "https://radio.example/live", host: "radio.example", port: "443", mount: "live", secure: true });
    expect(sourceFacts("/stations/night/live", { protocol: "http:", host: "10.0.0.2:8080", hostname: "10.0.0.2", port: "8080" }).mount)
      .toBe("stations/night/live");
    expect(transportNote(true, false)).toMatch(/SSL\/TLS/);
    expect(transportNote(false, false)).toMatch(/this computer only/);
    expect(transportNote(false, true)).toMatch(/local network/);
  });
});

describe("Live DJ settings", () => {
  beforeEach(() => { groups.length = 0; });
  it("shows the address, the facts, the DJs and the talk settings, never a password", async () => {
    const { ui, onInfo } = install();
    await ui.refresh();
    expect(onInfo).toHaveBeenCalledWith(expect.objectContaining({ available: true }));
    expect(byId("live-settings").hidden).toBe(false);
    expect(byId("live-unavailable").hidden).toBe(true);
    expect(byId("live-source-url").value).toBe("http://192.168.1.5:8080/stations/night/live");
    expect(byId("live-source-host").textContent).toBe("192.168.1.5");
    expect(byId("live-source-port").textContent).toBe("8080");
    expect(byId("live-source-mount").textContent).toBe("stations/night/live");
    expect(byId("live-source-tls").textContent).toBe("SSL/TLS off");
    expect(byId("live-transport-note").textContent).toMatch(/this computer only/);
    const rows = [...byId("live-dj-rows").querySelectorAll("tr")];
    expect(rows).toHaveLength(1);
    expect([...rows[0].cells].slice(0, 2).map((cell) => cell.textContent)).toEqual(["DJ Alice, username alice", "Saved"]);
    expect(rows[0].cells[0].getAttribute("role")).toBe("gridcell");
    expect(rows[0].querySelector("button").getAttribute("aria-label")).toBe("Edit DJ alice");
    expect(byId("live-dj-none").hidden).toBe(true);
    expect(byId("live-return-seconds").value).toBe("0.5");
    expect(byId("live-duck-db").value).toBe("12");
  });

  it("says why the station cannot go live", async () => {
    const { ui } = install({ "GET /api/live": { available: false, reason: "Live DJ needs stream mode." } });
    await ui.refresh();
    expect(byId("live-settings").hidden).toBe(false);
    expect(byId("live-return-seconds").disabled).toBe(false);
    expect(byId("live-duck-db").disabled).toBe(false);
    expect(byId("live-dj-add").disabled).toBe(false);
    expect(byId("live-unavailable").hidden).toBe(false);
    expect(byId("live-unavailable").textContent).toBe("Live DJ needs stream mode.");
  });

  it("adds a DJ with a write-only password and keeps the saved one on an edit", async () => {
    const { ui, calls, announce } = install();
    await ui.refresh();
    byId("live-dj-add").click();
    expect(byId("live-dj-form").hidden).toBe(false);
    expect(document.activeElement).toBe(byId("live-dj-username"));
    byId("live-dj-username").value = "bob";
    byId("live-dj-name").value = "DJ Bob";
    byId("live-dj-password").value = "correct horse battery";
    byId("live-dj-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(announce).toHaveBeenCalledWith("DJ bob saved."));
    const post = calls.find(([url, options]) => url === "/api/live/djs" && options.method === "POST");
    expect(JSON.parse(post[1].body)).toEqual({ username: "bob", display_name: "DJ Bob", password: "correct horse battery" });
    expect(byId("live-dj-password").value).toBe("");
    expect(byId("live-dj-form").hidden).toBe(true);

    // Edit: the username is fixed and a blank password keeps the saved one.
    byId("live-dj-rows").querySelector("button").click();
    expect(byId("live-dj-username").readOnly).toBe(true);
    expect(byId("live-dj-password-help").textContent).toMatch(/A password is saved\. Leave blank to keep it/);
    expect(document.activeElement).toBe(byId("live-dj-name"));
    byId("live-dj-name").value = "Alice";
    byId("live-dj-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(announce).toHaveBeenCalledWith("DJ alice saved."));
    const edit = calls.filter(([url, options]) => url === "/api/live/djs" && options.method === "POST").at(-1);
    expect(JSON.parse(edit[1].body)).toEqual({ username: "alice", display_name: "Alice", password: null });
  });

  it("names each mistake against its field, in words, and focuses it", async () => {
    const { ui, calls, announce } = install({
      "POST /api/live/djs": new Error("Password or display name has an invalid length"),
    });
    await ui.refresh();
    byId("live-dj-add").click();
    byId("live-dj-username").value = "has:colon";
    byId("live-dj-name").value = "X";
    byId("live-dj-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    const username = byId("live-dj-username");
    expect(username.getAttribute("aria-invalid")).toBe("true");
    expect(username.getAttribute("aria-describedby").split(" ")).toContain("live-dj-error");
    expect(byId("live-dj-error").hidden).toBe(false);
    expect(document.activeElement).toBe(username);
    expect(announce).toHaveBeenLastCalledWith(expect.stringMatching(/^Username/), "error");
    expect(calls.some(([url]) => url === "/api/live/djs")).toBe(false);
    // Typing clears the mark.
    username.value = "bob";
    username.dispatchEvent(new Event("input", { bubbles: true }));
    expect(username.hasAttribute("aria-invalid")).toBe(false);
    // A new DJ needs a password.
    byId("live-dj-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(byId("live-dj-password"));
    expect(byId("live-dj-error").textContent).toMatch(/needs a password/);
    // The server's refusal lands on the field it names.
    byId("live-dj-password").value = "correct horse battery";
    byId("live-dj-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(byId("live-dj-error").textContent).toMatch(/^Could not save the DJ/));
    expect(document.activeElement).toBe(byId("live-dj-password"));
  });

  it("removes a DJ after confirming, focus going to Add DJ", async () => {
    const { ui, calls, announce } = install();
    await ui.refresh();
    byId("live-dj-rows").querySelectorAll("button")[1].click();
    expect(byId("confirm-dialog").hasAttribute("open")).toBe(true);
    expect(byId("confirm-message").textContent).toMatch(/DJ Alice \(alice\)/);
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(announce).toHaveBeenCalledWith("DJ alice removed."));
    expect(calls.some(([url, options]) => url === "/api/live/djs/alice" && options.method === "DELETE")).toBe(true);
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("live-dj-add")));
  });

  it("validates talk settings and reads valid values into the station draft", async () => {
    const { ui, calls } = install();
    await ui.refresh();
    byId("live-return-seconds").value = "20";
    expect(groups.find((group) => group.group === "live").validate()).toBe(false);
    expect(byId("live-return-seconds").getAttribute("aria-invalid")).toBe("true");
    expect(byId("live-talkover-error").textContent).toMatch(/^Return time/);
    expect(document.activeElement).toBe(byId("live-return-seconds"));
    byId("live-return-seconds").value = "1.5";
    byId("live-duck-db").value = "18";
    expect(groups.find((group) => group.group === "live").validate()).toBe(true);
    expect(groups.find((group) => group.group === "live").read())
      .toEqual({ mic_return_seconds: 1.5, mic_duck_db: 18 });
    expect(byId("live-talkover-error").hidden).toBe(true);
    expect(byId("live-return-seconds").hasAttribute("aria-invalid")).toBe(false);
    expect(calls.map(([url]) => url)).toEqual(["/api/live"]);
  });

  it("does not persist talk settings from the feature module", async () => {
    const { ui, calls } = install();
    await ui.refresh();
    byId("live-duck-db").value = "18";
    expect(groups.find((group) => group.group === "live").read().mic_duck_db).toBe(18);
    expect(byId("live-talkover-save")).toBeNull();
    expect(calls.map(([url]) => url)).toEqual(["/api/live"]);
  });

  it("announces a slow removal in the confirmation dialog (#384)", async () => {
    let removed;
    const { ui, announce } = install({
      "DELETE /api/live/djs/alice": () => new Promise((resolve) => { removed = () => resolve({ ok: true }); }),
    });
    await ui.refresh();
    vi.useFakeTimers();
    try {
      byId("live-dj-rows").querySelectorAll("button")[1].click();
      byId("confirm-accept").click();
      expect(byId("confirm-accept").getAttribute("aria-busy")).toBe("true");
      await vi.advanceTimersByTimeAsync(350);
      expect(byId("confirm-status").textContent).toMatch(/Removing DJ alice/);
      removed();
      await vi.advanceTimersByTimeAsync(0);
      expect(announce.mock.calls.at(-1)).toEqual(["DJ alice removed."]);
      expect(byId("confirm-status").textContent).toBe("");
      expect(byId("confirm-accept").hasAttribute("aria-busy")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("ignores an answer that arrives after a station switch", async () => {
    let finish;
    const { ui, onInfo } = install({ "GET /api/live": () => new Promise((resolve) => { finish = resolve; }) });
    const pending = ui.refresh();
    ui.reset();
    finish({ ...INFO, djs: [{ username: "stale", display_name: "Stale", password_set: true }] });
    await pending;
    expect(byId("live-dj-rows").textContent).not.toContain("stale");
    expect(onInfo).not.toHaveBeenCalled();
  });
});
