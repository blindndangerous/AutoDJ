// Stereo programme capture and monitor watchdog. The owner module supplies the
// socket and consumption credit. This processor never waits or grows
// its transferable pool when the main thread falls behind.
class ProgrammeCapture extends AudioWorkletProcessor {
  constructor(options = {}) {
    super();
    if (sampleRate !== 44100) throw new Error("Local control requires 44,100 Hz");
    // 320 ms of fixed transfer storage covers the 200 ms jitter budget with
    // margin for message delivery and recycling. Server credit stays separate.
    this.slots = Array.from({ length: 32 }, () => new Float32Array(441 * 2));
    this.outstanding = new Set();
    this.slot = 0;
    this.used = 0;
    this.frame = 0;
    this.failed = false;
    this.deadline = Infinity;
    this.waiting = options.processorOptions?.waitForStart === true;
    this.port.onmessage = ({ data }) => {
      if (data.type === "start" && this.waiting) { this.waiting = false; return; }
      if (data.type === "clock") { this.clockPending = true; return; }
      if (data.type === "watch" && Number.isInteger(data.frames) && data.frames > 0
          && data.frames <= 44100 * 3) {
        this.deadline = this.frame + this.used + data.frames;
        this.clockPending = true;
        return;
      }
      if (data.type === "mark" && Number.isSafeInteger(data.event?.frame)
          && data.event.frame >= this.frame + this.used + 128
          && data.event.frame <= this.frame + this.used + 44100) {
        // This acknowledgement precedes the packet containing the marker.
        // The main thread sends it on the ordered socket before opening gain.
        this.port.postMessage({ type: "marked", event: data.event });
        return;
      }
      if (data.type !== "recycle" || !this.outstanding.has(data.slot)
          || !(data.buffer instanceof ArrayBuffer) || data.buffer.byteLength !== 441 * 8) return;
      this.slots[data.slot] = new Float32Array(data.buffer);
      this.outstanding.delete(data.slot);
    };
  }

  process(inputs, outputs = []) {
    for (const channel of outputs[0] || []) channel.fill(0);
    if (this.waiting) return true;
    if (this.failed) return true;
    if (this.clockPending) {
      this.clockPending = false;
      this.port.postMessage({ type: "clock", frame: this.frame + this.used, time: currentTime });
    }
    if (this.frame + this.used >= this.deadline) {
      this.failed = true;
      this.port.postMessage({ type: "failed", reason: "Local monitor lease expired" });
      return true;
    }
    const channels = inputs[0];
    // A disconnected input is zero PCM; a suspended context produces no packets.
    const frames = channels?.[0]?.length || 128;
    for (let i = 0; i < frames; i++) {
      if (this.outstanding.has(this.slot)) {
        this.failed = true;
        this.port.postMessage({ type: "failed", reason: "Programme capture transfer pool exhausted" });
        return true;
      }
      const pcm = this.slots[this.slot];
      pcm[this.used * 2] = channels?.[0]?.[i] ?? 0;
      pcm[this.used * 2 + 1] = channels?.[1]?.[i] ?? 0;
      for (let ch = 0; ch < (outputs[0]?.length || 0); ch++) {
        outputs[0][ch][i] = channels?.[ch]?.[i] ?? 0;
      }
      if (++this.used === 441) {
        this.port.postMessage({ type: "pcm", slot: this.slot, firstFrame: this.frame,
          buffer: pcm.buffer }, [pcm.buffer]);
        this.outstanding.add(this.slot);
        this.slots[this.slot] = null;
        this.slot = (this.slot + 1) % this.slots.length;
        this.frame += 441;
        this.used = 0;
      }
    }
    return true;
  }
}

registerProcessor("programme-capture", ProgrammeCapture);
