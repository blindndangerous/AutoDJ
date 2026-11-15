import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

// Subtab panels dispatch toggle too. Ignore nested cards' events so opening
// a child cannot start a second refresh of its owning panel.
export function onCardOpen(card, refresh, { onClose = () => {} } = {}) {
  if (!card) return () => {};
  const toggle = (event) => {
    if (event.target !== card) return;
    if (card.open) void refresh();
    else onClose();
  };
  card.addEventListener("toggle", toggle);
  return () => card.removeEventListener("toggle", toggle);
}

// Card IDs keep browser preferences stable when cards move or headings change.
export function installCardState(doc = document) {
  cardStateObservers.get(doc)?.disconnect();
  const storage = defaultStorage();
  const button = doc.getElementById("collapse-all-cards");
  const panel = () => doc.querySelector("section[data-view]:not([hidden])");
  const eligibleCards = (view = panel()) => [...(view?.querySelectorAll("details.card[open]") || [])]
    .filter((card) => !card.closest('[role="tabpanel"][hidden]'));
  const updateButton = () => {
    if (!button) return;
    const shouldHide = !eligibleCards().some((card) => !card.closest("[hidden]"));
    if (shouldHide && doc.activeElement === button) {
      const tab = doc.querySelector('#view-nav [role="tab"][aria-selected="true"]');
      tab?.focus();
    }
    if (button.hidden !== shouldHide) button.hidden = shouldHide;
  };
  button?.addEventListener("click", (event) => {
    event.currentTarget.focus();
    const view = panel();
    for (const card of eligibleCards(view)) {
      // Settings has its own tabs. Leave their hidden panels untouched too.
      card.open = false;
    }
    updateButton();
  });
  for (const card of doc.querySelectorAll("details.card[id]")) {
    const key = `autodj.card.${card.id}`;
    const saved = readStorage(storage, key);
    if (saved === "open" || saved === "closed") card.open = saved === "open";
    card.addEventListener("toggle", () => {
      writeStorage(storage, key, card.open ? "open" : "closed");
      updateButton();
    });
  }
  const observer = new doc.defaultView.MutationObserver(updateButton);
  const observedRoot = doc.querySelector("main") || doc.body || doc.documentElement;
  if (observedRoot) observer.observe(observedRoot, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "open"] });
  cardStateObservers.set(doc, observer);
  updateButton();
}

const cardStateObservers = new WeakMap();
