import { describe, expect, it, vi } from "vitest";

import { installMediaActionHandlers } from
  "../../src/autodj/static/modules/media-session.js";

describe("Media Session request fallbacks", () => {
  it("returns safely when Media Session is unsupported", () => {
    delete navigator.mediaSession;
    expect(() => installMediaActionHandlers()).not.toThrow();
  });

  it("requires an explicit request failure reporter when supported", () => {
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: { setActionHandler: vi.fn() },
    });
    expect(() => installMediaActionHandlers()).toThrow("onRequestError");
  });

  it("delegates play only when the caller returns false and reports failure", async () => {
    const handlers = {};
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: { setActionHandler: (name, handler) => { handlers[name] = handler; } },
    });
    const onRequestError = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ detail: "Player unavailable" }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )));
    installMediaActionHandlers({
      onPlay: () => false,
      onRequestError,
    });

    handlers.play();
    await vi.waitFor(() => expect(onRequestError).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledWith("/api/pause", { method: "POST" });
    vi.unstubAllGlobals();
  });

  it("does not delegate play when the caller handled unlock", async () => {
    const handlers = {};
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: { setActionHandler: (name, handler) => { handlers[name] = handler; } },
    });
    vi.stubGlobal("fetch", vi.fn());
    installMediaActionHandlers({ onPlay: () => true, onRequestError: vi.fn() });
    handlers.play();
    await Promise.resolve();
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("reports a rejected play callback without an unhandled rejection", async () => {
    const handlers = {};
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: { setActionHandler: (name, handler) => { handlers[name] = handler; } },
    });
    const failure = new Error("unlock rejected");
    const onRequestError = vi.fn();
    vi.stubGlobal("fetch", vi.fn());
    installMediaActionHandlers({
      onPlay: () => Promise.reject(failure),
      onRequestError,
    });

    await expect(handlers.play()).resolves.toBeUndefined();
    expect(onRequestError).toHaveBeenCalledWith(failure);
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("awaits and reports a rejected next-track callback", async () => {
    const handlers = {};
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: { setActionHandler: (name, handler) => { handlers[name] = handler; } },
    });
    const failure = new Error("skip rejected");
    const onRequestError = vi.fn();
    installMediaActionHandlers({
      onPauseOrSkipNext: () => Promise.reject(failure),
      onRequestError,
    });

    await expect(handlers.nexttrack()).resolves.toBeUndefined();
    expect(onRequestError).toHaveBeenCalledWith(failure);
  });
});
