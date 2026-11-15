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
  const eligibleCards = () => [...(panel()?.querySelectorAll("details.card") || [])]
    .filter((card) => !card.closest("[hidden]"));
  const updateButton = () => {
    if (!button) return;
    const cards = eligibleCards();
    const label = cards.some((card) => card.open) ? "Collapse all cards" : "Expand all cards";
    if (button.textContent !== label) button.textContent = label;
    button.setAttribute("aria-disabled", String(cards.length === 0));
  };
  button?.addEventListener("click", (event) => {
    event.currentTarget.focus();
    const cards = eligibleCards();
    const expand = !cards.some((card) => card.open);
    for (const card of cards) card.open = expand;
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
