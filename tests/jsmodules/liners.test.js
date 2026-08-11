import { beforeEach, describe, expect, it, vi } from "vitest";

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
        json: vi.fn().mockResolvedValue({
          config: { duck_db: -12 },
          files: ["station-id.mp3"],
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
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
    installLiners({
      lnFileList: document.createElement("ul"),
      lnFolderDisplay: document.createElement("span"),
      lnStatus: document.querySelector("#liner-status"),
      lnTestBtn: document.querySelector("#liner-test"),
    }, {
      canPlay: () => active,
      playLiner,
      postSettings: vi.fn(),
    });
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledOnce());
    await vi.waitFor(() => {
      expect(document.querySelector("#liner-test").disabled).toBe(false);
    });

    document.querySelector("#liner-test").click();
    await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2));
    active = false;
    resolveAudioBytes(new ArrayBuffer(1));
    await Promise.resolve();
    await Promise.resolve();

    expect(playLiner).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
