import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  COPIED_TEXT,
  COPY_FAILED_TEXT,
  FEED_PATH,
  NO_WEBHOOKS_TEXT,
  STATUS_COPIED_TEXT,
  STATUS_COPY_FAILED_TEXT,
  STATUS_PATH,
  WEBHOOK_REFRESH_MS,
  installNowPlayingSettings,
  testButtonName,
  webhookText,
} from "../../src/autodj/static/modules/nowplaying-settings.js";

const htmlSource = readFileSync(
  join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8",
);

// The real Stream panel from index.html, so the tests follow its markup.
function mount() {
  const template = document.createElement("template");
  template.innerHTML = htmlSource;
  const panel = template.content.getElementById("spanel-stream");
  document.body.innerHTML = `
    <div id="settings-status" role="status" aria-live="polite" aria-atomic="true"></div>
    <div id="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
  document.body.prepend(panel);
  panel.hidden = false;
  byId("stream-settings").hidden = false;
  return panel;
}

const byId = (id) => document.getElementById(id);
const said = (id, text) => vi.waitFor(() => expect(byId(id).textContent).toBe(text));
const setClipboard = (value) =>
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const HOOKS = [
  { index: 0, host: "discord.com", format: "discord", result: "Not sent" },
  { index: 1, host: "hooks.example.org", format: "generic", result: "Delivered" },
];
const STREAM_STATE = { stream_mode: true, settings: { playback: { nowplaying_show_next: false } } };

function authError() {
  const err = new Error("Sign in again.");
  err.name = "AuthenticationRequiredError";
  return err;
}

describe("now-playing settings", () => {
  let panel;
  let request;
  let clock;

  function install(overrides = {}) {
    return installNowPlayingSettings({
      doc: document,
      request,
      srStatus: byId("sr-status"),
      settingsStatus: byId("settings-status"),
      hostname: "192.168.1.20",
      now: () => clock,
      ...overrides,
    });
  }

  // Stream mode with the Stream subtab open, as subtabs.js reports it.
  function open(ui, state = STREAM_STATE) {
    panel.open = true;
    ui.apply(state);
  }

  beforeEach(() => {
    panel = mount();
    clock = 1000;
    request = vi.fn(async (url) => (url === "/api/webhooks" ? HOOKS : { delivered: true }));
  });
  afterEach(() => {
    delete navigator.clipboard;
    delete document.execCommand;
  });

  it("does nothing without its markup", () => {
    document.body.innerHTML = "";
    expect(install()).toBeNull();
  });

  it("shows this page's public feed link as read-only text", () => {
    install();
    const link = byId("np-url");
    expect(link.value).toBe(`${document.location.origin}${FEED_PATH}`);
    expect(link.readOnly).toBe(true);
    expect(byId("np-loopback-note").hidden).toBe(true);
    expect(link.getAttribute("aria-describedby")).toBe("np-url-desc");
    const status = byId("np-status-url");
    expect(status.value).toBe(`${document.location.origin}${STATUS_PATH}`);
    expect(status.readOnly).toBe(true);
    expect(byId("np-status-open").getAttribute("href")).toBe(STATUS_PATH);
  });

  it("copies the status page link, or says how to copy it by hand", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard({ writeText });
    install();
    byId("np-status-copy").click();
    await said("sr-status", STATUS_COPIED_TEXT);
    expect(writeText).toHaveBeenCalledWith(`${document.location.origin}${STATUS_PATH}`);
    setClipboard({ writeText: vi.fn(async () => { throw new Error("denied"); }) });
    document.execCommand = vi.fn(() => false);
    byId("np-status-copy").click();
    await said("sr-status", STATUS_COPY_FAILED_TEXT);
    expect(document.activeElement).toBe(byId("np-status-url"));
  });

  it("warns that a localhost link only works on this computer", () => {
    install({ hostname: "localhost" });
    expect(byId("np-loopback-note").hidden).toBe(false);
    expect(byId("np-url").getAttribute("aria-describedby")).toBe("np-url-desc np-loopback-note");
    expect(byId("np-copy").getAttribute("aria-describedby")).toBe("np-loopback-note");
    expect(byId("np-status-url").getAttribute("aria-describedby"))
      .toBe("np-status-url-desc np-loopback-note");
    expect(byId("np-status-copy").getAttribute("aria-describedby")).toBe("np-loopback-note");
  });

  it("copies the link and says so, every time", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard({ writeText });
    install();
    const copy = byId("np-copy");
    copy.focus();
    copy.click();
    await said("sr-status", COPIED_TEXT);
    expect(writeText).toHaveBeenCalledWith(`${document.location.origin}${FEED_PATH}`);
    // Focus stays on Copy, so NVDA does not read the whole link.
    expect(document.activeElement).toBe(copy);
  });

  it("falls back to the copy command without a clipboard API", async () => {
    setClipboard(undefined);
    document.execCommand = vi.fn(() => {
      const event = new Event("copy");
      event.clipboardData = { setData: vi.fn() };
      document.dispatchEvent(event);
      return true;
    });
    install();
    byId("np-copy").click();
    await said("sr-status", COPIED_TEXT);
  });

  it("says when copying fails and selects the link for Ctrl+C", async () => {
    setClipboard({ writeText: vi.fn(async () => { throw new Error("denied"); }) });
    document.execCommand = vi.fn(() => false);
    install();
    byId("np-copy").click();
    await said("sr-status", COPY_FAILED_TEXT);
    expect(document.activeElement).toBe(byId("np-url"));
  });

  it("stages Show the next song publicly and keeps the draft across pushes", () => {
    const ui = install();
    ui.apply(STREAM_STATE);
    const box = byId("np-show-next");
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    const changed = { stream_mode: true, settings: { playback: { nowplaying_show_next: false } } };
    ui.apply(changed);
    expect(box.checked).toBe(true);
  });

  it("in DMCA mode shows the status and keeps show-next focusable but unavailable", async () => {
    const ui = install();
    const box = byId("np-show-next");
    const status = byId("dmca-status");
    const reason = byId("np-show-next-dmca");
    expect(status.hidden).toBe(true);
    expect(reason.hidden).toBe(true);
    expect(box.hasAttribute("aria-disabled")).toBe(false);

    ui.apply({ stream_mode: true, settings: { playback: {
      dmca_enabled: true, nowplaying_show_next: true,
    } } });
    expect(status.hidden).toBe(false);
    expect(status.textContent).toBe("DMCA mode is on. Change it in Settings, General settings, Broadcasting.");
    expect(status.getAttribute("role")).toBeNull();
    expect(box.getAttribute("aria-disabled")).toBe("true");
    expect(box.disabled).toBe(false);
    expect(box.checked).toBe(false);
    expect(reason.hidden).toBe(false);
    expect(box.getAttribute("aria-describedby")).toBe("np-show-next-desc np-show-next-dmca");
    // A push without settings changes nothing.
    ui.apply({ stream_mode: true });
    expect(box.getAttribute("aria-disabled")).toBe("true");

    box.focus();
    box.click();
    expect(box.checked).toBe(false);
    await said("settings-status", reason.textContent.trim());
    expect(document.activeElement).toBe(box);

    ui.apply({ stream_mode: true, settings: { playback: {
      dmca_enabled: false, nowplaying_show_next: true,
    } } });
    expect(status.hidden).toBe(true);
    expect(reason.hidden).toBe(true);
    expect(box.hasAttribute("aria-disabled")).toBe(false);
    expect(box.getAttribute("aria-describedby")).toBe("np-show-next-desc");
    expect(box.checked).toBe(true);
  });

  it("shows the DMCA status outside stream mode too", () => {
    const ui = install();
    ui.apply({ stream_mode: false, settings: { playback: { dmca_enabled: true } } });
    expect(byId("dmca-status").hidden).toBe(false);
  });

  it("follows the pushed show-next setting", () => {
    const ui = install();
    const box = byId("np-show-next");
    ui.apply({ stream_mode: true, settings: { playback: { nowplaying_show_next: true } } });
    expect(box.checked).toBe(true);
    ui.apply({ stream_mode: true, settings: { playback: {} } });
    expect(box.checked).toBe(true);
  });

  it("lists webhooks by host, format and last result, with named Send test buttons", async () => {
    const ui = install();
    open(ui);
    const list = byId("np-webhooks-list");
    await vi.waitFor(() => expect(list.children).toHaveLength(2));
    expect(request).toHaveBeenCalledWith("/api/webhooks");
    expect(list.hidden).toBe(false);
    expect(byId("np-webhooks-empty").hidden).toBe(true);
    const [first, second] = list.children;
    expect(first.querySelector("span").textContent)
      .toBe("Webhook 1, discord.com, Discord format. Last delivery: Not sent.");
    expect(second.querySelector("span").textContent)
      .toBe("Webhook 2, hooks.example.org, JSON format. Last delivery: Delivered.");
    const button = first.querySelector("button");
    expect(button.type).toBe("button");
    expect(button.textContent).toBe("Send test");
    expect(button.getAttribute("aria-label")).toBe("Send test to webhook 1, discord.com");
    expect(list.querySelectorAll("button[tabindex='0']")).toHaveLength(1);
    button.focus();
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement).toBe(second.querySelector("button"));
    // The list is plain text: reading it never speaks by itself.
    expect(list.closest("[aria-live], [role=status], [role=alert]")).toBeNull();
  });

  it("sends station secrets to secrets.json in the Stream panel's help", () => {
    const template = document.createElement("template");
    template.innerHTML = htmlSource;
    const text = template.content.getElementById("spanel-stream").textContent;
    expect(text).not.toContain("config.local.toml");
    expect(text).toContain("listener_password in this station's secrets.json");
  });

  it("points to the station's secrets.json when no webhooks are set up", async () => {
    // Webhooks live in each station's secrets.json; config.local.toml is
    // read only once, to move old entries there.
    expect(NO_WEBHOOKS_TEXT).toContain("this station's secrets.json");
    expect(NO_WEBHOOKS_TEXT).not.toContain("config.local.toml");
    request.mockResolvedValue([]);
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(byId("np-webhooks-empty").textContent).toBe(NO_WEBHOOKS_TEXT));
    expect(byId("np-webhooks-empty").hidden).toBe(false);
    expect(byId("np-webhooks-list").hidden).toBe(true);
  });

  it("loads saved webhooks outside stream mode when the Stream panel is seen", async () => {
    const ui = install();
    ui.apply({ stream_mode: false });
    panel.open = true;
    panel.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  });

  it("reloads the list on pushes at most every refresh interval", async () => {
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(byId("np-webhooks-list").children).toHaveLength(2));
    ui.apply(STREAM_STATE);
    expect(request).toHaveBeenCalledTimes(1);
    clock += WEBHOOK_REFRESH_MS;
    ui.apply(STREAM_STATE);
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });

  it("updates results in place, keeping focus on Send test", async () => {
    const ui = install();
    open(ui);
    const list = byId("np-webhooks-list");
    await vi.waitFor(() => expect(list.children).toHaveLength(2));
    const button = list.querySelector("button");
    button.focus();
    request.mockResolvedValue([{ ...HOOKS[0], result: "Delivered" }, HOOKS[1]]);
    await ui.load();
    expect(list.querySelector("button")).toBe(button);
    expect(document.activeElement).toBe(button);
    expect(list.children[0].querySelector("span").textContent)
      .toBe("Webhook 1, discord.com, Discord format. Last delivery: Delivered.");
  });

  it("moves focus to a button that is left when the list changes shape", async () => {
    const ui = install();
    open(ui);
    const list = byId("np-webhooks-list");
    await vi.waitFor(() => expect(list.children).toHaveLength(2));
    list.querySelectorAll("button")[1].focus();
    request.mockResolvedValue([HOOKS[1]]);
    await ui.load();
    expect(document.activeElement).toBe(list.querySelector("button"));
    request.mockResolvedValue([]);
    await ui.load();
    expect(document.activeElement).toBe(byId("np-copy"));
  });

  it("sends a test and says the result in words", async () => {
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(byId("np-webhooks-list").children).toHaveLength(2));
    const button = byId("np-webhooks-list").querySelector("button");
    let answer;
    request.mockImplementation((url) => (url === "/api/webhooks"
      ? Promise.resolve(HOOKS)
      : new Promise((resolve) => { answer = resolve; })));
    button.click();
    await said("settings-status", "Sending a test to discord.com.");
    // The button stays enabled, so NVDA does not say "unavailable".
    expect(button.hasAttribute("aria-disabled")).toBe(false);
    // A second press while it is out sends nothing more.
    button.click();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/test"))).toHaveLength(1);
    expect(request).toHaveBeenCalledWith("/api/webhooks/0/test", { method: "POST" });
    answer({ delivered: true });
    await said("settings-status", "Test delivered to discord.com.");
  });

  it("says a failed delivery in words and where to look", async () => {
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(byId("np-webhooks-list").children).toHaveLength(2));
    request.mockImplementation(async (url) => (url === "/api/webhooks" ? HOOKS : { delivered: false }));
    byId("np-webhooks-list").querySelectorAll("button")[1].click();
    await said("settings-status",
      "Test to hooks.example.org failed. Check its address in this station's secrets.json and the AutoDJ log.");
  });

  it("says when the test request itself fails, and nothing after a sign-in prompt", async () => {
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(byId("np-webhooks-list").children).toHaveLength(2));
    const button = byId("np-webhooks-list").querySelector("button");
    request.mockRejectedValueOnce(new Error("Webhook not found"));
    button.click();
    await said("settings-status", "Could not send a test to discord.com: Webhook not found");
    request.mockRejectedValueOnce(authError());
    button.click();
    await said("settings-status", "Sending a test to discord.com.");
    await tick();
    await tick();
    expect(byId("settings-status").textContent).toBe("Sending a test to discord.com.");
  });

  it("says a list that cannot load once, and shows it as text", async () => {
    request.mockRejectedValue(new Error("Server error"));
    const ui = install();
    open(ui);
    await said("settings-status", "Could not load the webhooks: Server error");
    expect(byId("np-webhooks-empty").textContent).toBe("The webhook list could not be loaded.");
    byId("settings-status").textContent = "";
    clock += WEBHOOK_REFRESH_MS;
    ui.apply(STREAM_STATE);
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    await tick();
    expect(byId("settings-status").textContent).toBe("");
  });

  it("stays quiet when loading the list needs sign-in", async () => {
    request.mockRejectedValue(authError());
    const ui = install();
    open(ui);
    await vi.waitFor(() => expect(request).toHaveBeenCalled());
    await tick();
    expect(byId("settings-status").textContent).toBe("");
  });

  it("forgets the list and ignores late answers after sign-out", async () => {
    let answer;
    request.mockImplementation(() => new Promise((resolve) => { answer = resolve; }));
    const ui = install();
    const box = byId("np-show-next");
    box.checked = true;
    open(ui);
    ui.reset();
    answer(HOOKS);
    await tick();
    expect(byId("np-webhooks-list").children).toHaveLength(0);
    expect(byId("np-webhooks-list").hidden).toBe(true);
    expect(byId("np-webhooks-empty").hidden).toBe(true);
    expect(box.checked).toBe(false);
  });

  it("names formats it does not know by their own name", () => {
    expect(webhookText({ index: 2, host: "h", format: "other", result: "Delivery failed" }))
      .toBe("Webhook 3, h, other format. Last delivery: Delivery failed.");
    expect(testButtonName({ index: 0, host: "h" })).toBe("Send test to webhook 1, h");
  });
});
