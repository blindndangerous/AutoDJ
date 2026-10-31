// Test-only instrumentation. Served worklet source is changed only by Playwright.
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";

export async function instrumentCapture(page, output) {
    let source = await readFile("src/autodj/static/programme-worklet.js", "utf8");
    const replace = (from, to) => {
      assert.ok(source.includes(from), `Worklet diagnostic hook missing: ${from}`);
      source = source.replace(from, to);
    };
    replace("this.outstanding = new Set();", `this.outstanding = new Set();
      this.auditSent = new Map(); this.auditMax = 0;
      this.auditPosted = 0; this.auditRecycled = 0;`);
    replace("this.outstanding.delete(data.slot);", `this.outstanding.delete(data.slot);
      this.auditRecycled++;
      this.port.postMessage({type: "audit-recycle", audioTime: currentTime,
        milliseconds: (currentTime - this.auditSent.get(data.slot)) * 1000,
        outstanding: this.outstanding.size, maximum: this.auditMax,
        posted: this.auditPosted, recycled: this.auditRecycled});
      this.auditSent.delete(data.slot);`);
    replace("this.outstanding.add(this.slot);", `this.outstanding.add(this.slot);
      this.auditPosted++; this.auditSent.set(this.slot, currentTime);
      this.auditMax = Math.max(this.auditMax, this.outstanding.size);`);
    replace('this.port.postMessage({ type: "failed", reason: "Programme capture transfer pool exhausted" });',
      `this.port.postMessage({type: "audit-exhaustion", audioTime: currentTime,
        maximum: this.auditMax, outstanding: this.outstanding.size,
        posted: this.auditPosted, recycled: this.auditRecycled,
        ages: [...this.auditSent].map(([slot, time]) => ({slot, milliseconds: (currentTime-time)*1000}))});
       this.port.postMessage({ type: "failed", reason: "Programme capture transfer pool exhausted" });`);
    await writeFile(output, source);
  await page.addInitScript(() => {
    const capture = window.captureDiagnostics = { recycle: [], longTasks: [], timeline: [], maximum: 0,
      recentSends: [], maximumSendGap: 0 };
    const originalSend = window.WebSocket.prototype.send;
    window.WebSocket.prototype.send = function (data) {
      if (data instanceof ArrayBuffer && data.byteLength === 3576 && capture.endedAt === undefined) {
        const time = window.performance.now();
        const previous = capture.recentSends.at(-1);
        if (previous) capture.maximumSendGap = Math.max(capture.maximumSendGap, time - previous.time);
        capture.recentSends.push({time, sequence: Number(new DataView(data).getBigUint64(16, true))});
        if (capture.recentSends.length > 128) capture.recentSends.shift();
      }
      return originalSend.call(this, data);
    };
    new window.PerformanceObserver(list => {
      for (const entry of list.getEntries()) capture.longTasks.push({ start: entry.startTime, duration: entry.duration });
    }).observe({ type: "longtask", buffered: true });
    const Original = window.AudioWorkletNode;
    window.AudioWorkletNode = class extends Original {
      constructor(context, name, options) {
        super(context, name, options);
        if (name !== "programme-capture") return;
        this.port.addEventListener("message", ({data}) => {
          if (!data.type?.startsWith("audit-")) return;
          const receivedAt = window.performance.now();
          // A disconnected processor may finish queued callbacks. Their loans
          // belong to an ended session and cannot diagnose the original loss.
          if (capture.endedAt !== undefined) return;
          capture.maximum = Math.max(capture.maximum, data.maximum);
          if (data.type === "audit-recycle") {
            capture.recycle.push(data.milliseconds);
            if (!capture.timeline.length || receivedAt - capture.timeline.at(-1).receivedAt >= 1000) {
              capture.timeline.push({...data, receivedAt, contextTime: context.currentTime});
            }
          } else capture.exhaustion = {...data, receivedAt, contextTime: context.currentTime};
        });
      }
    };
  });
}

export async function captureReport(page) {
  return page.evaluate(() => {
    const data = window.captureDiagnostics;
    const sorted = data.recycle.toSorted((a, b) => a - b);
    const percentile = p => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))] ?? null;
    return {...data, recycle: undefined, recycle_ms: { count: sorted.length,
      p50: percentile(.5), p99: percentile(.99), max: sorted.at(-1) ?? null },
    clock_note: "PCM post to recycle receipt on worklet audio clock; resolution 128/44100 seconds. Main-thread times use performance.now().",
    nearby_long_tasks: data.exhaustion ? data.longTasks.filter(task =>
      Math.abs(task.start + task.duration - data.exhaustion.receivedAt) < 1000) : []};
  });
}
