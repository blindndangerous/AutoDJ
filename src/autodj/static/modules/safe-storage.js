// Browser storage can be unavailable, including when reading its global getter.
export function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export function readStorage(storage, key, fallback = null) {
  try {
    return storage?.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function writeStorage(storage, key, value) {
  try {
    storage?.setItem(key, value);
  } catch {
    // Browser storage is optional.
  }
}

export function removeStorage(storage, key) {
  try {
    storage?.removeItem(key);
  } catch {
    // Browser storage is optional.
  }
}
