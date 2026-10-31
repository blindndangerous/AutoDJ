// How the page writes times of day: one place, so every panel agrees.
// The server writes its own times (autodj/timefmt.py); both follow
// Settings > Global > Server, Time format (#286), 24 hour unless set to 12.
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

// A compact relative date for dates shown to people. The reference time is
// injectable so callers and tests can use a stable clock. Invalid dates return
// an empty string; intervals under half a minute round to "now".
function relativeParts(when, reference, maxUnit, dayAfterHours) {
  const instant = when instanceof Date ? when.getTime() : new Date(when).getTime();
  const baseline = reference instanceof Date ? reference.getTime() : new Date(reference).getTime();
  if (!Number.isFinite(instant) || !Number.isFinite(baseline)) return null;
  const delta = instant - baseline;
  const seconds = delta / 1000;
  const minutes = Math.round(seconds / 60);
  const hours = Math.round(seconds / 3600);
  const days = Math.round(seconds / 86400);
  const months = Math.round(seconds / (86400 * 30.4375));
  const years = Math.round(seconds / (86400 * 365.25));
  const order = ["minute", "hour", "day", "month", "year"];
  const limit = Math.max(0, order.indexOf(maxUnit));
  let unit;
  let value;
  if (Math.abs(seconds) < 30) { unit = "second"; value = 0; }
  else if (Math.abs(seconds) < 60 * 60) { unit = "minute"; value = minutes; }
  else if (Math.abs(seconds) < dayAfterHours * 3600) { unit = "hour"; value = hours; }
  else if (Math.abs(seconds) < 30.4375 * 86400) { unit = "day"; value = days; }
  else if (Math.abs(seconds) < 365.25 * 86400) { unit = "month"; value = months; }
  else { unit = "year"; value = years; }
  const selected = order.indexOf(unit);
  if (selected > limit) {
    unit = order[limit];
    value = { minute: minutes, hour: hours, day: days, month: months, year: years }[unit];
  }
  return { value, unit };
}

export function relativeTimeText(when, { reference = Date.now(), locale = undefined, maxUnit = "year", dayAfterHours = 24 } = {}) {
  const parts = relativeParts(when, reference, maxUnit, dayAfterHours);
  return parts ? new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(parts.value, parts.unit) : "";
}

// An approximate duration in the direction of the date, for example
// "about 4 days", "in about 4 months", or "about 4 years ago".
export function approximateDurationText(when, { reference = Date.now(), locale = undefined } = {}) {
  const parts = relativeParts(when, reference, "year", 24);
  if (!parts) return "";
  const unitText = new Intl.RelativeTimeFormat(locale, { numeric: "always" })
    .format(Math.abs(parts.value), parts.unit).replace(/^in /, "").replace(/ ago$/, "");
  if (parts.value === 0) return "about now";
  return parts.value < 0 ? `about ${unitText} ago` : `in about ${unitText}`;
}
