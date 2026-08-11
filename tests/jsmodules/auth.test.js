import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  bootstrapAuthenticatedApp,
  handleWebSocketAuthenticationClose,
  initAuthDialog,
} from "../../src/autodj/static/modules/auth.js";

function installDialogMarkup() {
  document.body.innerHTML = `<dialog id="auth-dialog"
      aria-labelledby="auth-title" aria-describedby="auth-help">
    <form id="auth-form" method="dialog">
      <h2 id="auth-title">Connect to AutoDJ</h2>
      <p id="auth-help">Enter the access token supplied by the server operator.</p>
      <label for="auth-token">Access token</label>
      <input id="auth-token" name="token" type="password"
             autocomplete="current-password" required
             aria-describedby="auth-help auth-error">
      <p id="auth-error" role="alert" aria-live="assertive"
         aria-atomic="true"></p>
      <button id="auth-submit" type="submit">Log in</button>
    </form><p id="auth-status" role="status" aria-live="polite"
      aria-atomic="true"></p>
  </dialog>`;
  const dialog = document.querySelector("#auth-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return {
    dialog,
    error: document.querySelector("#auth-error"),
    form: document.querySelector("#auth-form"),
    submitButton: document.querySelector("#auth-submit"),
    status: document.querySelector("#auth-status"),
    token: document.querySelector("#auth-token"),
  };
}

function response({ ok, status, json, retryAfter } = {}) {
  return {
    ok,
    status,
    json: vi.fn().mockResolvedValue(json),
    headers: {
      get: vi.fn((name) =>
        name.toLowerCase() === "retry-after" ? retryAfter ?? null : null),
    },
  };
}

describe("initAuthDialog", () => {
  let localStorageSet;
  let sessionStorageSet;

  beforeEach(() => {
    document.body.innerHTML = "";
    localStorageSet = vi.fn();
    sessionStorageSet = vi.fn();
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: { setItem: localStorageSet },
    });
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: { setItem: sessionStorageSet },
    });
  });

  it("posts token once, clears it immediately, and invokes success", async () => {
    const els = installDialogMarkup();
    let resolveLogin;
    const fetchImpl = vi.fn(() => new Promise((resolve) => {
      resolveLogin = resolve;
    }));
    const onSuccess = vi.fn();
    const auth = initAuthDialog({ document, fetchImpl, onSuccess });
    els.token.value = "secret-token";

    const first = auth.submit();
    const duplicate = auth.submit();

    expect(els.token.value).toBe("");
    expect(els.form.getAttribute("aria-busy")).toBe("true");
    expect(els.token.readOnly).toBe(true);
    expect(els.token.disabled).toBe(false);
    expect(els.submitButton.disabled).toBe(true);
    expect(els.status.textContent).toBe("Signing in…");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: "secret-token" }),
    });

    auth.show();
    expect(els.token.readOnly).toBe(true);
    expect(els.submitButton.disabled).toBe(true);
    expect(els.status.textContent).toBe("Signing in…");

    resolveLogin(response({ ok: true, status: 200 }));
    await Promise.all([first, duplicate]);

    expect(onSuccess).toHaveBeenCalledOnce();
    expect(els.token.value).toBe("");
    expect(document.body.textContent).not.toContain("secret-token");
    expect(localStorageSet).not.toHaveBeenCalled();
    expect(sessionStorageSet).not.toHaveBeenCalled();
  });

  it("prevents native cancellation and guards repeated modal display", () => {
    const els = installDialogMarkup();
    const auth = initAuthDialog({ document, fetchImpl: vi.fn() });

    auth.show();
    auth.show();
    const cancelEvent = new Event("cancel", { cancelable: true });
    els.dialog.dispatchEvent(cancelEvent);

    expect(els.dialog.showModal).toHaveBeenCalledOnce();
    expect(cancelEvent.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(els.token);
  });

  it("keeps forward and reverse Tab focus inside the required dialog", () => {
    const els = installDialogMarkup();
    const auth = initAuthDialog({ document, fetchImpl: vi.fn() });
    auth.show();

    els.token.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Tab",
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    }));
    expect(document.activeElement).toBe(els.submitButton);

    els.submitButton.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    }));
    expect(document.activeElement).toBe(els.token);
  });

  it("announces repeated failures and restores input state and focus", async () => {
    const els = installDialogMarkup();
    const auth = initAuthDialog({
      document,
      fetchImpl: vi.fn().mockResolvedValue(
        response({ ok: false, status: 401 }),
      ),
    });
    const mutations = [];
    const observer = new window.MutationObserver(
      () => mutations.push(els.error.textContent),
    );
    observer.observe(els.error, { childList: true });

    els.token.value = "bad-one";
    await auth.submit();
    els.token.value = "bad-two";
    await auth.submit();
    await Promise.resolve();
    observer.disconnect();

    expect(els.error.textContent).toBe("That access token was not accepted.");
    expect(mutations).toContain("");
    expect(els.token.getAttribute("aria-invalid")).toBe("true");
    expect(els.token.getAttribute("aria-describedby")).toContain("auth-error");
    expect(els.form.getAttribute("aria-busy")).toBe("false");
    expect(els.status.textContent).toBe("");
    expect(els.token.readOnly).toBe(false);
    expect(els.token.disabled).toBe(false);
    expect(els.submitButton.disabled).toBe(false);
    expect(document.activeElement).toBe(els.token);
    expect(document.body.textContent).not.toContain("bad-one");
    expect(document.body.textContent).not.toContain("bad-two");
  });

  it.each([
    [413, undefined, "That access token is too large."],
    [429, "17", "Too many login attempts. Wait about 17 seconds before trying again."],
    [500, undefined, "Login failed. Check the server and try again."],
  ])("uses safe message for HTTP %i without echoing server details",
    async (status, retryAfter, expected) => {
      const els = installDialogMarkup();
      const auth = initAuthDialog({
        document,
        fetchImpl: vi.fn().mockResolvedValue(
          response({ ok: false, status, retryAfter }),
        ),
      });
      els.token.value = "never-echo-this";

      await auth.submit();

      expect(els.error.textContent).toBe(expected);
      expect(els.error.textContent).not.toContain("never-echo-this");
    });

  it("reports network failure without leaking thrown details", async () => {
    const els = installDialogMarkup();
    const auth = initAuthDialog({
      document,
      fetchImpl: vi.fn().mockRejectedValue(
        new Error("secret-token appeared in upstream URL"),
      ),
    });
    els.token.value = "secret-token";

    await auth.submit();

    expect(els.error.textContent).toBe(
      "Login failed. Check the server and try again.",
    );
    expect(els.error.textContent).not.toContain("secret-token");
    expect(els.token.value).toBe("");
  });

  it("clears prior invalid state before a retry", async () => {
    const els = installDialogMarkup();
    let resolveRetry;
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({ ok: false, status: 401 }))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRetry = resolve;
      }));
    const auth = initAuthDialog({ document, fetchImpl });
    els.token.value = "bad";
    await auth.submit();

    els.token.value = "retry";
    const pending = auth.submit();

    expect(els.error.textContent).toBe("");
    expect(els.token.hasAttribute("aria-invalid")).toBe(false);
    resolveRetry(response({ ok: true, status: 200 }));
    await pending;
  });
});

describe("bootstrapAuthenticatedApp", () => {
  it("starts no protected work while authentication is required", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response({
      ok: true,
      status: 200,
      json: { required: true, authenticated: false },
    }));
    const auth = { show: vi.fn() };
    const startAuthenticatedApp = vi.fn();

    const started = await bootstrapAuthenticatedApp({
      fetchImpl,
      auth,
      startAuthenticatedApp,
    });

    expect(started).toBe(false);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith("/api/auth/status");
    expect(auth.show).toHaveBeenCalledOnce();
    expect(startAuthenticatedApp).not.toHaveBeenCalled();
  });

  it("fetches initial state then starts authenticated work exactly once", async () => {
    const initialState = { paused: false };
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({
        ok: true,
        status: 200,
        json: { required: true, authenticated: true },
      }))
      .mockResolvedValueOnce(response({ ok: true, status: 200, json: initialState }));
    const startAuthenticatedApp = vi.fn();
    const args = {
      fetchImpl,
      auth: { show: vi.fn() },
      startAuthenticatedApp,
    };

    const [first, second] = await Promise.all([
      bootstrapAuthenticatedApp(args),
      bootstrapAuthenticatedApp(args),
    ]);
    const third = await bootstrapAuthenticatedApp(args);

    expect([first, second, third]).toEqual([true, true, true]);
    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual([
      "/api/auth/status",
      "/api/status",
    ]);
    expect(startAuthenticatedApp).toHaveBeenCalledOnce();
    expect(startAuthenticatedApp).toHaveBeenCalledWith(initialState);
  });

  it.each([
    null,
    [],
    {},
    { required: "yes", authenticated: false },
    { required: true, authenticated: "no" },
  ])("rejects malformed authentication state %# without protected startup",
    async (authState) => {
      const fetchImpl = vi.fn().mockResolvedValue(response({
        ok: true,
        status: 200,
        json: authState,
      }));
      const onError = vi.fn();
      const startAuthenticatedApp = vi.fn();

      const started = await bootstrapAuthenticatedApp({
        fetchImpl,
        auth: { show: vi.fn() },
        startAuthenticatedApp,
        onError,
      });

      expect(started).toBe(false);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
      expect(startAuthenticatedApp).not.toHaveBeenCalled();
      expect(onError).toHaveBeenCalledOnce();
    });

  it("reopens login when initial protected state returns 401", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response({
        ok: true,
        status: 200,
        json: { required: true, authenticated: true },
      }))
      .mockResolvedValueOnce(response({ ok: false, status: 401 }));
    const auth = { show: vi.fn() };
    const startAuthenticatedApp = vi.fn();

    const started = await bootstrapAuthenticatedApp({
      fetchImpl,
      auth,
      startAuthenticatedApp,
    });

    expect(started).toBe(false);
    expect(auth.show).toHaveBeenCalledOnce();
    expect(startAuthenticatedApp).not.toHaveBeenCalled();
  });
});

describe("handleWebSocketAuthenticationClose", () => {
  it("reopens login and signals reconnect suppression only for code 4401", () => {
    const auth = { show: vi.fn() };
    const onExpired = vi.fn();

    expect(handleWebSocketAuthenticationClose(
      { code: 4401 }, { auth, onExpired },
    )).toBe(true);
    expect(auth.show).toHaveBeenCalledOnce();
    expect(onExpired).toHaveBeenCalledOnce();

    expect(handleWebSocketAuthenticationClose(
      { code: 1006 }, { auth, onExpired },
    )).toBe(false);
    expect(auth.show).toHaveBeenCalledOnce();
    expect(onExpired).toHaveBeenCalledOnce();
  });

  it("rechecks authentication after browser close code 1006", async () => {
    const authModule = await import(
      "../../src/autodj/static/modules/auth.js"
    );
    const fetchImpl = vi.fn().mockResolvedValue(response({
      ok: true,
      status: 200,
      json: { required: true, authenticated: false },
    }));
    const auth = { show: vi.fn() };
    const onExpired = vi.fn();
    const reconnect = vi.fn();

    const didReconnect = await authModule.reconnectWebSocketAfterClose({
      event: { code: 1006, wasClean: false },
      fetchImpl,
      auth,
      onExpired,
      reconnect,
    });

    expect(didReconnect).toBe(false);
    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(fetchImpl).toHaveBeenCalledWith("/api/auth/status");
    expect(onExpired).toHaveBeenCalledOnce();
    expect(auth.show).toHaveBeenCalledOnce();
    expect(reconnect).not.toHaveBeenCalled();
  });

  it.each([
    ["authenticated", response({
      ok: true,
      status: 200,
      json: { required: true, authenticated: true },
    })],
    ["auth status unavailable", new Error("server restarting")],
  ])("preserves transient reconnect when %s after code 1006",
    async (_caseName, authResult) => {
      const authModule = await import(
        "../../src/autodj/static/modules/auth.js"
      );
      const fetchImpl = authResult instanceof Error
        ? vi.fn().mockRejectedValue(authResult)
        : vi.fn().mockResolvedValue(authResult);
      const auth = { show: vi.fn() };
      const onExpired = vi.fn();
      const reconnect = vi.fn();

      const didReconnect = await authModule.reconnectWebSocketAfterClose({
        event: { code: 1006, wasClean: false },
        fetchImpl,
        auth,
        onExpired,
        reconnect,
      });

      expect(didReconnect).toBe(true);
      expect(fetchImpl).toHaveBeenCalledWith("/api/auth/status");
      expect(onExpired).not.toHaveBeenCalled();
      expect(auth.show).not.toHaveBeenCalled();
      expect(reconnect).toHaveBeenCalledOnce();
    });
});

describe("protected global callbacks", () => {
  it("ignores global hotkeys while authenticated interaction is disabled", async () => {
    document.body.innerHTML = `
      <section id="panel-now"></section>
      <div id="sr-status"></div>
      <button id="pause">Pause</button>
    `;
    const pause = document.querySelector("#pause");
    const click = vi.spyOn(pause, "click");
    const { installHotkeys } = await import(
      "../../src/autodj/static/modules/hotkeys.js"
    );
    installHotkeys({
      btnPause: pause,
      isEnabled: () => false,
    });

    window.dispatchEvent(new window.KeyboardEvent("keydown", {
      key: "k",
      bubbles: true,
      cancelable: true,
    }));
    window.dispatchEvent(new window.KeyboardEvent("keyup", {
      key: "k",
      bubbles: true,
    }));

    expect(click).not.toHaveBeenCalled();
  });

  it("ignores all Media Session actions while authentication is disabled", async () => {
    const handlers = {};
    Object.defineProperty(navigator, "mediaSession", {
      configurable: true,
      value: {
        setActionHandler: vi.fn((name, handler) => {
          handlers[name] = handler;
        }),
      },
    });
    const fetchImpl = vi.fn();
    vi.stubGlobal("fetch", fetchImpl);
    const onPlay = vi.fn();
    const onPauseOrSkipNext = vi.fn();
    const { installMediaActionHandlers } = await import(
      "../../src/autodj/static/modules/media-session.js"
    );
    installMediaActionHandlers({
      isEnabled: () => false,
      onPlay,
      onPauseOrSkipNext,
    });

    handlers.play();
    handlers.pause();
    handlers.nexttrack();

    expect(onPlay).not.toHaveBeenCalled();
    expect(onPauseOrSkipNext).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe("app startup integration", () => {
  it("loads app.js without protected installers, fetches, or WebSocket pre-auth", async () => {
    vi.resetModules();
    const html = readFileSync(
      join(process.cwd(), "src/autodj/static/index.html"), "utf8",
    );
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll('script, link[rel="stylesheet"]')
      .forEach((element) => element.remove());
    document.body.replaceChildren(template.content.cloneNode(true));
    const dialog = document.querySelector("#auth-dialog");
    dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));

    const installLibraryJobs = vi.fn();
    const installLiners = vi.fn();
    const installHotkeys = vi.fn();
    const installMediaActionHandlers = vi.fn();
    vi.doMock("../../src/autodj/static/modules/library-jobs.js", () => ({
      applyLibraryJobState: vi.fn(),
      installLibraryJobs,
    }));
    vi.doMock("../../src/autodj/static/modules/liners.js", () => ({
      bumpLinerTrackCount: vi.fn(),
      installLiners,
    }));
    vi.doMock("../../src/autodj/static/modules/hotkeys.js", () => ({
      installHotkeys,
      toggleShortcutsModal: vi.fn(),
    }));
    vi.doMock("../../src/autodj/static/modules/media-session.js", () => ({
      installMediaActionHandlers,
      updateMediaSession: vi.fn(),
    }));
    vi.doMock("../../src/autodj/static/modules/audio-engine.js", () => ({
      _beatmatchOnSkip: false,
      _ctx: null,
      _crossfadeSecondsCache: 0,
      _inBpmCache: 0,
      _lastBrowserPlayback: null,
      _nextTrackPathCache: null,
      _outBpmCache: 0,
      _volume: 1,
      activeIdx: 0,
      applyBrowserPlaybackState: vi.fn(),
      applyEqState: vi.fn(),
      applyTransitionFx: vi.fn(),
      crossfading: false,
      deckActive: vi.fn(),
      decks: [],
      deckStandby: vi.fn(),
      ensureAudioGraph: vi.fn(),
      eqValueLabel: vi.fn(),
      loadCoverArt: vi.fn(),
      playbackEnabled: false,
      playOnDeck: vi.fn(),
      postEq: vi.fn(),
      resetTrackCaches: vi.fn(),
      setApplyState: vi.fn(),
      setLastBrowserPlayback: vi.fn(),
      setSrcOnDeck: vi.fn(),
      setVolume: vi.fn(),
      startCrossfade: vi.fn(),
      stopAllDecks: vi.fn(),
      unlockAndPlay: vi.fn(),
    }));

    const fetchImpl = vi.fn((url) => {
      if (url === "/api/auth/status") {
        return Promise.resolve(response({
          ok: true,
          status: 200,
          json: { required: true, authenticated: false },
        }));
      }
      if (url === "/api/version") {
        return Promise.resolve(response({
          ok: true,
          status: 200,
          json: { version: "test", commit: "test", built_at: "test" },
        }));
      }
      throw new Error(`Protected fetch started before authentication: ${url}`);
    });
    const WebSocketImpl = vi.fn();
    vi.stubGlobal("fetch", fetchImpl);
    vi.stubGlobal("WebSocket", WebSocketImpl);

    await import("../../src/autodj/static/app.js");
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(installHotkeys).toHaveBeenCalledOnce());

    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual([
      "/api/auth/status",
      "/api/version",
    ]);
    expect(WebSocketImpl).not.toHaveBeenCalled();
    expect(installLibraryJobs).not.toHaveBeenCalled();
    expect(installLiners).not.toHaveBeenCalled();
    expect(installHotkeys.mock.calls[0][0].isEnabled).toEqual(expect.any(Function));
    expect(installHotkeys.mock.calls[0][0].isEnabled()).toBe(false);
    expect(installMediaActionHandlers.mock.calls[0][0].isEnabled)
      .toEqual(expect.any(Function));
    expect(installMediaActionHandlers.mock.calls[0][0].isEnabled()).toBe(false);

    vi.unstubAllGlobals();
    vi.doUnmock("../../src/autodj/static/modules/library-jobs.js");
    vi.doUnmock("../../src/autodj/static/modules/liners.js");
    vi.doUnmock("../../src/autodj/static/modules/hotkeys.js");
    vi.doUnmock("../../src/autodj/static/modules/media-session.js");
    vi.doUnmock("../../src/autodj/static/modules/audio-engine.js");
  });
});

describe("login dialog markup", () => {
  it("parses as a named, described, required password form", () => {
    const html = readFileSync(
      join(process.cwd(), "src/autodj/static/index.html"), "utf8",
    );
    const template = document.createElement("template");
    template.innerHTML = html;
    const parsed = template.content;
    const dialog = parsed.querySelector("#auth-dialog");
    const input = parsed.querySelector("#auth-token");
    const label = parsed.querySelector('label[for="auth-token"]');
    const error = parsed.querySelector("#auth-error");

    expect(dialog?.tagName).toBe("DIALOG");
    expect(parsed.querySelector(`#${dialog?.getAttribute("aria-labelledby")}`)
      ?.textContent.trim()).toBe("Connect to AutoDJ");
    expect(parsed.querySelector(`#${dialog?.getAttribute("aria-describedby")}`)
      ?.textContent.trim()).toBe(
        "Enter the access token supplied by the server operator.",
      );
    expect(label?.textContent.trim()).toBe("Access token");
    expect(input?.type).toBe("password");
    expect(input?.required).toBe(true);
    expect(input?.autocomplete).toBe("current-password");
    expect(input?.getAttribute("aria-describedby")).toBe(
      "auth-help auth-error",
    );
    expect(error?.getAttribute("role")).toBe("alert");
    expect(error?.getAttribute("aria-live")).toBe("assertive");
    expect(error?.getAttribute("aria-atomic")).toBe("true");
    expect(parsed.querySelector("#auth-status")?.getAttribute("role"))
      .toBe("status");
    expect(parsed.querySelector("#auth-form")?.contains(
      parsed.querySelector("#auth-status"),
    )).toBe(false);
  });
});
