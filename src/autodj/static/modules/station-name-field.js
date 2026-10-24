// The "Station name while active" field of the clockwheel and show editors (#472).
//
// The server checks the same rules (autodj/station_name.py): blank is
// allowed and means "keep the name in use"; otherwise at most 100 characters
// and nothing a player header cannot carry.

export const STATION_NAME_MAX = 100;
// Python's str.isprintable(): no control, format, unassigned or separator
// character except the plain space.
const UNPRINTABLE = /[\p{C}\p{Zl}\p{Zp}]|(?! )\p{Zs}/u;

// { name } with the trimmed name, or { error } saying why it cannot be saved.
export function checkStationName(text) {
  const raw = String(text ?? "");
  if ([...raw].length > STATION_NAME_MAX) {
    return { error: `Station name while active can be at most ${STATION_NAME_MAX} characters.` };
  }
  const name = raw.trim();
  if (UNPRINTABLE.test(name)) {
    return { error: "Station name while active cannot contain tabs, line breaks or other control characters." };
  }
  return { name };
}

// Whether a server error is about this field, so the page can put focus on it.
export const isStationNameError = (message) => /^Station name while active\b/.test(String(message || ""));
