import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  makeSingleFlight,
  isStaleStationReply,
  requestJson,
  setAuthRequiredHandler,
  staleActionText,
} from "../../src/autodj/static/modules/api-client.js";
import {
  DEFAULT_STATION,
  STATION_ROUTES,
  controllingText,
  currentStation,
  installStationSwitcher,
  isStationRoute,
  onStationChange,
  pinnedStationApi,
  rememberStationInAddress,
  selectStation,
  setKnownStations,
  severalStations,
  stationApi,
  stationEpoch,
  stationFromAddress,
  stationItemText,
  stationName,
  stationWsPath,
} from "../../src/autodj/static/modules/stations.js";

const AUTODJ = { slug: "default", name: "AutoDJ", state: "playing", listeners: 3, title: "Artist - Song" };
const OWL = { slug: "nightowl", name: "Night Owl Radio", state: "idle", listeners: 1, title: null };
const OLDIES = { slug: "oldies", name: "Oldies", state: "stopped", listeners: 0, title: null };

function jsonResponse(body, init = {}) {
  return new globalThis.Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "Content-Type": "application/json" },
  });
}

// Back to the default station between tests: the module keeps one page's state.
function reset() {
  setKnownStations([AUTODJ, OWL, OLDIES]);
  if (currentStation() !== DEFAULT_STATION) selectStation(DEFAULT_STATION);
}

describe("where a request goes", () => {
  beforeEach(reset);

  it("leaves every address alone for the default station", () => {
    expect(stationApi("/api/skip")).toBe("/api/skip");
    expect(stationApi("/api/history?page=2&per_page=50")).toBe("/api/history?page=2&per_page=50");
    expect(stationWsPath()).toBe("/ws");
  });

  it("puts a station route under the station's name, keeping the query", () => {
    selectStation("nightowl");
    expect(stationApi("/api/skip")).toBe("/api/stations/nightowl/skip");
    expect(stationApi("/api/history?page=2&per_page=50"))
      .toBe("/api/stations/nightowl/history?page=2&per_page=50");
    expect(stationApi("/api/carts/pages/2/slots/7/play"))
      .toBe("/api/stations/nightowl/carts/pages/2/slots/7/play");
    expect(stationApi("/api/library/clock/Morning%20drive/rename"))
      .toBe("/api/stations/nightowl/library/clock/Morning%20drive/rename");
    expect(stationApi("/api/library/clock/check"))
      .toBe("/api/stations/nightowl/library/clock/check");
    expect(stationApi("/api/stream/listeners/abc")).toBe("/api/stations/nightowl/stream/listeners/abc");
    expect(stationApi("/api/stream/relays")).toBe("/api/stations/nightowl/stream/relays");
    expect(stationApi("/api/stream/relays/backup")).toBe("/api/stations/nightowl/stream/relays/backup");
    expect(stationApi("/api/liners/upload?replace=true"))
      .toBe("/api/stations/nightowl/liners/upload?replace=true");
    expect(stationApi("/api/editions/edition/takes/take/reattach"))
      .toBe("/api/stations/nightowl/editions/edition/takes/take/reattach");
    expect(stationWsPath()).toBe("/api/stations/nightowl/ws");
  });

  it("leaves shared routes and named-station routes alone", () => {
    selectStation("nightowl");
    for (const url of [
      "/api/search?q=x", "/api/audio?path=a.mp3", "/api/art?path=a", "/api/library/tracks",
      "/api/library/folders", "/api/library/points", "/api/stations",
      "/api/stations/oldies/start", "/api/stereo-tool-presets/upload", "/api/devices", "/api/version",
      "/stream.mp3", "/api/library/clockwork",
    ]) {
      expect(stationApi(url)).toBe(url);
    }
    // A path that only starts like a station route is not one.
    expect(isStationRoute("/skip/extra")).toBe(false);
    expect(isStationRoute("/library/clock/a/b/c")).toBe(false);
  });

  it("pins a request to a station, the default one too, whatever is current", () => {
    selectStation("nightowl");
    expect(pinnedStationApi("/api/stream", DEFAULT_STATION)).toBe("/api/stations/default/stream");
    expect(pinnedStationApi("/api/stream", "nightowl")).toBe("/api/stations/nightowl/stream");
    expect(pinnedStationApi("/api/search?q=a", DEFAULT_STATION)).toBe("/api/search?q=a");
  });

  it("can name a station other than the current one", () => {
    expect(stationApi("/api/stream", "nightowl")).toBe("/api/stations/nightowl/stream");
    selectStation("nightowl");
    expect(stationApi("/api/stream", DEFAULT_STATION)).toBe("/api/stream");
  });

  it("lists only routes the server has, with no socket or machine routes", () => {
    expect(new Set(STATION_ROUTES).size).toBe(STATION_ROUTES.length);
    expect(STATION_ROUTES).toContain("/settings");
    expect(STATION_ROUTES).not.toContain("/ws");
    expect(STATION_ROUTES.some((route) => route.startsWith("/announcer"))).toBe(false);
  });
});

describe("the current station", () => {
  beforeEach(reset);

  it("bumps the epoch and tells listeners on a change only", () => {
    const seen = [];
    const stop = onStationChange((slug, previous) => seen.push([slug, previous]));
    const before = stationEpoch();
    expect(selectStation("nightowl")).toBe(true);
    expect(selectStation("nightowl")).toBe(false);
    expect(stationEpoch()).toBe(before + 1);
    expect(seen).toEqual([["nightowl", "default"]]);
    stop();
    expect(() => selectStation("../x")).toThrow(TypeError);
  });

  it("names the station in the switcher, the description and the announcement", () => {
    expect(stationItemText(AUTODJ)).toBe("AutoDJ, on air, 3 listeners");
    expect(stationItemText(OWL)).toBe("Night Owl Radio, on air, 1 listener");
    expect(stationItemText(OLDIES)).toBe("Oldies, off air");
    expect(controllingText(AUTODJ)).toBe("AutoDJ. Playing Artist - Song. On air, 3 listeners.");
    expect(controllingText(OWL, "Band - Tune"))
      .toBe("Night Owl Radio. Playing Band - Tune. On air, 1 listener.");
    expect(controllingText(OWL)).toBe("Night Owl Radio. Nothing playing. On air, 1 listener.");
    expect(controllingText(OLDIES)).toBe("Oldies. Off air.");
    expect(controllingText({ ...AUTODJ, listeners: 0 }))
      .toBe("AutoDJ. Playing Artist - Song. On air, 0 listeners.");
    expect(controllingText(null)).toBe("AutoDJ.");
    setKnownStations([AUTODJ]);
    expect(severalStations()).toBe(false);
  });

  it("reads and keeps ?station= in the address without a history entry", () => {
    expect(stationFromAddress("?station=nightowl&debug=1")).toBe("nightowl");
    expect(stationFromAddress("?station=Night%20Owl")).toBeNull();
    expect(stationFromAddress("")).toBeNull();
    const history = { state: { keep: 1 }, replaceState: vi.fn() };
    rememberStationInAddress("nightowl", {
      location: { href: "https://radio.example/?debug=1#queue" }, history,
    });
    expect(history.replaceState).toHaveBeenCalledWith(
      { keep: 1 }, "", "https://radio.example/?debug=1&station=nightowl#queue");
    history.replaceState.mockClear();
    rememberStationInAddress(DEFAULT_STATION, {
      location: { href: "https://radio.example/?station=nightowl" }, history,
    });
    expect(history.replaceState).toHaveBeenCalledWith({ keep: 1 }, "", "https://radio.example/");
  });
});

describe("requests while switching", () => {
  beforeEach(() => {
    reset();
    setAuthRequiredHandler(() => {});
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sends station routes to the station the page controls", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);
    selectStation("nightowl");
    await requestJson("/api/skip", { method: "POST" });
    await requestJson("/api/search?q=a");
    expect(fetchMock.mock.calls.map(([url]) => url))
      .toEqual(["/api/stations/nightowl/skip", "/api/search?q=a"]);
  });

  it("drops a station reply that comes back after a switch, but not a shared one", async () => {
    let answer;
    const fetchMock = vi.fn(() => new Promise((resolve) => { answer = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const queue = requestJson("/api/status");
    selectStation("nightowl");
    answer(jsonResponse({ queue: ["old station"] }));
    const failure = await queue.catch((error) => error);
    expect(isStaleStationReply(failure)).toBe(true);
    expect(failure.message).toMatch(/station this page has just left/);

    const shared = requestJson("/api/search?q=a");
    selectStation(DEFAULT_STATION);
    answer(jsonResponse({ results: [] }));
    await expect(shared).resolves.toEqual({ results: [] });
  });

  it("says what an action answered after a switch did on the station left", async () => {
    let answer;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { answer = resolve; })));
    const done = requestJson("/api/queue/remove", { method: "POST" });
    selectStation("nightowl");
    answer(jsonResponse({ ok: true }));
    const doneFailure = await done.catch((error) => error);
    expect(isStaleStationReply(doneFailure)).toBe(true);
    expect(staleActionText(doneFailure))
      .toBe(`Done on ${AUTODJ.name}, the station this page has just left.`);

    const refused = requestJson("/api/skip", { method: "POST" });
    selectStation(DEFAULT_STATION);
    answer(jsonResponse({ detail: "No" }, { status: 409 }));
    expect(staleActionText(await refused.catch((error) => error)))
      .toBe(`${OWL.name} did not do that; it is the station this page has just left.`);

    // A 200 whose body says ok: false was refused, as checkedResponse reads it.
    const notOk = requestJson("/api/skip", { method: "POST" });
    selectStation("nightowl");
    answer(jsonResponse({ ok: false, detail: "Nothing to skip" }));
    const notOkFailure = await notOk.catch((error) => error);
    expect(notOkFailure.actionDone).toBe(false);
    expect(staleActionText(notOkFailure))
      .toBe(`${AUTODJ.name} did not do that; it is the station this page has just left.`);
    const notSuccess = requestJson("/api/skip", { method: "POST" });
    selectStation(DEFAULT_STATION);
    answer(jsonResponse({ success: false }));
    expect(staleActionText(await notSuccess.catch((error) => error)))
      .toBe(`${OWL.name} did not do that; it is the station this page has just left.`);

    // A server error leaves it unknown.
    const crashed = requestJson("/api/skip", { method: "POST" });
    selectStation("nightowl");
    answer(jsonResponse({ detail: "boom" }, { status: 500 }));
    expect(staleActionText(await crashed.catch((error) => error)))
      .toBe(`${AUTODJ.name} may not have done that; it is the station this page has just left.`);
    selectStation(DEFAULT_STATION);

    // A load, not an action: nothing was done, so there is nothing to say.
    const load = requestJson("/api/status");
    selectStation("nightowl");
    answer(jsonResponse({ queue: [] }));
    expect(staleActionText(await load.catch((error) => error))).toBeNull();
    expect(staleActionText(new Error("boom"))).toBeNull();
  });

  it("never joins a request made for the station before", async () => {
    let calls = 0;
    const load = makeSingleFlight(async () => {
      calls += 1;
      await Promise.resolve();
      return calls;
    });
    const first = load();
    selectStation("nightowl");
    const second = load();
    expect(second).not.toBe(first);
    expect(load()).toBe(second);
    await Promise.all([first, second]);
    expect(calls).toBe(2);
  });
});

describe("the station switcher (a disclosure, #576)", () => {
  let chosen;
  let switcher;

  function setUp(list = [AUTODJ, OWL, OLDIES]) {
    document.body.innerHTML = `
      <button id="before">Before</button>
      <div id="wrap" hidden>
        <button type="button" id="station-button" aria-expanded="false"
          aria-controls="station-list"><span>Station:</span> <span class="station-switcher-name"></span></button>
        <ul id="station-list" hidden></ul>
      </div>
      <button id="after">After</button>`;
    setKnownStations(list);
    chosen = [];
    switcher = installStationSwitcher({
      wrap: document.getElementById("wrap"),
      button: document.getElementById("station-button"),
      list: document.getElementById("station-list"),
      onChoose: (slug) => chosen.push(slug),
    });
    switcher.render();
  }

  const button = () => document.getElementById("station-button");
  const list = () => document.getElementById("station-list");
  const items = () => [...list().querySelectorAll("button")];
  const key = (target, name, init = {}) => {
    const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...init });
    target.dispatchEvent(event);
    return event;
  };
  const open = () => {
    button().focus();
    button().click();
  };

  beforeEach(() => {
    reset();
    setUp();
  });

  it("is hidden with one station and names the current one in the button", () => {
    expect(document.getElementById("wrap").hidden).toBe(false);
    expect(button().textContent.replace(/\s+/g, " ").trim()).toBe("Station: AutoDJ");
    setUp([AUTODJ]);
    expect(document.getElementById("wrap").hidden).toBe(true);
  });

  it("lists every station as a plain button in a list item, the current one aria-current", () => {
    expect(list().getAttribute("role")).toBeNull();
    expect([...list().children].map((child) => child.tagName)).toEqual(["LI", "LI", "LI"]);
    expect(items().map((item) => item.textContent)).toEqual([
      "AutoDJ, on air, 3 listeners", "Night Owl Radio, on air, 1 listener", "Oldies, off air",
    ]);
    expect(items().map((item) => item.getAttribute("aria-current"))).toEqual(["true", null, null]);
    for (const item of items()) {
      expect(item.getAttribute("type")).toBe("button");
      expect(item.hasAttribute("role")).toBe(false);
      expect(item.hasAttribute("aria-pressed")).toBe(false);
      expect(item.hasAttribute("aria-checked")).toBe(false);
      // In the tab order, like any button.
      expect(item.tabIndex).toBe(0);
    }
    expect(document.querySelector('[role="menu"], [role="menuitemradio"], [aria-haspopup]')).toBeNull();
  });

  it("opens on the current station, setting aria-expanded before focus moves", () => {
    selectStation("nightowl");
    switcher.render();
    let expandedOnFocus = null;
    list().addEventListener("focusin", () => {
      expandedOnFocus = button().getAttribute("aria-expanded");
    });
    open();
    expect(list().hidden).toBe(false);
    expect(button().getAttribute("aria-expanded")).toBe("true");
    expect(expandedOnFocus).toBe("true");
    expect(document.activeElement).toBe(items()[1]);
    expect(chosen).toEqual([]);
  });

  it("moves with Up and Down (wrapping), Home and End, without choosing", () => {
    open();
    expect(key(items()[0], "ArrowUp").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(items()[2]);
    key(items()[2], "ArrowDown");
    expect(document.activeElement).toBe(items()[0]);
    key(items()[0], "ArrowDown");
    expect(document.activeElement).toBe(items()[1]);
    key(items()[1], "End");
    expect(document.activeElement).toBe(items()[2]);
    key(items()[2], "Home");
    expect(document.activeElement).toBe(items()[0]);
    expect(list().hidden).toBe(false);
    expect(chosen).toEqual([]);
  });

  it("has no first-letter jump: letters and Tab are left to the page", () => {
    open();
    for (const name of ["n", "o", "Tab"]) {
      const event = key(items()[0], name);
      expect(event.defaultPrevented, name).toBe(false);
      expect(document.activeElement, name).toBe(items()[0]);
    }
    const shifted = key(items()[0], "Tab", { shiftKey: true });
    expect(shifted.defaultPrevented).toBe(false);
    expect(list().hidden).toBe(false);
  });

  it("chooses with a press: switches, closes and puts focus on the button", () => {
    open();
    key(items()[0], "ArrowDown");
    document.activeElement.click();
    expect(chosen).toEqual(["nightowl"]);
    expect(list().hidden).toBe(true);
    expect(button().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button());
  });

  it("only closes when the current station is chosen", () => {
    open();
    items()[0].click();
    expect(chosen).toEqual([]);
    expect(list().hidden).toBe(true);
    expect(button().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button());
  });

  it("closes on Escape with focus back on the button, from a station or the button", () => {
    open();
    key(items()[0], "ArrowDown");
    const escape = key(items()[1], "Escape");
    expect(escape.defaultPrevented).toBe(true);
    expect(list().hidden).toBe(true);
    expect(button().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button());
    open();
    button().focus();
    key(button(), "Escape");
    expect(list().hidden).toBe(true);
    expect(document.activeElement).toBe(button());
    // Closed, Escape is left to the page.
    expect(key(button(), "Escape").defaultPrevented).toBe(false);
    expect(chosen).toEqual([]);
  });

  it("closes when the button is pressed again, and on nothing else", () => {
    open();
    button().click();
    expect(list().hidden).toBe(true);
    expect(button().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button());
    open();
    // Focus leaving and a click elsewhere leave it open.
    const after = document.getElementById("after");
    after.focus();
    items()[1].dispatchEvent(new window.FocusEvent("focusout", { bubbles: true, relatedTarget: after }));
    after.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    after.click();
    document.getElementById("before").click();
    expect(list().hidden).toBe(false);
    expect(button().getAttribute("aria-expanded")).toBe("true");
    expect(chosen).toEqual([]);
  });

  it("updates each station in place while open, keeping focus", () => {
    open();
    key(items()[0], "ArrowDown");
    const owl = items()[1];
    setKnownStations([AUTODJ, { ...OWL, listeners: 5 }, { ...OLDIES, state: "playing", listeners: 2 }]);
    switcher.render();
    expect(items()[1]).toBe(owl);
    expect(document.activeElement).toBe(owl);
    expect(owl.textContent).toBe("Night Owl Radio, on air, 5 listeners");
    expect(items()[2].textContent).toBe("Oldies, on air, 2 listeners");
    selectStation("nightowl");
    switcher.render();
    expect(document.activeElement).toBe(owl);
    expect(button().textContent).toContain("Night Owl Radio");
    expect(items().map((item) => item.getAttribute("aria-current"))).toEqual([null, "true", null]);
  });

  it("moves focus to the current station when the focused one leaves the list", () => {
    open();
    key(items()[0], "End");
    setKnownStations([AUTODJ, OWL]);
    switcher.render();
    expect(items()).toHaveLength(2);
    expect(document.activeElement).toBe(items()[0]);
  });

  it("closes when the server is down to one station", () => {
    open();
    setKnownStations([AUTODJ]);
    switcher.render();
    expect(document.getElementById("wrap").hidden).toBe(true);
    expect(list().hidden).toBe(true);
    expect(button().getAttribute("aria-expanded")).toBe("false");
  });

  it("names a station not yet listed by its slug", () => {
    setKnownStations([]);
    expect(stationName("nightowl")).toBe("nightowl");
    expect(stationName(DEFAULT_STATION)).toBe("AutoDJ");
  });
});
