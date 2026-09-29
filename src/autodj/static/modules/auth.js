import {
  AuthenticationRequiredError,
  requestJson,
} from "./api-client.js";

const MAX_SERVER_DETAIL_LENGTH = 200;
// Shown when a session this browser had is revoked or expires.
export const SIGNED_OUT_TEXT = "This browser was signed out. Enter a new pairing code.";

// The pairing lockout explains itself ("Too many wrong pairing codes
// from this device. Try again in 42 seconds."), which tells the user
// whether to wait or fetch a new code.  Anything else falls back.
async function pairingLockoutDetail(response) {
  try {
    const payload = await response.json();
    const detail = payload && typeof payload === "object" ? payload.detail : null;
    if (typeof detail !== "string") return null;
    const text = detail.trim();
    if (!text || text.length > MAX_SERVER_DETAIL_LENGTH) return null;
    return text;
  } catch (_) {
    return null;
  }
}

async function pairingFailureMessage(response) {
  if (response.status === 401) return "That pairing code is invalid or expired.";
  if (response.status === 413) return "That pairing request is too large.";
  if (response.status === 429) {
    const detail = await pairingLockoutDetail(response);
    if (detail) return detail;
    const retryAfter = response.headers?.get?.("Retry-After");
    if (/^[1-9]\d*$/.test(retryAfter || "")) {
      const seconds = Number(retryAfter);
      if (Number.isSafeInteger(seconds) && seconds <= 86400) {
        return `Too many pairing attempts. Wait about ${seconds} seconds before trying again.`;
      }
    }
    return "Too many pairing attempts. Wait before trying again.";
  }
  return "Pairing failed. Check the server and try again.";
}

export function initAuthDialog({
  document,
  fetchImpl = fetch,
  onSuccess = () => location.reload(),
}) {
  const dialog = document.querySelector("#auth-dialog");
  const form = document.querySelector("#auth-form");
  const token = document.querySelector("#auth-token");
  const deviceName = document.querySelector("#auth-device-name");
  const status = document.querySelector("#auth-status");
  const error = document.querySelector("#auth-error");
  const reasonText = document.querySelector("#auth-reason");
  const submitButton = form?.querySelector('button[type="submit"]');
  if (!dialog || !form || !token || !deviceName || !status || !error
      || !reasonText || !submitButton) {
    throw new Error("Authentication dialog markup is incomplete.");
  }

  let pendingSubmit = null;

  function clearError() {
    error.textContent = "";
    token.removeAttribute("aria-invalid");
  }

  // The role=alert region speaks the message; the field is focused
  // afterwards but does not also reference it, or NVDA reads it twice.
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
    deviceName.readOnly = busy;
    status.textContent = busy ? "Pairing browser…" : "";
  }

  async function performSubmit() {
    clearError();
    let candidate = token.value;
    token.value = "";
    if (!candidate) {
      await announceError("Enter the 8-digit pairing code.");
      return false;
    }

    setBusy(true);
    let failureMessage = null;
    try {
      const response = await fetchImpl("/api/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: candidate,
          device_name: deviceName.value.trim() || "Paired browser",
        }),
      });
      if (!response.ok) failureMessage = await pairingFailureMessage(response);
    } catch (_errorValue) {
      failureMessage = "Pairing failed. Check the server and try again.";
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

  // `reason` says why a browser that was paired is being asked again; it
  // is part of the dialog's description, so it is read as the dialog opens.
  function show(reason = "") {
    reasonText.textContent = reason;
    reasonText.hidden = !reason;
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
    const focusable = [token, deviceName, submitButton].filter(
      (element) => !element.disabled,
    );
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

// Checks pairing, then loads the first state and starts the page.  Resolves
// true once started; false when the pairing dialog opened or startup
// failed (onError has said why).
export async function bootstrapAuthenticatedApp({
  fetchImpl = fetch,
  requestState = requestJson,
  auth,
  startAuthenticatedApp,
  onError = () => {},
}) {
  try {
    let authResponse;
    try {
      authResponse = await fetchImpl("/api/auth/status");
    } catch (cause) {
      throw new Error("The AutoDJ server is not reachable.", { cause });
    }
    if (authResponse.status === 401) {
      auth.show();
      return false;
    }
    if (!authResponse.ok) {
      throw new Error("The AutoDJ server could not check whether this browser is paired.");
    }
    const authState = await authResponse.json();
    if (!validAuthState(authState)) {
      throw new Error("The AutoDJ server sent a reply this page cannot read.");
    }
    if (authState.required && !authState.authenticated) {
      auth.show();
      return false;
    }

    const initialState = await requestState("/api/status");
    startAuthenticatedApp(initialState);
    return true;
  } catch (errorValue) {
    if (errorValue instanceof AuthenticationRequiredError) return false;
    onError(errorValue);
    return false;
  }
}

export function handleWebSocketAuthenticationClose(
  event,
  { auth, onExpired = () => {} },
) {
  if (!event || event.code !== 4401) return false;
  onExpired();
  auth.show(SIGNED_OUT_TEXT);
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
        auth.show(SIGNED_OUT_TEXT);
        return false;
      }
      if (response.ok) {
        const authState = await response.json();
        if (!validAuthState(authState)
            || (authState.required && !authState.authenticated)) {
          onExpired();
          auth.show(SIGNED_OUT_TEXT);
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
