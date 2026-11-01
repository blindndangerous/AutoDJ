import { beforeEach, expect, it } from "vitest";
import { createSettingsSave } from "../../src/autodj/static/modules/standalone-settings.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

beforeEach(() => { document.body.innerHTML = '<button id="save">Save</button><p id="result"></p>'; });

it("runs one standalone save at a time without disabling its focused control", async () => {
  const button = document.getElementById("save");
  const result = document.getElementById("result");
  const gate = deferred();
  let submissions = 0;
  const saver = createSettingsSave({ button, save: async (current) => {
    submissions += 1;
    await gate.promise;
    if (current()) result.textContent = "Saved once";
  } });
  button.focus();
  const first = saver.run();
  await saver.run();
  expect(submissions).toBe(1);
  expect(saver.pending).toBe(true);
  expect(button.disabled).toBe(false);
  expect(button.hasAttribute("aria-disabled")).toBe(false);
  expect(button.getAttribute("aria-busy")).toBe("true");
  gate.resolve();
  await first;
  expect(result.textContent).toBe("Saved once");
  expect(saver.pending).toBe(false);
  expect(button.hasAttribute("aria-busy")).toBe(false);
});

it("keeps an unfocused Save enabled and busy while a save started elsewhere runs (#331)", async () => {
  document.body.insertAdjacentHTML("afterbegin", '<input id="field">');
  const button = document.getElementById("save");
  const gate = deferred();
  const saver = createSettingsSave({ button, save: () => gate.promise });
  document.getElementById("field").focus();
  const pending = saver.run();
  expect(button.disabled).toBe(false);
  expect(button.hasAttribute("aria-disabled")).toBe(false);
  expect(button.getAttribute("aria-busy")).toBe("true");
  gate.resolve();
  await pending;
  expect(button.hasAttribute("aria-busy")).toBe(false);
});

// ADR-0017-05
it("clears an old save immediately without letting its reply unlock or overwrite a newer save", async () => {
  const button = document.getElementById("save");
  const result = document.getElementById("result");
  const gates = [deferred(), deferred()];
  let index = 0;
  const saver = createSettingsSave({ button, save: async (current) => {
    const mine = index++;
    await gates[mine].promise;
    if (current()) result.textContent = `Saved ${mine}`;
  } });
  const old = saver.run();
  saver.clear();
  expect(saver.pending).toBe(false);
  expect(button.disabled).toBe(false);
  const fresh = saver.run();
  gates[0].resolve();
  await old;
  expect(result.textContent).toBe("");
  expect(saver.pending).toBe(true);
  expect(button.getAttribute("aria-busy")).toBe("true");
  gates[1].resolve();
  await fresh;
  expect(result.textContent).toBe("Saved 1");
  expect(saver.pending).toBe(false);
});

it("rejects replies from an expired authentication session", async () => {
  const gate = deferred();
  const result = document.getElementById("result");
  const saver = createSettingsSave({ button: document.getElementById("save"), save: async (current) => {
    await gate.promise;
    if (current()) result.textContent = "Stale reply";
  } });
  const request = saver.run();
  invalidateAuthenticatedRequestEpoch();
  gate.resolve();
  await request;
  expect(result.textContent).toBe("");
});

it("checks the owning view before saving and before accepting its reply", async () => {
  let active = false;
  let calls = 0;
  const gate = deferred();
  const result = document.getElementById("result");
  const saver = createSettingsSave({ button: document.getElementById("save"), isCurrent: () => active,
    save: async (current) => {
      calls += 1;
      await gate.promise;
      if (current()) result.textContent = "Saved";
    } });
  await saver.run();
  expect(calls).toBe(0);
  active = true;
  const request = saver.run();
  active = false;
  gate.resolve();
  await request;
  expect(result.textContent).toBe("");
});
