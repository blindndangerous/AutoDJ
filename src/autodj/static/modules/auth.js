import {
  AuthenticationRequiredError,
  requestJson,
} from "./api-client.js";

const bootstrapRuns = new WeakMap();

function loginFailureMessage(response) {
  if (response.status === 401) return "That access token was not accepted.";
  if (response.status === 413) return "That access token is too large.";
  if (response.status === 429) {
    const retryAfter = response.headers?.get?.("Retry-After");
    if (/^[1-9]\d*$/.test(retryAfter || "")) {
      const seconds = Number(retryAfter);
      if (Number.isSafeInteger(seconds) && seconds <= 86400) {
        return `Too many login attempts. Wait about ${seconds} seconds before trying again.`;
      }
    }
    return "Too many login attempts. Wait before trying again.";
  }
  return "Login failed. Check the server and try again.";
}

export function initAuthDialog({
  document,
  fetchImpl = fetch,
  onSuccess = () => location.reload(),
}) {
  const dialog = document.querySelector("#auth-dialog");
  const form = document.querySelector("#auth-form");
  const token = document.querySelector("#auth-token");
  const status = document.querySelector("#auth-status");
  const error = document.querySelector("#auth-error");
  const submitButton = form?.querySelector('button[type="submit"]');
  if (!dialog || !form || !token || !status || !error || !submitButton) {
    throw new Error("Authentication dialog markup is incomplete.");
  }

  let pendingSubmit = null;

  function clearError() {
    error.textContent = "";
    token.removeAttribute("aria-invalid");
  }

  async function announceError(message) {
    error.textContent = "";
    await Promise.resolve();
    error.textContent = message;
    token.setAttribute("aria-invalid", "true");
    token.focus();
  }

  function setBusy(busy) {
    form.setAttribute("aria-busy", String(busy));
    token.readOnly = busy;
    submitButton.disabled = busy;
    status.textContent = busy ? "Signing in…" : "";
  }

  async function performSubmit() {
    clearError();
    let candidate = token.value;
    token.value = "";
    if (!candidate) {
      await announceError("Enter an access token.");
      return false;
    }

    setBusy(true);
    let failureMessage = null;
    try {
      const response = await fetchImpl("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: candidate }),
      });
      if (!response.ok) failureMessage = loginFailureMessage(response);
    } catch (_errorValue) {
      failureMessage = "Login failed. Check the server and try again.";
    } finally {
      candidate = "";
      setBusy(false);
    }

    if (failureMessage) {
      await announceError(failureMessage);
      return false;
    }
    if (dialog.open) dialog.close();
    onSuccess();
    return true;
  }

  function submit() {
    if (pendingSubmit) return pendingSubmit;
    pendingSubmit = performSubmit().finally(() => {
      pendingSubmit = null;
    });
    return pendingSubmit;
  }

  function show() {
    if (!dialog.open) dialog.showModal();
    if (!pendingSubmit) setBusy(false);
    token.focus();
  }

  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    token.focus();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const focusable = [token, submitButton].filter((element) => !element.disabled);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });

  return { show, submit };
}

function validAuthState(value) {
  return value !== null
    && !Array.isArray(value)
    && typeof value === "object"
    && typeof value.required === "boolean"
    && typeof value.authenticated === "boolean";
}

async function runBootstrap({
  fetchImpl,
  requestState,
  auth,
  startAuthenticatedApp,
  onError,
  startupState,
}) {
  try {
    const authResponse = await fetchImpl("/api/auth/status");
    if (authResponse.status === 401) {
      auth.show();
      return false;
    }
    if (!authResponse.ok) {
      throw new Error(`/api/auth/status returned ${authResponse.status}`);
    }
    const authState = await authResponse.json();
    if (!validAuthState(authState)) {
      throw new Error("/api/auth/status returned malformed data");
    }
    if (authState.required && !authState.authenticated) {
      auth.show();
      return false;
    }

    const initialState = await requestState("/api/status");
    startupState.attempted = true;
    startAuthenticatedApp(initialState);
    return true;
  } catch (errorValue) {
    if (errorValue instanceof AuthenticationRequiredError) return false;
    onError(errorValue);
    return false;
  }
}

export function bootstrapAuthenticatedApp({
  fetchImpl = fetch,
  requestState = requestJson,
  auth,
  startAuthenticatedApp,
  onError = () => {},
}) {
  if (bootstrapRuns.has(startAuthenticatedApp)) {
    return bootstrapRuns.get(startAuthenticatedApp);
  }
  const startupState = { attempted: false };
  const run = runBootstrap({
    fetchImpl,
    requestState,
    auth,
    startAuthenticatedApp,
    onError,
    startupState,
  });
  bootstrapRuns.set(startAuthenticatedApp, run);
  void run.then(
    (started) => {
      if (
        !started
        && !startupState.attempted
        && bootstrapRuns.get(startAuthenticatedApp) === run
      ) {
        bootstrapRuns.delete(startAuthenticatedApp);
      }
    },
    () => {
      if (
        !startupState.attempted
        && bootstrapRuns.get(startAuthenticatedApp) === run
      ) {
        bootstrapRuns.delete(startAuthenticatedApp);
      }
    },
  );
  return run;
}

export function handleWebSocketAuthenticationClose(
  event,
  { auth, onExpired = () => {} },
) {
  if (!event || event.code !== 4401) return false;
  onExpired();
  auth.show();
  return true;
}

export async function reconnectWebSocketAfterClose({
  event,
  fetchImpl = fetch,
  auth,
  onExpired = () => {},
  reconnect,
}) {
  if (event?.code === 1006) {
    try {
      const response = await fetchImpl("/api/auth/status");
      if (response.status === 401) {
        onExpired();
        auth.show();
        return false;
      }
      if (response.ok) {
        const authState = await response.json();
        if (!validAuthState(authState)
            || (authState.required && !authState.authenticated)) {
          onExpired();
          auth.show();
          return false;
        }
      }
    } catch (_errorValue) {
      // Status can be unavailable during a genuine server restart.
      // Preserve the existing WebSocket retry path in that case.
    }
  }
  reconnect();
  return true;
}
