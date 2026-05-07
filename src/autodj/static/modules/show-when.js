// Hide-unchecked-children mechanism.
//
// Any element with `data-show-when="checkboxId"` is shown / hidden
// based on the referenced checkbox's checked state.  Toggles the
// `hidden` attribute on the wrapper element so the label, input, and
// description all collapse together (per accessibility-lead review:
// setting `hidden` on the wrapper avoids the orphaned-label /
// orphaned-desc state SR users would otherwise hit).

export function applyShowWhen() {
  const wrappers = document.querySelectorAll("[data-show-when]");
  for (const w of wrappers) {
    const id = w.getAttribute("data-show-when");
    const cb = id ? document.getElementById(id) : null;
    const visible = !!(cb && cb.checked);
    if (visible) w.removeAttribute("hidden");
    else w.setAttribute("hidden", "");
  }
}

export function installShowWhenListener() {
  // Single delegated listener picks up any number of [data-show-when]
  // sources without re-binding when checkboxes are added later.
  document.addEventListener("change", (e) => {
    if (e.target && e.target.matches && e.target.matches("input[type=checkbox]")) {
      applyShowWhen();
    }
  });
}
