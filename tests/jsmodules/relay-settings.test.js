import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  installRelaySettings,
  passwordWords,
  relayBody,
  relayFormError,
  relayServerText,
  relayStatusText,
} from "../../src/autodj/static/modules/relay-settings.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const section = html.match(/<section id="stream-relays"[\s\S]*?<\/section>/)[0];
const relayDialog = html.match(/<dialog id="relay-dialog"[\s\S]*?<\/dialog>/)[0];
const confirmDialog = html.match(/<dialog id="confirm-dialog"[\s\S]*?<\/dialog>/)[0];

const SECRET = "Typed-Pa55word";
const MAIN = {
  name: "main", type: "icecast", host: "relay.example.org", port: 8000, mount: "/live",
  stream_id: null, user: "source", bitrate: null, public: false, enabled: true,
  target: "relay.example.org:8000/live", source: "saved", password_set: true,
  password_source: "saved", password_variable: "AUTODJ_STREAM_RELAY_MAIN_PASSWORD",
  removable: true, revertable: false, state: "connecting", error: null, title_error: null,
};
const TOML = {
  ...MAIN, name: "backup", type: "shoutcast2", host: "sc.example.org", mount: null, user: null,
  stream_id: 1, target: "sc.example.org:8000 stream 1", source: "config", password_source: "config",
  removable: false, revertable: false,
};

const el = (id) => document.getElementById(id);
const rowsOf = () => [...el("stream-relays-list").querySelectorAll("tr")];
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const apiError = (message, { status = 422, field = null } = {}) =>
  Object.assign(new Error(message), { status, field });
const said = () => el("settings-status").textContent;
const stateWith = (relays) => ({ stream_mode: true, stream_relays: relays });

function fakeDialog(dialog) {
  dialog.showModal = function showModal() { this.open = true; };
  dialog.close = function close(value = "") {
    if (!this.open) return;
    this.open = false;
    this.returnValue = value;
    this.dispatchEvent(new Event("close"));
  };
}

// A fake server keeping a list of relays; each request is recorded.
function server(initial = [MAIN, TOML]) {
  let relays = initial.map((relay) => ({ ...relay }));
  const calls = [];
  const request = vi.fn(async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, body });
    if (url === "/api/stream/relays" && !options.method) {
      return { relays: relays.map((relay) => ({ ...relay })), max_relays: 8, problem: null };
    }
    if (url === "/api/stream/relays") {
      const added = { ...MAIN, ...body, password_set: true, target: `${body.host}:${body.port}${body.mount || ""}` };
      delete added.password;
      relays.push(added);
      return { relay: added };
    }
    if (url === "/api/stream/relays/edit") {
      relays = relays.map((relay) => relay.name === body.name ? { ...relay, ...body, password: undefined } : relay);
      return { relay: relays.find((relay) => relay.name === body.name) };
    }
    if (url === "/api/stream/relays/enabled") {
      relays = relays.map((relay) => relay.name === body.name ? { ...relay, enabled: body.enabled } : relay);
      return { relay: relays.find((relay) => relay.name === body.name) };
    }
    if (url === "/api/stream/relays/remove") {
      relays = relays.filter((relay) => relay.name !== body.name);
      return { result: "removed", name: body.name };
    }
    throw new Error(`unexpected ${url}`);
  });
  return { request, calls, relays: () => relays };
}

async function setup(fake = server()) {
  const relays = installRelaySettings({ document, request: fake.request, settingsStatus: el("settings-status") });
  relays.apply(stateWith([]));
  await settle();
  await settle();
  return relays;
}

beforeEach(() => {
  document.body.innerHTML = `<div id="settings-status" role="status" aria-live="polite"></div>
    ${section}${relayDialog}${confirmDialog}<div id="status-toast" hidden></div>`;
  fakeDialog(el("relay-dialog"));
  fakeDialog(el("confirm-dialog"));
});

describe("relay words", () => {
  it("says where the password comes from, never what it is", () => {
    expect(passwordWords(MAIN)).toBe("set here");
    expect(passwordWords(TOML)).toBe("set in the config files");
    expect(passwordWords({ ...MAIN, password_source: "environment" }))
      .toBe("set by AUTODJ_STREAM_RELAY_MAIN_PASSWORD");
    expect(passwordWords({ ...MAIN, password_set: false })).toBe("not set");
    expect(relayServerText(MAIN))
      .toBe("Icecast at relay.example.org:8000/live, the station's quality. Password set here.");
    expect(relayServerText({ ...TOML, bitrate: 128 })).toContain("Shoutcast 2 at sc.example.org:8000 stream 1, 128 kbps");
  });

  it.each([
    [{ state: "live" }, "Live."],
    [{ state: "connecting" }, "Connecting."],
    [{ state: "retrying", error: "the server rejected the password" },
      "Not connected, trying again. Last error: the server rejected the password."],
    [{ state: "off" }, "Off."],
    [{ state: "off", error: "No password is set." }, "Off: No password is set."],
    [{ state: "stopped" }, "Stopped."],
    [{ state: "live", title_error: "the title update got HTTP status 404" },
      "Live. Song titles are not updating: the title update got HTTP status 404."],
  ])("describes %o", (relay, text) => {
    expect(relayStatusText(relay)).toBe(text);
  });

  it("checks the form like the server", () => {
    const good = {
      name: "main", type: "icecast", host: "relay.example.org", port: "8000", mount: "/live",
      user: "", streamId: "1", password: "x", bitrate: "", public: false, enabled: true, passwordRequired: true,
    };
    expect(relayFormError(good)).toBeNull();
    expect(relayFormError({ ...good, name: "bad name" })[0]).toBe("name");
    expect(relayFormError({ ...good, name: "bad name" }, { creating: false })).toBeNull();
    expect(relayFormError({ ...good, host: "http://relay.example.org" })[0]).toBe("host");
    expect(relayFormError({ ...good, port: "65536" })[0]).toBe("port");
    expect(relayFormError({ ...good, type: "shoutcast1", port: "65535" })[0]).toBe("port");
    expect(relayFormError({ ...good, mount: "live" })[0]).toBe("mount");
    expect(relayFormError({ ...good, type: "shoutcast1", mount: "live" })).toBeNull();
    expect(relayFormError({ ...good, user: "a:b" })[0]).toBe("user");
    expect(relayFormError({ ...good, type: "shoutcast2", streamId: "0" })[0]).toBe("stream_id");
    expect(relayFormError({ ...good, password: "" })[0]).toBe("password");
    expect(relayFormError({ ...good, password: "" }, { passwordSet: true })).toBeNull();
    expect(relayFormError({ ...good, password: "", passwordRequired: false })).toBeNull();
  });

  it("sends only what the server type uses, and the password only when typed", () => {
    const values = {
      name: " main ", type: "shoutcast2", host: " sc.example.org ", port: "8010", mount: "/live",
      user: "x", streamId: "3", password: "", bitrate: "128", public: true, enabled: false,
    };
    expect(relayBody(values)).toEqual({
      name: "main", type: "shoutcast2", host: "sc.example.org", port: 8010, bitrate: 128,
      public: true, enabled: false, stream_id: 3,
    });
    expect(relayBody({ ...values, type: "icecast", user: "", password: "p", bitrate: "" }))
      .toMatchObject({ mount: "/live", user: null, password: "p", bitrate: null });
  });
});

describe("relay table", () => {
  it("lists each relay in a grid row with its actions", async () => {
    await setup();
    const rows = rowsOf();
    expect(rows.map((row) => row.dataset.name)).toEqual(["main", "backup"]);
    const [main, backup] = rows;
    expect(main.cells[0].getAttribute("role")).toBe("rowheader");
    expect(main.cells[2].textContent).toBe("Connecting.");
    expect(main.querySelector("button[data-action=edit]").getAttribute("aria-label")).toBe("Edit main");
    expect(main.querySelector("button[data-action=remove]").getAttribute("aria-label")).toBe("Remove main");
    expect(main.querySelector("input[data-action=enabled]").checked).toBe(true);
    // Each On box says which relay it turns on or off.
    expect(main.querySelector("input[data-action=enabled]").closest("label").textContent).toBe(" On, main");
    expect(backup.querySelector("input[data-action=enabled]").closest("label").textContent).toBe(" On, backup");
    // A relay only config.toml defines has no Remove.
    expect(backup.querySelector("button[data-action=remove]")).toBeNull();
    expect(backup.cells[5].textContent).toBe("Set in config.toml");
    expect(el("stream-relays").querySelector("[aria-live]")).toBeNull();
  });

  it("updates the status in place and says nothing while it changes", async () => {
    const relays = await setup();
    const cell = rowsOf()[0].cells[2];
    const row = rowsOf()[0];
    relays.apply(stateWith([{ name: "main", state: "retrying", error: "the server refused the connection" }]));
    expect(rowsOf()[0]).toBe(row);
    expect(cell.textContent).toBe("Not connected, trying again. Last error: the server refused the connection.");
    expect(cell.querySelector(".relay-error") || cell.firstChild.classList?.contains("relay-error")).toBeTruthy();
    relays.apply(stateWith([{ name: "main", state: "live" }]));
    expect(cell.textContent).toBe("Live.");
    expect(said()).toBe("");
  });

  it("loads again when another page added a relay", async () => {
    const fake = server();
    const relays = await setup(fake);
    fake.relays().push({ ...MAIN, name: "other", mount: "/other" });
    relays.apply(stateWith([{ name: "other", state: "live" }]));
    await settle();
    await settle();
    expect(rowsOf().map((row) => row.dataset.name)).toEqual(["main", "backup", "other"]);
  });

  it("shows why the saved relays cannot be changed", async () => {
    const fake = server();
    fake.request.mockImplementationOnce(async () => ({ relays: [], problem: "The saved relay settings (relays.json) could not be read." }));
    await setup(fake);
    expect(el("stream-relays-problem").hidden).toBe(false);
    expect(el("stream-relays-problem").textContent).toContain("relays.json");
    expect(el("stream-relays-list").textContent).toContain("No relays yet");
  });

  it("forgets everything on sign-out", async () => {
    const relays = await setup();
    relays.reset();
    expect(el("stream-relays-list").textContent).toBe("Relays not loaded.");
  });
});

describe("relay dialog", () => {
  it("adds a relay, keeps the password out of the page, and says the first state once", async () => {
    const fake = server([]);
    const relays = await setup(fake);
    el("stream-relay-add").focus();
    el("stream-relay-add").click();
    expect(el("relay-dialog").open).toBe(true);
    expect(el("relay-dialog-title").textContent).toBe("Add relay");
    expect(document.activeElement).toBe(el("relay-name"));
    expect(el("relay-password").value).toBe("");
    expect(el("relay-password-state").textContent).toContain("Not set");
    el("relay-name").value = "main";
    el("relay-host").value = "relay.example.org";
    el("relay-password").value = SECRET;
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el("relay-dialog").open).toBe(false));
    expect(fake.calls.find((call) => call.url === "/api/stream/relays" && call.body).body)
      .toMatchObject({ name: "main", type: "icecast", mount: "/live", password: SECRET });
    expect(el("relay-password").value).toBe("");
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(document.activeElement).toBe(rowsOf()[0].querySelector("button[data-action=edit]"));
    await vi.waitFor(() => expect(said()).toBe("Added main. It is connecting."));
    relays.apply(stateWith([{ name: "main", state: "live" }]));
    await vi.waitFor(() => expect(said()).toBe("main is live."));
    el("settings-status").textContent = "";
    relays.apply(stateWith([{ name: "main", state: "retrying", error: "x" }]));
    relays.apply(stateWith([{ name: "main", state: "live" }]));
    await settle();
    expect(said()).toBe("");
  });

  it("edits a relay without showing or resending its password", async () => {
    const fake = server();
    await setup(fake);
    const edit = rowsOf()[0].querySelector("button[data-action=edit]");
    edit.focus();
    edit.click();
    expect(el("relay-dialog-title").textContent).toBe("Edit main");
    expect(el("relay-name").readOnly).toBe(true);
    expect(el("relay-password").value).toBe("");
    expect(el("relay-password-state").textContent).toBe("A password is set here. Leave this empty to keep it.");
    expect(document.activeElement).toBe(el("relay-type"));
    el("relay-type").value = "shoutcast2";
    el("relay-type").dispatchEvent(new Event("change"));
    expect(el("relay-mount-field").hidden).toBe(true);
    expect(el("relay-stream-id-field").hidden).toBe(false);
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el("relay-dialog").open).toBe(false));
    const sent = fake.calls.find((call) => call.url === "/api/stream/relays/edit").body;
    expect(sent).not.toHaveProperty("password");
    expect(sent).toMatchObject({ type: "shoutcast2", stream_id: 1 });
    expect(document.activeElement.getAttribute("aria-label")).toBe("Edit main");
  });

  it("links a refused field to its error and focuses it", async () => {
    const fake = server();
    await setup(fake);
    el("stream-relay-add").click();
    el("relay-name").value = "main";
    el("relay-host").value = "relay.example.org";
    el("relay-password").value = SECRET;
    fake.request.mockImplementationOnce(async () => {
      throw apiError("A relay named main already exists.", { status: 409, field: "name" });
    });
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el("relay-name").getAttribute("aria-invalid")).toBe("true"));
    expect(el("relay-dialog").open).toBe(true);
    expect(document.activeElement).toBe(el("relay-name"));
    expect(el("relay-name-error").hidden).toBe(false);
    expect(el("relay-name").getAttribute("aria-describedby")).toContain("relay-name-error");
    el("relay-name").dispatchEvent(new Event("input", { bubbles: true }));
    expect(el("relay-name").hasAttribute("aria-invalid")).toBe(false);
    // A refusal for no field is said inside the dialog.
    fake.request.mockImplementationOnce(async () => { throw apiError("Could not save the relay settings.", { status: 503 }); });
    el("relay-name").value = "second";
    el("relay-mount").value = "/second";
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el("relay-form-error").textContent).toContain("Could not save second"));
    // Checked on the page first: nothing is sent.
    const sent = fake.calls.length;
    el("relay-port").value = "0";
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(fake.calls.length).toBe(sent);
    expect(document.activeElement).toBe(el("relay-port"));
  });

  it("closes and shows what was saved when the relay could not start after saving", async () => {
    const fake = server([]);
    await setup(fake);
    const plain = fake.request.getMockImplementation();
    fake.request.mockImplementation(async (url, options = {}) => {
      const reply = await plain(url, options);
      return url === "/api/stream/relays" && options.method
        ? { ...reply, warning: "Saved, but the encoder for main could not start, so the relay is not running yet." }
        : reply;
    });
    el("stream-relay-add").click();
    el("relay-name").value = "main";
    el("relay-host").value = "relay.example.org";
    el("relay-password").value = SECRET;
    el("relay-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(el("relay-dialog").open).toBe(false));
    expect(rowsOf().map((row) => row.dataset.name)).toEqual(["main"]);
    await vi.waitFor(() => expect(said()).toContain("Saved, but the encoder for main could not start"));
  });

  it("does not offer a password field the environment overrides", async () => {
    await setup(server([{ ...MAIN, password_source: "environment" }]));
    rowsOf()[0].querySelector("button[data-action=edit]").click();
    expect(el("relay-password-field").hidden).toBe(true);
    expect(el("relay-password-env").hidden).toBe(false);
    expect(el("relay-password-env").textContent).toContain("AUTODJ_STREAM_RELAY_MAIN_PASSWORD");
  });

  it("cancel puts focus back on the button that opened it", async () => {
    await setup();
    const edit = rowsOf()[1].querySelector("button[data-action=edit]");
    edit.focus();
    edit.click();
    el("relay-password").value = "typed";
    el("relay-cancel").click();
    expect(el("relay-dialog").open).toBe(false);
    expect(el("relay-password").value).toBe("");
    expect(document.activeElement).toBe(edit);
  });
});

describe("relay actions", () => {
  it("turns a relay off and on, keeping focus on its box", async () => {
    const fake = server();
    await setup(fake);
    const box = () => rowsOf()[0].querySelector("input[data-action=enabled]");
    box().focus();
    box().checked = false;
    box().dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(said()).toBe("main turned off."));
    expect(fake.relays()[0].enabled).toBe(false);
    expect(document.activeElement).toBe(box());
    fake.request.mockImplementationOnce(async () => { throw apiError("main cannot be turned on: No password is set.", { status: 409 }); });
    box().checked = true;
    box().dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(said()).toContain("Could not turn main on"));
    expect(box().checked).toBe(false);
  });

  it("removes a relay after asking, handing focus to the next row", async () => {
    const fake = server([MAIN, { ...MAIN, name: "second", mount: "/second" }]);
    await setup(fake);
    rowsOf()[0].querySelector("button[data-action=remove]").click();
    expect(el("confirm-dialog").open).toBe(true);
    expect(el("confirm-title").textContent).toBe("Remove main?");
    el("confirm-accept").click();
    await vi.waitFor(() => expect(el("confirm-dialog").open).toBe(false));
    expect(rowsOf().map((row) => row.dataset.name)).toEqual(["second"]);
    expect(document.activeElement.getAttribute("aria-label")).toBe("Remove second");
    await vi.waitFor(() => expect(said()).toBe("Removed main."));
  });

  it("reverts a relay changed here to its config.toml settings", async () => {
    const fake = server([{ ...TOML, source: "config_saved", revertable: true }]);
    await setup(fake);
    const revert = rowsOf()[0].querySelector("button[data-action=remove]");
    expect(revert.textContent).toBe("Revert");
    expect(revert.getAttribute("aria-label")).toBe("Revert backup to config.toml");
  });
});
