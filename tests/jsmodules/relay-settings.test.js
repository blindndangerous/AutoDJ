import { beforeEach, describe, expect, it, vi } from "vitest";
import { installRelaySettings } from "../../src/autodj/static/modules/relay-settings.js";

function markup() {
  document.body.innerHTML = `
    <button id="stream-relay-add"></button><form id="stream-relay-form" hidden>
      <h5 id="stream-relay-form-title"></h5>
      <input id="stream-relay-name"><select id="stream-relay-type"><option value="icecast">Icecast</option><option value="shoutcast2">Shoutcast 2</option></select>
      <input id="stream-relay-host"><input id="stream-relay-port"><input id="stream-relay-password"><p id="stream-relay-password-help"></p>
      <div id="stream-relay-icecast"><input id="stream-relay-mount"><input id="stream-relay-user"></div>
      <div id="stream-relay-shoutcast2"><input id="stream-relay-stream-id"></div>
      <select id="stream-relay-bitrate"><option value=""></option><option value="128"></option></select>
      <input id="stream-relay-public" type="checkbox"><input id="stream-relay-enabled" type="checkbox">
      <button type="submit"></button><button type="button" id="stream-relay-cancel"></button>
    </form><button id="stream-metadata-save"></button>
    <input id="stream-station-name"><input id="stream-station-genre"><input id="stream-station-description"><input id="stream-station-url">
    <table role="grid"><tbody id="stream-relay-rows"></tbody></table>
    <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
      <button id="confirm-accept"></button><button id="confirm-cancel"></button></dialog>`;
}

describe("relay settings", () => {
  beforeEach(markup);

  it("saves a relay without putting its password in a returned row", async () => {
    const post = vi.fn(async () => true);
    const ui = installRelaySettings({
      doc: document, request: vi.fn(async () => ({ relays: [] })), post,
      remove: vi.fn(), announce: vi.fn(),
    });
    ui.setAvailable(true);
    await vi.waitFor(() => expect(document.getElementById("stream-relay-add")).toBeTruthy());
    document.getElementById("stream-relay-add").click();
    document.getElementById("stream-relay-name").value = "main";
    document.getElementById("stream-relay-host").value = "ice.example";
    document.getElementById("stream-relay-password").value = "never-return-this";
    document.getElementById("stream-relay-form").dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
    await vi.waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][1].password).toBe("never-return-this");
    expect(document.getElementById("stream-relay-rows").textContent).not.toContain("never-return-this");
    ui.reset();
  });

  it("keeps rows and focus stable while relay status changes", async () => {
    const ui = installRelaySettings({
      doc: document, request: vi.fn(async () => ({ relays: [{ name: "main", enabled: true }] })),
      post: vi.fn(), remove: vi.fn(), announce: vi.fn(),
    });
    ui.setAvailable(true);
    await vi.waitFor(() => expect(document.querySelector("#stream-relay-rows tr")).toBeTruthy());
    const row = document.querySelector("#stream-relay-rows tr");
    const button = row.querySelector("button");
    button.focus();
    ui.applyStatuses([{ name: "main", state: "connected" }]);
    expect(document.querySelector("#stream-relay-rows tr")).toBe(row);
    expect(document.activeElement).toBe(button);
    ui.reset();
  });

  it("ignores a relay refresh that finishes after reset", async () => {
    let finish;
    const request = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    const ui = installRelaySettings({ doc: document, request, post: vi.fn(), remove: vi.fn(), announce: vi.fn() });
    ui.setAvailable(true);
    ui.reset();
    finish({ relays: [{ name: "stale", enabled: true }] });
    await Promise.resolve();
    expect(document.getElementById("stream-relay-rows").textContent).not.toContain("stale");
  });

  it("keeps focus on a row's Enable or Disable button after it saves", async () => {
    const post = vi.fn(async () => true);
    const ui = installRelaySettings({
      doc: document, request: vi.fn(async () => ({ relays: [{ name: "main", enabled: true }] })),
      post, remove: vi.fn(), announce: vi.fn(),
    });
    ui.setAvailable(true);
    await vi.waitFor(() => expect(document.querySelector("#stream-relay-rows tr")).toBeTruthy());
    const toggle = document.querySelectorAll("#stream-relay-rows tr button")[1];
    toggle.focus();
    toggle.click();
    await vi.waitFor(() => expect(post).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.activeElement).toBe(toggle);
    ui.reset();
  });
});
