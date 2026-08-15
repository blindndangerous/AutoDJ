import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  AuthenticationRequiredError,
  checkedResponse,
  makeSingleFlight,
  probeResource,
  requestBinary,
  requestJson,
  requestJsonBestEffort,
  setAuthRequiredHandler,
  withDisabled,
} from "../../src/autodj/static/modules/api-client.js";

function jsonResponse(body, init = {}) {
  return new globalThis.Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

describe("checkedResponse", () => {
  beforeEach(() => setAuthRequiredHandler(() => {}));

  it("surfaces JSON detail from an HTTP error", async () => {
    await expect(checkedResponse(jsonResponse(
      { detail: "Index is empty" }, { status: 409 },
    ), { url: "/api/random-track" })).rejects.toMatchObject({
      name: "ApiError",
      message: "Index is empty",
      status: 409,
      url: "/api/random-track",
    });
  });

  it("rejects HTML, malformed JSON, and application-level failure", async () => {
    const html = new globalThis.Response("<h1>proxy failed</h1>", {
      status: 502,
      headers: { "Content-Type": "text/html" },
    });
    const malformed = new globalThis.Response("{", {
      headers: { "Content-Type": "application/json" },
    });

    await expect(checkedResponse(html, { url: "/api/status" }))
      .rejects.toBeInstanceOf(ApiError);
    await expect(checkedResponse(malformed, { url: "/api/status" }))
      .rejects.toThrow("malformed JSON");
    await expect(checkedResponse(jsonResponse({ success: false, error: "No track" }), {
      url: "/api/advance",
    })).rejects.toThrow("No track");
    await expect(checkedResponse(jsonResponse({ ok: false }), {
      url: "/api/queue/add",
    })).rejects.toThrow("request was not accepted");
  });

  it("opens the shared authentication dialog on a mid-session 401", async () => {
    const showAuth = vi.fn();
    setAuthRequiredHandler(showAuth);

    await expect(checkedResponse(jsonResponse(
      { detail: "Authentication required" }, { status: 401 },
    ), { url: "/api/status" })).rejects.toBeInstanceOf(
      AuthenticationRequiredError,
    );
    expect(showAuth).toHaveBeenCalledOnce();
  });

  it("accepts JSON and structured JSON MIME types case-insensitively", async () => {
    const types = [
      "application/json; charset=utf-8",
      "Application/Problem+JSON; Charset=UTF-8",
      "application/vnd.autodj+json",
    ];
    for (const type of types) {
      const response = new globalThis.Response('{"ok":true}', {
        headers: { "Content-Type": type },
      });
      await expect(checkedResponse(response, { url: "/api/test" }))
        .resolves.toEqual({ ok: true });
    }
    await expect(checkedResponse(new globalThis.Response("{}", {
      headers: { "Content-Type": "text/json" },
    }), { url: "/api/test" })).rejects.toThrow("non-JSON");
  });

  it("preserves AuthenticationRequiredError when the auth handler fails", async () => {
    setAuthRequiredHandler(() => { throw new Error("dialog crashed"); });
    await expect(checkedResponse(jsonResponse({}, { status: 401 }), {
      url: "/api/status",
    })).rejects.toBeInstanceOf(AuthenticationRequiredError);

    setAuthRequiredHandler(() => Promise.reject(new Error("dialog rejected")));
    await expect(checkedResponse(jsonResponse({}, { status: 401 }), {
      url: "/api/status",
    })).rejects.toBeInstanceOf(AuthenticationRequiredError);
    await Promise.resolve();
  });
});

describe("request helpers", () => {
  beforeEach(() => setAuthRequiredHandler(() => {}));

  it("requires best-effort failures to have a reporter", async () => {
    expect(() => requestJsonBestEffort("/api/seek", {}, null))
      .toThrow("reporter");

    const reporter = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(
      { detail: "seek failed" }, { status: 500 },
    )));
    await expect(requestJsonBestEffort("/api/seek", {}, reporter))
      .resolves.toBeNull();
    expect(reporter).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("rejects failed binary requests", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(
      { detail: "missing bytes" }, { status: 404 },
    )));
    await expect(requestBinary("/api/audio?path=bad"))
      .rejects.toThrow("missing bytes");
    vi.unstubAllGlobals();
  });

  it("accepts only audio or octet-stream binary success payloads", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(new Uint8Array([1]), {
        headers: { "Content-Type": "audio/mpeg" },
      }))
      .mockResolvedValueOnce(new globalThis.Response(new Uint8Array([2]), {
        headers: { "Content-Type": "application/octet-stream" },
      }))
      .mockResolvedValueOnce(jsonResponse({ bytes: "not really" }))
      .mockResolvedValueOnce(new globalThis.Response("proxy page", {
        headers: { "Content-Type": "text/html" },
      }))
      .mockResolvedValueOnce(new globalThis.Response(new Uint8Array([3]))));

    await expect(requestBinary("/audio.mp3")).resolves.toBeInstanceOf(ArrayBuffer);
    await expect(requestBinary("/audio.bin")).resolves.toBeInstanceOf(ArrayBuffer);
    await expect(requestBinary("/fake-json")).rejects.toThrow("unexpected content type");
    await expect(requestBinary("/fake-html")).rejects.toThrow("unexpected content type");
    await expect(requestBinary("/missing-type")).rejects.toThrow("missing content type");
    vi.unstubAllGlobals();
  });

  it("treats only a 404 resource probe as an ordinary miss", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "No art" }, { status: 404 }))
      .mockResolvedValueOnce(jsonResponse({ detail: "Server broke" }, { status: 500 })));
    await expect(probeResource("/api/art?path=none")).resolves.toBe(false);
    await expect(probeResource("/api/art?path=broken")).rejects.toThrow("Server broke");
    vi.unstubAllGlobals();
  });

  it("settles a 404 probe body before returning an ordinary miss", async () => {
    const cancel = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      body: { cancel },
      headers: { get: () => "application/json" },
      ok: false,
      status: 404,
    }));

    await expect(probeResource("/missing-cover")).resolves.toBe(false);
    expect(cancel).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("requires a successful resource probe to be an image", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(new Uint8Array([1]), {
        headers: { "Content-Type": "image/jpeg" },
      }))
      .mockResolvedValueOnce(jsonResponse({ detail: "not image bytes" }))
      .mockResolvedValueOnce(new globalThis.Response("pairing", {
        headers: { "Content-Type": "text/html" },
      })));
    await expect(probeResource("/cover.jpg")).resolves.toBe(true);
    await expect(probeResource("/cover-json")).rejects.toThrow("unexpected content type");
    await expect(probeResource("/cover-html")).rejects.toThrow("unexpected content type");
    vi.unstubAllGlobals();
  });

  it("cancels a successful probe body without exposing cancellation failures", async () => {
    const firstCancel = vi.fn().mockResolvedValue(undefined);
    const secondCancel = vi.fn().mockRejectedValue(new Error("already closed"));
    const imageResponse = (cancel) => ({
      body: { cancel },
      headers: { get: () => "image/jpeg" },
      ok: true,
      status: 200,
    });
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(imageResponse(firstCancel))
      .mockResolvedValueOnce(imageResponse(secondCancel)));

    await expect(probeResource("/first-cover")).resolves.toBe(true);
    await expect(probeResource("/second-cover")).resolves.toBe(true);
    expect(firstCancel).toHaveBeenCalledOnce();
    expect(secondCancel).toHaveBeenCalledOnce();
    vi.unstubAllGlobals();
  });

  it("invalidates captured authenticated request epochs", async () => {
    const apiClient = await import("../../src/autodj/static/modules/api-client.js");
    const first = apiClient.captureAuthenticatedRequestEpoch?.();
    expect(apiClient.isAuthenticatedRequestCurrent?.(first)).toBe(true);

    apiClient.invalidateAuthenticatedRequestEpoch?.();

    expect(apiClient.isAuthenticatedRequestCurrent?.(first)).toBe(false);
    const second = apiClient.captureAuthenticatedRequestEpoch?.();
    expect(apiClient.isAuthenticatedRequestCurrent?.(second)).toBe(true);
  });

  it("clears single-flight ownership after rejection so retry can run", async () => {
    const operation = vi.fn()
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce("recovered");
    const run = makeSingleFlight(operation);

    const first = run();
    expect(run()).toBe(first);
    await expect(first).rejects.toThrow("temporary");
    await expect(run()).resolves.toBe("recovered");
    expect(operation).toHaveBeenCalledTimes(2);
  });

  it("coalesces only calls with identical arguments", async () => {
    const pending = new Map();
    const operation = vi.fn((key) => new Promise((resolve) => {
      pending.set(key, resolve);
    }));
    const run = makeSingleFlight(operation);

    const firstA = run("a");
    const secondA = run("a");
    const firstB = run("b");
    expect(secondA).toBe(firstA);
    expect(firstB).not.toBe(firstA);
    await Promise.resolve();
    expect(operation).toHaveBeenCalledTimes(2);
    pending.get("b")("result-b");
    pending.get("a")("result-a");
    await expect(firstA).resolves.toBe("result-a");
    await expect(firstB).resolves.toBe("result-b");
  });

  it("restores a disabled control after both success and failure", async () => {
    const control = document.createElement("button");
    await expect(withDisabled(control, async () => "ok")).resolves.toBe("ok");
    expect(control.disabled).toBe(false);
    await expect(withDisabled(control, async () => {
      throw new Error("nope");
    })).rejects.toThrow("nope");
    expect(control.disabled).toBe(false);

    control.disabled = true;
    await withDisabled(control, async () => {});
    expect(control.disabled).toBe(true);
  });

  it("keeps a control disabled until every overlapping owner settles", async () => {
    const control = document.createElement("button");
    let finishFirst;
    let failSecond;
    const first = withDisabled(control, () => new Promise((resolve) => {
      finishFirst = resolve;
    }));
    const second = withDisabled(control, () => new Promise((_resolve, reject) => {
      failSecond = reject;
    }));

    finishFirst();
    await first;
    expect(control.disabled).toBe(true);
    failSecond(new Error("aborted"));
    await expect(second).rejects.toThrow("aborted");
    expect(control.disabled).toBe(false);
  });

  it("uses the shared transport for valid JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ value: 3 })));
    await expect(requestJson("/api/status")).resolves.toEqual({ value: 3 });
    vi.unstubAllGlobals();
  });
});
