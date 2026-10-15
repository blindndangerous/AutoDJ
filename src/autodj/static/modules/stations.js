// Several stations on one server (#70): which station this page controls,
// where each request for it goes, and the station switcher.
//
// Every route that acts on one station answers at /api/<route> for the
// default station and at /api/stations/<slug>/<route> for any station.
// The page keeps writing /api/<route>; api-client.js sends each request
// through stationApi(), which puts a station route under the station
// this page controls.  Shared routes (the library, search, pairing,
// presets...) are left as they are.  STATION_ROUTES must list exactly
// the station router's routes: tests/integration/test_station_routes.py
// compares the two, so a new station route cannot be forgotten here.
//
// The switcher is the APG menu button with menuitemradio items, in the
// banner.  A native <select> was not used: Chrome and Edge on Windows
// fire change on every arrow key while it is closed, and each change
// would tear down the socket and reload every panel.  The menu commits
// only on Enter, Space or a click, and the button's name always says
// which station the page controls.

export const DEFAULT_STATION = "default";
const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;

// Relative to /api; {name} is one path segment.  The socket (/ws) and the
// announcer's machine routes are not requested by this page.
export const STATION_ROUTES = Object.freeze([
  "/status",
  "/nowplaying",
  "/history",
  "/history/export",
  "/skip",
  "/seek",
  "/pause",
  "/volume",
  "/mute",
  "/play-next",
  "/queue/add",
  "/queue/remove",
  "/queue/reorder",
  "/advance",
  "/repick-next",
  "/random-track",
  "/preset",
  "/transition",
  "/djmix",
  "/playback-settings",
  "/bpm-range",
  "/discovery",
  "/discovery/toggle",
  "/eq",
  "/stream",
  "/stream/listeners",
  "/stream/listeners/{ident}",
  "/stream/bans",
  "/stream/stats",
  "/stream/stats/export",
  "/stream/settings",
  "/stream/relays",
  "/stream/relays/{name}",
  "/recording",
  "/recording/start",
  "/recording/stop",
  "/recording/split",
  "/recording/settings",
  "/webhooks",
  "/webhooks/{index}/test",
  "/profiles",
  "/profiles/{name}",
  "/profiles/{name}/apply",
  "/liners",
  "/liners/upload",
  "/liners/file/{name}",
  "/liners/test",
  "/liners/dates/{liner_id}",
  "/library/clock",
  "/library/clock/active",
  "/library/clock/import",
  "/library/clock/check",
  "/library/clock/{name}",
  "/library/clock/{name}/duplicate",
  "/library/clock/{name}/rename",
  "/schedule",
  "/schedule/upcoming",
  "/schedule/{event_id}",
  "/schedule/{event_id}/enable",
  "/schedule/{event_id}/run",
  "/rotation-test",
  "/hour-preview",
  "/intros",
  "/intros/{intro_id}",
  "/intros/{intro_id}/audio",
  "/intros/rules",
  "/intros/rules/{rule_id}",
  "/shows",
  "/shows/on-air",
  "/shows/{show_id}",
  "/shows/{show_id}/songs",
  "/shows/{show_id}/generate",
  "/shows/{show_id}/start",
  "/carts",
  "/carts/pages/{page}/slots/{slot}",
  "/carts/pages/{page}/slots/{slot}/play",
  "/carts/pages/{page}/name",
  "/carts/active-page/{page}",
  "/carts/upload",
  "/carts/uploads/{name}",
  "/carts/stop",
  "/time-checks",
  "/time-checks/test",
  "/time-checks/preview",
  "/time-checks/packs/{name}/dates",
  "/voice-tracks",
  "/voice-tracks/{break_id}",
  "/ads",
  "/ads/spots",
  "/ads/spots/{spot_id}",
  "/ads/spots/{spot_id}/audio",
  "/ads/breaks",
  "/ads/breaks/{break_id}",
  "/ads/breaks/{break_id}/run",
  "/ads/settings",
  "/ads/report",
  "/ads/report/export",
  "/requests",
  "/requests/{request_id}/approve",
  "/requests/{request_id}/play-next",
  "/requests/{request_id}/reject",
  "/landmarks",
  "/requests/settings",
  "/live",
  "/live/settings",
  "/live/djs",
  "/live/djs/{username}",
  "/live/mic",
]);

// The routes are plain letters, hyphens and slashes, so only the {name}
// segments need turning into a pattern.
const ROUTE_PATTERNS = STATION_ROUTES.map(
  (route) => new RegExp(`^${route.replace(/\{[^}]+\}/g, "[^/]+")}$`),
);

let current = DEFAULT_STATION;
// Bumped by every switch: a reply to a request sent before it is for the
// station the page has left.
let epoch = 0;
let known = [];
const listeners = new Set();

function isStationSlug(value) {
  return typeof value === "string" && SLUG_PATTERN.test(value);
}

export function currentStation() {
  return current;
}

export function stationEpoch() {
  return epoch;
}

// Whether *relative* (a path under /api, without a query) acts on one station.
export function isStationRoute(relative) {
  return ROUTE_PATTERNS.some((pattern) => pattern.test(relative));
}

// *url* for station *slug*: a station route under /api/stations/<slug>/,
// and anything else unchanged.  The default station keeps its /api/...
// address, which every script and test already uses.
export function stationApi(url, slug = current) {
  if (typeof url !== "string" || !url.startsWith("/api/") || url.startsWith("/api/stations/")) {
    return url;
  }
  const cut = url.search(/[?#]/);
  const path = cut < 0 ? url : url.slice(0, cut);
  const rest = cut < 0 ? "" : url.slice(cut);
  const relative = path.slice("/api".length);
  if (slug === DEFAULT_STATION || !isStationRoute(relative)) return url;
  return `/api/stations/${encodeURIComponent(slug)}${relative}${rest}`;
}

// *url* for station *slug*, the default one included, even while the page
// controls another (Listen here's reconnect).  While *slug* is the station
// the page controls, its usual address.
export function pinnedStationApi(url, slug) {
  if (slug !== DEFAULT_STATION || slug === current) return stationApi(url, slug);
  const routed = stationApi(url, "x");
  return routed === url ? url : routed.replace("/api/stations/x/", "/api/stations/default/");
}

// Whether a request to *url* acts on one station (so its reply belongs to
// the station that was current when it was sent).
export function isStationUrl(url) {
  return stationApi(url, "x") !== url;
}

export function stationWsPath(slug = current) {
  return slug === DEFAULT_STATION ? "/ws" : `/api/stations/${encodeURIComponent(slug)}/ws`;
}

// The station list from /api/stations or a socket push: [{slug, name, state, listeners, title}].
export function setKnownStations(list) {
  if (!Array.isArray(list)) return;
  known = list.filter((item) => item && isStationSlug(item.slug));
}

export function knownStations() {
  return known.slice();
}

export function stationEntry(slug = current) {
  return known.find((item) => item.slug === slug) || null;
}

export function severalStations() {
  return known.length > 1;
}

export function stationName(slug = current) {
  return stationEntry(slug)?.name || (slug === DEFAULT_STATION ? "AutoDJ" : slug);
}

// Makes *slug* the station this page controls.  Returns whether it changed.
export function selectStation(slug) {
  if (!isStationSlug(slug)) throw new TypeError(`not a station name: ${slug}`);
  if (slug === current) return false;
  const previous = current;
  current = slug;
  epoch += 1;
  for (const listener of [...listeners]) listener(slug, previous);
  return true;
}

export function onStationChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function isOnAir(entry) {
  return Boolean(entry) && entry.state !== "stopped" && entry.state !== "stopping"
    && entry.state !== "stuck stopping";
}

function listenerText(count) {
  const n = Number.isFinite(count) ? count : 0;
  return `${n} listener${n === 1 ? "" : "s"}`;
}

// How a station reads in the menu: "Night Owl Radio, on air, 3 listeners".
export function stationItemText(entry) {
  if (!isOnAir(entry)) {
    return `${entry.name}, ${entry.state === "stopped" ? "off air" : "stopping"}`;
  }
  return `${entry.name}, on air, ${listenerText(entry.listeners)}`;
}

// What the page says after a switch or on Shift+C.
export function controllingText(entry, nowPlaying = null) {
  if (!entry) return `Now controlling ${stationName()}.`;
  const parts = [`Now controlling ${entry.name}.`];
  if (!isOnAir(entry)) {
    parts.push("Off air.");
  } else {
    parts.push(`On air, ${listenerText(entry.listeners)}.`);
    const title = nowPlaying || entry.title;
    parts.push(title ? `Playing ${title}.` : "Nothing playing.");
  }
  return parts.join(" ");
}

// The ?station= in the page address, when it names a configured station.
export function stationFromAddress(search) {
  const value = new URLSearchParams(search || "").get("station");
  return isStationSlug(value) ? value : null;
}

// Keeps ?station=<slug> in the address bar without a history entry, so a
// reload opens the same station.  The default station has none.
export function rememberStationInAddress(slug, { location: loc = globalThis.location,
  history: hist = globalThis.history } = {}) {
  if (!loc || !hist?.replaceState) return;
  const url = new URL(loc.href);
  if (slug === DEFAULT_STATION) url.searchParams.delete("station");
  else url.searchParams.set("station", slug);
  if (url.href !== loc.href) hist.replaceState(hist.state, "", url.href);
}

// ----------------------------------------------------------------
// The switcher: APG menu button, menuitemradio items.
//
//   Button: Enter, Space or Down Arrow opens on the checked item; Up Arrow
//   opens on the last item.
//   Menu: Up and Down Arrow move (wrapping), Home and End go to the ends,
//   a letter moves to the next station starting with it.  Enter or Space
//   chooses and closes; Escape closes; both put focus on the button.  Tab
//   closes and lets focus move on.  A click outside closes it.
//
// The button's name is always "Station: <name>", so the current station
// is heard on every visit and nothing has to be announced while moving.
// ----------------------------------------------------------------

export function installStationSwitcher({ wrap, button, menu, onChoose }) {
  if (!wrap || !button || !menu) return null;
  const doc = button.ownerDocument;
  let items = [];
  let typed = "";
  let typedAt = 0;

  function isOpen() {
    return !menu.hidden;
  }

  function render() {
    const list = knownStations();
    wrap.hidden = list.length < 2;
    // The button's name comes from its text, "Station: <name>", so what
    // is seen is what is heard (WCAG 2.5.3).
    const label = button.querySelector(".station-switcher-name");
    const name = stationName();
    if (label && label.textContent !== name) label.textContent = name;
    const focusedSlug = isOpen() ? doc.activeElement?.dataset?.station : null;
    items = list.map((station) => {
      let item = menu.querySelector(`[data-station="${station.slug}"]`);
      if (!item) {
        item = doc.createElement("li");
        item.setAttribute("role", "menuitemradio");
        item.tabIndex = -1;
        item.dataset.station = station.slug;
        item.className = "station-switcher-item";
      }
      const text = stationItemText(station);
      if (item.textContent !== text) item.textContent = text;
      const checked = station.slug === currentStation() ? "true" : "false";
      if (item.getAttribute("aria-checked") !== checked) item.setAttribute("aria-checked", checked);
      return item;
    });
    const stale = [...menu.children].filter((child) => !items.includes(child));
    for (const child of stale) child.remove();
    items.forEach((item, index) => {
      if (menu.children[index] !== item) menu.insertBefore(item, menu.children[index] || null);
    });
    if (isOpen() && focusedSlug && !items.some((item) => item.dataset.station === focusedSlug)) {
      focusItem(checkedIndex());
    }
    if (wrap.hidden && isOpen()) close(false);
  }

  function checkedIndex() {
    const index = items.findIndex((item) => item.getAttribute("aria-checked") === "true");
    return index < 0 ? 0 : index;
  }

  function focusItem(index) {
    if (!items.length) return;
    const wrapped = (index + items.length) % items.length;
    items[wrapped].focus();
  }

  function open(index) {
    if (wrap.hidden || !items.length) return;
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    focusItem(index);
  }

  function close(returnFocus = true) {
    if (!isOpen()) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    typed = "";
    if (returnFocus) button.focus();
  }

  function choose(item) {
    const slug = item?.dataset?.station;
    close(true);
    if (slug) onChoose(slug);
  }

  // A letter moves to the next station whose name starts with it; more
  // letters typed quickly spell out a longer start, from the item focused.
  function typeAhead(key) {
    const now = Date.now();
    typed = now - typedAt > 700 ? key : typed + key;
    typedAt = now;
    const at = items.indexOf(doc.activeElement);
    const from = typed.length > 1 ? at : at + 1;
    const order = [...items.slice(from), ...items.slice(0, from)];
    const match = order.find((item) =>
      stationName(item.dataset.station).toLowerCase().startsWith(typed));
    if (match) match.focus();
  }

  button.addEventListener("click", () => {
    if (isOpen()) close(true);
    else open(checkedIndex());
  });
  button.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (["Enter", " ", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      open(checkedIndex());
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      open(items.length - 1);
    }
  });
  menu.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const index = items.indexOf(doc.activeElement);
    switch (event.key) {
      case "ArrowDown":
        focusItem(index + 1);
        break;
      case "ArrowUp":
        focusItem(index - 1);
        break;
      case "Home":
        focusItem(0);
        break;
      case "End":
        focusItem(items.length - 1);
        break;
      case "Enter":
      case " ":
        choose(doc.activeElement);
        break;
      case "Escape":
        close(true);
        break;
      case "Tab":
        close(false);
        return;
      default:
        if (event.key.length === 1 && /\S/.test(event.key)) {
          typeAhead(event.key.toLowerCase());
          break;
        }
        return;
    }
    event.preventDefault();
    event.stopPropagation();
  });
  menu.addEventListener("click", (event) => {
    const item = event.target.closest?.('[role="menuitemradio"]');
    if (item && menu.contains(item)) choose(item);
  });
  doc.addEventListener("pointerdown", (event) => {
    if (isOpen() && !wrap.contains(event.target)) close(false);
  }, true);
  menu.addEventListener("focusout", (event) => {
    if (isOpen() && event.relatedTarget && !wrap.contains(event.relatedTarget)) close(false);
  });

  return { render, close, isOpen, focus: () => button.focus() };
}
