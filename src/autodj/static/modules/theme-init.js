// Applies the stored Theme choice (Settings, Display and keys) before the
// page is first painted, so a Light or Dark choice never flashes the
// other theme.  A classic script loaded from <head>, not a module: module
// scripts run only after parsing.  It is a file rather than inline code
// so the /status page's Content-Security-Policy (script-src 'self')
// allows it.  modules/theme.js owns the setting itself; the key and the
// two pinned values must match it.  Blocked storage means System.
(function applyStoredTheme() {
  let choice = null;
  try {
    choice = globalThis.localStorage.getItem("autodj.theme");
  } catch {
    return;
  }
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  }
})();
