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
  const storage = defaultStorage();
  doc.getElementById("collapse-all-cards")?.addEventListener("click", (event) => {
    event.currentTarget.focus();
    const panel = doc.querySelector("section[data-view]:not([hidden])");
    for (const card of panel?.querySelectorAll("details.card[open]") || []) {
      // Settings has its own tabs. Leave their hidden panels untouched too.
      if (!card.closest('[role="tabpanel"][hidden]')) card.open = false;
    }
  });
  for (const card of doc.querySelectorAll("details.card[id]")) {
    const key = `autodj.card.${card.id}`;
    const saved = readStorage(storage, key);
    if (saved === "open" || saved === "closed") card.open = saved === "open";
    card.addEventListener("toggle", () => {
      writeStorage(storage, key, card.open ? "open" : "closed");
    });
  }
}
