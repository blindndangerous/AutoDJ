// Speech production only: previews use native browser audio and never the mix bus.
import { isStaleStationReply, requestJson, withDisabled } from "./api-client.js";
import { makeSay } from "./live-region.js";
import { onCardOpen } from "./card-state.js";
import { stationApi } from "./stations.js";

// getRandomValues also works on explicitly permitted HTTP LAN pages.
const newToken = () => Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)),
  (byte) => byte.toString(16).padStart(2, "0")).join("");

export function installSpeechStudio(root = document.getElementById("speech-studio"), { request = requestJson } = {}) {
  if (!root) return { refresh: async () => {}, pause() {} };
  const doc = root.ownerDocument;
  const get = (id) => doc.getElementById(`speech-${id}`);
  const rows = [];
  const linerNames = new Map();
  let connections = [];
  let state = {};
  let generation = 0;
  let timer = null;
  let inventory = null;
  let pendingComparison = null;
  let pending = new AbortController();
  const say = makeSay(get("status"));
  const node = (tag, text) => {
    const element = doc.createElement(tag);
    if (text) element.textContent = text;
    return element;
  };
  const send = (url, body, method = "POST") => request(`/api/speech${url}`, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  async function act(button, work) {
    try { await withDisabled(button, work, { signal: pending.signal }); }
    catch (error) { if (!isStaleStationReply(error)) say(error.message, "error"); }
  }
  function bind(id, event, work) {
    get(id).addEventListener(event, (e) => {
      e.preventDefault();
      void act(e.submitter || e.currentTarget, work);
    });
  }
  function button(text, work) {
    const control = node("button", text);
    control.type = "button";
    control.addEventListener("click", () => { void act(control, work); });
    return control;
  }
  function field(parent, title, tag = "input") {
    const wrapper = node("div");
    wrapper.className = "settings-row";
    const label = node("label", title);
    const control = node(tag);
    control.id = `speech-candidate-${rows.length}-${title.toLowerCase().replaceAll(" ", "-")}`;
    label.htmlFor = control.id;
    wrapper.append(label, control);
    parent.append(wrapper);
    return control;
  }
  function options(select, items, placeholder) {
    const chosen = select.value;
    const option = (value, text) => { const item = node("option", text); item.value = value; return item; };
    select.replaceChildren(...(placeholder ? [option("", placeholder)] : []),
      ...items.map(([value, label]) => option(value, label)));
    if ([...select.options].some((o) => o.value === chosen)) select.value = chosen;
  }
  function candidate(row) {
    return { connection: row.connection.value, voice: row.voice.value, model: row.model.value,
      speed: Number(row.speed.value), instructions: row.instructions.value };
  }
  function updateCapabilities(row) {
    const engine = connections.find((c) => c.id === row.connection.value)?.engine;
    const cloud = engine === "openai" || engine === "speechify";
    const command = row.connection.value === "station-command";
    row.voice.disabled = command;
    row.voice.placeholder = command ? "Configured by command" : "";
    if (command) row.voice.value = "";
    row.model.placeholder = command ? "Configured by command" : "";
    row.model.disabled = !cloud;
    row.speed.disabled = !cloud;
    row.instructions.disabled = engine !== "openai";
    if (!cloud) { row.model.value = ""; row.speed.value = "1"; }
    if (engine !== "openai") row.instructions.value = "";
  }
  function addCandidate(saved = {}) {
    if (rows.length >= 8) return;
    const wrapper = node("fieldset");
    wrapper.append(node("legend", `Comparison voice ${rows.length + 1}`));
    const row = {};
    row.include = field(wrapper, "Include sample"); row.include.type = "checkbox"; row.include.checked = true;
    row.connection = field(wrapper, "Connection", "select");
    row.voice = field(wrapper, "Voice ID"); row.voice.maxLength = 256;
    row.model = field(wrapper, "Model"); row.model.maxLength = 256;
    row.speed = field(wrapper, "Speed"); row.speed.type = "number"; row.speed.step = "0.05"; row.speed.min = "0.25"; row.speed.max = "4";
    row.instructions = field(wrapper, "Delivery instructions", "textarea"); row.instructions.maxLength = 4096;
    const presetName = field(wrapper, "Preset name"); presetName.maxLength = 64; presetName.pattern = "[a-z0-9][a-z0-9-]{0,63}";
    wrapper.append(button("Save voice preset", async () => {
      await send(`/presets/${encodeURIComponent(presetName.value)}`, candidate(row), "PUT");
      say("Voice preset saved. The station voice has not changed."); await refresh();
    }));
    rows.push(row);
    options(row.connection, connections.map((c) => [c.id, c.name || c.id]), "Choose connection");
    for (const key of ["connection", "voice", "model", "instructions"]) row[key].value = saved[key] || "";
    row.speed.value = String(saved.speed || 1);
    row.connection.addEventListener("change", () => updateCapabilities(row));
    updateCapabilities(row);
    get("candidates").append(wrapper);
    get("add").disabled = rows.length >= 8;
  }
  function audio(url) {
    const player = node("audio"); player.controls = true; player.preload = "none";
    player.src = stationApi(`/api/speech${url}`);
    return player;
  }
  function safeReplace(container, children) {
    // Keep typed names, focus and an actively playing audition intact during polling.
    if (container.contains(doc.activeElement) || container.querySelector(":active")
      || [...container.querySelectorAll("audio")].some((a) => !a.paused)) return;
    container.replaceChildren(...children);
  }
  function render() {
    options(get("preset"), Object.keys(state.presets || {}).map((name) => [name, name]), "Choose preset");
    if (!get("preset").value && state.default_preset) get("preset").value = state.default_preset;
    get("default-name").textContent = `Station voice: ${state.default_preset || "none selected"}.`;
    const results = [];
    for (const [token, batch] of Object.entries(state.batches || {}).reverse()) {
      if (batch.set_name) continue;
      const group = node("li");
      group.append(node("h5", batch.text));
      group.append(button("Remove comparison", async () => {
        await send(`/batches/${encodeURIComponent(token)}`, {}, "DELETE");
        say("Comparison removed. Saved liners remain available."); await refresh();
      }));
      group.append(button("Edit comparison", async () => {
        get("text").value = batch.text;
        rows.splice(0); get("candidates").replaceChildren();
        for (const item of batch.candidates) addCandidate(item.request || { connection: item.connection, ...item.plan?.settings });
        get("text").focus();
      }));
      batch.candidates.forEach((item, index) => {
        const row = node("div");
        row.append(node("p", `${item.connection || "Sample"}, ${item.plan?.settings?.voice || "voice"}: ${item.state}${item.duration ? `, ${item.duration.toFixed(2)} seconds` : ""}${item.cached ? ", reused audio" : ""}. ${item.error || ""}`));
        if (item.plan) row.append(node("p", Object.entries(item.plan.controls || {}).map(([k, v]) => `${k}: ${v}`).join(". ")));
        if (item.state === "succeeded") {
          row.append(audio(`/batches/${encodeURIComponent(token)}/samples/${index}`));
          const label = node("label", "Liner name");
          const input = node("input"); input.maxLength = 100;
          const nameKey = `${token}:${index}`;
          input.value = linerNames.get(nameKey) ?? "Station ID";
          input.addEventListener("input", () => linerNames.set(nameKey, input.value));
          label.append(input); row.append(label);
          row.append(button("Save liner", async () => {
            await send(`/batches/${encodeURIComponent(token)}/save`, { index, label: input.value });
            say("Liner saved. Assign it to rotation when ready."); await refresh();
          }));
        } else if (["failed", "interrupted", "cancelled", "expired"].includes(item.state)) {
          const retryToken = newToken();
          row.append(button(item.state === "expired" ? "Regenerate expired sample" : "Retry failed sample", async () => {
            await send(`/batches/${encodeURIComponent(token)}/retry`, { indices: [index], token: retryToken });
            say("Retry started. A previous interrupted request may already have been billed."); await refresh();
          }));
        }
        group.append(row);
      });
      results.push(group);
    }
    safeReplace(get("results"), results);
    safeReplace(get("saved"), Object.entries(state.saved || {}).map(([name, item]) => {
      const row = node("li", `${item.label}. ${item.published ? "Assigned to liner rotation." : "Saved; not assigned."} `);
      row.append(audio(`/saved/${encodeURIComponent(name)}/audio`));
      if (!item.published) row.append(button("Assign to liner rotation", async () => {
        await send(`/saved/${encodeURIComponent(name)}/publish`, {});
        say("Liner assigned to rotation."); await refresh();
      }));
      return row;
    }));
    safeReplace(get("time-sets"), Object.entries(state.sets || {}).map(([name, item]) => {
      const row = node("li", `${name}: ${item.state}${state.active_set === name ? ", active" : ""}. `);
      if (item.state === "complete") {
        for (const minute of ["00_00", "12_00", "15_05"]) {
          row.append(node("p", minute.replace("_", ":")), audio(`/time-sets/${encodeURIComponent(name)}/samples/${minute}`));
        }
        row.append(button("Activate time-check set", async () => {
          await send("/active-set", { name, reviewed: get("time-reviewed").checked }, "PUT");
          get("time-reviewed").checked = false;
          say("Generated time checks activated. Playback uses local audio."); await refresh();
        }));
      } else {
        const batch = state.batches?.[name];
        const failed = batch?.candidates.map((c, i) => ["failed", "interrupted", "cancelled", "expired"].includes(c.state) ? i : -1).filter((i) => i >= 0) || [];
        row.append(node("p", `${batch?.candidates.filter((c) => c.state === "succeeded").length || 0} samples completed. ${item.error || ""}`));
        if (failed.length) {
          const retryToken = newToken();
          row.append(button("Retry failed or expired time checks", async () => {
            await send(`/batches/${encodeURIComponent(name)}/retry`, { indices: failed, token: retryToken }); await refresh();
          }));
        }
      }
      return row;
    }));
  }
  async function refresh() {
    const current = generation;
    clearTimeout(timer);
    try {
      const [providers, next] = await Promise.all([request("/api/speech/providers"), request("/api/speech")]);
      if (current !== generation) return;
      connections = providers.connections;
      if (next.command_available) connections = [...connections, { id: "station-command", name: "Station TTS command", engine: "command" }];
      const wasBusy = state.busy;
      state = next;
      get("connections").replaceChildren(...connections.filter((c) => c.engine !== "command").map((c) => {
        const row = node("li", `${c.name || c.id}: ${c.engine}. ${c.credential_present ? "Key configured." : "No saved key."} `);
        row.append(button("Edit connection", async () => {
          get("provider-id").value = c.id; get("provider-name").value = c.name || c.id;
          get("provider-engine").value = c.engine; get("provider-url").value = c.url;
          get("provider-local").checked = c.trusted_local || false;
          get("provider-key").value = ""; get("provider-key").disabled = c.credential_environment;
          get("provider-id").focus();
        }));
        return row;
      }));
      for (const row of rows) { options(row.connection, connections.map((c) => [c.id, c.name || c.id]), "Choose connection"); updateCapabilities(row); }
      if (!rows.length) addCandidate(state.presets?.[state.default_preset] || {});
      render();
      if (wasBusy && !state.busy) say("Speech generation finished. Review each sample's result.");
      if (state.busy && root.open) timer = setTimeout(() => { void refresh(); }, 2000);
    } catch (error) { if (!isStaleStationReply(error) && current === generation) say(error.message, "error"); }
  }
  const comparison = () => ({ text: get("text").value, candidates: rows.filter((r) => r.include.checked).map(candidate) });
  bind("provider-form", "submit", async () => {
    const value = { name: get("provider-name").value, engine: get("provider-engine").value,
      url: get("provider-url").value, trusted_local: get("provider-local").checked };
    if (get("provider-key").value) value.api_key = get("provider-key").value;
    await send(`/providers/${encodeURIComponent(get("provider-id").value)}`, value, "PUT");
    get("provider-key").value = ""; say("Shared speech connection saved."); await refresh();
  });
  get("provider-id").addEventListener("input", () => {
    get("provider-key").disabled = Boolean(connections.find((c) => c.id === get("provider-id").value)?.credential_environment);
  });
  bind("add", "click", async () => addCandidate());
  bind("preflight", "click", async () => {
    const result = await send("/preflight", comparison());
    get("preflight-result").textContent = result.candidates.map((c, i) => `Sample ${i + 1}: ${c.error || "ready"}. ${Object.entries(c.controls).map(([k, v]) => `${k}: ${v}`).join("; ")}`).join(" ") + " Cost unknown; charges may apply.";
    say("Comparison checked. Review the results before generating.");
  });
  bind("compare-form", "submit", async () => {
    const body = { ...comparison(), fresh: get("fresh").checked };
    const signature = JSON.stringify(body);
    if (pendingComparison?.signature !== signature) pendingComparison = { signature, token: newToken() };
    await send("/batches", { ...body, token: pendingComparison.token });
    pendingComparison = null;
    say("Generating selected samples. Cost is unknown; provider charges may apply."); await refresh();
  });
  bind("cancel", "click", async () => { await send("/cancel", {}); say("Cancellation requested. Submitted requests may already have been billed."); });
  bind("load-preset", "click", async () => {
    const value = state.presets?.[get("preset").value];
    if (!value) throw new Error("Choose a voice preset.");
    rows.splice(0); get("candidates").replaceChildren(); addCandidate(value);
  });
  bind("default", "click", async () => { await send("/default", { name: get("preset").value }, "PUT"); say("Station voice selected. Existing audio stays as rendered."); await refresh(); });
  bind("time-check", "click", async () => {
    inventory = await send("/time-inventory", { preset: get("preset").value, hour_format: get("time-format").value });
    inventory.token = newToken();
    get("time-inventory").textContent = `1440 minutes, ${inventory.requests} distinct samples, ${inventory.cache_hits} reusable, ${inventory.missing} to generate. Cost unknown; charges may apply.`;
    get("time-generate").disabled = false; say("Time-check set checked. Review the inventory before generating.");
  });
  bind("time-generate", "click", async () => {
    if (!inventory || inventory.preset !== get("preset").value || inventory.hour_format !== get("time-format").value) throw new Error("Check the time-check set again after changing its settings.");
    await send("/time-sets", { preset: inventory.preset, hour_format: inventory.hour_format, fingerprint: inventory.fingerprint, token: inventory.token });
    inventory = null; say("Time-check generation started. The current on-air set is unchanged."); await refresh();
  });
  bind("time-legacy", "click", async () => { await send("/active-set", { name: null }, "PUT"); say("Using existing recorded time-check sources and their configured fallback."); await refresh(); });
  onCardOpen(root, refresh, { onClose: () => clearTimeout(timer) });
  root.addEventListener("focusin", ({ target }) => {
    // WebKit can reveal only the caret, leaving the bottom of a textarea off screen.
    if (target.matches("textarea")) target.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  });
  root.addEventListener("focusout", () => { setTimeout(render, 0); });
  root.addEventListener("ended", render, true);
  return {
    refresh,
    pause() {
      pending.abort(); pending = new AbortController();
      generation += 1; clearTimeout(timer);
      for (const player of root.querySelectorAll("audio")) { player.pause(); player.removeAttribute("src"); }
      state = {}; rows.splice(0); get("candidates").replaceChildren(); get("results").replaceChildren();
      linerNames.clear();
      get("saved").replaceChildren(); get("time-sets").replaceChildren(); get("preset").replaceChildren();
      get("provider-form").reset(); get("provider-key").value = "";
      get("provider-key").disabled = false;
      get("text").value = ""; inventory = null; pendingComparison = null; get("time-generate").disabled = true;
    },
  };
}
