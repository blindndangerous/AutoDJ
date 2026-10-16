
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import { installTimeChecks } from "../../src/autodj/static/modules/time-checks.js";

// Voice packs added from a zip and removed, and clips chosen and uploaded,
// in the real Settings markup (#388).

const byId = (id) => document.getElementById(id);
const said = () => byId("tc-status").textContent.replace(/\u00a0$/, "");
const liveText = () => vi.waitFor(() => {
  const text = said();
  if (!text) throw new Error("nothing said yet");
  return text;
});

function pack(name, extra = {}) {
  return { name, enabled: true, minutes_covered: 1440, summary: "All covered.",
    source: "station", removable: true, ...extra };
}

function state(overrides = {}) {
  return { enabled: true, hour_format: "12", rule: "off", timezone: "UTC", duck_db: -12,
    missing_minutes: 1440, tts_available: false, enabled_packs: [], pack_order: "rotate",
    recorded_folder: "", intro: null, outro: null, folders: [], clip_files: [],
    intro_config: null, outro_config: null, pack_upload_max_mb: 256, clip_upload_max_mb: 50,
    packs: [], ...overrides };
}

function apiError(message, status, field = null) {
  const error = new Error(message);
  error.status = status;
  error.field = field;
  return error;
}

function choose(input, name, type = "application/zip") {
  const file = new globalThis.File(["data"], name, { type });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  return file;
}

beforeEach(installIndex);

describe("Time checks panel text and controls", () => {
  it("no longer sends people to config.toml for packs and clips", () => {
    const panel = byId("spanel-time-checks");
    expect(panel.querySelector(".card-desc").textContent).toContain(
      "Only the text-to-speech command is set in config.toml.");
    expect(byId("tc-packs-empty").textContent).not.toContain("config.toml");
    expect(byId("tc-packs-desc").textContent).not.toContain("config.toml");
  });

  it("labels the upload, name and clip controls with visible labels", () => {
    for (const [id, text] of [
      ["tc-pack-file", "Voice pack zip file"], ["tc-pack-name", "Pack name"],
      ["tc-clip-file", "Clip file"], ["tc-recorded-folder", "Recorded minute clips folder"],
      ["tc-intro", "Intro clip"], ["tc-outro", "Outro clip"],
    ]) {
      expect(document.querySelector(`label[for="${id}"]`)?.textContent.trim(), id).toBe(text);
    }
    expect(byId("tc-pack-file").type).toBe("file");
    expect(byId("tc-pack-file").accept).toContain(".zip");
    expect(byId("tc-clip-file").accept).toContain(".wav");
    // Each new control says its help when it takes focus.
    for (const [id, help] of [
      ["tc-pack-name", "tc-pack-name-desc"], ["tc-pack-file", "tc-add-pack-desc"],
      ["tc-clip-file", "tc-clip-file-desc"], ["tc-recorded-folder", "tc-recorded-folder-desc"],
      ["tc-intro", "tc-clip-choice-desc"], ["tc-outro", "tc-clip-choice-desc"],
      ["tc-rescan", "tc-rescan-desc"],
    ]) {
      expect(byId(id).getAttribute("aria-describedby"), id).toBe(help);
      expect(byId(help)?.textContent.trim(), id).toBeTruthy();
    }
    for (const id of ["tc-pack-error", "tc-clip-error"]) {
      expect(byId(id).classList.contains("field-error"), id).toBe(true);
      expect(byId(id).hidden, id).toBe(true);
    }
    for (const id of ["tc-pack-add", "tc-clip-add", "tc-rescan"]) {
      expect(byId(id).tagName, id).toBe("BUTTON");
      expect(byId(id).type, id).toBe("button");
    }
    expect(byId("tc-add-pack").querySelector("legend").textContent).toBe("Add a voice pack");
    expect(byId("tc-add-clip").querySelector("legend").textContent).toBe("Upload a clip");
    // Focus goes to the group after the last pack is removed.
    expect(byId("tc-packs").getAttribute("tabindex")).toBe("-1");
  });
});

describe("adding and removing voice packs", () => {
  it("gives a pack kept in the time checks folder a named Remove button, and no other", async () => {
    const request = vi.fn(async () => state({ packs: [
      pack("Studio"), pack("Listed", { source: "config", removable: false }),
      pack("Shared", { source: "shared", removable: false })] }));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const buttons = [...byId("tc-pack-list").querySelectorAll("button")];
    expect(buttons.map((button) => [button.textContent, button.getAttribute("aria-label")])).toEqual([
      ["Remove", "Remove voice pack Studio"]]);
    const descs = [...byId("tc-pack-list").querySelectorAll("input")].map(
      (box) => byId(box.getAttribute("aria-describedby")).textContent);
    expect(descs).toEqual(["All covered.", "All covered. Listed in config.toml.",
      "All covered. From the library's shared time checks folder."]);
  });

  it("uploads the zip with its name, focuses the new pack and says it", async () => {
    let packs = [pack("Night")];
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "POST") {
        packs = [pack("Night"), pack("Studio", { enabled: false })];
        return state({ packs, added: "Studio" });
      }
      return state({ packs });
    });
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "voices.zip");
    byId("tc-pack-name").value = "  Studio ";
    const add = byId("tc-pack-add");
    add.focus();
    add.click();
    // Busy while it uploads: aria-busy, never aria-disabled.
    expect(add.getAttribute("aria-busy")).toBe("true");
    expect(add.hasAttribute("aria-disabled")).toBe(false);
    expect(await liveText()).toBe("Added voice pack Studio. Tick it to use it.");
    const [url, options] = request.mock.calls.find(([, call]) => call?.method === "POST");
    expect(url).toBe("/api/time-checks/packs?name=Studio");
    expect(options.body.get("file").name).toBe("voices.zip");
    expect(document.activeElement).toBe(
      [...byId("tc-pack-list").querySelectorAll("input")].find((box) => box.value === "Studio"));
    expect(byId("tc-pack-name").value).toBe("");
    await vi.waitFor(() => expect(add.hasAttribute("aria-busy")).toBe(false));
  });

  it("asks before sending when the typed name is a pack added here, and sends once", async () => {
    const request = vi.fn(async () => state({ packs: [pack("Studio")], added: "Studio" }));
    const confirm = vi.fn(async () => true);
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "voices.zip");
    byId("tc-pack-name").value = "studio";
    byId("tc-pack-add").click();
    await vi.waitFor(() => expect(said()).toBe("Replaced voice pack Studio. It is ticked."));
    expect(confirm).toHaveBeenCalledWith(document, expect.objectContaining({
      title: "Replace the voice pack?", confirmLabel: "Replace voice pack", station: true }));
    expect(confirm.mock.calls[0][1].message).toContain("A voice pack named Studio already exists.");
    expect(confirm.mock.calls[0][1].message).not.toContain("Other stations");
    const posts = request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts.map(([url]) => url)).toEqual(["/api/time-checks/packs?name=studio&replace=true"]);
  });

  it("does not guess a blank name from the zip's file name, and leaves the asking to the server", async () => {
    // Studio.zip may hold a folder named Studio Voice: the server names the
    // pack after that folder, so asking to replace Studio would be wrong.
    const request = vi.fn(async (url, options = {}) => (options.method === "POST"
      ? state({ packs: [pack("Studio"), pack("Studio Voice", { enabled: false })], added: "Studio Voice" })
      : state({ packs: [pack("Studio")] })));
    const confirm = vi.fn(async () => true);
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "Studio.zip");
    byId("tc-pack-add").click();
    await vi.waitFor(() => expect(said()).toBe("Added voice pack Studio Voice. Tick it to use it."));
    expect(confirm).not.toHaveBeenCalled();
    const posts = request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts.map(([url]) => url)).toEqual(["/api/time-checks/packs"]);
  });

  it("says where a folder of minute clips went, and focuses the folder choice", async () => {
    const request = vi.fn(async (url, options = {}) => (options.method === "POST"
      ? state({ packs: [pack("Night")], folders: ["Minutes"], added: "Minutes" })
      : state({ packs: [pack("Night")] })));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "Minutes.zip");
    byId("tc-pack-add").click();
    await vi.waitFor(() => expect(said()).toBe(
      "Added folder Minutes of minute clips. Choose it under Recorded minute clips folder."));
    expect(document.activeElement).toBe(byId("tc-recorded-folder"));
  });

  it("asks when the server finds the name taken, then sends it again with replace", async () => {
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "POST" && !url.includes("replace=true")) {
        throw apiError("A voice pack named Studio already exists", 409, "replace");
      }
      return state({ packs: [pack("Studio", { shared_with_stations: true })], added: "Studio" });
    });
    const confirm = vi.fn(async () => true);
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    // The zip's own folder names the pack, so the page could not tell.
    choose(byId("tc-pack-file"), "voices.zip");
    byId("tc-pack-add").click();
    await vi.waitFor(() => expect(said()).toBe("Replaced voice pack Studio. It is ticked."));
    expect(confirm.mock.calls[0][1].message).toContain("A voice pack named Studio already exists.");
    expect(confirm.mock.calls[0][1].message).toContain("Other stations use this pack too.");
    const posts = request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts.map(([url]) => url)).toEqual([
      "/api/time-checks/packs", "/api/time-checks/packs?replace=true"]);
  });

  it("says the upload has started before it is sent", async () => {
    let release;
    const request = vi.fn((url, options = {}) => (options.method === "POST"
      ? new Promise((resolve) => {
        release = () => resolve(state({ packs: [pack("Night")], added: "Night" }));
      })
      : Promise.resolve(state())));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "Night.zip");
    byId("tc-pack-add").click();
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    await vi.waitFor(() => expect(said()).toBe(
      "Uploading the voice pack. This can take a few minutes."));
    release();
    await vi.waitFor(() => expect(said()).toBe("Added voice pack Night. It is ticked."));
  });

  it("shows a refused upload at the file input, focused and linked, until another file is chosen", async () => {
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "POST") throw apiError("The upload is not a zip file", 400);
      return state();
    });
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const input = byId("tc-pack-file");
    choose(input, "notes.zip");
    byId("tc-pack-add").click();
    const message = "Could not add the voice pack. The upload is not a zip file";
    await vi.waitFor(() => expect(said()).toBe(message));
    expect(document.activeElement).toBe(input);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(input.getAttribute("aria-describedby")).toBe("tc-pack-error tc-add-pack-desc");
    expect(byId("tc-pack-error").textContent).toBe(message);
    expect(byId("tc-pack-error").hidden).toBe(false);
    input.dispatchEvent(new Event("change"));
    expect(input.hasAttribute("aria-invalid")).toBe(false);
    expect(input.getAttribute("aria-describedby")).toBe("tc-add-pack-desc");
    expect(byId("tc-pack-error").hidden).toBe(true);
  });

  it("adds nothing when the replace is cancelled, and says a refusal", async () => {
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "POST") {
        throw apiError("A voice pack named Studio already exists", 409, "replace");
      }
      return state();
    });
    const confirm = vi.fn(async () => false);
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-pack-file"), "Studio.zip");
    byId("tc-pack-add").click();
    expect(await liveText()).toBe("The voice pack was not added.");
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);

    request.mockImplementationOnce(async () => {
      throw apiError("notes.txt is not an audio file; a pack holds only .wav, .mp3, .flac, .ogg files", 400);
    });
    byId("tc-status").textContent = "";
    byId("tc-pack-add").click();
    expect(await liveText()).toBe("Could not add the voice pack. notes.txt is not an audio "
      + "file; a pack holds only .wav, .mp3, .flac, .ogg files");
    // A refusal that cannot be replaced is not offered as a replace.
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it("asks for a zip before sending anything", async () => {
    const request = vi.fn(async () => state());
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    byId("tc-pack-add").click();
    expect(await liveText()).toBe("Choose a zip file first.");
    expect(document.activeElement).toBe(byId("tc-pack-file"));
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("removes a pack after the confirm dialog and focuses the next pack", async () => {
    let packs = [pack("A"), pack("B"), pack("C", { shared_with_stations: true })];
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "DELETE") {
        const name = decodeURIComponent(url.split("/").pop());
        packs = packs.filter((item) => item.name !== name);
      }
      return state({ packs });
    });
    let focusTarget = null;
    const confirm = vi.fn(async (_doc, options) => {
      focusTarget = await options.onConfirm();
      focusTarget?.focus();
      return true;
    });
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    const remove = byId("tc-pack-list").querySelector('button[data-pack="B"]');
    remove.click();
    expect(await liveText()).toBe("Removed voice pack B.");
    expect(confirm.mock.calls[0][1]).toEqual(expect.objectContaining({
      title: "Remove voice pack B?", confirmLabel: "Remove voice pack", station: true }));
    expect(request).toHaveBeenCalledWith("/api/time-checks/packs/B", { method: "DELETE" });
    expect(document.activeElement.value).toBe("C");
    // The last pack in the list: focus goes to the Voice packs group.
    byId("tc-status").textContent = "";
    byId("tc-pack-list").querySelector('button[data-pack="C"]').click();
    expect(await liveText()).toBe("Removed voice pack C.");
    // The default station's shared folder: other stations lose the pack too.
    expect(confirm.mock.calls[0][1].message).not.toContain("Other stations");
    expect(confirm.mock.calls[1][1].message).toContain("Other stations use this pack too.");
    expect(document.activeElement).toBe(byId("tc-packs"));
  });

  it("keeps the pack and focus on Remove when the dialog is cancelled or the remove fails", async () => {
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "DELETE") throw apiError("A file in it is in use", 503);
      return state({ packs: [pack("Studio")] });
    });
    const confirm = vi.fn(async (_doc, options) => {
      const target = await options.onConfirm();
      target?.focus();
      return true;
    });
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    const remove = byId("tc-pack-list").querySelector("button");
    remove.click();
    expect(await liveText()).toBe("Could not remove voice pack Studio. A file in it is in use");
    expect(document.activeElement).toBe(remove);

    const cancelled = vi.fn(async () => false);
    installIndex();
    const again = installTimeChecks({ request, confirm: cancelled, interval: () => {} });
    await again.refresh();
    const button = byId("tc-pack-list").querySelector("button");
    button.click();
    await vi.waitFor(() => expect(cancelled).toHaveBeenCalled());
    await vi.waitFor(() => expect(document.activeElement).toBe(button));
    expect(request.mock.calls.filter(([, options]) => options?.method === "DELETE")).toHaveLength(1);
  });

  it("looks for new packs and names the ones found", async () => {
    let packs = [pack("Studio")];
    const request = vi.fn(async () => state({ packs }));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    packs = [pack("Studio"), pack("Night")];
    byId("tc-rescan").click();
    expect(await liveText()).toBe("Found Night. 2 voice packs listed.");
    byId("tc-status").textContent = "";
    byId("tc-rescan").click();
    expect(await liveText()).toBe("No new voice packs. 2 voice packs listed.");
  });
});

describe("recorded clips, intro and outro", () => {
  it("sends no save before the settings have loaded, so no pack is unticked", async () => {
    const request = vi.fn(async () => { throw apiError("Server unavailable", 503); });
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    byId("tc-enabled").click();
    expect(await liveText()).toBe("Time check settings are still loading; try again in a moment.");
    expect(request.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
  });

  it("says where an uploaded minute clip went", async () => {
    const request = vi.fn(async (url, options = {}) => (options.method === "POST"
      ? state({ uploaded: "15_05.wav" }) : state()));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-clip-file"), "15_05.wav", "audio/wav");
    byId("tc-clip-add").click();
    expect(await liveText()).toBe("Uploaded 15_05.wav to the recorded minute clips folder.");
  });

  it("offers the time checks folder's folders and clips, and the config.toml clip", async () => {
    const request = vi.fn(async () => state({ folders: ["Clips", "Studio"],
      clip_files: ["hello.wav"], intro_config: "intro.wav", outro: "gone.wav" }));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const options = (id) => [...byId(id).options].map((option) => [option.value, option.textContent]);
    expect(options("tc-recorded-folder")).toEqual([
      ["", "The time checks folder itself"], ["Clips", "Folder Clips"], ["Studio", "Folder Studio"]]);
    expect(options("tc-intro")).toEqual([
      ["", "None"], [":config", "intro.wav, from config.toml"], ["hello.wav", "hello.wav"]]);
    expect(byId("tc-intro").value).toBe(":config");
    // A chosen clip no longer there still shows.
    expect(options("tc-outro")).toEqual([
      ["", "None"], ["hello.wav", "hello.wav"], ["gone.wav", "gone.wav (not found)"]]);
    expect(byId("tc-outro").value).toBe("gone.wav");
  });

  it("saves the choices by name, keeping the config.toml clip as null", async () => {
    const request = vi.fn(async (url, options = {}) => (options.method === "PUT"
      ? state({ ...JSON.parse(options.body), folders: ["Clips"], clip_files: ["hello.wav"],
        intro_config: "intro.wav" })
      : state({ folders: ["Clips"], clip_files: ["hello.wav"], intro_config: "intro.wav" })));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    byId("tc-recorded-folder").value = "Clips";
    byId("tc-recorded-folder").dispatchEvent(new Event("change"));
    expect(await liveText()).toBe("Time check settings saved.");
    let body = JSON.parse(request.mock.calls.find(([, options]) => options?.method === "PUT")[1].body);
    expect(body).toEqual(expect.objectContaining({ recorded_folder: "Clips", intro: null, outro: null }));
    byId("tc-outro").value = "hello.wav";
    byId("tc-outro").dispatchEvent(new Event("change"));
    byId("tc-intro").value = "";
    byId("tc-intro").dispatchEvent(new Event("change"));
    await vi.waitFor(() => {
      const puts = request.mock.calls.filter(([, options]) => options?.method === "PUT");
      body = JSON.parse(puts.at(-1)[1].body);
      expect(body.intro).toBe("");
    });
    // None while config.toml has an intro turns it off; the outro is a file name.
    expect(body).toEqual(expect.objectContaining({ intro: "", outro: "hello.wav" }));
  });

  it("uploads a clip, asks before replacing one, and says the result", async () => {
    const request = vi.fn(async (url, options = {}) => {
      if (options.method === "POST" && !url.includes("replace=true")) {
        throw apiError("A clip named hello.wav already exists", 409, "replace");
      }
      return state({ clip_files: ["hello.wav"], uploaded: "hello.wav" });
    });
    const confirm = vi.fn(async () => true);
    const controls = installTimeChecks({ request, confirm, interval: () => {} });
    await controls.refresh();
    choose(byId("tc-clip-file"), "hello.wav", "audio/wav");
    const upload = byId("tc-clip-add");
    upload.focus();
    upload.click();
    expect(upload.hasAttribute("aria-disabled")).toBe(false);
    expect(await liveText()).toBe(
      "Uploaded hello.wav. Choose it as the intro or outro clip to use it.");
    expect(confirm.mock.calls[0][1]).toEqual(expect.objectContaining({
      title: "Replace hello.wav?", confirmLabel: "Replace clip" }));
    const posts = request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts.map(([url]) => url)).toEqual([
      "/api/time-checks/clips", "/api/time-checks/clips?replace=true"]);
    expect([...byId("tc-intro").options].map((option) => option.value)).toEqual(["", "hello.wav"]);
    expect(document.activeElement).toBe(upload);
  });
});
