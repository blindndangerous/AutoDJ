// Whether beets' songs are the library's songs (#386).
//
// The server matches beets' paths to the library's (autodj/beets_paths.py)
// and says so in Library tools, Categories and the station state.  A
// mismatch empties every beets query category, so it is shown as static
// text the operator can find, never announced: a polled status would
// otherwise repeat it.

// The words for the server's beets_paths view; "" without beets.
// *full* adds the match count, for Library tools.
export function beetsPathsText(view, { full = false } = {}) {
  if (!view) return "";
  if (view.problem) return `Beets: ${view.problem} ${view.fix || ""}`.trim();
  if (view.note) return `Beets: ${full && view.summary ? `${view.summary}. ` : ""}${view.note}`;
  return full && view.summary ? `Beets: ${view.summary}.` : "";
}

// Write *text* into *element* only when it changed, hiding it when empty.
// *problem* marks it as an error (red edge) rather than plain help.
export function showStaticText(element, text, problem = false) {
  if (!element) return;
  if (element.textContent !== text) element.textContent = text;
  element.hidden = !text;
  element.classList.toggle("field-error", Boolean(text) && problem);
}

export function showBeetsPaths(element, view, options = {}) {
  showStaticText(element, beetsPathsText(view, options), Boolean(view?.problem));
}
