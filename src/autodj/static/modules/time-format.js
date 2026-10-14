// How the page writes times of day: one place, so every panel agrees.
// The server writes its own times (autodj/timefmt.py, 24 hour); these
// follow the reader's browser locale.
const TIME = { hour: "numeric", minute: "2-digit" };

// "3:04 PM" or "15:04", with "second" and any date parts in *options*.
export function clockText(when, { seconds = false, locale = undefined, ...options } = {}) {
  return new Intl.DateTimeFormat(locale, {
    ...TIME, ...(seconds ? { second: "2-digit" } : {}), ...options,
  }).format(when);
}
