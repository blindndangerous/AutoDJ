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

export class ApiError extends Error {
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

function rawRequest(url, options) {
  return fetch(url, options);
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
    throw new ApiError(`${url} returned missing content type`, {
      status: response.status,
      url,
    });
  }
  if (!acceptedPrefixes.some((prefix) => type.startsWith(prefix))) {
    throw new ApiError(`${url} returned unexpected content type ${type}`, {
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

export async function checkedResponse(response, { url = "" } = {}) {
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
      response.ok
        ? `${requestUrl} returned non-JSON content`
        : `${requestUrl} returned HTTP ${response.status}`,
      { status: response.status, url: requestUrl },
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch (cause) {
    throw new ApiError(`${requestUrl} returned malformed JSON`, {
      status: response.status,
      url: requestUrl,
      cause,
    });
  }

  if (!response.ok) {
    throw new ApiError(
      payloadMessage(payload, `${requestUrl} returned HTTP ${response.status}`),
      { status: response.status, url: requestUrl },
    );
  }
  if (payload && typeof payload === "object"
      && (payload.ok === false || payload.success === false)) {
    throw new ApiError(
      payloadMessage(payload, "The request was not accepted."),
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
    throw new ApiError(`${url} returned HTTP ${response.status}`, {
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
      throw new ApiError(`${url} returned HTTP ${response.status}`, {
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

export async function withDisabled(control, operation) {
  if (!control) return operation();
  let ownership = disabledOwners.get(control);
  if (!ownership) {
    ownership = { count: 0, wasDisabled: control.disabled };
    disabledOwners.set(control, ownership);
  }
  ownership.count += 1;
  control.disabled = true;
  try {
    return await operation();
  } finally {
    ownership.count -= 1;
    if (ownership.count === 0) {
      control.disabled = ownership.wasDisabled;
      disabledOwners.delete(control);
    }
  }
}
