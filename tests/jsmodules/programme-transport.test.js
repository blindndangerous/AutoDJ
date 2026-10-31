import { expect, it, vi } from "vitest";
import { ProgrammeTransport } from "../../src/autodj/static/modules/programme-transport.js";

function fixture() {
  const socket = { readyState: 1, bufferedAmount: 0, send: vi.fn(), close: vi.fn() };
  return { socket, sender: new ProgrammeTransport(socket, { generation: 7, limit_frame: 8820 }) };
}

// ADR-0019-14
it("encodes exactly one little endian stereo packet and never overspends credit", () => {
  const { socket, sender } = fixture();
  for (let i = 0; i < 20; i++) expect(sender.send(new Float32Array(882).buffer, i * 441)).toBe(true);
  const view = new DataView(socket.send.mock.calls[0][0]);
  expect(view.byteLength).toBe(3576);
  expect(view.getBigUint64(8, true)).toBe(7n);
  expect(view.getUint32(36, true)).toBe(44100);
  expect(sender.send(new Float32Array(882).buffer, 8820)).toBe(false);
  expect(socket.send).toHaveBeenCalledTimes(20);
  expect(socket.close).toHaveBeenCalledOnce();
});

// ADR-0019-14
it("uses cumulative consumption credit and stops on socket backpressure", () => {
  const { socket, sender } = fixture();
  sender.credit({ type: "credit", limit_frame: 9261 });
  sender.credit({ type: "credit", limit_frame: 8820 });
  expect(sender.limitFrame).toBe(9261);
  socket.bufferedAmount = 20000;
  expect(sender.send(new Float32Array(882).buffer, 0)).toBe(false);
  expect(socket.send).not.toHaveBeenCalled();
  expect(sender.reason).toContain("backpressure");
});
