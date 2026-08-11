import { describe, expect, it } from "vitest";

import { createLatestRequestOwner } from
  "../../src/autodj/static/modules/latest-request.js";

describe("createLatestRequestOwner", () => {
  it("aborts the predecessor and suppresses its stale result", () => {
    const owner = createLatestRequestOwner();
    const coverA = owner.begin();
    const coverB = owner.begin();

    expect(coverA.signal.aborted).toBe(true);
    expect(owner.isCurrent(coverA)).toBe(false);
    expect(owner.isCurrent(coverB)).toBe(true);
    owner.finish(coverB);
    expect(owner.isCurrent(coverB)).toBe(false);
  });

  it("invalidates an in-flight history request when cancelled", () => {
    const owner = createLatestRequestOwner();
    const history = owner.begin();
    owner.cancel();

    expect(history.signal.aborted).toBe(true);
    expect(owner.isCurrent(history)).toBe(false);
  });
});
