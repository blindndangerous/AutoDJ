import { readFileSync } from "node:fs";
import { join } from "node:path";

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

  it("says nothing for a failed liner load that was for the station just left (#70)", async () => {
    let answer;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { answer = resolve; })));
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const { selectStation, setKnownStations } = await import("../../src/autodj/static/modules/stations.js");
    setKnownStations([{ slug: "default", name: "Main" }, { slug: "night", name: "Night" }]);
    const status = document.querySelector("#liner-status");
    installLiners({
      lnFileList: document.createElement("ul"),
      lnFolderDisplay: document.createElement("span"),
      lnStatus: status,
      lnTestBtn: document.querySelector("#liner-test"),
    }, { canPlay: () => false, playLiner: vi.fn(), postSettings: vi.fn(), testOnServer: () => false });
    await vi.waitFor(() => expect(answer).toBeTypeOf("function"));
    selectStation("night");
    answer(new globalThis.Response(JSON.stringify({ detail: "boom" }),
      { status: 500, headers: { "Content-Type": "application/json" } }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(status.textContent).toBe("");
  });

  it("clears the old station's liners on a switch, and says when the new one's fail to load (#70)", async () => {
    const json = (body, status = 200) => new globalThis.Response(JSON.stringify(body),
      { status, headers: { "Content-Type": "application/json" } });
    const replies = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { replies.push(resolve); })));
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const status = document.querySelector("#liner-status");
    const fileList = document.createElement("ul");
    const folder = document.createElement("span");
    const controls = installLiners({
      lnFileList: fileList, lnFolderDisplay: folder, lnStatus: status,
      lnTestBtn: document.querySelector("#liner-test"),
    }, { canPlay: () => false, playLiner: vi.fn(), postSettings: vi.fn(), testOnServer: () => false });
    await vi.waitFor(() => expect(replies).toHaveLength(1));
    replies[0](json({ folder: "N:/old", config: { enabled: true }, files: ["old.mp3"] }));
    await vi.waitFor(() => expect(fileList.textContent).toContain("old.mp3"));

    // A load still on its way when the switch comes writes nothing.
    const late = controls.refresh();
    await vi.waitFor(() => expect(replies).toHaveLength(2));
    controls.clear();
    expect(fileList.children).toHaveLength(0);
    expect(folder.textContent).toBe("");
    replies[1](json({ folder: "N:/old", config: { enabled: true }, files: ["late.mp3"] }));
    await late;
    expect(fileList.children).toHaveLength(0);

    // The new station's load fails: that is said.
    const failed = controls.refresh();
    await vi.waitFor(() => expect(replies).toHaveLength(3));
    replies[2](json({ detail: "Liners folder not found." }, 404));
    await failed;
    await vi.waitFor(() => expect(status.textContent).toBe("Could not load liners: Liners folder not found."));
    expect(fileList.children).toHaveLength(0);
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
  async function installWithDialog(fetchImpl, { browseStatus = false } = {}) {
    document.body.innerHTML = `
      <p id="status"></p>
      <button id="upload" type="button">Upload liner</button>
      <ul id="files" tabindex="-1"></ul>
      <p id="browse-status"></p>
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
    const browse = document.querySelector("#browse-status");
    installLiners({
      lnFileList: list, lnStatus: status, lnUploadSubmit: upload,
      ...(browseStatus ? { lnBrowseStatus: browse } : {}),
    }, {
      canPlay: () => false,
      playLiner: vi.fn(),
      postSettings: vi.fn(),
    });
    const answer = async (value) => {
      await vi.waitFor(() => expect(dialog.hasAttribute("open")).toBe(true));
      document.getElementById(value === "confirm" ? "confirm-accept" : "confirm-cancel").click();
    };
    return { list, status, browse, upload, answer };
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

  it("keeps duplicate filenames distinct and offers a Track info action", async () => {
    const { renderLinerFileList } = await import("../../src/autodj/static/modules/liners.js");
    const list = document.createElement("ul");
    document.body.append(list);
    const onDelete = vi.fn();
    renderLinerFileList(list, [
      { id: "record-a", name: "same.wav", path: "X:/one/same.wav" },
      { id: "record-b", name: "same.wav", path: "Y:/two/same.wav" },
    ], onDelete);
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(list.querySelectorAll('button[aria-label="Track info for same.wav"]')).toHaveLength(2);
    expect([...list.querySelectorAll("li")].every((row) => row.lastElementChild.textContent === "Track info")).toBe(true);
    expect(list.querySelectorAll("button[tabindex='0']")).toHaveLength(1);
    const first = list.querySelector("button[tabindex='0']");
    first.focus();
    first.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
    expect(document.activeElement.textContent).toBe("Track info");
    expect([...list.querySelectorAll("button")].some((button) => button.textContent === "Track info")).toBe(true);
    const info = list.querySelectorAll('button[aria-label="Track info for same.wav"]')[1];
    const edit = vi.fn();
    document.addEventListener("autodj:edit-points", edit, { once: true });
    info.click();
    expect(edit.mock.calls[0][0].detail).toEqual({ path: "Y:/two/same.wav", kind: "liner", opener: info });
    list.querySelector("button").click();
    expect(onDelete).toHaveBeenCalledWith("record-a", expect.anything(), "same.wav");
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

  // The list sits in Queue & Search, and Upload in Settings, a hidden
  // tab there: focus stays on the list, which now says it is empty.
  it("focuses the liner list, not the hidden Upload, after deleting the last file", async () => {
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

    expect(document.activeElement).toBe(list);
    expect(document.activeElement).not.toBe(upload);
    expect(list.textContent).toBe("No liner files yet.");
  });

  it("says a Delete result once, in the status beside the list", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(inventory(["first.mp3", "second.mp3"]))
      .mockResolvedValueOnce(response({ ok: true }))
      .mockResolvedValueOnce(inventory(["second.mp3"]));
    const { list, status, browse, answer } = await installWithDialog(fetchImpl, { browseStatus: true });
    await vi.waitFor(() => expect(list.querySelectorAll("button")).toHaveLength(2));
    list.querySelector("button").focus();
    list.querySelector("button").click();
    await answer("confirm");

    await vi.waitFor(() => expect(browse.textContent).toBe("Deleted first.mp3"));
    expect(status.textContent).toBe("");
    expect(document.activeElement.getAttribute("aria-label")).toBe("Delete second.mp3");
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
    await vi.waitFor(() => expect(status.textContent.trim()).toBe(message));
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
    await vi.waitFor(() => expect(status.textContent.trim()).toBe("Liner playing: station-id.mp3"));
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
      <input type="number" id="duck" value="3">
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

  it("shows duck depth as positive dB and saves the existing negative API value", async () => {
    vi.stubGlobal("fetch", vi.fn(() => ok({ files: [], config: { duck_db: -9 } })));
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const postSettings = vi.fn().mockResolvedValue(true);
    const elements = els();
    installLiners(elements, { postSettings, canPlay: () => false });
    await vi.waitFor(() => expect(elements.lnDuckDb.value).toBe("9"));

    for (const typed of ["31", "-1", ""]) {
      elements.lnDuckDb.value = typed;
      elements.lnDuckDb.dispatchEvent(new Event("change"));
      expect(elements.lnDuckDb.value).toBe("9");
      await vi.waitFor(() => expect(elements.lnStatus.textContent).toBe(
        "Could not save Music duck: enter a number from 0 to 30.  It is still 9.",
      ));
      elements.lnStatus.textContent = "";
    }
    expect(postSettings).not.toHaveBeenCalled();

    elements.lnDuckDb.value = "0";
    elements.lnDuckDb.dispatchEvent(new Event("change"));
    expect(postSettings.mock.calls[0][1]).toMatchObject({ liners_duck_db: 0 });
    elements.lnDuckDb.value = "8";
    elements.lnDuckDb.dispatchEvent(new Event("change"));
    expect(postSettings.mock.calls[1][1]).toMatchObject({ liners_duck_db: -8 });
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

  it("says why a file that is not audio was refused (#323)", async () => {
    const refusal = "broken.mp3 is not an audio file AutoDJ can play.";
    const fetchImpl = vi.fn(() => Promise.resolve(new globalThis.Response(
      JSON.stringify({ detail: refusal }),
      { status: 422, headers: { "Content-Type": "application/json" } },
    )));
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const elements = els();
    installLiners(elements, { postSettings: vi.fn(), canPlay: () => false });
    Object.defineProperty(elements.lnUpload, "files", {
      value: [new globalThis.File(["not audio"], "broken.mp3")], configurable: true,
    });

    elements.lnUploadSubmit.click();
    await vi.waitFor(() => expect(elements.lnStatus.textContent).toBe(refusal));
  });
});


describe("liner play dates", () => {
  const json = (body, status = 200) => new globalThis.Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
  const PROMO = {
    id: "promo.mp3", name: "promo.mp3", kind: "liner", editable: false,
    not_before: null, not_after: "2026-12-26", dates_text: "Plays until 26 December 2026",
    playable: false, unplayable_text: "expired after 26 December 2026",
  };
  const ID = {
    id: "id.wav", name: "id.wav", kind: "liner", editable: false,
    not_before: null, not_after: null, dates_text: "", playable: true, unplayable_text: "",
  };

  async function install(onPut = () => json({ ...ID, not_after: "2027-01-31", dates_text: "Plays until 31 January 2027" }), listed = {}) {
    vi.resetModules();
    const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll("script, link").forEach((node) => node.remove());
    document.body.replaceChildren(template.content.cloneNode(true));
    let items = [PROMO, ID];
    const fetchImpl = vi.fn((url, options = {}) => {
      if (url === "/api/liners") return Promise.resolve(json({ config: { duck_db: -12 }, files: ["id.wav", "promo.mp3"], items, ...listed }));
      if (url.startsWith("/api/liners/dates/")) {
        const reply = onPut(JSON.parse(options.body));
        if (reply.status === 200) items = [PROMO, { ...ID, not_after: "2027-01-31", dates_text: "Plays until 31 January 2027" }];
        return Promise.resolve(reply);
      }
      if (url === "/api/liners/test") return Promise.resolve(json({ played: JSON.parse(options.body).name }));
      if (url.startsWith("/api/liners/file/") && options.method === "DELETE") {
        items = items.filter((item) => `/api/liners/file/${encodeURIComponent(item.id)}` !== url);
        return Promise.resolve(json({ deleted: "id.wav", message: "Its play dates could not be forgotten just now" }));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });
    vi.stubGlobal("fetch", fetchImpl);
    const { installLiners } = await import("../../src/autodj/static/modules/liners.js");
    const list = document.getElementById("ln-file-list");
    installLiners({
      lnFileList: list,
      lnStatus: document.getElementById("ln-status"),
      lnBrowseStatus: document.getElementById("ln-browse-status"),
      lnTestBtn: document.getElementById("ln-test"),
    }, { canPlay: () => false, prepareTest: () => null, playLiner: vi.fn(), postSettings: vi.fn(), testOnServer: () => true });
    await vi.waitFor(() => expect(list.querySelectorAll("li")).toHaveLength((listed.items || items).length));
    return { list, fetchImpl, status: document.getElementById("ln-browse-status") };
  }

  const datesButton = (list, name) => list.querySelector(`button[aria-label="Play dates for ${name}"]`);

  it("says each liner's dates in words and that an expired one is skipped", async () => {
    const { list } = await install();
    expect(list.querySelectorAll("li > span")[0].textContent).toBe(
      "promo.mp3. Plays until 26 December 2026. Skipped now: expired after 26 December 2026");
    expect(list.querySelectorAll("li > span")[1].textContent).toBe("id.wav");
    expect(datesButton(list, "id.wav").textContent).toBe("Play dates");
  });

  it("never picks an expired liner, even for Test", async () => {
    const { fetchImpl } = await install();
    const test = document.getElementById("ln-test");
    for (let press = 0; press < 6; press += 1) {
      await vi.waitFor(() => expect(test.disabled).toBe(false));
      test.click();
      await vi.waitFor(() => expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/liners/test")).toHaveLength(press + 1));
    }
    const names = fetchImpl.mock.calls.filter(([url]) => url === "/api/liners/test")
      .map(([, options]) => JSON.parse(options.body).name);
    expect(new Set(names)).toEqual(new Set(["id.wav"]));
  });

  it("skips a liner that expires while the page is open, by the station's day", async () => {
    // Loaded at 23:50 on 26 December in the station's zone, when the promo
    // may still play; Test after midnight there must not pick it.
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      vi.setSystemTime(new Date("2026-12-27T06:50:00Z"));
      const { fetchImpl } = await install(undefined, {
        timezone: "America/Denver",
        items: [{ ...PROMO, playable: true, unplayable_text: "" }, ID],
      });
      vi.setSystemTime(new Date("2026-12-27T07:30:00Z"));
      const test = document.getElementById("ln-test");
      for (let press = 0; press < 6; press += 1) {
        await vi.waitFor(() => expect(test.disabled).toBe(false));
        test.click();
        await vi.waitFor(() => expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/liners/test")).toHaveLength(press + 1));
      }
      const names = fetchImpl.mock.calls.filter(([url]) => url === "/api/liners/test")
        .map(([, options]) => JSON.parse(options.body).name);
      expect(new Set(names)).toEqual(new Set(["id.wav"]));
    } finally {
      vi.useRealTimers();
    }
  });

  it("says Skipped now when the station's day changes, in place and without a sound", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval"] });
    try {
      vi.setSystemTime(new Date("2026-12-27T06:50:00Z")); // 23:50 on 26 December in Denver
      const open = { ...PROMO, playable: true, unplayable_text: "" };
      const listed = { timezone: "America/Denver", today: "2026-12-26", items: [open, ID] };
      const { list, fetchImpl, status } = await install(undefined, listed);
      const row = list.querySelector('li[data-liner-id="promo.mp3"]');
      expect(row.firstElementChild.textContent).toBe("promo.mp3. Plays until 26 December 2026");
      const button = datesButton(list, "promo.mp3");
      button.focus();
      const said = status.textContent;
      vi.advanceTimersByTime(60_000); // the same day: nothing is asked
      expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/liners")).toHaveLength(1);
      vi.setSystemTime(new Date("2026-12-27T07:01:00Z"));
      Object.assign(listed, { today: "2026-12-27", items: [PROMO, ID] }); // as the server says then
      vi.advanceTimersByTime(60_000);
      await vi.waitFor(() => expect(row.firstElementChild.textContent).toBe(
        "promo.mp3. Plays until 26 December 2026. Skipped now: expired after 26 December 2026"));
      expect(list.querySelector('li[data-liner-id="promo.mp3"]')).toBe(row); // not redrawn
      expect(document.activeElement).toBe(button);
      expect(status.textContent).toBe(said);
      vi.advanceTimersByTime(60_000);
      expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/liners")).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps focus on the same liner when a midnight refresh finds the list changed", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval"] });
    try {
      vi.setSystemTime(new Date("2026-12-27T06:50:00Z"));
      const open = { ...PROMO, playable: true, unplayable_text: "" };
      const listed = { timezone: "America/Denver", today: "2026-12-26", items: [open, ID] };
      const { list } = await install(undefined, listed);
      datesButton(list, "id.wav").focus();
      // A liner was added outside this page meanwhile, so the rows are drawn again.
      const added = { ...ID, id: "new.wav", name: "new.wav" };
      Object.assign(listed, { today: "2026-12-27", items: [added, PROMO, ID] });
      vi.setSystemTime(new Date("2026-12-27T07:01:00Z"));
      vi.advanceTimersByTime(60_000);
      await vi.waitFor(() => expect(list.querySelectorAll("li")).toHaveLength(3));
      await vi.waitFor(() => expect(document.activeElement).toBe(datesButton(list, "id.wav")));
    } finally {
      vi.useRealTimers();
    }
  });

  it("says when a deleted liner's play dates could not be forgotten", async () => {
    const { list, status } = await install();
    const dialog = document.getElementById("confirm-dialog");
    dialog.showModal = () => { dialog.open = true; };
    dialog.close = (value = "") => {
      dialog.returnValue = value;
      dialog.open = false;
      dialog.dispatchEvent(new Event("close"));
    };
    list.querySelector('button[aria-label="Delete id.wav"]').click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    document.getElementById("confirm-accept").click();
    await vi.waitFor(() => expect(status.textContent).toBe(
      "Deleted id.wav. Its play dates could not be forgotten just now."));
  });

  it("puts focus on the list heading when a midnight redraw leaves no liner", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval"] });
    try {
      vi.setSystemTime(new Date("2026-12-27T06:50:00Z"));
      const listed = { timezone: "America/Denver", today: "2026-12-26", items: [ID] };
      const { list } = await install(undefined, listed);
      datesButton(list, "id.wav").focus();
      Object.assign(listed, { today: "2026-12-27", items: [], files: [] });
      vi.setSystemTime(new Date("2026-12-27T07:01:00Z"));
      vi.advanceTimersByTime(60_000);
      await vi.waitFor(() => expect(document.activeElement?.id).toBe("ln-files-heading"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("says so when Test finds every liner outside its play dates", async () => {
    const { fetchImpl } = await install(undefined, { items: [PROMO] });
    document.getElementById("ln-test").click();
    await vi.waitFor(() => expect(document.getElementById("ln-status").textContent)
      .toContain("Every liner is outside its play dates"));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/liners/test")).toBe(false);
  });

  it("edits a liner's dates and returns focus to its Play dates button", async () => {
    const { list, fetchImpl, status } = await install();
    datesButton(list, "id.wav").click();
    const form = document.getElementById("ln-dates-form");
    expect(form.hidden).toBe(false);
    expect(document.getElementById("ln-dates-legend").textContent).toBe("Play dates for id.wav");
    expect(document.activeElement).toBe(document.getElementById("ln-not-before"));
    document.getElementById("ln-not-after").value = "2027-01-31";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(form.hidden).toBe(true));
    const [url, options] = fetchImpl.mock.calls.find(([called]) => called.startsWith("/api/liners/dates/"));
    expect(url).toBe("/api/liners/dates/id.wav");
    expect(JSON.parse(options.body)).toEqual({ not_before: null, not_after: "2027-01-31" });
    expect(document.activeElement).toBe(datesButton(list, "id.wav"));
    await vi.waitFor(() => expect(status.textContent).toBe("Saved: id.wav. Plays until 31 January 2027."));
    expect(list.querySelectorAll("li > span")[1].textContent).toBe("id.wav. Plays until 31 January 2027");

    datesButton(list, "promo.mp3").click();
    expect(document.getElementById("ln-not-after").value).toBe("2026-12-26");
    document.getElementById("ln-dates-cancel").click();
    expect(form.hidden).toBe(true);
    expect(document.activeElement).toBe(datesButton(list, "promo.mp3"));
  });

  it("names a refused date, links it to the field and keeps the form open", async () => {
    const { list } = await install(() => json(
      { detail: "Do not play after: Input should be a valid date", field: "not_after" }, 400));
    datesButton(list, "id.wav").click();
    const form = document.getElementById("ln-dates-form");
    const before = document.getElementById("ln-not-before");
    const after = document.getElementById("ln-not-after");
    before.value = "2026-12-26";
    after.value = "2026-12-01";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(document.activeElement).toBe(after);
    expect(after.getAttribute("aria-invalid")).toBe("true");
    expect(after.getAttribute("aria-describedby")).toBe("ln-dates-error");
    expect(document.getElementById("ln-dates-error").textContent)
      .toBe("Do not play after must be on or after the Not before date");
    after.dispatchEvent(new Event("input"));
    expect(after.getAttribute("aria-describedby")).toBe("");

    after.value = "2027-01-01";
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(document.getElementById("ln-dates-error").textContent)
      .toBe("Do not play after: Input should be a valid date"));
    expect(document.activeElement).toBe(after);
    expect(form.hidden).toBe(false);

    // A half-typed date gets a plain error, not the server's.
    Object.defineProperty(before, "validity", { value: { badInput: true } });
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(document.getElementById("ln-dates-error").textContent)
      .toBe("Enter Not before as a full date, or leave it empty.");
    expect(document.activeElement).toBe(before);
  });
});
