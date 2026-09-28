import { describe, expect, it, vi } from "vitest";

import { installSeekController } from
  "../../src/autodj/static/modules/seek-controller.js";

function pointerEvent(pointerId, overrides = {}) {
  return {
    button: 0,
    clientX: 50,
    clientY: 10,
    isPrimary: true,
    pointerId,
    preventDefault: vi.fn(),
    ...overrides,
  };
}

function fakeTrack() {
  const handlers = new Map();
  return {
    addEventListener: vi.fn((type, handler) => handlers.set(type, handler)),
    emit(type, event) {
      return handlers.get(type)(event);
    },
    getBoundingClientRect: vi.fn(() => ({
      bottom: 20, left: 0, right: 100, top: 0,
    })),
    releasePointerCapture: vi.fn(),
    setPointerCapture: vi.fn(),
  };
}

describe("seek pointer ownership", () => {
  it("captures and previews one primary pointer while ignoring every other pointer", () => {
    const track = fakeTrack();
    const preview = vi.fn();
    const commit = vi.fn();
    const controller = installSeekController(track, { preview, commit });
    const down = pointerEvent(7);

    track.emit("pointerdown", down);
    track.emit("pointerdown", pointerEvent(8));
    track.emit("pointermove", pointerEvent(8));
    const ownedMove = pointerEvent(7);
    track.emit("pointermove", ownedMove);
    track.emit("pointerup", pointerEvent(8));

    expect(track.setPointerCapture).toHaveBeenCalledOnce();
    expect(track.setPointerCapture).toHaveBeenCalledWith(7);
    expect(preview).toHaveBeenCalledTimes(2);
    expect(preview).toHaveBeenNthCalledWith(1, down);
    expect(preview).toHaveBeenNthCalledWith(2, ownedMove);
    expect(down.preventDefault).toHaveBeenCalledOnce();
    expect(commit).not.toHaveBeenCalled();
    expect(controller.isDragging()).toBe(true);
  });

  it("ignores non-primary and non-left pointer starts", () => {
    const track = fakeTrack();
    const preview = vi.fn();
    const controller = installSeekController(track, { preview, commit: vi.fn() });

    track.emit("pointerdown", pointerEvent(1, { isPrimary: false }));
    track.emit("pointerdown", pointerEvent(2, { button: 2 }));

    expect(track.setPointerCapture).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
    expect(controller.isDragging()).toBe(false);
  });

  it("seeks once when released over the track", () => {
    const track = fakeTrack();
    let controller;
    const commit = vi.fn(() => {
      expect(controller.isDragging()).toBe(false);
    });
    controller = installSeekController(track, { preview: vi.fn(), commit });

    track.emit("pointerdown", pointerEvent(11));
    const up = pointerEvent(11);
    track.emit("pointerup", up);
    track.emit("pointerup", up);

    expect(commit).toHaveBeenCalledOnce();
    expect(commit).toHaveBeenCalledWith(up);
    expect(track.releasePointerCapture).toHaveBeenCalledOnce();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(11);
  });

  it("does not seek when released outside the track bounds", () => {
    const track = fakeTrack();
    const commit = vi.fn();
    const controller = installSeekController(track, { preview: vi.fn(), commit });

    track.emit("pointerdown", pointerEvent(35));
    track.emit("pointerup", pointerEvent(35, { clientX: 101 }));

    expect(commit).not.toHaveBeenCalled();
    expect(controller.isDragging()).toBe(false);
  });

  it("cancels an owned pointer without committing", () => {
    const track = fakeTrack();
    const commit = vi.fn();
    const controller = installSeekController(track, { preview: vi.fn(), commit });

    track.emit("pointerdown", pointerEvent(12));
    controller.cancel();
    track.emit("pointerup", pointerEvent(12));

    expect(commit).not.toHaveBeenCalled();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(12);
    expect(controller.isDragging()).toBe(false);
  });

  it.each(["pointercancel", "lostpointercapture"])(
    "%s drops the drag without committing",
    (eventType) => {
      const track = fakeTrack();
      const commit = vi.fn();
      const controller = installSeekController(track, { preview: vi.fn(), commit });

      track.emit("pointerdown", pointerEvent(13));
      track.emit(eventType, pointerEvent(13));
      track.emit("pointerup", pointerEvent(13));

      expect(commit).not.toHaveBeenCalled();
      expect(controller.isDragging()).toBe(false);
    },
  );
});
