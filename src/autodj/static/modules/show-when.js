// Hide-unchecked-children mechanism.
//
// Any element with `data-show-when="checkboxId"` is shown / hidden
// based on the referenced checkbox's checked state.  Toggles the
// `hidden` attribute on the wrapper element so the label, input, and
// description all collapse together (per accessibility-lead review:
// setting `hidden` on the wrapper avoids the orphaned-label /
// orphaned-desc state SR users would otherwise hit).

// Resolve a data-show-when value to a boolean.
// Supports two forms:
//   "checkboxId"        — true when that checkbox is checked
//   "selectId=value"    — true when that select's value matches
function _isVisible(spec) {
  const eq = spec.indexOf("=");
  if (eq === -1) {
    const cb = document.getElementById(spec);
    return !!(cb && cb.checked);
  }
  const el = document.getElementById(spec.slice(0, eq));
  return !!(el && el.value === spec.slice(eq + 1));
}

export function applyShowWhen() {
  const wrappers = document.querySelectorAll("[data-show-when]");
  for (const w of wrappers) {
    const spec = w.getAttribute("data-show-when");
    if (_isVisible(spec)) w.removeAttribute("hidden");
    else w.setAttribute("hidden", "");
  }
}

export function installShowWhenListener() {
  // Single delegated listener covers checkboxes and selects.
  document.addEventListener("change", (e) => {
    if (e.target && e.target.matches &&
        e.target.matches("input[type=checkbox], select")) {
      applyShowWhen();
    }
  });
}
