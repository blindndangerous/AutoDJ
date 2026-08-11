import { beforeEach, describe, expect, it, vi } from "vitest";

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
