import { beforeEach, describe, expect, it, vi } from "vitest";

import { createListenerControls } from "../../src/autodj/static/modules/listener-controls.js";
import { requestJson } from "../../src/autodj/static/modules/api-client.js";

vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => ({
  ...await importOriginal(), requestJson: vi.fn(),
}));

const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    groups.push(spec);
    return { accept: (body) => spec.write(body), dirty: () => false };
  },
}));

const tick = () => new Promise((resolve) => setTimeout(resolve, 10));

function setup() {
  document.body.innerHTML = `
    <div id="settings-status" role="status"></div><div id="status-toast" hidden></div>
    <span id="stream-url-desc"></span><p id="listener-password-state"></p>
    <label for="stream-per-address">Connections per address</label><input id="stream-per-address" type="number">
    <h4 id="listener-control-title" tabindex="-1">Listeners</h4><ul id="listener-control-list"></ul>
    <p id="listener-control-empty" hidden>No listeners connected.</p>
    <button type="button" id="listener-ban-new">Add address ban</button>
    <form id="listener-ban-form" hidden><label for="listener-ban-address">Address</label>
      <input id="listener-ban-address" aria-required="true"><label for="listener-ban-duration">Duration</label>
      <select id="listener-ban-duration"><option value="60">1 hour</option><option value="">Until removed</option></select>
      <label for="listener-ban-note">Note</label><input id="listener-ban-note">
      <button type="submit">Ban address</button><button type="button" id="listener-ban-cancel">Cancel</button></form>
    <h4 id="listener-bans-title" tabindex="-1">Bans</h4><ul id="listener-ban-list"></ul>
    <p id="listener-ban-empty" hidden>No bans.</p>`;
  const controls = createListenerControls(document, document.getElementById("settings-status"));
  return { controls };
}

describe("listener controls", () => {
  beforeEach(() => {
    groups.length = 0;
    vi.clearAllMocks();
    requestJson.mockImplementation(async (url, options) => {
      if (url === "/api/stream/listeners") return {
        listeners: [{ id: "a", address: "192.0.2.4", player: "VLC", connected_at: 1000 }],
        revision: 1, password_set: false, max_per_address: 3,
      };
      if (url === "/api/stream/bans") return { bans: [] };
      if (options?.method === "DELETE") return { address: "192.0.2.4" };
      if (options?.method === "POST") return { ban: { address: "192.0.2.4" } };
      throw new Error(`unexpected ${url}`);
    });
  });

  it("refuses an empty ban address itself, linked to the field and focused", async () => {
    setup();
    document.getElementById("listener-ban-new").click();
    const address = document.getElementById("listener-ban-address");
    document.getElementById("listener-ban-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await tick();
    const error = document.getElementById("listener-ban-error");
    expect(error.textContent).toBe("Enter an address or CIDR range to ban.");
    expect(error.hidden).toBe(false);
    expect(address.getAttribute("aria-invalid")).toBe("true");
    expect(address.getAttribute("aria-describedby")).toContain("listener-ban-error");
    expect(document.activeElement).toBe(address);
    expect(requestJson).not.toHaveBeenCalledWith("/api/stream/bans", expect.objectContaining({ method: "POST" }));
    address.value = "192.0.2.4";
    address.dispatchEvent(new Event("input", { bubbles: true }));
    expect(address.hasAttribute("aria-invalid")).toBe(false);
    expect(error.hidden).toBe(true);
  });

  it("lists listeners with named buttons and announces a disconnect", async () => {
    const { controls } = setup();
    controls.apply({ stream_mode: true, stream_listener_revision: 1 });
    await tick();
    const button = document.querySelector("#listener-control-list button");
    expect(button.textContent).toBe("Disconnect 192.0.2.4, VLC");
    button.focus();
    button.click();
    await tick();
    expect(document.activeElement.id).toBe("listener-control-title");
    await vi.waitFor(() => {
      expect(document.getElementById("settings-status").textContent)
        .toBe("Disconnected listener 192.0.2.4");
    });
  });

  it("opens the native ban form and reports the chosen duration", async () => {
    const { controls } = setup();
    controls.apply({ stream_mode: true, stream_listener_revision: 1 });
    await tick();
    document.querySelectorAll("#listener-control-list button")[1].click();
    expect(document.getElementById("listener-ban-form").hidden).toBe(false);
    expect(document.activeElement.id).toBe("listener-ban-address");
    document.getElementById("listener-ban-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await tick();
    expect(requestJson).toHaveBeenCalledWith("/api/stream/bans", expect.objectContaining({ method: "POST" }));
    await vi.waitFor(() => {
      expect(document.getElementById("settings-status").textContent).toBe("Banned 192.0.2.4 for 1 hour");
    });
  });

  it("reads the per-address limit into the stream draft and shows only the password state", async () => {
    const { controls } = setup();
    controls.apply({ stream_mode: true, stream_listener_revision: 1 });
    await tick();
    expect(document.getElementById("listener-password-state").textContent).toBe("Listener password not set.");
    const limit = document.getElementById("stream-per-address");
    limit.value = "2";
    limit.dispatchEvent(new Event("change"));
    await tick();
    expect(groups.find((group) => group.group === "stream").read())
      .toEqual({ max_listeners_per_address: 2 });
    expect(requestJson.mock.calls.some(([, options]) => options?.method)).toBe(false);
  });

  it("moves focus to the next listener and updates names after refresh", async () => {
    const { controls } = setup();
    const people = [
      { id: "a", address: "192.0.2.4", player: "VLC", connected_at: 1000 },
      { id: "b", address: "192.0.2.4", player: "VLC", connected_at: 2000 },
    ];
    requestJson.mockImplementation(async (url) => url === "/api/stream/listeners"
      ? { listeners: [...people], revision: 2, password_set: false, max_per_address: 0 }
      : { bans: [] });
    controls.apply({ stream_mode: true, stream_listener_revision: 2 });
    await tick();
    const buttons = document.querySelectorAll("#listener-control-list button:first-of-type");
    expect(buttons[0].textContent).toContain("connected");
    buttons[0].focus();
    people.shift();
    await controls.refresh();
    expect(document.activeElement).toBe(document.querySelector("#listener-control-list button"));
    expect(document.activeElement.textContent).toBe("Disconnect 192.0.2.4, VLC");
  });

  it("distinguishes two connections with the same player and connected time", async () => {
    const { controls } = setup();
    requestJson.mockImplementation(async (url) => url === "/api/stream/listeners"
      ? { listeners: [
        { id: "a", address: "192.0.2.4", player: "VLC", connected_at: 1000 },
        { id: "b", address: "192.0.2.4", player: "VLC", connected_at: 1000 },
      ], revision: 2, password_set: false, max_per_address: 0 }
      : { bans: [] });
    controls.apply({ stream_mode: true, stream_listener_revision: 2 });
    await tick();
    const names = [...document.querySelectorAll("#listener-control-list button:first-of-type")]
      .map((button) => button.textContent);
    expect(names[0]).toContain("connected");
    expect(names[0]).toContain("session 1");
    expect(names[1]).toContain("session 2");
  });

  it("shows empty states and announces one load failure until recovery", async () => {
    const { controls } = setup();
    requestJson.mockRejectedValue(new Error("offline"));
    controls.apply({ stream_mode: true });
    await tick();
    const status = document.getElementById("settings-status");
    expect(status.textContent).toBe("Could not load listeners: offline");
    status.textContent = "";
    controls.apply({ stream_mode: true });
    await tick();
    expect(status.textContent).toBe("");
    requestJson.mockImplementation(async (url) => url === "/api/stream/listeners"
      ? { listeners: [], revision: 3, password_set: false, max_per_address: 0 }
      : { bans: [] });
    controls.apply({ stream_mode: true, stream_listener_revision: 3 });
    await tick();
    expect(document.getElementById("listener-control-empty").hidden).toBe(false);
    expect(document.getElementById("listener-ban-empty").hidden).toBe(false);
    expect(document.getElementById("listener-control-empty").textContent).toBe("No listeners connected.");
    expect(document.getElementById("listener-ban-empty").textContent).toBe("No bans.");
  });

  it("ignores a response superseded by a newer revision", async () => {
    const { controls } = setup();
    let completeFirst;
    let reads = 0;
    requestJson.mockImplementation(async (url) => {
      if (url === "/api/stream/bans") return { bans: [] };
      reads++;
      if (reads === 1) return new Promise((resolve) => { completeFirst = resolve; });
      return { listeners: [{ id: "new", address: "192.0.2.8", player: "VLC", connected_at: 2000 }],
        revision: 2, password_set: false, max_per_address: 0 };
    });
    controls.apply({ stream_mode: true, stream_listener_revision: 1 });
    await tick();
    controls.apply({ stream_mode: true, stream_listener_revision: 2 });
    await tick();
    completeFirst({ listeners: [{ id: "old", address: "192.0.2.4", player: "VLC", connected_at: 1000 }],
      revision: 1, password_set: false, max_per_address: 0 });
    await tick();
    expect(document.querySelector("#listener-control-list").textContent).toContain("192.0.2.8");
    expect(document.querySelector("#listener-control-list").textContent).not.toContain("192.0.2.4");
  });
});
