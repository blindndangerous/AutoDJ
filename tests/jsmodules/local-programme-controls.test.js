import { afterEach, describe, expect, it, vi } from "vitest";
import { installLocalProgrammeControls } from "../../src/autodj/static/modules/local-programme-controls.js";
import { ProgrammeOwner } from "../../src/autodj/static/modules/local-programme.js";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

function setup(owner = new ProgrammeOwner()) {
  document.body.innerHTML = `<section id="controls"><button id="take">Take local control</button>
    <button id="return">Return to automation</button><p id="status"></p></section>`;
  const elements = { wrap: document.getElementById("controls"), take: document.getElementById("take"),
    returnButton: document.getElementById("return"), status: document.getElementById("status") };
  const announce = vi.fn();
  const controls = installLocalProgrammeControls({ ...elements, announce, owner });
  const apply = state => controls.apply({ stream_mode: true, local_programme: { state, epoch: "e", generation: 1 } });
  apply("automation");
  return { ...elements, announce, apply, owner };
}

describe("local control accessibility", () => {
  function checkTakeUnavailable(state, reason) {
    const { take, apply, announce } = setup();
    apply(state);
    take.focus();
    take.click();
    expect(announce).toHaveBeenLastCalledWith(reason);
    expect(document.activeElement).toBe(take);
  }

  // ADR-0019-08
  it("explains why Take is unavailable while preparing", () => {
    checkTakeUnavailable("preparing", "Local control is being prepared. Wait or select Return to automation to cancel.");
  });

  // ADR-0019-08
  it("explains why Take is unavailable while returning", () => {
    checkTakeUnavailable("returning", "Wait for the return to automation to finish.");
  });

  // ADR-0019-08
  it("explains why Take is unavailable while recovering", () => {
    checkTakeUnavailable("recovering", "Wait for automation to recover before taking local control.");
  });

  async function checkReturnRefusal(state) {
    vi.useFakeTimers();
    const { apply, take, returnButton, announce, status } = setup();
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({ detail: "Ownership changed" }), {
        status: 409, headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({ state, epoch: "new", generation: 2 }), {
        headers: { "Content-Type": "application/json" },
      }));
    vi.stubGlobal("fetch", fetcher);
    apply("local");
    announce.mockClear();
    returnButton.focus();
    returnButton.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0][1].method).toBe("POST");
    expect(fetcher.mock.calls[1][1]?.method || "GET").toBe("GET");
    expect(status.textContent).toBe(state === "local" ? "Local control" : "Automation");
    expect(take.getAttribute("aria-disabled")).toBe(String(state !== "automation"));
    expect(returnButton.getAttribute("aria-disabled")).toBe(String(state === "automation"));
    expect(announce.mock.calls.filter(([message]) => message === "Ownership changed")).toHaveLength(1);
    expect(document.activeElement).toBe(returnButton);
    if (state === "local") {
      fetcher.mockResolvedValue(new globalThis.Response(JSON.stringify({ state: "automation" }), {
        headers: { "Content-Type": "application/json" },
      }));
      returnButton.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ epoch: "new", generation: 2 });
    }
  }

  // ADR-0019-08
  it("refreshes authoritative local state after Return is refused", async () => {
    await checkReturnRefusal("local");
  });

  // ADR-0019-08
  it("refreshes authoritative automation state after Return is refused", async () => {
    await checkReturnRefusal("automation");
  });

  // ADR-0019-08
  it("announces only the final ownership state during a clean return", async () => {
    vi.useFakeTimers();
    const { owner, apply, returnButton, announce, status } = setup();
    owner.statusUrl = "/api/local-programme";
    owner.grant = { epoch: "e", generation: 1, capability: "secret" };
    owner.socket = { readyState: 1, send: vi.fn(), close: vi.fn() };
    apply("local");
    announce.mockClear();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(JSON.stringify({ state: "automation" }),
      { headers: { "Content-Type": "application/json" } })));
    returnButton.focus();
    returnButton.click();
    owner.stop("Returned to automation");
    await vi.advanceTimersByTimeAsync(200);
    expect(status.textContent).toBe("Automation");
    expect(announce.mock.calls).toEqual([["Automation"]]);
    expect(document.activeElement).toBe(returnButton);
  });

  // ADR-0019-08
  it("reports a disconnected return once and confirms the final server state", async () => {
    vi.useFakeTimers();
    const { owner, apply, returnButton, announce, status } = setup();
    owner.statusUrl = "/api/local-programme";
    owner.grant = { epoch: "e", generation: 1, capability: "secret" };
    owner.socket = { readyState: 3, send: vi.fn(), close: vi.fn() };
    apply("local");
    announce.mockClear();
    const fetcher = vi.fn().mockResolvedValue(new globalThis.Response(JSON.stringify({ state: "automation" }),
      { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetcher);
    returnButton.click();
    await vi.advanceTimersByTimeAsync(200);
    expect(announce.mock.calls).toEqual([["Local control is disconnected"], ["Automation"]]);
    expect(fetcher).toHaveBeenCalled();
    expect(owner.owned).toBe(false);
    expect(status.textContent).toBe("Automation");
    apply("automation");
    expect(announce).toHaveBeenCalledTimes(2);
  });

  // ADR-0019-08
  it("explains unavailable local control actions when activated", () => {
    const { take, returnButton, apply, announce } = setup();
    returnButton.click();
    expect(announce).toHaveBeenLastCalledWith("Automation already controls this station.");
    apply("local");
    take.click();
    expect(announce).toHaveBeenLastCalledWith("Return to automation before taking local control.");
  });

  // ADR-0019-08
  it("announces an acquisition refusal once without intermediate state messages", async () => {
    const engine = { prepareLocalProgramme: () => { throw new Error("Audio is unavailable"); }, endLocalProgramme: vi.fn() };
    const { take, announce, status } = setup(new ProgrammeOwner({ engine }));
    take.click();
    await Promise.resolve();
    await Promise.resolve();
    expect(announce.mock.calls).toEqual([["Audio is unavailable"]]);
    expect(status.textContent).toBe("Audio is unavailable");
    expect(document.activeElement).not.toBe(status);
  });

  // ADR-0019-08
  it("hides disabled installations and announces each change once without moving focus", async () => {
    document.body.innerHTML = `<section id="controls" hidden><button id="take">Take local control</button>
      <button id="return">Return to automation</button><p id="status"></p></section>`;
    const wrap = document.getElementById("controls");
    const take = document.getElementById("take");
    const returnButton = document.getElementById("return");
    const status = document.getElementById("status");
    const announce = vi.fn();
    const owner = { owned: false, take: vi.fn(), release: vi.fn(), stop: vi.fn() };
    const controls = installLocalProgrammeControls({ wrap, take, returnButton, status, announce, owner });
    controls.apply({ stream_mode: true });
    expect(wrap.hidden).toBe(true);
    controls.apply({ stream_mode: true, local_programme: { state: "automation", generation: 0 } });
    expect(wrap.hidden).toBe(false);
    take.focus();
    controls.apply({ stream_mode: true, local_programme: { state: "local", generation: 1 } });
    controls.apply({ stream_mode: true, local_programme: { state: "local", generation: 1 } });
    expect(announce).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(take);
    expect(take.getAttribute("aria-disabled")).toBe("true");
    expect(take.disabled).toBe(false);
    expect(status.textContent).toContain("Local control");
    take.click();
    expect(owner.take).not.toHaveBeenCalled();
  });
});
