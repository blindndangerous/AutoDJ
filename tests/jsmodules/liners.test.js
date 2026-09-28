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
      canPlay: () => active,
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
    }, { canPlay: () => true, playLiner, postSettings: vi.fn(), testOnServer: () => false });
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
    vi.stubGlobal("confirm", vi.fn(() => true));
  });

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

  it("re-enables Delete after a failure and leaves focus where the user put it", async () => {
    document.body.innerHTML = '<input id="elsewhere"><p id="status"></p><ul id="files"></ul>';
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({
        config: {}, files: ["first.mp3"], folder: "liners",
      }), { headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({
        detail: "disk unavailable",
      }), { status: 500, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.querySelector("#files");
    const status = document.querySelector("#status");
    installLiners({ lnFileList: list, lnStatus: status }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    await vi.waitFor(() => expect(list.querySelector("button")).not.toBeNull());
    const originalButton = list.querySelector("button");
    const elsewhere = document.querySelector("#elsewhere");
    elsewhere.focus();

    originalButton.click();
    await vi.waitFor(() => expect(status.textContent).toContain("disk unavailable"));

    expect(originalButton.disabled).toBe(false);
    expect(document.activeElement).toBe(elsewhere);
  });

  it("focuses a stable control when inventory refresh fails after deletion", async () => {
    document.body.innerHTML = `
      <p id="status"></p>
      <button id="upload" type="button">Upload liner</button>
      <ul id="files"></ul>
    `;
    const response = (body, status = 200) => new globalThis.Response(
      JSON.stringify(body),
      { status, headers: { "Content-Type": "application/json" } },
    );
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({
        config: {}, files: ["first.mp3", "second.mp3"], folder: "liners",
      }))
      .mockResolvedValueOnce(response({ ok: true }))
      .mockResolvedValueOnce(response({ detail: "inventory unavailable" }, 500)));
    const { installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.querySelector("#files");
    const status = document.querySelector("#status");
    installLiners({
      lnFileList: list,
      lnStatus: status,
      lnUploadSubmit: document.querySelector("#upload"),
    }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(2));
    const originalButton = list.querySelectorAll("button")[0];

    originalButton.click();
    await vi.waitFor(() => expect(status.textContent).toContain("inventory unavailable"));

    expect(originalButton.isConnected).toBe(true);
    expect(originalButton.disabled).toBe(false);
    expect(document.activeElement).toBe(originalButton);
  });

  it("focuses the next Delete button at the deleted index after refresh", async () => {
    document.body.innerHTML = '<p id="status"></p><ul id="files"></ul>';
    const response = (files) => new globalThis.Response(JSON.stringify({
      config: {}, files, folder: "liners",
    }), { headers: { "Content-Type": "application/json" } });
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response(["first.mp3", "second.mp3", "third.mp3"]))
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response(["first.mp3", "third.mp3"]));
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.querySelector("#files");
    installLiners({
      lnFileList: list,
      lnStatus: document.querySelector("#status"),
    }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(3));

    list.querySelectorAll("button")[1].click();
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(2));

    expect(document.activeElement).toBe(list.querySelectorAll("button")[1]);
  });

  it("focuses the labelled upload button after deleting the last file", async () => {
    document.body.innerHTML = `
      <p id="status"></p>
      <button id="upload" type="button">Upload liner</button>
      <ul id="files"></ul>
    `;
    const response = (files) => new globalThis.Response(JSON.stringify({
      config: {}, files, folder: "liners",
    }), { headers: { "Content-Type": "application/json" } });
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response(["only.mp3"]))
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response([])));
    const { installLiners } = await import(
      "../../src/autodj/static/modules/liners.js"
    );
    const list = document.querySelector("#files");
    const upload = document.querySelector("#upload");
    installLiners({
      lnFileList: list,
      lnStatus: document.querySelector("#status"),
      lnUploadSubmit: upload,
    }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    await vi.waitFor(() => expect(list.querySelector("button")).not.toBeNull());

    list.querySelector("button").click();
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(0));

    expect(document.activeElement).toBe(upload);
  });
});

describe("Test liner in stream mode", () => {
  const json = (body, status = 200) => new globalThis.Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });

  async function install({ testOnServer = () => true, onTest }) {
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
    }, { canPlay: () => false, playLiner, postSettings: vi.fn(), testOnServer });
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
