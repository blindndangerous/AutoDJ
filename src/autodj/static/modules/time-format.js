// How the page writes times of day: one place, so every panel agrees.
// The server writes its own times (autodj/timefmt.py); both follow
// Settings > General settings > Server, Time format (#286), 24 hour unless set to 12.
const TIME = { hour: "numeric", minute: "2-digit" };
let hourCycle = "h23";

// "12" or "24", from /api/stations or the time settings.
export function setTimeFormat(format) {
  hourCycle = format === "12" ? "h12" : "h23";
}

// "15:04" or "3:04 PM", with "second" and any date parts in *options*.
export function clockText(when, { seconds = false, locale = undefined, ...options } = {}) {
  return new Intl.DateTimeFormat(locale, {
    ...TIME, ...(seconds ? { second: "2-digit" } : {}), hourCycle, ...options,
  }).format(when);
}
