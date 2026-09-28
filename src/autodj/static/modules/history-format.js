// History tab: when each track played.
//
// History holds up to 500 tracks and survives restarts, so a bare
// "09:14:02" can be today or last week.  The date is added whenever the
// rows on the page are not all from today; a page of today's tracks keeps
// the short time.

function localDay(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

export function historyNeedsDates(items, now = new Date()) {
  const today = localDay(now);
  return items.some((item) => {
    const played = new Date(item.played_at);
    return !Number.isNaN(played.getTime()) && localDay(played) !== today;
  });
}

export function formatPlayedAt(iso, withDate, locale = undefined) {
  const played = new Date(iso);
  if (Number.isNaN(played.getTime())) return String(iso);
  const time = { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return withDate
    ? played.toLocaleString(locale, { day: "numeric", month: "short", year: "numeric", ...time })
    : played.toLocaleTimeString(locale, time);
}
