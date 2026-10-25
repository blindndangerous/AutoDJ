// Several stations on one server (#70): which station this page controls,
// where each request for it goes, and the station switcher.
//
// Every route that acts on one station answers at /api/<route> for the
// default station and at /api/stations/<slug>/<route> for any station.
// The page keeps writing /api/<route>; api-client.js sends each request
// through stationApi(), which puts a station route under the station
// this page controls.  Shared routes stay shared; Track info adds the
// selected station as query context for its station-specific play time.
// STATION_ROUTES must list exactly
// the station router's routes: tests/integration/test_station_routes.py
// compares the two, so a new station route cannot be forgotten here.
//
// The switcher is a disclosure in the banner (#576): the Station button
// shows or hides a list of station buttons right after it.  A native
// <select> was not used: Chrome and Edge on Windows fire change on every
// arrow key while it is closed, and each change would tear down the
// socket and reload every panel.  An APG menu button was used before, but
// NVDA raced its popup: it announced the menu container and sometimes
// swallowed the first Escape (#571).  A disclosure has no popup role, so
// there is no popup event to race.  A switch happens only when a station
// button is pressed, and the Station button's name always says which
// station the page controls.

export const DEFAULT_STATION = "default";
const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;

// Relative to /api; {name} is one path segment.  The socket (/ws) and the
// announcer's machine routes are not requested by this page.
export const STATION_ROUTES = Object.freeze([
  "/speech",
  "/speech/preflight",
  "/speech/batches",
  "/speech/batches/{token}",
  "/speech/batches/{token}/retry",
  "/speech/batches/{token}/save",
  "/speech/batches/{token}/samples/{index}",
  "/speech/cancel",
  "/speech/saved/{name}/publish",
  "/speech/saved/{name}/audio",
  "/speech/presets/{name}",
  "/speech/default",
  "/speech/time-inventory",
  "/speech/time-sets",
  "/speech/time-sets/{name}/samples/{minute}",
  "/speech/active-set",
  "/status",
  "/settings",
  "/stereo-tool-presets",
  "/time-settings",
  "/station-settings",
  "/public-settings",
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
  "/discovery/toggle",
  "/eq",
  "/stream",
  "/stream/listeners",
  "/stream/listeners/{ident}",
  "/stream/bans",
  "/stream/stats",
  "/stream/stats/export",
  "/stream/relays",
  "/stream/relays/{name}",
  "/recording",
  "/recording/start",
  "/recording/stop",
  "/recording/split",
  "/webhooks",
  "/webhooks/{index}/test",
  "/profiles",
  "/profiles/{name}",
  "/categories",
  "/categories/check",
  "/categories/{category_id}",
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
  "/reviews",
  "/reviews/{review_id}",
  "/reviews/{review_id}/audio/{format}",
  "/reviews/jobs/{job_id}",
  "/reviews/jobs/{job_id}/cancel",
  "/editions",
  "/editions/{edition_id}",
  "/editions/{edition_id}/takes",
  "/editions/{edition_id}/assets/{asset_id}",
  "/editions/{edition_id}/takes/{take_id}/approve",
  "/editions/{edition_id}/takes/{take_id}/reattach",
  "/editions/{edition_id}/takes/{take_id}",
  "/editions/{edition_id}/start",
  "/intros",
  "/intros/{intro_id}",
  "/intros/{intro_id}/audio",
  "/intros/rules",
  "/intros/rules/test",
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
  "/time-checks/packs",
  "/time-checks/packs/{name}",
  "/time-checks/clips",
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
  "/breaks/settings",
  "/live",
  "/live/djs",
  "/live/djs/{username}",
  "/live/mic",
  "/stereo-tool",
  "/stereo-tool/license",
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

// How a station reads in the switcher: "Night Owl Radio, on air, 3 listeners".
export function stationItemText(entry) {
  if (!isOnAir(entry)) {
    return `${entry.name}, ${entry.state === "stopped" ? "off air" : "stopping"}`;
  }
  return `${entry.name}, on air, ${listenerText(entry.listeners)}`;
}

// What the page says after a switch or on Shift+C.
export function controllingText(entry, nowPlaying = null) {
  if (!entry) return `${stationName()}.`;
  const parts = [`${entry.name}.`];
  if (!isOnAir(entry)) {
    parts.push("Off air.");
  } else {
    const title = nowPlaying || entry.title;
    parts.push(title ? `Playing ${title}.` : "Nothing playing.");
    parts.push(`On air, ${listenerText(entry.listeners)}.`);
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
// The switcher: a disclosure button and a list of station buttons.
//
//   Station button: Enter, Space or a click shows the list and puts focus
//   on the current station; pressed again, it hides the list.
//   List: Tab and Shift+Tab move as on any buttons; Up and Down Arrow move
//   between the stations (wrapping), Home and End go to the ends.  Enter,
//   Space or a click chooses.  Escape hides the list.  Both put focus on
//   the Station button.  Nothing else hides it: not focus leaving, not a
//   click elsewhere.  Letters are the page's shortcuts, as on any button.
//
// The Station button's name is always "Station: <name>", so the current
// station is heard on every visit and nothing has to be announced while
// moving.  The current station's button has aria-current="true".
// ----------------------------------------------------------------

// How long the list stays shown, collapsed, before focus goes back to the
// Station button.  NVDA with Firefox reads the old state if focus moves
// sooner: after one animation frame it still said "expanded" on the
// collapsed button, and after 60 ms it said "collapsed" (#576).
export const CLOSE_FOCUS_DELAY_MS = 100;

export function installStationSwitcher({ wrap, button, list, onChoose }) {
  if (!wrap || !button || !list) return null;
  const doc = button.ownerDocument;
  let items = [];
  let expanded = false;
  let closeTimer = 0;
  // The station chosen while its switch is under way: the list stays open,
  // with focus on it, until the switch has renamed the Station button.
  let choosing = null;

  function isOpen() {
    return expanded;
  }

  // Each station is an <li> holding its <button>, kept and updated in
  // place, so a push while the list is open never moves focus.  Each
  // button has its own click listener: one on the list made NVDA call the
  // list "clickable".
  function render() {
    const stations = knownStations();
    wrap.hidden = stations.length < 2;
    // The button's name comes from its text, "Station: <name>", so what
    // is seen is what is heard (WCAG 2.5.3).
    const label = button.querySelector(".station-switcher-name");
    const name = stationName();
    if (label && label.textContent !== name) label.textContent = name;
    const focusedSlug = isOpen() && list.contains(doc.activeElement)
      ? doc.activeElement.dataset.station : null;
    items = stations.map((station) => {
      let item = list.querySelector(`button[data-station="${station.slug}"]`);
      if (!item) {
        item = doc.createElement("button");
        item.type = "button";
        item.dataset.station = station.slug;
        item.className = "station-switcher-item";
        const chosen = item;
        item.addEventListener("click", () => choose(chosen));
        const row = doc.createElement("li");
        row.append(item);
      }
      const text = stationItemText(station);
      if (item.textContent !== text) item.textContent = text;
      if (station.slug === currentStation()) {
        if (item.getAttribute("aria-current") !== "true") item.setAttribute("aria-current", "true");
      } else if (item.hasAttribute("aria-current")) {
        item.removeAttribute("aria-current");
      }
      return item;
    });
    const rows = items.map((item) => item.parentElement);
    const stale = [...list.children].filter((child) => !rows.includes(child));
    for (const child of stale) child.remove();
    rows.forEach((row, index) => {
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    if (wrap.hidden) {
      close(false);
      return;
    }
    // The switch has happened, and the Station button already has its
    // new name: now the list closes.
    if (choosing && choosing === currentStation()) {
      close(true);
      return;
    }
    if (isOpen() && focusedSlug && !items.some((item) => item.dataset.station === focusedSlug)) {
      focusItem(currentIndex());
    }
  }

  function currentIndex() {
    const index = items.findIndex((item) => item.getAttribute("aria-current") === "true");
    return index < 0 ? 0 : index;
  }

  function focusItem(index) {
    if (!items.length) return;
    const wrapped = (index + items.length) % items.length;
    items[wrapped].focus();
  }

  function cancelPendingClose() {
    if (closeTimer) globalThis.clearTimeout(closeTimer);
    closeTimer = 0;
  }

  // The list shows and aria-expanded changes before focus moves, all in
  // one task, with no delay: there is no popup for NVDA to race.  Pressed
  // while a close is pending, the Station button keeps the list open.
  function open() {
    if (wrap.hidden || !items.length) return;
    cancelPendingClose();
    expanded = true;
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
    focusItem(currentIndex());
  }

  // Collapsed at once, with the list still shown; CLOSE_FOCUS_DELAY_MS
  // later, focus goes to the Station button and then the list hides, in
  // the same task.  Moved sooner, focus reached NVDA before the collapsed
  // state did.  Hidden first, the list dropped focus on the page body for
  // a moment, and after a switch's title change NVDA read the whole
  // document again.  A second Escape or choice while the close is pending
  // does nothing, as the list is already collapsed.
  function close(returnFocus = true) {
    choosing = null;
    if (!expanded) return;
    expanded = false;
    button.setAttribute("aria-expanded", "false");
    cancelPendingClose();
    if (!returnFocus) {
      list.hidden = true;
      return;
    }
    closeTimer = globalThis.setTimeout(() => {
      closeTimer = 0;
      if (expanded) return;
      const active = doc.activeElement;
      const returning = !active || active === doc.body || active === button || list.contains(active);
      if (returning && !doc.querySelector("dialog[open]")) button.focus();
      list.hidden = true;
    }, CLOSE_FOCUS_DELAY_MS);
  }

  // The current station only closes the list.  Another one starts the
  // switch, and the list closes once the switch has renamed the Station
  // button (in render).  A switch that does not happen, such as one
  // cancelled in a confirmation, leaves the list open on that station.
  function choose(item) {
    const slug = item?.dataset?.station;
    if (!slug || !expanded || choosing) return;
    if (slug === currentStation()) {
      close(true);
      return;
    }
    choosing = slug;
    let switching = null;
    try {
      switching = onChoose(slug);
    } catch (_) {
      switching = null;
    }
    Promise.resolve(switching).catch(() => {}).then(() => {
      if (choosing === slug) choosing = null;
    });
  }

  button.addEventListener("click", () => {
    if (isOpen()) close(true);
    else open();
  });
  wrap.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !isOpen()) return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    event.stopPropagation();
    close(true);
  });
  list.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const index = items.indexOf(doc.activeElement);
    if (index < 0) return;
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
      default:
        return;
    }
    event.preventDefault();
    event.stopPropagation();
  });

  return { render, close, isOpen, focus: () => button.focus() };
}
