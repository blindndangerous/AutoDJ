import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), requestBinary: vi.fn() }));
vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => ({
  ...(await importOriginal()),
  requestJson: api.requestJson,
  requestBinary: api.requestBinary,
}));

import { installCartWall } from "../../src/autodj/static/modules/cart-wall.js";

const byId = (id) => document.getElementById(id);
const cart = (number) => byId("cart-slots").querySelector(`[data-slot="${number}"]`);

function installIndex() {
  const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll("script, link").forEach((node) => node.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  byId("panel-carts").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

// A stand-in for the cart routes, holding the wall the way the server does.
function fakeServer() {
  const pages = ["Page 1", "Page 2", "Page 3", "Page 4"].map((name) => ({
    name, slots: Array(12).fill(null),
  }));
  pages[0].slots[2] = { name: "Station ID", source: "liner", clip: "rec-1", mode: "duck", duration: 7.4 };
  pages[1].slots[0] = { name: "Sweeper", source: "upload", clip: "sweep.wav", mode: "over", duration: 61 };
  const server = {
    wall: { schema_version: 1, active_page: 1, pages },
    liners: [{ id: "rec-1", name: "station-id.wav" }, { id: "news.wav", name: "news.wav" }],
    uploads: ["sweep.wav"],
    calls: [],
    playUrl: "/api/liners/file/rec-1",
  };
  api.requestJson.mockReset().mockImplementation(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = typeof options.body === "string" ? JSON.parse(options.body) : options.body;
    server.calls.push({ method, url, body });
    if (url === "/api/carts") return JSON.parse(JSON.stringify(server));
    let match = url.match(/^\/api\/carts\/pages\/(\d+)\/slots\/(\d+)(\/play)?$/);
    if (match && match[3]) {
      return { label: "Cart", mode: "replace", duck_db: -12, duration: 7.4, url: server.playUrl };
    }
    if (match && method === "PUT") {
      server.wall.pages[match[1] - 1].slots[match[2] - 1] = { ...body, duration: 3 };
      return { slot: body };
    }
    if (match && method === "DELETE") {
      server.wall.pages[match[1] - 1].slots[match[2] - 1] = null;
      return { cleared: true };
    }
    match = url.match(/^\/api\/carts\/active-page\/(\d+)$/);
    if (match) {
      server.wall.active_page = Number(match[1]);
      return { active_page: Number(match[1]) };
    }
    match = url.match(/^\/api\/carts\/pages\/(\d+)\/name$/);
    if (match) {
      server.wall.pages[match[1] - 1].name = body.name.trim();
      return { name: body.name.trim() };
    }
    if (url === "/api/carts/upload") {
      server.uploads.push("bed.wav");
      return { name: "bed.wav", size: 4 };
    }
    if (url.startsWith("/api/carts/uploads/") && method === "DELETE") {
      server.uploads = server.uploads.filter((name) => `/api/carts/uploads/${name}` !== url);
      return { deleted: true };
    }
    if (url === "/api/carts/stop") return { stopped: server.stopped ?? false };
    throw new Error(`unexpected ${method} ${url}`);
  });
  api.requestBinary.mockReset().mockResolvedValue(new ArrayBuffer(8));
  return server;
}

function press(target, key, options = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...options });
  target.dispatchEvent(event);
  target.dispatchEvent(new KeyboardEvent("keyup", { key, bubbles: true }));
  return event;
}

const plays = (server) => server.calls.filter((call) => call.url.endsWith("/play"));

async function install(hooks = {}) {
  installIndex();
  const server = fakeServer();
  const controls = installCartWall(undefined, hooks);
  await controls.refresh();
  return { server, controls };
}

afterEach(() => vi.unstubAllGlobals());

describe("cart wall markup", () => {
  it("is a Carts main tab right after History, with a panel heading and no aria-controls", () => {
    installIndex();
    const tab = byId("tab-carts");
    expect(byId("tab-history").nextElementSibling).toBe(tab);
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.textContent).toBe("Carts");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    const panel = byId("panel-carts");
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-carts");
    expect(panel.querySelector(":scope > h2").textContent).toBe("Carts");
    expect(panel.contains(byId("cart-wall"))).toBe(true);
  });

  it("labels every control and shows the key help as visible text", () => {
    installIndex();
    const wall = byId("cart-wall");
    for (const control of wall.querySelectorAll("input, select")) {
      const label = document.querySelector(`label[for="${control.id}"]`);
      expect(label?.textContent.trim(), control.id).toBeTruthy();
    }
    for (const button of wall.querySelectorAll("button")) {
      expect(button.getAttribute("type")).toBe("button");
      expect(button.textContent.trim()).toBeTruthy();
    }
    const help = byId("cart-keys-help");
    expect(help.closest(".visually-hidden")).toBeNull();
    expect(help.textContent).toMatch(/1 to 9 and 0, minus and equals play carts 1 to 12/);
    expect(help.textContent).toMatch(/Page Up and Page Down change the page/);
    expect(byId("cart-mode").hasAttribute("aria-describedby")).toBe(false);
    expect(byId("cart-mode-desc").hidden).toBe(false);
    expect(byId("cart-status").getAttribute("role")).toBe("status");
    const shortcuts = byId("hotkey-help-modal").textContent;
    expect(shortcuts).toMatch(/Play carts 1 to 12/);
    expect(shortcuts).toMatch(/next cart page/);
  });
});

describe("cart buttons", () => {
  it("names each cart by number, name and length, or as empty", async () => {
    await install();
    const buttons = byId("cart-slots").querySelectorAll("li > button");
    expect(buttons).toHaveLength(12);
    expect(cart(3).textContent).toBe("Cart 3, Station ID, 0:07");
    expect(cart(4).textContent).toBe("Cart 4, empty");
    expect(cart(4).disabled).toBe(false);
    expect([...byId("cart-edit-slot").options].slice(2, 4).map((item) => item.textContent))
      .toEqual(["Cart 3, Station ID, 0:07", "Cart 4, empty"]);
    expect([...byId("cart-page").options].map((item) => item.textContent))
      .toEqual(["Page 1", "Page 2", "Page 3", "Page 4"]);
  });

  it("plays a cart from its button through the page's liner path in browser playback", async () => {
    const playAudio = vi.fn().mockResolvedValue(true);
    const stopAudio = vi.fn();
    const { server } = await install({ playAudio, stopAudio });
    cart(3).click();
    await vi.waitFor(() => expect(playAudio).toHaveBeenCalledOnce());
    expect(plays(server)).toEqual([{ method: "POST", url: "/api/carts/pages/1/slots/3/play", body: undefined }]);
    expect(api.requestBinary).toHaveBeenCalledWith("/api/liners/file/rec-1");
    expect(playAudio).toHaveBeenCalledWith(expect.any(ArrayBuffer), "replace", -12);
    // One cart at a time, as in the server mix.
    expect(stopAudio).toHaveBeenCalledOnce();
  });

  it("leaves playing to the server when it mixes the audio", async () => {
    const playAudio = vi.fn();
    const { server } = await install({ playAudio });
    server.playUrl = null;
    cart(3).click();
    await vi.waitFor(() => expect(plays(server)).toHaveLength(1));
    await Promise.resolve();
    expect(api.requestBinary).not.toHaveBeenCalled();
    expect(playAudio).not.toHaveBeenCalled();
  });

  it("says why this page cannot play a cart instead of asking the server", async () => {
    const { server } = await install({ prepare: () => "Muted, so the cart was not played." });
    cart(3).click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Muted, so the cart was not played."));
    expect(plays(server)).toHaveLength(0);
  });

  it("says why an expired cart plays nothing, by mouse and by its key", async () => {
    const { server } = await install();
    const refusal = "Cart 3, Station ID was skipped: it is expired after 26 December 2026";
    api.requestJson.mockImplementation(async (url) => {
      if (url.endsWith("/play")) throw new Error(refusal);
      return JSON.parse(JSON.stringify(server));
    });
    cart(3).click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe(`Cart 3 did not play. ${refusal}`));
    expect(byId("cart-status").getAttribute("role") || byId("cart-status").getAttribute("aria-live")).toBeTruthy();
    byId("cart-status").textContent = "";
    cart(1).focus();
    press(cart(1), "3");
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe(`Cart 3 did not play. ${refusal}`));
  });

  it("reports a cart the server refuses", async () => {
    const { server } = await install();
    api.requestJson.mockImplementationOnce(async () => { throw new Error("Playback is paused"); });
    cart(3).click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Cart 3 did not play. Playback is paused"));
    expect(plays(server)).toHaveLength(0);
  });

  it("opens an empty cart in the editor with focus on its name", async () => {
    await install();
    cart(4).click();
    expect(byId("cart-edit-slot").value).toBe("4");
    expect(document.activeElement).toBe(byId("cart-name"));
    expect(byId("cart-name").value).toBe("");
    await vi.waitFor(() => expect(byId("cart-status").textContent).toMatch(/^Cart 4 is empty/));
  });
});

describe("cart keys", () => {
  it("play carts 1 to 12 only with focus on the wall, outside text fields", async () => {
    const playAudio = vi.fn().mockResolvedValue(true);
    const { server } = await install({ playAudio });
    cart(1).focus();
    expect(press(cart(1), "3").defaultPrevented).toBe(true);
    await vi.waitFor(() => expect(playAudio).toHaveBeenCalledOnce());
    expect(plays(server)[0].url).toBe("/api/carts/pages/1/slots/3/play");

    press(cart(1), "=");
    // Cart 12 is empty: reported, not played.
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Cart 12 is empty."));

    for (const target of [byId("cart-name"), byId("cart-page"), byId("cart-clip"), byId("search-input"), document.body]) {
      expect(press(target, "3").defaultPrevented, target.id || target.tagName).toBe(false);
    }
    expect(press(cart(1), "3", { ctrlKey: true }).defaultPrevented).toBe(false);
    expect(press(cart(1), "3", { shiftKey: true }).defaultPrevented).toBe(false);
    expect(press(cart(1), "a").defaultPrevented).toBe(false);
    expect(plays(server)).toHaveLength(1);
  });

  it("acts once per press while a key is held", async () => {
    const { server } = await install();
    const down = () => cart(3).dispatchEvent(new KeyboardEvent("keydown", { key: "3", bubbles: true, cancelable: true }));
    down();
    down();
    cart(3).dispatchEvent(new KeyboardEvent("keydown", { key: "3", bubbles: true, repeat: true, cancelable: true }));
    await vi.waitFor(() => expect(plays(server)).toHaveLength(1));
    cart(3).dispatchEvent(new KeyboardEvent("keyup", { key: "3", bubbles: true }));
    down();
    await vi.waitFor(() => expect(plays(server)).toHaveLength(2));
  });

  it("change the page with Page Up and Page Down, keeping focus on the same cart", async () => {
    const { server } = await install();
    const button = cart(1);
    button.focus();
    press(button, "PageUp");
    expect(server.calls.some((call) => call.url.startsWith("/api/carts/active-page"))).toBe(false);
    expect(press(button, "PageDown").defaultPrevented).toBe(true);
    expect(cart(1)).toBe(button);
    expect(document.activeElement).toBe(button);
    expect(button.textContent).toBe("Cart 1, Sweeper, 1:01");
    expect(byId("cart-page").value).toBe("2");
    expect(byId("cart-page-name").value).toBe("Page 2");
    await vi.waitFor(() => expect(server.wall.active_page).toBe(2));
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Page 2, page 2 of 4."));
    for (let i = 0; i < 4; i += 1) press(button, "PageDown");
    await vi.waitFor(() => expect(server.wall.active_page).toBe(4));
  });

  it("are off when the keyboard shortcuts are turned off", async () => {
    // Node's own localStorage shadows happy-dom's, so a plain one stands in.
    vi.stubGlobal("localStorage", { getItem: (key) => (key === "autodj.keyboardShortcuts" ? "off" : null) });
    vi.resetModules();
    const { installCartWall: fresh } = await import("../../src/autodj/static/modules/cart-wall.js");
    installIndex();
    const server = fakeServer();
    const controls = fresh();
    await controls.refresh();
    expect(press(cart(3), "3").defaultPrevented).toBe(false);
    expect(plays(server)).toHaveLength(0);
    cart(3).click();
    await vi.waitFor(() => expect(plays(server)).toHaveLength(1));
  });
});

describe("start, end and stop", () => {
  it("are spoken once each, from any tab, through the page's status region", async () => {
    const stopAudio = vi.fn();
    const { controls } = await install({ stopAudio });
    byId("panel-carts").hidden = true;
    const spoken = [];
    const region = byId("sr-status");
    new globalThis.MutationObserver(() => { if (region.textContent) spoken.push(region.textContent); })
      .observe(region, { childList: true, characterData: true, subtree: true });
    const event = (name) => controls.event({ type: "cart", event: name, page: 1, slot: 3, label: "Cart 3, Station ID" });
    event("start");
    await vi.waitFor(() => expect(spoken).toEqual(["Cart 3, Station ID, playing."]));
    event("end");
    await vi.waitFor(() => expect(spoken).toHaveLength(2));
    expect(stopAudio).not.toHaveBeenCalled();
    event("stop");
    await vi.waitFor(() => expect(spoken).toHaveLength(3));
    expect(spoken).toEqual([
      "Cart 3, Station ID, playing.", "Cart 3, Station ID, ended.", "Cart 3, Station ID, stopped.",
    ]);
    expect(stopAudio).toHaveBeenCalledOnce();
    expect(byId("cart-status").textContent).toBe("");
  });

  it("Stop carts stops this page's audio and asks the server", async () => {
    const stopAudio = vi.fn();
    const { server } = await install({ stopAudio });
    byId("cart-stop").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("No cart is playing."));
    expect(stopAudio).toHaveBeenCalledOnce();
    server.stopped = true;
    byId("cart-stop").click();
    await vi.waitFor(() => expect(server.calls.filter((call) => call.url === "/api/carts/stop")).toHaveLength(2));
  });
});

describe("cart editor", () => {
  it("asks for a name and a clip before saving, with focus on the field", async () => {
    const { server, controls } = await install();
    server.uploads = [];
    await controls.refresh();
    cart(4).click();
    byId("cart-save").click();
    expect(byId("cart-name").getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(byId("cart-name"));
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Enter a cart name."));
    byId("cart-name").value = "News";
    byId("cart-name").dispatchEvent(new Event("input"));
    expect(byId("cart-name").hasAttribute("aria-invalid")).toBe(false);

    const source = byId("cart-source");
    source.value = "upload";
    source.dispatchEvent(new Event("change"));
    const clip = byId("cart-clip");
    expect([...clip.options].map((item) => [item.value, item.textContent]))
      .toEqual([["", "No uploaded clips yet"]]);
    byId("cart-save").click();
    expect(clip.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(clip);
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Choose a clip, or upload one first."));
    expect(server.calls.some((call) => call.method === "PUT")).toBe(false);
  });

  it("saves the name, clip and mode and announces the new cart name", async () => {
    const { server } = await install();
    const slotSelect = byId("cart-edit-slot");
    slotSelect.value = "3";
    slotSelect.dispatchEvent(new Event("change"));
    expect(byId("cart-name").value).toBe("Station ID");
    expect(byId("cart-clip").value).toBe("rec-1");
    byId("cart-name").value = "News ID";
    byId("cart-clip").value = "news.wav";
    byId("cart-mode").value = "replace";
    byId("cart-save").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Saved. Cart 3, News ID, 0:03."));
    expect(server.calls.find((call) => call.method === "PUT" && call.url.includes("/slots/")).body)
      .toEqual({ name: "News ID", source: "liner", clip: "news.wav", mode: "replace" });
    expect(cart(3).textContent).toBe("Cart 3, News ID, 0:03");
  });

  it("saves play dates and says in words when a cart is outside them", async () => {
    const { server, controls } = await install();
    server.today = "2026-12-27";
    server.wall.pages[0].slots[2].not_after = "2026-12-26";
    server.wall.pages[0].slots[0] = { name: "Advent", source: "liner", clip: "rec-1", mode: "duck", not_before: "2026-12-28" };
    await controls.refresh();
    expect(cart(3).textContent).toBe("Cart 3, Station ID, 0:07, expired");
    expect(cart(1).textContent).toBe("Cart 1, Advent, not playable yet");
    expect([...byId("cart-edit-slot").options][2].textContent).toBe("Cart 3, Station ID, 0:07, expired");

    byId("cart-edit-slot").value = "3";
    byId("cart-edit-slot").dispatchEvent(new Event("change"));
    expect(byId("cart-not-after").value).toBe("2026-12-26");
    expect(byId("cart-not-before").value).toBe("");
    byId("cart-not-before").value = "2026-12-01";
    byId("cart-save").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Saved. Cart 3, Station ID, 0:03, expired."));
    expect(server.calls.find((call) => call.method === "PUT" && call.url.includes("/slots/")).body).toEqual({
      name: "Station ID", source: "liner", clip: "rec-1", mode: "duck",
      not_before: "2026-12-01", not_after: "2026-12-26",
    });
  });

  it("names a backwards date range in the editor and links it to the field", async () => {
    const { server } = await install();
    byId("cart-edit-slot").value = "3";
    byId("cart-edit-slot").dispatchEvent(new Event("change"));
    const until = byId("cart-not-after");
    byId("cart-not-before").value = "2026-12-26";
    until.value = "2026-11-27";
    byId("cart-save").click();
    const message = "Do not play after must be on or after the Not before date.";
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("cart-dates-desc cart-edit-error");
    expect(byId("cart-edit-error").textContent).toBe(message);
    expect(byId("cart-edit-error").hidden).toBe(false);
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe(message));
    until.value = "2026-12-31";
    until.dispatchEvent(new Event("input"));
    expect(until.getAttribute("aria-describedby")).toBe("cart-dates-desc");
    expect(server.calls.some((call) => call.method === "PUT")).toBe(false);
  });

  it("links a server refusal of the dates to its field", async () => {
    const { server } = await install();
    api.requestJson.mockImplementation(async (url, options = {}) => {
      if (options.method === "PUT") {
        const error = new Error("Do not play after must be on or after the Not before date");
        error.field = "not_after";
        throw error;
      }
      return JSON.parse(JSON.stringify(server));
    });
    byId("cart-edit-slot").value = "3";
    byId("cart-edit-slot").dispatchEvent(new Event("change"));
    byId("cart-not-after").value = "2026-12-26";
    byId("cart-save").click();
    const until = byId("cart-not-after");
    const message = "Could not save cart 3. Do not play after must be on or after the Not before date";
    await vi.waitFor(() => expect(byId("cart-edit-error").textContent).toBe(message));
    expect(document.activeElement).toBe(until);
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("cart-dates-desc cart-edit-error");
    expect(byId("cart-status").textContent).toBe(message);
  });

  it("says a cart has expired when the station's day changes, without moving focus", async () => {
    let now = new Date("2026-12-27T06:50:00Z"); // 23:50 on 26 December in Denver
    const ticks = [];
    installIndex();
    const server = fakeServer();
    server.today = "2026-12-26";
    server.timezone = "America/Denver";
    server.wall.pages[0].slots[2].not_after = "2026-12-26";
    const controls = installCartWall(undefined, {
      now: () => now, interval: (callback) => ticks.push(callback),
    });
    await controls.refresh();
    expect(cart(3).textContent).toBe("Cart 3, Station ID, 0:07");
    cart(3).focus();
    const option = byId("cart-edit-slot").options[2];
    const status = byId("cart-status").textContent;
    ticks.forEach((tick) => tick());
    expect(cart(3).textContent).toBe("Cart 3, Station ID, 0:07"); // still 26 December there
    now = new Date("2026-12-27T07:01:00Z");
    ticks.forEach((tick) => tick());
    expect(cart(3).textContent).toBe("Cart 3, Station ID, 0:07, expired");
    expect(byId("cart-edit-slot").options[2]).toBe(option); // relabelled, not rebuilt
    expect(option.textContent).toBe("Cart 3, Station ID, 0:07, expired");
    expect(document.activeElement).toBe(cart(3));
    expect(byId("cart-status").textContent).toBe(status); // nothing announced
    expect(server.calls.filter((call) => call.url === "/api/carts")).toHaveLength(1);
  });

  it("clears a cart", async () => {
    const { server } = await install();
    byId("cart-edit-slot").value = "3";
    byId("cart-edit-slot").dispatchEvent(new Event("change"));
    byId("cart-clear").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Cart 3 cleared."));
    expect(server.wall.pages[0].slots[2]).toBeNull();
    expect(cart(3).textContent).toBe("Cart 3, empty");
  });

  it("keeps what is being typed when another page changes the wall", async () => {
    const { server, controls } = await install();
    cart(4).click();
    byId("cart-name").value = "Half typed";
    server.wall.pages[0].slots[5] = { name: "Promo", source: "liner", clip: "news.wav", mode: "duck", duration: 30 };
    controls.event({ type: "cart_changed" });
    await vi.waitFor(() => expect(cart(6).textContent).toBe("Cart 6, Promo, 0:30"));
    expect(byId("cart-name").value).toBe("Half typed");
    expect(byId("cart-edit-slot").value).toBe("4");
  });

  it("renames the page shown", async () => {
    const { server } = await install();
    byId("cart-page-name").value = "  ";
    byId("cart-rename").click();
    expect(document.activeElement).toBe(byId("cart-page-name"));
    byId("cart-page-name").value = "Jingles";
    byId("cart-rename").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Page renamed to Jingles."));
    expect(server.wall.pages[0].name).toBe("Jingles");
    expect(byId("cart-page").options[0].textContent).toBe("Jingles");
  });
});

describe("uploaded clips", () => {
  it("uploads a clip and chooses it for the cart being edited", async () => {
    const { server } = await install();
    const input = byId("cart-upload-file");
    // The Uploaded clips card is closed by default; Upload with no file
    // opens it before focus moves to the file field.
    const card = input.closest("details");
    expect(card.open).toBe(false);
    byId("cart-upload").click();
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(card.open).toBe(true);
    expect(document.activeElement).toBe(input);
    Object.defineProperty(input, "files", { value: [new globalThis.File(["RIFF"], "bed.wav")], configurable: true });
    byId("cart-upload").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toMatch(/^Uploaded bed.wav/));
    expect(server.calls.find((call) => call.url === "/api/carts/upload").body).toBeInstanceOf(globalThis.FormData);
    expect(byId("cart-source").value).toBe("upload");
    expect(byId("cart-clip").value).toBe("bed.wav");
    expect(byId("cart-delete-upload").disabled).toBe(false);
  });

  it("deletes an uploaded clip after confirming, then focuses the clip list", async () => {
    const { server } = await install();
    expect(byId("cart-delete-upload").disabled).toBe(true);
    const source = byId("cart-source");
    source.value = "upload";
    source.dispatchEvent(new Event("change"));
    expect(byId("cart-clip").value).toBe("sweep.wav");
    byId("cart-delete-upload").click();
    const dialog = byId("confirm-dialog");
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Delete uploaded clip sweep.wav?");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("cart-status").textContent).toBe("Deleted sweep.wav."));
    expect(server.uploads).toEqual([]);
    expect(document.activeElement).toBe(byId("cart-clip"));
    expect(byId("cart-clip").options[0].textContent).toBe("No uploaded clips yet");
    expect(byId("cart-delete-upload").disabled).toBe(true);
  });
});

describe("signing out", () => {
  it("forgets the wall and stops the audio", async () => {
    const stopAudio = vi.fn();
    const { controls } = await install({ stopAudio });
    controls.clear();
    expect(cart(3).textContent).toBe("Cart 3, empty");
    expect(byId("cart-page").options).toHaveLength(0);
    expect(stopAudio).toHaveBeenCalledOnce();
    expect(press(cart(3), "3").defaultPrevented).toBe(false);
  });
});
