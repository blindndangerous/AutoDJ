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
    const {
      bumpLinerTrackCount,
      getLinerTrackCountForTest,
      resetLinerStateForTest,
    } = await import("../../src/autodj/static/modules/liners.js");
    resetLinerStateForTest();

    expect(bumpLinerTrackCount(null)).toBe(false);
    expect(bumpLinerTrackCount({})).toBe(false);
    expect(bumpLinerTrackCount({ current_track: {} })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(false);
    expect(getLinerTrackCountForTest()).toBe(0);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: null })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "two.mp3" } })).toBe(true);
    expect(getLinerTrackCountForTest()).toBe(1);
    expect(bumpLinerTrackCount({ current_track: { path: "two.mp3" } })).toBe(false);
    expect(bumpLinerTrackCount({ current_track: { path: "one.mp3" } })).toBe(true);
    expect(getLinerTrackCountForTest()).toBe(2);
  });

  it("resets both the count and distinct-track baseline", async () => {
    const {
      bumpLinerTrackCount,
      getLinerTrackCountForTest,
      resetLinerStateForTest,
    } = await import("../../src/autodj/static/modules/liners.js");
    bumpLinerTrackCount({ current_track: { path: "one.mp3" } });
    bumpLinerTrackCount({ current_track: { path: "two.mp3" } });
    expect(getLinerTrackCountForTest()).toBe(1);

    resetLinerStateForTest();

    expect(getLinerTrackCountForTest()).toBe(0);
    expect(bumpLinerTrackCount({ current_track: { path: "two.mp3" } })).toBe(false);
    expect(getLinerTrackCountForTest()).toBe(0);
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
    });
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(fileList.textContent).toContain("station-id.mp3"));

    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
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
    }, { canPlay: () => true, playLiner, postSettings: vi.fn() });
    await vi.waitFor(() => expect(fileList.textContent).toContain("late.mp3"));

    button.click();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
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

  it("reenables and refocuses the original Delete button after failure", async () => {
    document.body.innerHTML = '<p id="status"></p><ul id="files"></ul>';
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
    originalButton.focus();

    originalButton.click();
    await vi.waitFor(() => expect(status.textContent).toContain("disk unavailable"));

    expect(originalButton.disabled).toBe(false);
    expect(document.activeElement).toBe(originalButton);
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
