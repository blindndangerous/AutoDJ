import { registerSettingsGroup } from "./settings-drafts.js";
// Settings > Stream listener admission and ban controls.
import { requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const FORMAT_NAMES = { aac: "AAC", opus: "Opus" };

export function createListenerControls(doc, status) {
  const list = doc.getElementById("listener-control-list");
  const bans = doc.getElementById("listener-ban-list");
  const heading = doc.getElementById("listener-control-title");
  const banHeading = doc.getElementById("listener-bans-title");
  const form = doc.getElementById("listener-ban-form");
  const address = doc.getElementById("listener-ban-address");
  const duration = doc.getElementById("listener-ban-duration");
  const note = doc.getElementById("listener-ban-note");
  const limit = doc.getElementById("stream-per-address");
  const limitDraft = registerSettingsGroup({ panel: limit?.closest('[id^="spanel-"]'), group: "stream", controls: [limit],
    read: () => ({ max_listeners_per_address: Number(limit.value) }),
    write: (value) => { if (value.max_listeners_per_address != null) limit.value = value.max_listeners_per_address; },
  });
  const listenersEmpty = doc.getElementById("listener-control-empty");
  const bansEmpty = doc.getElementById("listener-ban-empty");
  let revision = null;
  let loadedOnce = false;
  let failedSpoken = false;
  let requestSequence = 0;
  let active = false;
  let opener = null;

  function say(message, tone = "info") {
    announceStatus(status, message, { dwellMs: 6000, force: true, tone });
  }

  function nextFocus(container, index, fallback) {
    const rows = [...container.children];
    (rows[index]?.querySelector("button") || rows[index - 1]?.querySelector("button") || fallback).focus();
  }

  function makeButton(label, action) {
    const button = doc.createElement("button");
    button.type = "button";
    button.textContent = label;
    // Disconnect and Ban act on this station's listeners only (#70).
    button.addEventListener("click", action);
    return button;
  }

  function openBan(target, trigger) {
    opener = trigger;
    address.value = target;
    note.value = "";
    form.hidden = false;
    address.focus();
  }

  function syncRows(container, rows, fallback) {
    const focused = doc.activeElement?.closest?.("li");
    const focusedIndex = focused ? [...container.children].indexOf(focused) : -1;
    for (const child of [...container.children]) {
      if (!rows.includes(child)) child.remove();
    }
    rows.forEach((row, index) => {
      if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null);
    });
    if (focusedIndex >= 0 && focused && !focused.isConnected) {
      nextFocus(container, focusedIndex, fallback);
    }
  }

  function renderListeners(items) {
    const old = new Map([...list.children].map((row) => [row.dataset.id, row]));
    const names = new Map();
    items.forEach((item) => {
      const name = `${item.address}, ${item.player}`;
      names.set(name, (names.get(name) || 0) + 1);
    });
    const repeatedTimes = new Map();
    const rows = items.map((item) => {
      let row = old.get(item.id);
      if (!row) {
        row = doc.createElement("li");
        row.dataset.id = item.id;
        row.append(doc.createElement("span"));
        row.append(makeButton("Disconnect", async () => {
        const current = row._listener;
        const index = [...list.children].indexOf(row);
        try {
          await requestJson(`/api/stream/listeners/${encodeURIComponent(current.id)}`, { method: "DELETE" });
          row.remove();
          nextFocus(list, index, heading);
          say(`Disconnected listener ${current.address}`);
          await refresh();
        } catch (error) { say(`Could not disconnect listener: ${error.message}`, "error"); }
        }));
        row.append(" ");
        row.append(makeButton("Ban", () => {
          openBan(row._listener.address, row.querySelector("button:last-of-type"));
        }));
      }
      row._listener = item;
      const connected = new Date(item.connected_at * 1000).toLocaleString();
      // The AAC and Opus streams (#50) are named; the MP3 one is the usual stream.
      const format = FORMAT_NAMES[item.format] ? `, ${FORMAT_NAMES[item.format]}` : "";
      row.querySelector("span").textContent = `${item.address}, ${item.player}${format}, connected ${connected}. `;
      const duplicate = names.get(`${item.address}, ${item.player}`) > 1;
      const timeKey = `${item.address}\u0000${item.player}\u0000${connected}`;
      const session = (repeatedTimes.get(timeKey) || 0) + 1;
      repeatedTimes.set(timeKey, session);
      const sameTime = items.some((other) => other.id !== item.id
        && other.address === item.address && other.player === item.player
        && new Date(other.connected_at * 1000).toLocaleString() === connected);
      const name = `${item.address}, ${item.player}${duplicate ? `, connected ${connected}` : ""}`
        + (sameTime ? `, session ${session}` : "");
      row.querySelectorAll("button")[0].textContent = `Disconnect ${name}`;
      row.querySelectorAll("button")[1].textContent = `Ban ${name}`;
      return row;
    });
    syncRows(list, rows, heading);
    listenersEmpty.hidden = items.length > 0;
  }

  function renderBans(items) {
    const old = new Map([...bans.children].map((row) => [row.dataset.address, row]));
    const rows = items.map((item) => {
      let row = old.get(item.address);
      if (!row) {
        row = doc.createElement("li");
        row.dataset.address = item.address;
        row.append(doc.createElement("span"));
        row.append(makeButton(`Remove ban for ${item.address}`, async () => {
          const current = row._ban;
          const index = [...bans.children].indexOf(row);
          try {
            await requestJson(`/api/stream/bans?address=${encodeURIComponent(current.address)}`, { method: "DELETE" });
            row.remove();
            nextFocus(bans, index, banHeading);
            say(`Removed ban for ${current.address}`);
            await refresh();
          } catch (error) { say(`Could not remove ban: ${error.message}`, "error"); }
        }));
      }
      row._ban = item;
      const ending = item.expires_at
        ? `until ${new Date(item.expires_at * 1000).toLocaleString()}` : "until removed";
      row.querySelector("span").textContent = `${item.address}, ${ending}${item.note ? `, ${item.note}` : ""}. `;
      return row;
    });
    syncRows(bans, rows, banHeading);
    bansEmpty.hidden = items.length > 0;
  }

  async function refresh() {
    if (!active) return;
    const sequence = ++requestSequence;
    loadedOnce = true;
    try {
      const [people, blocked] = await Promise.all([
        requestJson("/api/stream/listeners"), requestJson("/api/stream/bans"),
      ]);
      if (!active || sequence !== requestSequence) return;
      revision = people.revision;
      failedSpoken = false;
      renderListeners(people.listeners);
      renderBans(blocked.bans);
      const password = doc.getElementById("listener-password-state");
      password.textContent = people.password_set ? "Listener password set." : "Listener password not set.";
      doc.getElementById("stream-url-desc").textContent = people.password_set
        ? "This stream requires HTTP Basic credentials. Add user:password to the URL in a player that supports it."
        : "Add this address to Sonos or any player as a radio station. Anyone who can reach AutoDJ can listen.";
      limitDraft.accept({ max_listeners_per_address: people.max_per_address });
    } catch (error) {
      if (!active || sequence !== requestSequence) return;
      if (!failedSpoken && error.name !== "AuthenticationRequiredError") {
        say(`Could not load listeners: ${error.message}`, "error");
        failedSpoken = true;
      }
    }
  }

  function apply(state) {
    active = Boolean(state?.stream_mode);
    if (!active) return;
    const hasRevision = Object.hasOwn(state, "stream_listener_revision");
    if (!loadedOnce || (hasRevision && revision !== state.stream_listener_revision)) {
      if (hasRevision) revision = state.stream_listener_revision;
      void refresh();
    }
  }

  function reset() {
    active = false;
    revision = null;
    loadedOnce = false;
    failedSpoken = false;
    requestSequence++;
    list.replaceChildren();
    bans.replaceChildren();
    form.hidden = true;
  }

  doc.getElementById("listener-ban-cancel").addEventListener("click", () => {
    form.hidden = true;
    (opener?.isConnected ? opener : heading).focus();
  });
  doc.getElementById("listener-ban-new").addEventListener("click", (event) => {
    openBan("", event.currentTarget);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const target = address.value.trim();
    const chosen = duration.selectedOptions[0].textContent;
    try {
      await requestJson("/api/stream/bans", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: target, minutes: duration.value ? Number(duration.value) : null, note: note.value }),
      });
      form.hidden = true;
      banHeading.focus();
      say(duration.value ? `Banned ${target} for ${chosen}` : `Banned ${target} until removed`);
      await refresh();
    } catch (error) { say(`Could not ban address: ${error.message}`, "error"); }
  });

  return { apply, reset, refresh };
}
