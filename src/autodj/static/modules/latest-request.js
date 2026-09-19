export function createLatestRequestOwner() {
  let active = null;

  function begin() {
    if (active) active.controller.abort();
    const request = {
      controller: new AbortController(),
    };
    request.signal = request.controller.signal;
    active = request;
    return request;
  }

  function isCurrent(request) {
    return active === request && !request.signal.aborted;
  }

  function finish(request) {
    if (active === request) active = null;
  }

  function cancel() {
    if (active) active.controller.abort();
    active = null;
  }

  return { begin, cancel, finish, isCurrent };
}
