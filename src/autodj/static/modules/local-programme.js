import { ProgrammeTransport } from "./programme-transport.js";
import { currentStation, stationEpoch } from "./stations.js";
import { requestJson } from "./api-client.js";

export function localProgrammeUrl(suffix = "") {
  const station = currentStation();
  const prefix = station === "default" ? "/api" : `/api/stations/${encodeURIComponent(station)}`;
  return `${prefix}/local-programme${suffix}`;
}

// A resolved play promise alone does not prove this media produced audio.
export function mediaReady(audio, signal) {
  if (signal?.aborted) return Promise.reject(new Error("Local control was cancelled"));
  return new Promise((resolve, reject) => {
    let playing = false;
    let accepted = false;
    const cleanup = () => {
      clearTimeout(timer);
      audio.removeEventListener("playing", observed);
      audio.removeEventListener("error", failed);
      signal?.removeEventListener("abort", failed);
    };
    const failed = () => { cleanup(); reject(new Error("Programme media did not become ready")); };
    const finish = () => { if (accepted && playing) { cleanup(); resolve(); } };
    const observed = () => { playing = audio.readyState >= 3; finish(); };
    const timer = setTimeout(failed, 3000);
    audio.addEventListener("playing", observed);
    audio.addEventListener("error", failed);
    signal?.addEventListener("abort", failed, { once: true });
    Promise.resolve().then(() => audio.play()).then(() => {
      accepted = true;
      // HAVE_FUTURE_DATA after play resolves also observes render readiness.
      playing ||= audio.readyState >= 3;
      finish();
    }, error => { cleanup(); reject(error); });
  });
}

export class ProgrammeOwner {
  constructor({ engine = null, stopListening = () => {}, change = () => {} } = {}) {
    this.engine = engine;
    this.stopListening = stopListening;
    this.change = change;
    this.grant = null;
    this.socket = null;
    this.current = null;
    this.occurrences = [];
    this.used = new Set();
    this.markers = new Map();
    this.sequence = 0;
    this.index = 0;
    this.clock = null;
    this.transitioning = false;
    this.paused = false;
    this.abort = new AbortController();
    this.captureQueue = [];
    this.sendTimer = null;
    this.sendTokens = 2;
    this.sendBudgetAt = performance.now();
    this.run = 0;
    this.taking = false;
    this.returning = false;
    this.progressLimit = 0;
  }

  get owned() { return this.grant !== null; }
  get acquiring() { return this.taking || (this.socket !== null && !this.owned); }

  control(type, extra = {}) {
    if (!this.grant || this.socket?.readyState !== 1) throw new Error("Local control is disconnected");
    const { epoch, generation, capability } = this.grant;
    this.socket.send(JSON.stringify({ type, version: 1, epoch, generation, capability, ...extra }));
  }

  async take() {
    if (this.socket || this.taking) return;
    const run = ++this.run;
    const epoch = stationEpoch();
    this.statusUrl = localProgrammeUrl();
    const current = () => {
      if (run !== this.run) return false;
      if (epoch === stationEpoch()) return true;
      this.stop("Station changed; local control cancelled");
      return false;
    };
    this.taking = true;
    this.change("preparing", "Preparing local control");
    this.engine ||= await import("./audio-engine.js");
    if (!current()) return;
    this.sum = this.engine.prepareLocalProgramme();
    const context = this.engine._ctx;
    try {
      await context.resume();
      if (!current()) return;
      await context.audioWorklet.addModule("/programme-worklet.js");
    } catch (error) {
      if (!current()) return;
      throw error;
    }
    if (!current()) return;
    this.node = new AudioWorkletNode(context, "programme-capture", {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
      channelCount: 2, channelCountMode: "explicit",
      processorOptions: { waitForStart: true },
    });
    this.abort = new AbortController();
    this.sequence = 0;
    this.used.clear();
    this.current = null;
    this.clock = null;
    const node = this.node;
    node.port.onmessage = ({ data }) => { if (node === this.node) this.capture(data); };
    this.stopListening();
    const url = new URL(`${this.statusUrl}/ws`, location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = this.socket = new WebSocket(url);
    socket.onopen = () => socket.send(JSON.stringify({ type: "claim", version: 1, rate: 44100 }));
    socket.onmessage = ({ data }) => {
      if (socket !== this.socket) return;
      if (!current()) return;
      this.receive(JSON.parse(data)).catch(error => {
        if (socket === this.socket) this.stop(error.message);
      });
    };
    socket.onclose = event => this.stop(event.reason || "Local control ended; automation is recovering");
    socket.onerror = () => this.stop("Local control connection failed");
  }

  async receive(data) {
    if (data.type === "granted") {
      this.taking = false;
      this.stopListening();
      this.grant = data;
      this.occurrences = data.occurrences || [];
      this.transport = new ProgrammeTransport(this.socket, data);
      this.sendTokens = 2;
      this.sendBudgetAt = performance.now();
      this.progressLimit = data.limit_frame;
      this.node.port.postMessage({ type: "start" });
      this.sum.disconnect(this.engine._master);
      this.sum.connect(this.node);
      this.node.connect(this.engine._master);
      this.node.port.postMessage({ type: "watch", frames: 33075 });
      this.timer = setInterval(() => {
        try {
          this.control("heartbeat");
          if (this.current && !this.paused && !this.transitioning) {
            const audio = this.engine.decks[this.index].audio;
            if (Number.isFinite(audio.duration) && audio.duration - audio.currentTime < .8) {
              this.transition().catch(error => this.stop(error.message));
            }
          }
        } catch (error) { this.stop(error.message); }
      }, 250);
      const run = this.run;
      await this.renderClock();
      if (run !== this.run) return;
      await this.transition();
    } else if (data.type === "return-ready" && this.returning) {
      this.fadeAndRelease();
    } else if (data.type === "credit") {
      this.transport?.credit(data);
      if (Number.isSafeInteger(data.limit_frame) && data.limit_frame > this.progressLimit) {
        this.progressLimit = data.limit_frame;
        this.node?.port.postMessage({ type: "watch", frames: 33075 });
      }
    } else if (data.type === "renewed") {
      this.occurrences = data.occurrences || this.occurrences;
      if (data.state === "local" && !this.returning) this.change("local", "Local control. Monitor level affects this browser only.");
    }
  }

  capture(data) {
    if (data.type === "clock") {
      this.clock = data;
      this.clockWait?.resolve();
      return;
    }
    if (data.type === "failed") { this.stop(data.reason); return; }
    if (data.type === "marked") {
      const pending = this.markers.get(data.event.sequence);
      if (!pending) return;
      this.markers.delete(data.event.sequence);
      clearTimeout(pending.timer);
      try {
        this.control("marker", { event: data.event });
        pending.resolve();
      } catch (error) { pending.reject(error); }
      return;
    }
    if (data.type === "pcm") {
      // Hold only the worklet's 32 existing loaned buffers; never grow the pool.
      if (this.captureQueue.length >= 32) { this.stop("Programme capture queue exhausted"); return; }
      this.captureQueue.push(data);
      if (this.sendTimer === null) this.sendCaptured();
    }
  }

  sendCaptured() {
    this.sendTimer = null;
    const now = performance.now();
    // Absolute elapsed time avoids accumulating timer rounding as queue delay.
    // 8.2 ms also accounts for the packet header in the receiver's byte budget.
    // At most two sends may catch up in one callback; there is no new PCM pool.
    this.sendTokens = Math.min(2, this.sendTokens + Math.max(0, now - this.sendBudgetAt) / 8.2);
    this.sendBudgetAt = now;
    while (this.captureQueue.length && this.sendTokens >= 1) {
      const data = this.captureQueue.shift();
      if (!this.transport?.send(data.buffer, data.firstFrame)) {
        this.stop(this.transport?.reason || "Programme sender is unavailable");
        return;
      }
      this.sendTokens--;
      try {
        this.node.port.postMessage({ type: "recycle", slot: data.slot, buffer: data.buffer }, [data.buffer]);
      } catch (_) {
        this.stop("Programme capture recycle failed");
        return;
      }
    }
    if (this.captureQueue.length) {
      this.sendTimer = setTimeout(() => this.sendCaptured(), Math.ceil((1 - this.sendTokens) * 8.2));
    }
  }

  renderClock(refresh = false) {
    if (refresh) {
      this.clock = null;
      this.node.port.postMessage({ type: "clock" });
    }
    if (this.clock) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const finish = error => {
        clearTimeout(timer);
        this.clockWait = null;
        if (error) reject(error); else resolve();
      };
      const timer = setTimeout(() => finish(new Error("Programme render clock timed out")), 1000);
      this.clockWait = { resolve: () => finish(), reject: error => finish(error) };
    });
  }

  async marker(kind, occurrence, at) {
    if (!this.clock) throw new Error("Programme render clock is unavailable");
    const frame = Math.round(this.clock.frame + (at - this.clock.time) * 44100);
    const event = { sequence: this.sequence++, kind, occurrence, frame };
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.markers.delete(event.sequence);
        reject(new Error("Programme render marker timed out"));
      }, 500);
      this.markers.set(event.sequence, { resolve, reject, timer });
      this.node.port.postMessage({ type: "mark", event });
    });
    if (this.engine._ctx.currentTime >= at) throw new Error("Programme render marker arrived too late");
  }

  async transition() {
    if (this.transitioning || this.paused || this.returning || this.returnRequested) return;
    const occurrence = this.occurrences.find(item => !this.used.has(item.id));
    if (!occurrence) throw new Error("No reserved song is ready; returning to automation");
    this.transitioning = true;
    const run = this.run;
    const index = this.current ? this.index ^ 1 : 0;
    const deck = this.engine.decks[index];
    const context = this.engine._ctx;
    const old = this.current;
    const oldIndex = this.index;
    try {
      deck.gain.gain.cancelScheduledValues(context.currentTime);
      deck.gain.gain.setValueAtTime(0, context.currentTime);
      this.engine.setSrcOnDeck(deck, occurrence.path);
      deck.audio.currentTime = 0;
      await mediaReady(deck.audio, this.abort.signal);
      if (this.node) await this.renderClock(true);
      if (this.returning) { deck.audio.pause(); this.transitioning = false; return; }
      if (!this.grant || run !== this.run) throw new Error("Local control ended during media preparation");
      const at = context.currentTime + .25;
      await this.marker("started", occurrence.id, at);
      if (!this.grant || run !== this.run || this.returning) throw new Error("Local control ended during render preparation");
      if (old) await this.marker("ended", old.id, at + .25);
      if (!this.grant || run !== this.run || this.returning) throw new Error("Local control ended during render preparation");
      deck.gain.gain.setValueAtTime(0, at);
      deck.gain.gain.linearRampToValueAtTime(1, at + .25);
      if (old) {
        const outgoing = this.engine.decks[oldIndex].gain.gain;
        outgoing.cancelScheduledValues(at);
        outgoing.setValueAtTime(1, at);
        outgoing.linearRampToValueAtTime(0, at + .25);
      }
      this.used.add(occurrence.id);
      // Only the two held occurrences need client deduplication.
      for (const id of this.used) if (!this.occurrences.some(item => item.id === id)) this.used.delete(id);
      this.current = occurrence;
      this.index = index;
      this.finishTimer = setTimeout(() => {
        if (old) this.engine.decks[oldIndex].audio.pause();
        this.transitioning = false;
        if (this.returnRequested) this.release();
      }, Math.max(0, at + .25 - context.currentTime) * 1000);
    } catch (error) {
      if (run !== this.run) return;
      deck.audio.pause();
      this.transitioning = false;
      throw error;
    }
  }

  async togglePause() {
    if (!this.current || this.transitioning || this.returning || this.pausePending) return this.paused;
    this.pausePending = true;
    const run = this.run;
    const deck = this.engine.decks[this.index];
    try {
      clearTimeout(this.pauseTimer);
      if (this.paused) await mediaReady(deck.audio, this.abort.signal);
      if (run !== this.run || this.returning) return this.paused;
      const at = this.engine._ctx.currentTime + .15;
      await this.marker(this.paused ? "resumed" : "paused", this.current.id, at);
      if (run !== this.run || this.returning) return this.paused;
      this.paused = !this.paused;
      deck.gain.gain.setValueAtTime(this.paused ? 0 : 1, at);
      if (this.paused) this.pauseTimer = setTimeout(() => deck.audio.pause(), 170);
      return this.paused;
    } catch (error) {
      if (run !== this.run) return this.paused;
      throw error;
    } finally {
      if (run === this.run) {
        this.pausePending = false;
        if (this.returnRequested) this.release();
      }
    }
  }

  release() {
    if (!this.grant) { this.stop("Local control cancelled"); return; }
    if (this.returning) return;
    this.change("returning", "Returning to automation");
    if (this.transitioning || this.pausePending) {
      this.returnRequested = true;
      return;
    }
    this.returnRequested = false;
    this.returning = true;
    try { this.control("prepare-return"); }
    catch (error) { this.stop(error.message); }
  }

  fadeAndRelease() {
    if (this.grant) {
      const end = this.engine._ctx.currentTime + .02;
      for (const deck of this.engine.decks) {
        deck.gain.gain.cancelScheduledValues(this.engine._ctx.currentTime);
        deck.gain.gain.linearRampToValueAtTime(0, end);
      }
      const frame = this.clock ? Math.ceil(this.clock.frame + (end - this.clock.time) * 44100) : 0;
      this.control("release", { frame });
    }
  }

  async returnToAutomation() {
    const url = this.statusUrl;
    if (!url) { this.stop(); return; }
    if (this.owned) this.release();
    else this.stop("Local control cancelled");
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
      const status = await requestJson(url);
      if (status.state === "automation") {
        this.stop(undefined, { notify: false });
        this.change("automation", "Automation");
        return;
      }
    }
    throw new Error("Automation has not recovered. Stay on this station and retry Return.");
  }

  stop(reason = "Local control ended", { notify = true } = {}) {
    const wasOpen = this.socket !== null || this.taking;
    this.run++;
    this.taking = false;
    this.returning = false;
    this.returnRequested = false;
    const socket = this.socket;
    this.socket = null;
    if (socket) { socket.onclose = null; socket.onerror = null; socket.close(); }
    this.abort.abort();
    this.clockWait?.reject(new Error(reason));
    clearInterval(this.timer);
    clearTimeout(this.sendTimer);
    this.sendTimer = null;
    this.captureQueue = [];
    for (const timer of [this.finishTimer, this.pauseTimer, this.releaseTimer]) clearTimeout(timer);
    for (const pending of this.markers.values()) { clearTimeout(pending.timer); pending.reject(new Error(reason)); }
    this.markers.clear();
    if (this.node) {
      this.node.port.onmessage = null;
      this.node.disconnect();
      try { this.sum.disconnect(this.node); } catch (_) { /* Never attached. */ }
      this.sum.connect(this.engine._master);
      this.node = null;
    }
    this.engine?.endLocalProgramme();
    this.grant = null;
    this.transport = null;
    this.current = null;
    this.transitioning = false;
    this.paused = false;
    this.pausePending = false;
    if (wasOpen && notify) this.change("recovering", reason);
  }
}
