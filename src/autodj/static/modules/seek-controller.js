const installedControllers = new WeakMap();

export function installSeekController(element, { preview, commit }) {
  installedControllers.get(element)?.destroy();

  let activePointerId = null;
  let fallbackTarget = null;
  let destroyed = false;
  const ownerDocument = element.ownerDocument;
  const windowTarget = ownerDocument?.defaultView;

  function isDragging() {
    return activePointerId !== null;
  }

  function owns(event) {
    return activePointerId !== null && event.pointerId === activePointerId;
  }

  function release(pointerId) {
    try {
      element.releasePointerCapture(pointerId);
    } catch (_) {}
  }

  function clearFallback() {
    if (!fallbackTarget) return;
    fallbackTarget.removeEventListener("pointerup", onOutsidePointerUp);
    fallbackTarget.removeEventListener("pointercancel", onPointerCancel);
    fallbackTarget = null;
  }

  function finish(event, shouldCommit) {
    if (!owns(event)) return;
    const pointerId = activePointerId;
    activePointerId = null;
    clearFallback();
    try {
      if (shouldCommit) commit(event);
    } finally {
      release(pointerId);
    }
  }

  function cancel() {
    if (!isDragging()) return;
    const pointerId = activePointerId;
    activePointerId = null;
    clearFallback();
    release(pointerId);
  }

  function previewOwned(event) {
    try {
      preview(event);
    } catch (errorValue) {
      cancel();
      throw errorValue;
    }
  }

  function onPointerDown(event) {
    if (destroyed || isDragging() || event.isPrimary === false
        || (event.button !== undefined && event.button !== 0)) return;
    activePointerId = event.pointerId;
    installFallback();
    try {
      element.setPointerCapture(event.pointerId);
    } catch (_) {}
    try {
      previewOwned(event);
    } finally {
      event.preventDefault();
    }
  }

  function onPointerMove(event) {
    if (owns(event)) previewOwned(event);
  }

  function releasedOutside(event) {
    if (typeof event.clientX !== "number" || typeof event.clientY !== "number"
        || typeof element.getBoundingClientRect !== "function") return false;
    const rect = element.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom;
  }

  function onPointerUp(event) {
    finish(event, !releasedOutside(event));
  }

  function onOutsidePointerUp(event) {
    finish(event, false);
  }

  function onPointerCancel(event) {
    finish(event, false);
  }

  function onLostPointerCapture(event) {
    if (!owns(event)) return;
    activePointerId = null;
    clearFallback();
  }

  function onVisibilityChange() {
    if (ownerDocument?.visibilityState === "hidden") cancel();
  }

  function installFallback() {
    if (!ownerDocument?.addEventListener) return;
    fallbackTarget = ownerDocument;
    ownerDocument.addEventListener("pointerup", onOutsidePointerUp);
    ownerDocument.addEventListener("pointercancel", onPointerCancel);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    cancel();
    element.removeEventListener("pointerdown", onPointerDown);
    element.removeEventListener("pointermove", onPointerMove);
    element.removeEventListener("pointerup", onPointerUp);
    element.removeEventListener("pointercancel", onPointerCancel);
    element.removeEventListener("lostpointercapture", onLostPointerCapture);
    ownerDocument?.removeEventListener("visibilitychange", onVisibilityChange);
    windowTarget?.removeEventListener("blur", cancel);
    if (installedControllers.get(element) === controller) {
      installedControllers.delete(element);
    }
  }

  const controller = { cancel, destroy, isDragging };
  element.addEventListener("pointerdown", onPointerDown);
  element.addEventListener("pointermove", onPointerMove);
  element.addEventListener("pointerup", onPointerUp);
  element.addEventListener("pointercancel", onPointerCancel);
  element.addEventListener("lostpointercapture", onLostPointerCapture);
  ownerDocument?.addEventListener("visibilitychange", onVisibilityChange);
  windowTarget?.addEventListener("blur", cancel);
  installedControllers.set(element, controller);
  return controller;
}
