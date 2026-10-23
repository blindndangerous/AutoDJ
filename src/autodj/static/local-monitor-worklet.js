// Server PCM is 44.1 kHz; this ring bounds latency and linear interpolation
// adapts it to the browser AudioContext's output sample rate.
class AutodjMonitor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 8820; // 200 ms at 44.1 kHz
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.read = 0;
    this.write = 0;
    this.count = 0;
    this.phase = 0;
    this.volume = 1;
    this.muted = false;
    this.port.onmessage = ({ data }) => {
      if (data.level) {
        this.volume = data.volume;
        this.muted = data.muted;
        return;
      }
      const samples = new Float32Array(data);
      let start = 0;
      let frames = samples.length / 2;
      if (frames > this.capacity) {
        start = frames - this.capacity;
        frames = this.capacity;
      }
      const overflow = Math.max(0, this.count + frames - this.capacity);
      this.read = (this.read + overflow) % this.capacity;
      this.count -= overflow;
      for (let i = start; i < start + frames; i += 1) {
        this.left[this.write] = samples[i * 2];
        this.right[this.write] = samples[i * 2 + 1];
        this.write = (this.write + 1) % this.capacity;
      }
      this.count += frames;
      this.port.postMessage({ bufferedFrames: this.count });
    };
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    if (!output || output.length < 2) return true;
    const left = output[0];
    const right = output[1];
    const level = this.muted ? 0 : this.volume;
    const step = 44100 / sampleRate;
    for (let i = 0; i < left.length; i += 1) {
      if (this.count < 2) {
        left[i] = 0;
        right[i] = 0;
        continue;
      }
      const next = (this.read + 1) % this.capacity;
      const fraction = this.phase;
      left[i] = (this.left[this.read] + (this.left[next] - this.left[this.read]) * fraction) * level;
      right[i] = (this.right[this.read] + (this.right[next] - this.right[this.read]) * fraction) * level;
      this.phase += step;
      const consumed = Math.floor(this.phase);
      if (consumed) {
        const actual = Math.min(consumed, this.count - 1);
        this.read = (this.read + actual) % this.capacity;
        this.count -= actual;
        this.phase -= consumed;
      }
    }
    return true;
  }
}
registerProcessor("autodj-local-monitor", AutodjMonitor);
