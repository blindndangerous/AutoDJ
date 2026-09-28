// Dragging on the progress bar: preview while the pointer moves, seek
// when it is released over the bar.  Pointer capture keeps the moves and
// the release coming to the bar even off its edge.  A release outside it,
// a cancelled pointer or a lost capture (alt-tab, a touch the browser
// took over) drops the drag without seeking.  The keyboard and
// aria-valuetext side of the slider lives in app.js.

export function installSeekController(element, { preview, commit }) {
  let pointerId = null;
  const owns = (event) => pointerId !== null && event.pointerId === pointerId;

  function cancel() {
    if (pointerId === null) return;
    const id = pointerId;
    pointerId = null;
    element.releasePointerCapture(id);
  }

  element.addEventListener("pointerdown", (event) => {
    if (pointerId !== null || !event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    pointerId = event.pointerId;
    element.setPointerCapture(pointerId);
    preview(event);
  });
  element.addEventListener("pointermove", (event) => {
    if (owns(event)) preview(event);
  });
  element.addEventListener("pointerup", (event) => {
    if (!owns(event)) return;
    const rect = element.getBoundingClientRect();
    const inside = event.clientX >= rect.left && event.clientX <= rect.right
      && event.clientY >= rect.top && event.clientY <= rect.bottom;
    cancel();
    if (inside) commit(event);
  });
  element.addEventListener("pointercancel", (event) => {
    if (owns(event)) cancel();
  });
  element.addEventListener("lostpointercapture", (event) => {
    if (owns(event)) pointerId = null;
  });

  return { cancel, isDragging: () => pointerId !== null };
}
