let authRequiredHandler = () => {};
let authenticatedRequestEpoch = 0;

export function captureAuthenticatedRequestEpoch() {
  return authenticatedRequestEpoch;
}

export function invalidateAuthenticatedRequestEpoch() {
  authenticatedRequestEpoch += 1;
}

export function isAuthenticatedRequestCurrent(epoch) {
  return epoch === authenticatedRequestEpoch;
}

class ApiError extends Error {
  constructor(message, { status = null, url = "", cause } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ApiError";
    this.status = status;
    this.url = url;
  }
}

export class AuthenticationRequiredError extends ApiError {
  constructor(message = "Authentication required", options = {}) {
    super(message, options);
    this.name = "AuthenticationRequiredError";
  }
}

export function setAuthRequiredHandler(handler) {
  if (typeof handler !== "function") {
    throw new TypeError("authentication-required handler must be a function");
  }
  authRequiredHandler = handler;
}

// What a failed request says out loud.  Every failure reaches speech
// through a status region, so it is a plain sentence: never "Failed to
// fetch", a URL or a bare status code.
const UNREACHABLE_TEXT = "The AutoDJ server is not reachable.";
const UNREADABLE_TEXT = "The AutoDJ server sent a reply this page cannot read.";
const NOT_ACCEPTED_TEXT = "The AutoDJ server did not accept that request.";

function httpFailureText(status) {
  return status >= 500
    ? "The AutoDJ server hit an error. The server log has the details."
    : NOT_ACCEPTED_TEXT;
}

async function rawRequest(url, options) {
  try {
    return await fetch(url, options);
  } catch (cause) {
    // A cancelled request is the caller's own doing, not a failure.
    if (cause?.name === "AbortError") throw cause;
    throw new ApiError(UNREACHABLE_TEXT, { url, cause });
  }
}

function responseUrl(response, fallback) {
  return fallback || response.url || "request";
}

function isJsonResponse(response) {
  const type = mediaType(response);
  return type === "application/json"
    || /^application\/[-!#$%&'*+.^_`|~0-9a-z]+\+json$/.test(type);
}

function mediaType(response) {
  return (response.headers?.get?.("Content-Type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
}

function requireMediaType(response, url, acceptedPrefixes) {
  const type = mediaType(response);
  if (!type) {
    throw new ApiError(UNREADABLE_TEXT, {
      status: response.status,
      url,
    });
  }
  if (!acceptedPrefixes.some((prefix) => type.startsWith(prefix))) {
    throw new ApiError(UNREADABLE_TEXT, {
      status: response.status,
      url,
    });
  }
}

function payloadMessage(payload, fallback) {
  if (payload && typeof payload === "object") {
    for (const key of ["detail", "error", "message"]) {
      if (typeof payload[key] === "string" && payload[key].trim()) {
        return payload[key].trim();
      }
    }
  }
  return fallback;
}

function notifyAuthenticationRequired() {
  try {
    Promise.resolve(authRequiredHandler()).catch(() => {});
  } catch (_) {
    // AuthenticationRequiredError remains the public request failure.
  }
}

async function checkedResponse(response, { url = "" } = {}) {
  const requestUrl = responseUrl(response, url);
  if (response.status === 401) {
    notifyAuthenticationRequired();
    throw new AuthenticationRequiredError("Authentication required", {
      status: 401,
      url: requestUrl,
    });
  }
  if (!isJsonResponse(response)) {
    throw new ApiError(
      response.ok ? UNREADABLE_TEXT : httpFailureText(response.status),
      { status: response.status, url: requestUrl },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new ApiError(UNREADABLE_TEXT, {
      status: response.status,
      url: requestUrl,
      cause,
    });
  }

  if (!response.ok) {
    throw new ApiError(
      payloadMessage(payload, httpFailureText(response.status)),
      { status: response.status, url: requestUrl },
    );
  }
  if (payload && typeof payload === "object"
      && (payload.ok === false || payload.success === false)) {
    throw new ApiError(
      payloadMessage(payload, NOT_ACCEPTED_TEXT),
      { status: response.status, url: requestUrl },
    );
  }
  return payload;
}

export async function requestJson(url, options = {}) {
  const response = await rawRequest(url, options);
  return checkedResponse(response, { url });
}

export function requestJsonBestEffort(url, options = {}, reporter) {
  if (typeof reporter !== "function") {
    throw new TypeError("requestJsonBestEffort requires a reporter function");
  }
  return requestJson(url, options).catch((errorValue) => {
    reporter(errorValue);
    return null;
  });
}

export function postJsonBestEffort(url, body, reporter, options = {}) {
  return requestJsonBestEffort(url, {
    ...options,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
    body: JSON.stringify(body),
  }, reporter);
}

export async function requestBinary(url, options = {}) {
  const response = await rawRequest(url, options);
  if (response.status === 401) {
    notifyAuthenticationRequired();
    throw new AuthenticationRequiredError("Authentication required", {
      status: 401,
      url,
    });
  }
  if (!response.ok) {
    if (isJsonResponse(response)) await checkedResponse(response, { url });
    throw new ApiError(httpFailureText(response.status), {
      status: response.status,
      url,
    });
  }
  requireMediaType(response, url, ["audio/", "application/octet-stream"]);
  return response.arrayBuffer();
}

export async function probeResource(url, options = {}) {
  const response = await rawRequest(url, options);
  try {
    if (response.status === 404) return false;
    if (response.status === 401) {
      notifyAuthenticationRequired();
      throw new AuthenticationRequiredError("Authentication required", {
        status: 401,
        url,
      });
    }
    if (!response.ok) {
      if (isJsonResponse(response)) await checkedResponse(response, { url });
      throw new ApiError(httpFailureText(response.status), {
        status: response.status,
        url,
      });
    }
    requireMediaType(response, url, ["image/"]);
    return true;
  } finally {
    try {
      await response.body?.cancel?.();
    } catch (_) {
      // Headers/status already determined the result; teardown is best-effort.
    }
  }
}

export function makeSingleFlight(operation) {
  const flights = [];
  return function singleFlight(...args) {
    const existing = flights.find((flight) =>
      flight.args.length === args.length
      && flight.args.every((arg, index) => Object.is(arg, args[index]))
    );
    if (existing) return existing.promise;
    const flight = { args, promise: null };
    flight.promise = Promise.resolve()
      .then(() => operation.apply(this, args))
      .finally(() => {
        const index = flights.indexOf(flight);
        if (index >= 0) flights.splice(index, 1);
      });
    flights.push(flight);
    return flight.promise;
  };
}

const disabledOwners = new WeakMap();

function holdsFocus(control) {
  const active = control.ownerDocument?.activeElement;
  return Boolean(active) && (active === control || Boolean(control.contains?.(active)));
}

function isButtonLike(control) {
  if (control.tagName === "BUTTON") return true;
  return control.tagName === "INPUT"
    && ["button", "submit", "reset"].includes(control.type);
}

// Setting `disabled` on the focused control throws focus to <body> in
// Chromium, and a closed <select> loses the arrow key that follows, so a
// control that holds focus is never really disabled:
//   - a focused button stays enabled and a capture-phase guard swallows
//     its clicks until the request settles, so a double press still sends
//     one request and focus never moves.  It gets no aria-disabled: NVDA
//     speaks a state change on the focused control, so every press was
//     heard as "unavailable" and nothing else.  The pending look comes
//     from the .is-pending class, which assistive tech never sees;
//   - any other focused field keeps working, and its requests run one
//     after another so the server sees the changes in the order made.
// An unfocused control is disabled as before.
function startHold(control) {
  if (!holdsFocus(control)) {
    const wasDisabled = control.disabled;
    control.disabled = true;
    return { count: 0, mode: "disabled", restore: () => { control.disabled = wasDisabled; } };
  }
  if (!isButtonLike(control)) {
    return { count: 0, mode: "serial", tail: Promise.resolve(), restore: () => {} };
  }
  const doc = control.ownerDocument;
  const guard = (event) => {
    if (!control.contains(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  control.classList.add("is-pending");
  doc.addEventListener("click", guard, true);
  return {
    count: 0,
    mode: "guard",
    restore: () => {
      doc.removeEventListener("click", guard, true);
      control.classList.remove("is-pending");
    },
  };
}

export async function withDisabled(control, operation) {
  if (!control) return operation();
  let hold = disabledOwners.get(control);
  if (!hold) {
    hold = startHold(control);
    disabledOwners.set(control, hold);
  }
  hold.count += 1;
  try {
    if (hold.mode !== "serial") return await operation();
    const run = hold.tail.then(() => operation());
    hold.tail = run.catch(() => {});
    return await run;
  } finally {
    hold.count -= 1;
    if (hold.count === 0) {
      hold.restore();
      disabledOwners.delete(control);
    }
  }
}
