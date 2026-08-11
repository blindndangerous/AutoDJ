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
