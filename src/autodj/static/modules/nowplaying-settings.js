import { failureText, withDisabled } from "./api-client.js";
import { copyByCommand } from "./clipboard.js";
// Settings > Stream > Now-playing feed: the public feed's address, whether
// it shows the next song, and the webhooks set in the station's secrets.json.
//
// Speech rules (docs/accessibility-testing.md, Announcement contract):
//   - Copy, the show-next save and Send test are user actions, so their
//     results are forced: pressing twice is heard twice.
//   - The webhook list is plain text, never a live region.  Its items are
//     rewritten in place only when their text changes, so reading the
//     list is not disturbed and focus stays on a Send test button.
//   - The server never sends a webhook's address, only its host; nothing
//     here asks for more.
//   - Failures are said in words with the error tone.  A 401 already
//     opened the sign-in dialog, so it says nothing more.
//   - DMCA mode (set in Settings > Station > Broadcasting) shows a plain-text status line and
//     keeps the show-next box focusable but aria-disabled, its reason
//     added to the box's description.  Pressing it says the reason and
//     leaves it unticked: listeners never get the next song in that mode.

import { DEFAULT_STATION, currentStation } from "./stations.js";
import { makeSay } from "./live-region.js";
import { onCardOpen } from "./card-state.js";
import { registerSettingsGroup } from "./settings-drafts.js";
import { listRow, rowButton } from "./dom-helpers.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { isLoopbackHost } from "./stream-mode.js";

export const FEED_PATH = "/api/nowplaying";
export const STATUS_PATH = "/status";

// The station's own public feed and status page (#70): the default
// station keeps the addresses above; another is under its name.
function publicPaths(slug = currentStation()) {
  if (slug === DEFAULT_STATION) return { feed: FEED_PATH, status: STATUS_PATH };
  const name = encodeURIComponent(slug);
  return { feed: `/api/stations/${name}/nowplaying`, status: `/stations/${name}/status` };
}
export const COPIED_TEXT = "Now-playing link copied.";
export const COPY_FAILED_TEXT =
  "Could not copy. Select the now-playing link and copy it yourself.";
export const STATUS_COPIED_TEXT = "Status page link copied.";
export const STATUS_COPY_FAILED_TEXT =
  "Could not copy. Select the status page link and copy it yourself.";
export const NO_WEBHOOKS_TEXT =
  "No webhooks are set up. Add them to this station's secrets.json on the AutoDJ computer, then restart AutoDJ.";
// While the panel is open, a state push reloads the list at most this
// often, so each track change's delivery result shows up.
export const WEBHOOK_REFRESH_MS = 15000;

const FORMAT_NAMES = { generic: "JSON", discord: "Discord" };

function isAuthFailure(err) {
  return Boolean(err) && err.name === "AuthenticationRequiredError";
}

// "Webhook 1, discord.com, Discord format. Last delivery: Delivered."
export function webhookText(hook) {
  const number = Number(hook.index) + 1;
  const format = FORMAT_NAMES[hook.format] || hook.format;
  return `Webhook ${number}, ${hook.host}, ${format} format. Last delivery: ${hook.result}.`;
}

// The name a screen reader hears: it starts with the visible "Send test"
// (WCAG 2.5.3) and says which destination, since hosts can repeat.
export function testButtonName(hook) {
  return `Send test to webhook ${Number(hook.index) + 1}, ${hook.host}`;
}

export function installNowPlayingSettings({
  doc = document,
  // requestJson from api-client.js.
  request,
  srStatus,
  settingsStatus,
  hostname = doc.location.hostname,
  now = () => Date.now(),
}) {
  const panel = doc.getElementById("spanel-stream");
  const link = doc.getElementById("np-url");
  const copy = doc.getElementById("np-copy");
  const statusLink = doc.getElementById("np-status-url");
  const statusCopy = doc.getElementById("np-status-copy");
  const loopbackNote = doc.getElementById("np-loopback-note");
  const showNext = doc.getElementById("np-show-next");
  const dmcaStatus = doc.getElementById("dmca-status");
  const dmcaReason = doc.getElementById("np-show-next-dmca");
  const list = doc.getElementById("np-webhooks-list");
  const empty = doc.getElementById("np-webhooks-empty");
  if (!link || !copy || !showNext || !list || !empty) return null;
  const settingsDraft = registerSettingsGroup({
    panel,
    group: "stream",
    controls: [showNext],
    read: () => ({ show_next: showNext.checked }),
    write: (body) => {
      if (body.show_next != null) showNext.checked = Boolean(body.show_next);
    },
    describe: (body) => (body.show_next
      ? "The public now-playing feed now shows the next song."
      : "The public now-playing feed now hides the next song."),
  });
  const listNavigation = installActionListNavigation(list);

  const statusOpen = doc.getElementById("np-status-open");
  // The addresses follow the station the page controls.
  function showAddresses() {
    const paths = publicPaths();
    const feed = `${doc.location.origin}${paths.feed}`;
    if (link.value !== feed) link.value = feed;
    const status = `${doc.location.origin}${paths.status}`;
    if (statusLink && statusLink.value !== status) statusLink.value = status;
    if (statusOpen && statusOpen.getAttribute("href") !== paths.status) {
      statusOpen.setAttribute("href", paths.status);
    }
  }
  showAddresses();
  if (loopbackNote && isLoopbackHost(hostname)) {
    loopbackNote.hidden = false;
    // Heard when tabbing to a link or its Copy, not only in browse mode.
    for (const control of [link, copy, statusLink, statusCopy].filter(Boolean)) {
      const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      if (!ids.includes(loopbackNote.id)) ids.push(loopbackNote.id);
      control.setAttribute("aria-describedby", ids.join(" "));
    }
  }

  let active = false;
  let generation = 0;
  let loading = null;
  let lastLoad = -Infinity;
  let loadFailureSpoken = false;
  let sendController = new AbortController();

  const saySettings = makeSay(settingsStatus, { dwellMs: 4000, errorDwellMs: 6000 });
  const sayStatus = makeSay(srStatus, { dwellMs: 4000, errorDwellMs: 6000 });

  function setEmptyText(text) {
    if (empty.textContent !== text) empty.textContent = text;
    if (empty.hidden) empty.hidden = false;
  }

  function sendTest(hook, button) {
    saySettings(`Sending a test to ${hook.host}.`);
    const mine = generation;
    void withDisabled(button, async () => {
      try {
        const reply = await request(`/api/webhooks/${hook.index}/test`, { method: "POST" });
        if (mine !== generation) return;
        if (reply && reply.delivered === true) {
          saySettings(`Test delivered to ${hook.host}.`);
        } else {
          saySettings(
            `Test to ${hook.host} failed. Check its address in this station's secrets.json and the AutoDJ log.`,
            "error");
        }
        void load();
      } catch (err) {
        if (mine !== generation || isAuthFailure(err)) return;
        saySettings(failureText(`send a test to ${hook.host}`, err), "error");
      }
    }, { signal: sendController.signal });
  }

  // The configured list only changes when AutoDJ restarts, so an update
  // with the same destinations rewrites item text and keeps the buttons
  // (and focus on one of them).
  function render(hooks) {
    const items = [...list.children];
    const sameShape = items.length === hooks.length
      && items.every((item, at) => item.dataset.key === `${hooks[at].index}|${hooks[at].host}`);
    if (sameShape) {
      items.forEach((item, at) => {
        const text = item.querySelector("span");
        const wanted = webhookText(hooks[at]);
        if (text && text.textContent !== wanted) text.textContent = wanted;
      });
    } else {
      const hadFocus = list.contains(doc.activeElement);
      list.replaceChildren(...hooks.map((hook) => {
        const button = rowButton(doc, "Send test", testButtonName(hook),
          (button) => sendTest(hook, button));
        const item = listRow(doc, webhookText(hook), "", button);
        item.dataset.key = `${hook.index}|${hook.host}`;
        return item;
      }));
      // Focus never falls to the page top: it goes to the first Send test
      // left, else the feed's Copy button.
      if (hadFocus) (list.querySelector("button") || copy).focus();
    }
    list.hidden = hooks.length === 0;
    listNavigation.refresh();
    if (hooks.length === 0) setEmptyText(NO_WEBHOOKS_TEXT);
    else empty.hidden = true;
  }

  async function load() {
    if (!active || loading) return;
    const mine = generation;
    const token = {};
    loading = token;
    lastLoad = now();
    try {
      const hooks = await request("/api/webhooks");
      if (mine !== generation) return;
      render(Array.isArray(hooks) ? hooks : []);
      loadFailureSpoken = false;
    } catch (err) {
      if (mine !== generation || isAuthFailure(err)) return;
      if (!list.children.length) setEmptyText("The webhook list could not be loaded.");
      // Reloads repeat while the panel is open; the failure is said once.
      if (!loadFailureSpoken) {
        loadFailureSpoken = true;
        saySettings(failureText("load the webhooks", err), "error");
      }
    } finally {
      if (loading === token) loading = null;
    }
  }

  function seen() {
    return active && Boolean(panel?.open);
  }

  onCardOpen(panel, () => { if (seen()) void load(); });

  function installCopy(button, field, copied, failed) {
    button?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(field.value);
      } catch (_err) {
        if (!copyByCommand(field.value, doc)) {
          // Focus moves to the selected link, ready for Ctrl+C.
          field.focus();
          field.select();
          sayStatus(failed, "error");
          return;
        }
      }
      sayStatus(copied);
    });
  }
  installCopy(copy, link, COPIED_TEXT, COPY_FAILED_TEXT);
  if (statusLink) installCopy(statusCopy, statusLink, STATUS_COPIED_TEXT, STATUS_COPY_FAILED_TEXT);

  function dmcaLocked() {
    return showNext.getAttribute("aria-disabled") === "true";
  }

  // Before the change: a prevented click leaves the box as it was.
  showNext.addEventListener("click", (event) => {
    if (!dmcaLocked()) return;
    event.preventDefault();
    saySettings(dmcaReason?.textContent.trim() || "DMCA mode is on.");
  });

  function applyDmca(on) {
    if (dmcaStatus) dmcaStatus.hidden = !on;
    const ids = (showNext.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean)
      .filter((id) => id !== dmcaReason?.id);
    if (on) {
      showNext.setAttribute("aria-disabled", "true");
      showNext.checked = false;
      if (dmcaReason) ids.push(dmcaReason.id);
    } else {
      showNext.removeAttribute("aria-disabled");
    }
    if (dmcaReason) dmcaReason.hidden = !on;
    showNext.setAttribute("aria-describedby", ids.join(" "));
  }

  function apply(state) {
    showAddresses();
    // A push without settings leaves DMCA mode as it was.
    const dmcaSetting = state?.settings?.playback?.dmca_enabled;
    if (typeof dmcaSetting === "boolean" && dmcaSetting !== dmcaLocked()) applyDmca(dmcaSetting);
    const dmca = dmcaLocked();
    if (!state) {
      active = false;
      return;
    }
    const pushed = state.settings?.playback?.nowplaying_show_next;
    if (!dmca && typeof pushed === "boolean") settingsDraft.accept({ show_next: pushed });
    const wasActive = active;
    active = true;
    if (!seen()) return;
    if (!wasActive || now() - lastLoad >= WEBHOOK_REFRESH_MS) void load();
  }

  // Sign-out: forget the list along with the rest of the session state.
  function reset() {
    sendController.abort();
    sendController = new AbortController();
    active = false;
    generation += 1;
    loading = null;
    lastLoad = -Infinity;
    loadFailureSpoken = false;
    list.replaceChildren();
    list.hidden = true;
    empty.textContent = "";
    empty.hidden = true;
    showNext.checked = showNext.defaultChecked;
  }

  return { apply, reset, load };
}
