import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultStorage, readStorage, removeStorage, writeStorage } from "../../src/autodj/static/modules/safe-storage.js";

describe("safe browser storage", () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  afterEach(() => {
    if (descriptor) Object.defineProperty(globalThis, "localStorage", descriptor);
    else delete globalThis.localStorage;
  });

  it("returns null when accessing the browser storage getter throws", () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new Error("blocked"); },
    });
    expect(defaultStorage()).toBeNull();
  });

  it("uses the fallback on failed reads and ignores failed writes and removals", () => {
    const storage = {
      getItem: vi.fn(() => { throw new Error("blocked"); }),
      setItem: vi.fn(() => { throw new Error("blocked"); }),
      removeItem: vi.fn(() => { throw new Error("blocked"); }),
    };
    expect(readStorage(storage, "key", "fallback")).toBe("fallback");
    expect(readStorage(null, "key")).toBeNull();
    expect(() => writeStorage(storage, "key", "value")).not.toThrow();
    expect(() => removeStorage(storage, "key")).not.toThrow();
  });
});
