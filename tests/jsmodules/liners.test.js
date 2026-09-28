import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("liner distinct-track cadence", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("counts only transitions after the first distinct track", async () => {
    const { bumpLinerTrackCount } = await import(
      "../../src/autodj/static/modules/liners.js"
    );

    expect(bumpLinerTrackCount(null)).toBe(false);
    expect(bumpLinerTrackCount({})).toBe(false);
    expect(bumpLinerTrackCount({ current_track: {} })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: null })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "two.mp3" } })).toBe(true);
    expect(bumpLinerTrackCount({ current_track: { path: "two.mp3" } })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(true);
  });
});

describe("liner authentication races", () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = `
      <button id="liner-test">Test liner</button>
      <p id="liner-status"></p>
    `;
  });

  it("discards a liner fetched after authenticated playback becomes inactive", async () => {
    let resolveAudioBytes;
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: {
          get: vi.fn((name) => name.toLowerCase() === "content-type"
            ? "application/json" : null),
        },
        json: vi.fn().mockResolvedValue({
          config: { duck_db: -12 },
          files: ["station-id.mp3"],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: {
          get: vi.fn((name) => name.toLowerCase() === "content-type"
            ? "audio/mpeg" : null),
        },
        arrayBuffer: vi.fn(() => new Promise((resolve) => {
          resolveAudioBytes = resolve;
        })),
      });
    vi.stubGlobal("fetch", fetchImpl);
    let active = true;
    const playLiner = vi.fn().mockResolvedValue(true);
    const { installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const fileList = document.createElement("ul");
    installLiners({
      lnFileList: fileList,
      lnFolderDisplay: document.createElement("span"),
      lnStatus: document.querySelector("#liner-status"),
      lnTestBtn: document.querySelector("#liner-test"),
    }, {
      canPlay: () => false,
      prepareTest: () => (active ? null : "Muted, so the liner was not played."),
      playLiner,
      postSettings: vi.fn(),
      testOnServer: () => false,
    });
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(fileList.textContent).toContain("station-id.mp3"));

    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(resolveAudioBytes).toEqual(expect.any(Function)));
    active = false;
    resolveAudioBytes(new ArrayBuffer(1));
    await Promise.resolve();
    await Promise.resolve();

    expect(playLiner).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("allows only one scheduled liner request until the prior one settles", async () => {
    vi.useFakeTimers();
    let rejectFirstAudio;
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({
        config: { enabled: true, every_n_songs: 1, pick_mode: "sequential" },
        files: ["station-id.mp3"],
      }), { headers: { "Content-Type": "application/json" } }))
      .mockImplementationOnce(() => new Promise((_resolve, reject) => {
        rejectFirstAudio = reject;
      }))
      .mockResolvedValue(new globalThis.Response(new Uint8Array([1]), {
        headers: { "Content-Type": "audio/mpeg" },
      }));
    vi.stubGlobal("fetch", fetchImpl);
    const { bumpLinerTrackCount, installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    installLiners({ lnStatus: document.querySelector("#liner-status") }, {
      canPlay: () => true,
      playLiner: vi.fn().mockResolvedValue(true),
      postSettings: vi.fn(),
    });
    await vi.advanceTimersByTimeAsync(0);
    bumpLinerTrackCount({ current_track: { path: "one.mp3" } });
    bumpLinerTrackCount({ current_track: { path: "two.mp3" } });

    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    rejectFirstAudio(new Error("network failed"));
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("does not play bytes completed after the authenticated epoch expires", async () => {
    let resolveAudioBytes;
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({
        config: { duck_db: -12 }, files: ["late.mp3"],
      }), { headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce({
        body: null,
        headers: { get: () => "audio/mpeg" },
        ok: true,
        status: 200,
        arrayBuffer: () => new Promise((resolve) => { resolveAudioBytes = resolve; }),
      });
    vi.stubGlobal("fetch", fetchImpl);
    const apiClient = await import("../../src/autodj/static/modules/api-client.js");
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const button = document.querySelector("#liner-test");
    const fileList = document.createElement("ul");
    const playLiner = vi.fn().mockResolvedValue(true);
    installLiners({
      lnFileList: fileList,
      lnStatus: document.querySelector("#liner-status"),
      lnTestBtn: button,
    }, {
      canPlay: () => false, prepareTest: () => null, playLiner, postSettings: vi.fn(),
      testOnServer: () => false,
    });
    await vi.waitFor(() => expect(fileList.textContent).toContain("late.mp3"));

    button.click();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(resolveAudioBytes).toEqual(expect.any(Function)));
    apiClient.invalidateAuthenticatedRequestEpoch?.();
    resolveAudioBytes(new ArrayBuffer(1));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(playLiner).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("liner file controls", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  const response = (body, status = 200) => new globalThis.Response(
    JSON.stringify(body),
    { status, headers: { "Content-Type": "application/json" } },
  );
  const inventory = (files) => response({ config: {}, files, folder: "liners" });

  // The shared confirmation as a browser runs it: closing puts focus back
  // on the element that had it when the dialog opened.  answer() presses
  // the dialog's own button.
  async function installWithDialog(fetchImpl) {
    document.body.innerHTML = `
      <p id="status"></p>
      <button id="upload" type="button">Upload liner</button>
      <ul id="files"></ul>
      <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
        <button id="confirm-cancel"></button><button id="confirm-accept"></button></dialog>`;
    const dialog = document.querySelector("#confirm-dialog");
    let opener = null;
    dialog.showModal = vi.fn(() => {
      opener = document.activeElement;
      dialog.setAttribute("open", "");
    });
    dialog.close = vi.fn((value) => {
      if (!dialog.hasAttribute("open")) return;
      if (value !== undefined) dialog.returnValue = value;
      dialog.removeAttribute("open");
      if (opener?.isConnected) opener.focus();
      dialog.dispatchEvent(new Event("close"));
    });
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const list = document.querySelector("#files");
    const status = document.querySelector("#status");
    const upload = document.querySelector("#upload");
    installLiners({ lnFileList: list, lnStatus: status, lnUploadSubmit: upload }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    const answer = async (value) => {
      await vi.waitFor(() => expect(dialog.hasAttribute("open")).toBe(true));
      document.getElementById(value === "confirm" ? "confirm-accept" : "confirm-cancel").click();
    };
    return { list, status, upload, answer };
  }

  it("renders hostile filenames as text with an accurate Delete name", async () => {
    const { renderLinerFileList } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.createElement("ul");
    const onDelete = vi.fn();
    const filename = '<img src=x onerror="alert(1)">.mp3';

    expect(renderLinerFileList).toEqual(expect.any(Function));
    if (typeof renderLinerFileList !== "function") return;
    renderLinerFileList(list, [filename], onDelete);

    const button = list.querySelector("button");
    expect(list.querySelector("img")).toBeNull();
    expect(list.querySelector("li > span").textContent).toBe(filename);
    expect(button.textContent).toBe("Delete");
    expect(button.getAttribute("aria-label")).toBe(`Delete ${filename}`);
    button.click();
    expect(onDelete).toHaveBeenCalledWith(filename, button);
  });

  it("says so when there are no liner files", async () => {
    const { renderLinerFileList } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.createElement("ul");

    renderLinerFileList(list, [], vi.fn());
    expect(list.textContent).toBe("No liner files yet.");

    renderLinerFileList(list, null, vi.fn());
    expect(list.querySelector(".no-results")).not.toBeNull();
  });

  it("asks in the shared dialog and deletes nothing on Cancel", async () => {
    const fetchImpl = vi.fn().mockResolvedValueOnce(inventory(["only.mp3"]));
    const { list, answer } = await installWithDialog(fetchImpl);
    await vi.waitFor(() => expect(list.querySelector("button")).not.toBeNull());
    const remove = list.querySelector("button");
    remove.focus();
    remove.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));
    expect(document.getElementById("confirm-title").textContent).toBe("Delete liner only.mp3?");
    expect(document.getElementById("confirm-accept").textContent).toBe("Delete liner");

    await answer("cancel");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.activeElement).toBe(remove);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("puts focus back on Delete and says why when the delete fails", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(inventory(["first.mp3"]))
      .mockResolvedValueOnce(response({ detail: "disk unavailable" }, 500));
    const { list, status, answer } = await installWithDialog(fetchImpl);
    await vi.waitFor(() => expect(list.querySelector("button")).not.toBeNull());
    const remove = list.querySelector("button");
    remove.focus();
    remove.click();

    await answer("confirm");
    await vi.waitFor(() => expect(status.textContent).toContain("disk unavailable"));

    expect(document.activeElement).toBe(remove);
  });

  it("focuses a stable control when inventory refresh fails after deletion", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(inventory(["first.mp3", "second.mp3"]))
      .mockResolvedValueOnce(response({ ok: true }))
      .mockResolvedValueOnce(response({ detail: "inventory unavailable" }, 500));
    const { list, status, answer } = await installWithDialog(fetchImpl);
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(2));
    const originalButton = list.querySelectorAll("button")[0];

    originalButton.focus();
    originalButton.click();
    await answer("confirm");
    await vi.waitFor(() => expect(status.textContent).toContain("inventory unavailable"));

    expect(originalButton.isConnected).toBe(true);
    expect(document.activeElement).toBe(originalButton);
  });

  it("moves focus from the dialog straight to the next Delete button", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(inventory(["first.mp3", "second.mp3", "third.mp3"]))
      .mockResolvedValueOnce(response({ ok: true }))
      .mockResolvedValueOnce(inventory(["first.mp3", "third.mp3"]));
    const { list, status, answer } = await installWithDialog(fetchImpl);
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(3));
    const remove = list.querySelectorAll("button")[1];
    remove.focus();
    remove.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));

    // Focus that went back to the pressed button first made NVDA read a
    // line from the top of the page before the new target (D14).
    const focused = [];
    const record = (event) => focused.push(event.target);
    document.addEventListener("focusin", record);
    await answer("confirm");
    await vi.waitFor(() => expect(status.textContent).toBe("Deleted second.mp3"));
    document.removeEventListener("focusin", record);

    expect(focused).toEqual([list.querySelectorAll("button")[1]]);
    expect(document.activeElement.getAttribute("aria-label")).toBe("Delete third.mp3");
  });

  it("focuses the labelled upload button after deleting the last file", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(inventory(["only.mp3"]))
      .mockResolvedValueOnce(response({ ok: true }))
      .mockResolvedValueOnce(inventory([]));
    const { list, upload, answer } = await installWithDialog(fetchImpl);
    await vi.waitFor(() => expect(list.querySelector("button")).not.toBeNull());

    list.querySelector("button").focus();
    list.querySelector("button").click();
    await answer("confirm");
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(0));

    expect(document.activeElement).toBe(upload);
  });
});

describe("Test liner in stream mode", () => {
  const json = (body, status = 200) => new globalThis.Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });

  async function install({ testOnServer = () => true, prepareTest = () => null, onTest }) {
    vi.resetModules();
    document.body.innerHTML = `
      <button id="liner-test">Test now</button>
      <ul id="liner-files"></ul>
      <div id="liner-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
    const fetchImpl = vi.fn((url, options) => {
      if (url === "/api/liners") {
        return Promise.resolve(json({ config: { duck_db: -12 }, files: ["station-id.mp3"] }));
      }
      if (url === "/api/liners/test") return Promise.resolve(onTest(options));
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    vi.stubGlobal("fetch", fetchImpl);
    const playLiner = vi.fn().mockResolvedValue(true);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const fileList = document.querySelector("#liner-files");
    installLiners({
      lnFileList: fileList,
      lnStatus: document.querySelector("#liner-status"),
      lnTestBtn: document.querySelector("#liner-test"),
    }, { canPlay: () => false, prepareTest, playLiner, postSettings: vi.fn(), testOnServer });
    await vi.waitFor(() => expect(fileList.textContent).toContain("station-id.mp3"));
    return { fetchImpl, playLiner, status: document.querySelector("#liner-status") };
  }

  it("plays the liner into the stream through the server", async () => {
    const { fetchImpl, playLiner, status } = await install({
      onTest: () => json({ played: "station-id.mp3" }),
    });
    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(status.textContent).toBe("Liner playing: station-id.mp3"));
    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/liners/test");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toEqual({ name: "station-id.mp3" });
    expect(fetchImpl.mock.calls.some(([url]) => url.startsWith("/api/liners/file/"))).toBe(false);
    expect(playLiner).not.toHaveBeenCalled();
  });

  it("says so when the server played nothing", async () => {
    const { status } = await install({ onTest: () => json({ played: null }) });
    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(status.textContent).toBe("Could not play station-id.mp3."));
  });

  it("says why when the station is idle", async () => {
    const message = "Nobody is listening, so the liner was not played.";
    const { status } = await install({ onTest: () => json({ played: null, message }) });
    const button = document.querySelector("#liner-test");
    button.click();
    await vi.waitFor(() => expect(status.textContent).toBe(message));
    const writes = [];
    const observer = new window.MutationObserver((batch) => writes.push(...batch));
    observer.observe(status, { childList: true, characterData: true, subtree: true });
    button.click();
    await vi.waitFor(() => expect(writes.length).toBeGreaterThanOrEqual(2));
    await vi.waitFor(() => expect(status.textContent).toBe(message));
    observer.disconnect();
  });

  it("reports a failed request", async () => {
    const { status } = await install({
      onTest: () => json({ detail: "Liner folder is not readable" }, 500),
    });
    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(status.textContent).toMatch(/^Liner playback failed: /));
  });

  it("reports a repeated press again rather than staying silent", async () => {
    const { status } = await install({ onTest: () => json({ played: "station-id.mp3" }) });
    const button = document.querySelector("#liner-test");
    button.click();
    await vi.waitFor(() => expect(status.textContent).toBe("Liner playing: station-id.mp3"));
    const writes = [];
    const observer = new window.MutationObserver((batch) => writes.push(...batch));
    observer.observe(status, { childList: true, characterData: true, subtree: true });
    button.click();
    await vi.waitFor(() => expect(writes.length).toBeGreaterThanOrEqual(2));
    await vi.waitFor(() => expect(status.textContent).toBe("Liner playing: station-id.mp3"));
    observer.disconnect();
  });

  it("keeps local playback when the page is not in stream mode", async () => {
    const { fetchImpl } = await install({
      testOnServer: () => false,
      onTest: () => json({ played: "station-id.mp3" }),
    });
    document.querySelector("#liner-test").click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/liners/test")).toBe(false);
  });

  it("says why Test plays nothing in browser playback, and fetches nothing", async () => {
    const { fetchImpl, playLiner, status } = await install({
      testOnServer: () => false,
      prepareTest: () => "Muted, so the liner was not played.",
      onTest: () => json({ played: "station-id.mp3" }),
    });
    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(status.textContent)
      .toBe("Muted, so the liner was not played."));
    expect(fetchImpl.mock.calls.some(([url]) => url.startsWith("/api/liners/file/"))).toBe(false);
    expect(playLiner).not.toHaveBeenCalled();
  });

  it("plays Test in browser playback when the scheduled liners may not", async () => {
    vi.resetModules();
    document.body.innerHTML = `
      <button id="liner-test">Test now</button>
      <ul id="liner-files"></ul>
      <div id="liner-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
    vi.stubGlobal("fetch", vi.fn((url) => Promise.resolve(url === "/api/liners"
      ? json({ config: { duck_db: -12 }, files: ["station-id.mp3"] })
      : new globalThis.Response(new Uint8Array([1]), {
        headers: { "Content-Type": "audio/mpeg" },
      }))));
    const playLiner = vi.fn().mockResolvedValue(true);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const fileList = document.querySelector("#liner-files");
    installLiners({
      lnFileList: fileList,
      lnStatus: document.querySelector("#liner-status"),
      lnTestBtn: document.querySelector("#liner-test"),
    }, {
      // Paused: nothing scheduled may play, but Test previews the liner.
      canPlay: () => false,
      prepareTest: () => null,
      playLiner,
      postSettings: vi.fn(),
      testOnServer: () => false,
    });
    await vi.waitFor(() => expect(fileList.textContent).toContain("station-id.mp3"));

    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(document.querySelector("#liner-status").textContent)
      .toBe("Liner playing: station-id.mp3"));
    expect(playLiner).toHaveBeenCalledOnce();
  });
});

describe("liner settings and upload", () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = `
      <input type="checkbox" id="enabled" checked>
      <input type="number" id="every-n" value="">
      <input type="number" id="every-min" value="">
      <input type="number" id="rand-min" value="">
      <input type="number" id="rand-max" value="">
      <select id="pick"><option value="random">Random</option></select>
      <input type="number" id="duck" value="-12">
      <input type="file" id="upload"><button id="submit">Upload</button>
      <input type="checkbox" id="replace">
      <p id="status"></p><div id="status-toast" hidden></div>`;
  });

  function els() {
    const $ = (id) => document.getElementById(id);
    return {
      lnEnabled: $("enabled"), lnEveryN: $("every-n"), lnEveryMin: $("every-min"),
      lnRandMin: $("rand-min"), lnRandMax: $("rand-max"), lnPickMode: $("pick"),
      lnDuckDb: $("duck"), lnUpload: $("upload"), lnUploadSubmit: $("submit"),
      lnUploadReplace: $("replace"), lnStatus: $("status"),
    };
  }

  const ok = (body) => Promise.resolve(new globalThis.Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  }));

  it("sends 0 for a cleared trigger so clearing it turns the trigger off", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok({ files: [], config: {} })));
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const postSettings = vi.fn().mockResolvedValue(true);
    const elements = els();
    installLiners(elements, { postSettings, canPlay: () => false });

    elements.lnEveryN.dispatchEvent(new Event("change"));

    expect(postSettings.mock.calls[0][1]).toMatchObject({
      liners_every_n_songs: 0,
      liners_every_minutes: 0,
      liners_random_min_minutes: 0,
      liners_random_max_minutes: 0,
    });
  });

  it("refuses a duck depth outside minus 30 to 0 and says so", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok({ files: [], config: { duck_db: -9 } })));
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const postSettings = vi.fn().mockResolvedValue(true);
    const elements = els();
    installLiners(elements, { postSettings, canPlay: () => false });
    await vi.waitFor(() => expect(elements.lnDuckDb.value).toBe("-9"));

    // +60 dB would multiply the music by 1000 while a liner plays.
    for (const typed of ["60", "-31", ""]) {
      elements.lnDuckDb.value = typed;
      elements.lnDuckDb.dispatchEvent(new Event("change"));
      expect(elements.lnDuckDb.value).toBe("-9");
      await vi.waitFor(() => expect(elements.lnStatus.textContent).toBe(
        "Could not save Duck depth: enter a number from minus 30 to 0.  It is still minus 9.",
      ));
      elements.lnStatus.textContent = "";
    }
    expect(postSettings).not.toHaveBeenCalled();

    elements.lnDuckDb.value = "0";
    elements.lnDuckDb.dispatchEvent(new Event("change"));
    expect(postSettings.mock.calls[0][1]).toMatchObject({ liners_duck_db: 0 });
  });

  it("asks the server to replace an existing file only when ticked", async () => {
    const fetchImpl = vi.fn(() => ok({ files: [], config: {}, filename: "id.mp3", size: 1 }));
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const elements = els();
    installLiners(elements, { postSettings: vi.fn(), canPlay: () => false });
    Object.defineProperty(elements.lnUpload, "files", {
      value: [new globalThis.File(["x"], "id.mp3")], configurable: true,
    });

    elements.lnUploadReplace.checked = true;
    elements.lnUploadSubmit.click();

    await vi.waitFor(() => expect(fetchImpl.mock.calls
      .some(([url]) => url === "/api/liners/upload?replace=true")).toBe(true));
  });
});
