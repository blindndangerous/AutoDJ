import { afterEach, expect, it, vi } from "vitest";
import { renderLinerFileList } from "../../src/autodj/static/modules/liners.js";
import { installProfiles } from "../../src/autodj/static/modules/profiles.js";
import { installAccess } from "../../src/autodj/static/modules/devices.js";

afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });
const json = (body) => new globalThis.Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
const press = (key) => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));

// ADR-0021-31 ADR-0021-39
it("keeps liner rows keyed with dates and availability in Details and Options return focus", () => {
  document.body.innerHTML = '<div id="files"></div>';
  const host = document.getElementById("files");
  const liners = [
    { id: "a", name: "First.wav", path: "/first.wav", dates_text: "Any day", playable: true },
    { id: "b", name: "Second.wav", path: "/second.wav", dates_text: "Until tomorrow", playable: false, unplayable_text: "Not before tomorrow" },
  ];
  renderLinerFileList(host, liners, vi.fn());
  const row = host.querySelector('tr[data-row-key="b"]');
  expect(row).not.toBeNull();
  expect([...row.cells].slice(0, 2).map((cell) => cell.textContent)).toEqual(["Second.wav", "Until tomorrow. Skipped now: Not before tomorrow."]);
  expect(row.querySelectorAll('button:not([role="menuitem"])')).toHaveLength(1);
  expect([...row.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent)).toEqual(["Play dates", "Track info", "Delete"]);
  const options = row.querySelector('[aria-haspopup="menu"]');
  options.click();
  press("Escape");
  expect(document.activeElement).toBe(options.closest("td"));
  renderLinerFileList(host, [{ ...liners[1], playable: true }, liners[0]], vi.fn());
  expect(host.querySelector('tr[data-row-key="b"]')).toBe(row);
  expect(document.activeElement).toBe(options.closest("td"));
  expect(host.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
});

// ADR-0021-32
it("shows profile active state in its own cell and keeps reset and deletion in Options", async () => {
  document.body.innerHTML = '<details id="card"><summary>Profiles</summary></details><input id="name"><p id="profile-name-error" hidden></p><button id="save">Create profile</button><div id="list"></div><p id="status"></p>';
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json({ profiles: ["Morning"], active: "Morning" }))));
  const $ = (id) => document.getElementById(id);
  const controls = installProfiles({ card: $("card"), nameInput: $("name"), saveButton: $("save"), list: $("list"), status: $("status") });
  await controls.refresh();
  const row = $("list").querySelector('tbody tr[data-row-key]');
  expect(row).not.toBeNull();
  expect([...row.cells].slice(0, 2).map((cell) => cell.textContent)).toEqual(["Morning", "Yes"]);
  expect(row.querySelector('button[data-action="activate"]').getAttribute("aria-disabled")).toBe("false");
  row.querySelector('[aria-haspopup="menu"]').click();
  expect([...row.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent)).toEqual(["Activate", "Reset", "Delete"]);
  press("Escape");
  expect(document.activeElement.dataset.action).toBe("options");
  controls.clear();
  expect($("list").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(0);
  await controls.refresh();
  expect($("list").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(1);
});

// ADR-0021-33
it("shows paired device data in columns with Rename and a revocation menu", async () => {
  document.body.innerHTML = '<details id="card"><summary>Access</summary></details><p id="note"></p><div id="controls"></div><button id="out">Sign out</button><button id="refresh">Refresh</button><div id="list"></div><p id="status"></p>';
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json({ pairing: true, devices: [{ device_id: "a", name: "Desk", current: true, paired_at: 1, last_seen_at: 2 }] }))));
  const $ = (id) => document.getElementById(id);
  const controls = installAccess({ card: $("card"), note: $("note"), controls: $("controls"), signOut: $("out"), refresh: $("refresh"), list: $("list"), status: $("status") }, { onSignedOut: vi.fn() });
  $("refresh").click();
  await vi.waitFor(() => expect($("list").querySelector('tbody tr[data-row-key]')).not.toBeNull());
  const row = $("list").querySelector('tbody tr[data-row-key]');
  expect(row.cells[0].textContent).toBe("Desk, this browser");
  expect([...$("list").querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Name", "Paired", "Last seen", "Actions"]);
  expect(row.querySelectorAll('button:not([role="menuitem"])')).toHaveLength(1);
  row.querySelector('[aria-haspopup="menu"]').click();
  expect(document.activeElement.textContent).toBe("Rename");
  press("ArrowDown");
  expect(document.activeElement.textContent).toBe("Revoke access");
  controls.clear();
  expect($("list").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(0);
  $("refresh").click();
  await vi.waitFor(() => expect($("list").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(1));
});
