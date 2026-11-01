import { isStationUrl, stationApi, stationEpoch, stationName } from "./stations.js";
import { sayIfStillPending } from "./live-region.js";

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
  // field: the request field the server blamed, when it named one (the
  // schedule form focuses it).
  constructor(message, { status = null, cause, field = null, detail = null } = {}) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ApiError";
    this.status = status;
    this.field = field;
    this.detail = detail;
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

// A reply that came back after the page switched to another station
// (#70).  It is never applied: it would show the old station's data
// under the new one's name.  Callers end their pending work as for any
// failure; reportBackgroundRequestError and the like say nothing for it.
const STALE_STATION_TEXT = "That reply was for the station this page has just left.";

export function isStaleStationReply(errorValue) {
  return errorValue instanceof ApiError && errorValue.staleStation === true;
}

// An action (any request but GET or HEAD) answered after a switch was
// still done, or refused, on the station the page left.  This says which,
// for the caller to say in place of "Could not ...": the server may well
// have done it.  A server error or a reply that cannot be read leaves it
// unknown.  Null for any other failure.
export function staleActionText(errorValue) {
  if (!isStaleStationReply(errorValue) || !errorValue.stationLeft) return null;
  const name = errorValue.stationLeft;
  if (errorValue.actionOutcome === "done") return `Done on ${name}, the station this page has just left.`;
  if (errorValue.actionOutcome === "refused") return `${name} did not do that; it is the station this page has just left.`;
  return `${name} may not have done that; it is the station this page has just left.`;
}

export function failureText(action, error, fallback = "error") {
  const stale = staleActionText(error);
  if (stale) return stale;
  if (isStaleStationReply(error)) return "";
  // An empty fallback leaves the sentence at the action when the error has
  // no words of its own: "Could not start Night Owl."
  const detail = error?.message || (typeof error === "string" && error) || fallback;
  if (!detail) return `Could not ${action}`;
  const separator = /[.!?]$/.test(action) ? " " : ": ";
  return `Could not ${action}${separator}${detail}`;
}

// What an action's reply says it did, judged as checkedResponse judges
// a reply: "done", "refused", or "unknown" for a server error or a reply
// that cannot be read.
async function actionOutcome(response) {
  if (response.status >= 500) return "unknown";
  if (!response.ok) return "refused";
  if (!isJsonResponse(response)) return "unknown";
  try {
    return refusedPayload(await response.json()) ? "refused" : "done";
  } catch (_) {
    return "unknown";
  }
}

// Every request goes through here.  A route that acts on one station goes
// to the station this page controls (modules/stations.js).
async function rawRequest(url, options) {
  const routed = stationApi(url);
  const sentFor = isStationUrl(url) ? stationEpoch() : null;
  const sentName = sentFor === null ? null : stationName();
  let response;
  try {
    response = await fetch(routed, options);
  } catch (cause) {
    // A cancelled request is the caller's own doing, not a failure.
    if (cause?.name === "AbortError") throw cause;
    const error = new ApiError(UNREACHABLE_TEXT, { cause });
    error.unreachable = true;
    throw error;
  }
  if (sentFor !== null && sentFor !== stationEpoch()) {
    const error = new ApiError(STALE_STATION_TEXT, { status: response.status });
    error.staleStation = true;
    const method = String(options?.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      error.stationLeft = sentName;
      error.actionOutcome = await actionOutcome(response);
      error.actionDone = error.actionOutcome === "done";
    }
    try {
      await response.body?.cancel?.();
    } catch (_) {
      // The reply is dropped whether or not its body can be cancelled.
    }
    throw error;
  }
  return response;
}

// True for a request that never reached the server (the network or a
// tunnel in between was down), as opposed to one the server answered
// with an error.  Such a request can be sent again once the link is back.
export function isServerUnreachable(errorValue) {
  return errorValue instanceof ApiError && errorValue.unreachable === true;
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

function requireMediaType(response, acceptedPrefixes) {
  const type = mediaType(response);
  if (!type || !acceptedPrefixes.some((prefix) => type.startsWith(prefix))) {
    throw new ApiError(UNREADABLE_TEXT, { status: response.status });
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

// A reply that succeeded in HTTP terms but says the server did not do it.
function refusedPayload(payload) {
  return Boolean(payload && typeof payload === "object"
    && (payload.ok === false || payload.success === false));
}

// A 401 opens the shared authentication dialog and fails the request.
function throwIfUnauthorized(response) {
  if (response.status !== 401) return;
  try {
    Promise.resolve(authRequiredHandler()).catch(() => {});
  } catch (_) {
    // AuthenticationRequiredError remains the public request failure.
  }
  throw new AuthenticationRequiredError("Authentication required", { status: 401 });
}

async function checkedResponse(response) {
  throwIfUnauthorized(response);
  if (!isJsonResponse(response)) {
    throw new ApiError(
      response.ok ? UNREADABLE_TEXT : httpFailureText(response.status),
      { status: response.status },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new ApiError(UNREADABLE_TEXT, { status: response.status, cause });
  }

  if (!response.ok) {
    throw new ApiError(
      payloadMessage(payload, httpFailureText(response.status)),
      {
        status: response.status,
        field: typeof payload?.field === "string" ? payload.field : null,
        detail: payload?.detail ?? null,
      },
    );
  }
  if (refusedPayload(payload)) {
    throw new ApiError(
      payloadMessage(payload, NOT_ACCEPTED_TEXT),
      { status: response.status },
    );
  }
  return payload;
}

export async function requestJson(url, options = {}) {
  const response = await rawRequest(url, options);
  return checkedResponse(response);
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

// A successful response whose media type starts with one of
// *acceptedPrefixes*, for a caller that reads the body and headers itself
// (a file to save, such as the play log CSV).
export async function requestFile(url, acceptedPrefixes, options = {}) {
  const response = await rawRequest(url, options);
  throwIfUnauthorized(response);
  if (!response.ok) {
    if (isJsonResponse(response)) await checkedResponse(response);
    throw new ApiError(httpFailureText(response.status), { status: response.status });
  }
  requireMediaType(response, acceptedPrefixes);
  return response;
}

export async function requestBinary(url, options = {}) {
  const response = await requestFile(url, ["audio/", "application/octet-stream"], options);
  return response.arrayBuffer();
}

export async function probeResource(url, options = {}) {
  const response = await rawRequest(url, options);
  try {
    if (response.status === 404) return false;
    throwIfUnauthorized(response);
    if (!response.ok) {
      if (isJsonResponse(response)) await checkedResponse(response);
      throw new ApiError(httpFailureText(response.status), { status: response.status });
    }
    requireMediaType(response, ["image/"]);
    return true;
  } finally {
    try {
      await response.body?.cancel?.();
    } catch (_) {
      // Headers/status already determined the result; teardown is best-effort.
    }
  }
}

// One call per set of arguments at a time; a call made after a station
// switch never joins one made for the station before (#70).
export function makeSingleFlight(operation) {
  const flights = [];
  return function singleFlight(...args) {
    const station = stationEpoch();
    const existing = flights.find((flight) =>
      flight.station === station
      && flight.args.length === args.length
      && flight.args.every((arg, index) => Object.is(arg, args[index]))
    );
    if (existing) return existing.promise;
    const flight = { args, station, promise: null };
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
// An unfocused control is disabled as before, unless *keepEnabled* asks for
// the focused button's treatment: a button whose disabled state NVDA would
// report later, such as Save changes pressed from a dialog (#331).
function startHold(control, keepEnabled) {
  // Containers expose pending state without disabling descendants or deferring
  // the operation. Their controllers own submission/cancellation guards.
  if (!control.matches("button, input, select, textarea, option, optgroup")) return { count: 0, mode: "container", restore: () => {} };
  if (!holdsFocus(control) && !(keepEnabled && isButtonLike(control))) {
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
  doc.addEventListener("click", guard, true);
  return {
    count: 0,
    mode: "guard",
    restore: () => {
      doc.removeEventListener("click", guard, true);
    },
  };
}

// The one busy presentation: aria-busy for assistive technology and
// .is-pending for the look.  withDisabled uses it for a request; a caller
// whose work spans several requests (a polled server job) sets and clears it.
export function markPending(control, pending) {
  if (!control) return;
  if (pending) control.setAttribute("aria-busy", "true");
  else control.removeAttribute("aria-busy");
  control.classList.toggle("is-pending", pending);
}

export async function withDisabled(control, operation, { pendingText, say, signal, keepEnabled = false } = {}) {
  // A view already cleared: nothing runs and nothing flashes busy.
  if (signal?.aborted) throw signal.reason ?? new globalThis.DOMException("The operation was aborted.", "AbortError");
  if (!control) return operation();
  let hold = disabledOwners.get(control);
  if (!hold) {
    hold = startHold(control, keepEnabled);
    const busy = control.getAttribute("aria-busy");
    const pending = control.classList.contains("is-pending");
    const restore = hold.restore;
    markPending(control, true);
    hold.restore = () => {
      restore();
      if (busy === null) control.removeAttribute("aria-busy");
      else control.setAttribute("aria-busy", busy);
      control.classList.toggle("is-pending", pending);
    };
    disabledOwners.set(control, hold);
  }
  hold.count += 1;
  const stopPending = pendingText && say ? sayIfStillPending(say, pendingText) : () => {};
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    signal?.removeEventListener("abort", release);
    stopPending();
    hold.count -= 1;
    if (hold.count === 0) {
      hold.restore();
      disabledOwners.delete(control);
    }
  };
  // Clearing a view releases its controls even if an old request is still
  // outstanding. Its eventual finally must not release a newer owner.
  signal?.addEventListener("abort", release, { once: true });
  try {
    if (hold.mode !== "serial") return await operation();
    const run = hold.tail.then(() => operation());
    hold.tail = run.catch(() => {});
    return await run;
  } finally {
    release();
  }
}
