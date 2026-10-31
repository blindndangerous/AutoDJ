// Independent PCM sender. Ownership UI and graph attachment come in step 4.
export class ProgrammeTransport {
  constructor(socket, grant) {
    this.socket = socket;
    this.generation = BigInt(grant.generation);
    this.limitFrame = grant.limit_frame;
    this.nextFrame = 0;
    this.sequence = 0;
    this.reason = "";
    this.preparing = grant.state === "preparing";
  }

  get waitingForCredit() {
    return this.preparing && !this.reason && this.nextFrame + 441 > this.limitFrame;
  }

  credit(message) {
    if (message.type === "credit" && Number.isSafeInteger(message.limit_frame)
        && message.limit_frame >= this.limitFrame) {
      this.limitFrame = message.limit_frame;
      if (message.state === "local") this.preparing = false;
    }
  }

  fail(reason) {
    if (!this.reason) {
      this.reason = reason;
      this.socket.close(4000, reason);
    }
    return false;
  }

  send(buffer, firstFrame) {
    if (this.reason) return false;
    if (this.socket.readyState !== 1) return this.fail("Programme socket is not open");
    if (this.socket.bufferedAmount + 3576 > 21216) return this.fail("Programme socket backpressure");
    if (!(buffer instanceof ArrayBuffer) || buffer.byteLength !== 3528
        || firstFrame !== this.nextFrame) return this.fail("Invalid programme capture packet");
    if (this.nextFrame + 441 > this.limitFrame) return this.fail("Programme credit exhausted");
    const packet = new ArrayBuffer(3576);
    const view = new DataView(packet);
    new Uint8Array(packet, 0, 4).set([65, 68, 74, 80]); // ADJP
    view.setUint8(4, 1);
    view.setUint16(6, 2, true);
    view.setBigUint64(8, this.generation, true);
    view.setBigUint64(16, BigInt(this.sequence), true);
    view.setBigUint64(24, BigInt(firstFrame), true);
    view.setUint32(32, 441, true);
    view.setUint32(36, 44100, true);
    const samples = new Float32Array(buffer);
    for (let i = 0; i < samples.length; i++) view.setFloat32(48 + i * 4, samples[i], true);
    try { this.socket.send(packet); }
    catch (_) { return this.fail("Programme socket send failed"); }
    this.nextFrame += 441;
    this.sequence++;
    return true;
  }
}
