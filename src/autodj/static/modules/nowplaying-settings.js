// Settings > Stream > Now-playing feed: the public feed's address, whether
// it shows the next song, and the webhooks set in config.local.toml.
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

import { announceStatus } from "./live-region.js";
import { saveHolds } from "./settings-panel.js";
import { listRow, rowButton } from "./dom-helpers.js";
import { isLoopbackHost } from "./stream-mode.js";

export const FEED_PATH = "/api/nowplaying";
export const STATUS_PATH = "/status";
export const COPIED_TEXT = "Now-playing link copied.";
export const COPY_FAILED_TEXT =
  "Could not copy. Select the now-playing link and copy it yourself.";
export const STATUS_COPIED_TEXT = "Status page link copied.";
export const STATUS_COPY_FAILED_TEXT =
  "Could not copy. Select the status page link and copy it yourself.";
export const NO_WEBHOOKS_TEXT =
  "No webhooks are set up. Add them to config.local.toml on the AutoDJ computer, then restart AutoDJ.";
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

// Plain http is not a secure context, so it has no clipboard API.  The
// older copy command still works there through a one-off copy listener,
// so nothing needs selecting and focus stays put.
function copyByCommand(doc, text) {
  let wrote = false;
  const onCopy = (event) => {
    if (!event.clipboardData) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
    wrote = true;
  };
  doc.addEventListener("copy", onCopy);
  try {
    return typeof doc.execCommand === "function" && doc.execCommand("copy") === true && wrote;
  } catch (_) {
    return false;
  } finally {
    doc.removeEventListener("copy", onCopy);
  }
}

export function installNowPlayingSettings({
  doc = document,
  // requestJson from api-client.js.
  request,
  // (checked, control) => Promise<boolean>, through postSettings, which
  // says why a save failed and puts the box back.
  saveShowNext,
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
  const list = doc.getElementById("np-webhooks-list");
  const empty = doc.getElementById("np-webhooks-empty");
  if (!link || !copy || !showNext || !list || !empty) return null;

  link.value = `${doc.location.origin}${FEED_PATH}`;
  if (statusLink) statusLink.value = `${doc.location.origin}${STATUS_PATH}`;
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
  const sending = new Set();

  function say(region, text, tone = "info") {
    announceStatus(region, text, { dwellMs: tone === "error" ? 6000 : 4000, force: true, tone });
  }

  function setEmptyText(text) {
    if (empty.textContent !== text) empty.textContent = text;
    if (empty.hidden) empty.hidden = false;
  }

  function sendTest(hook, button) {
    if (sending.has(hook.index)) return;
    sending.add(hook.index);
    button.setAttribute("aria-disabled", "true");
    say(settingsStatus, `Sending a test to ${hook.host}.`);
    const mine = generation;
    void (async () => {
      try {
        const reply = await request(`/api/webhooks/${hook.index}/test`, { method: "POST" });
        if (mine !== generation) return;
        if (reply && reply.delivered === true) {
          say(settingsStatus, `Test delivered to ${hook.host}.`);
        } else {
          say(settingsStatus,
            `Test to ${hook.host} failed. Check its address in config.local.toml and the AutoDJ log.`,
            "error");
        }
        void load();
      } catch (err) {
        if (mine !== generation || isAuthFailure(err)) return;
        say(settingsStatus, `Could not send a test to ${hook.host}: ${err.message}`, "error");
      } finally {
        sending.delete(hook.index);
        button.removeAttribute("aria-disabled");
      }
    })();
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
          (pressed) => sendTest(hook, pressed));
        const item = listRow(doc, webhookText(hook), "", button);
        item.dataset.key = `${hook.index}|${hook.host}`;
        return item;
      }));
      // Focus never falls to the page top: it goes to the first Send test
      // left, else the feed's Copy button.
      if (hadFocus) (list.querySelector("button") || copy).focus();
    }
    list.hidden = hooks.length === 0;
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
        say(settingsStatus, `Could not load the webhooks: ${err.message}`, "error");
      }
    } finally {
      if (loading === token) loading = null;
    }
  }

  function seen() {
    return active && Boolean(panel?.open);
  }

  panel?.addEventListener("toggle", () => {
    if (seen()) void load();
  });

  function installCopy(button, field, copied, failed) {
    button?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(field.value);
      } catch (_err) {
        if (!copyByCommand(doc, field.value)) {
          // Focus moves to the selected link, ready for Ctrl+C.
          field.focus();
          field.select();
          say(srStatus, failed, "error");
          return;
        }
      }
      say(srStatus, copied);
    });
  }
  installCopy(copy, link, COPIED_TEXT, COPY_FAILED_TEXT);
  if (statusLink) installCopy(statusCopy, statusLink, STATUS_COPIED_TEXT, STATUS_COPY_FAILED_TEXT);

  showNext.addEventListener("change", async () => {
    const chosen = showNext.checked;
    const saved = await saveShowNext(chosen, showNext);
    if (!saved) {
      // postSettings has said why; the box shows what the server has.
      showNext.checked = !chosen;
      return;
    }
    say(settingsStatus, chosen
      ? "The public now-playing feed now shows the next song."
      : "The public now-playing feed now hides the next song.");
  });

  function apply(state) {
    if (!state || !state.stream_mode) {
      active = false;
      return;
    }
    const pushed = state.settings?.playback?.nowplaying_show_next;
    if (typeof pushed === "boolean" && !saveHolds(showNext, pushed)
        && showNext.checked !== pushed) {
      showNext.checked = pushed;
    }
    const wasActive = active;
    active = true;
    if (!seen()) return;
    if (!wasActive || now() - lastLoad >= WEBHOOK_REFRESH_MS) void load();
  }

  // Sign-out: forget the list along with the rest of the session state.
  function reset() {
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
