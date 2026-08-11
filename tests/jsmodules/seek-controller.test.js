import { describe, expect, it, vi } from "vitest";

import { installSeekController } from
  "../../src/autodj/static/modules/seek-controller.js";

function pointerEvent(pointerId, overrides = {}) {
  return {
    button: 0,
    isPrimary: true,
    pointerId,
    preventDefault: vi.fn(),
    ...overrides,
  };
}

function fakeTrack() {
  const handlers = new Map();
  const documentHandlers = new Map();
  const windowHandlers = new Map();
  const defaultView = {
    addEventListener: vi.fn((type, handler) => windowHandlers.set(type, handler)),
    removeEventListener: vi.fn((type, handler) => {
      if (windowHandlers.get(type) === handler) windowHandlers.delete(type);
    }),
  };
  const ownerDocument = {
    addEventListener: vi.fn((type, handler) => documentHandlers.set(type, handler)),
    defaultView,
    removeEventListener: vi.fn((type, handler) => {
      if (documentHandlers.get(type) === handler) documentHandlers.delete(type);
    }),
    visibilityState: "visible",
  };
  return {
    addEventListener: vi.fn((type, handler) => handlers.set(type, handler)),
    emit(type, event) {
      return handlers.get(type)(event);
    },
    emitOutside(type, event) {
      return documentHandlers.get(type)?.(event);
    },
    emitWindow(type, event = {}) {
      return windowHandlers.get(type)?.(event);
    },
    getBoundingClientRect: vi.fn(() => ({
      bottom: 20, left: 0, right: 100, top: 0,
    })),
    ownerDocument,
    removeEventListener: vi.fn((type, handler) => {
      if (handlers.get(type) === handler) handlers.delete(type);
    }),
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
    const controller = installSeekController(track, {
      preview,
      commit: vi.fn(),
    });

    track.emit("pointerdown", pointerEvent(1, { isPrimary: false }));
    track.emit("pointerdown", pointerEvent(2, { button: 2 }));

    expect(track.setPointerCapture).not.toHaveBeenCalled();
    expect(preview).not.toHaveBeenCalled();
    expect(controller.isDragging()).toBe(false);
  });

  it("clears ownership before committing and cannot re-enter through lost capture", () => {
    const track = fakeTrack();
    let controller;
    const commit = vi.fn(() => {
      expect(controller.isDragging()).toBe(false);
    });
    controller = installSeekController(track, { preview: vi.fn(), commit });
    track.releasePointerCapture.mockImplementation((pointerId) => {
      track.emit("lostpointercapture", pointerEvent(pointerId));
    });

    track.emit("pointerdown", pointerEvent(11));
    const up = pointerEvent(11);
    track.emit("pointerup", up);
    track.emit("pointerup", up);

    expect(commit).toHaveBeenCalledOnce();
    expect(commit).toHaveBeenCalledWith(up);
    expect(track.releasePointerCapture).toHaveBeenCalledOnce();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(11);
    expect(controller.isDragging()).toBe(false);
  });

  it("cancels an owned pointer without committing", () => {
    const track = fakeTrack();
    const commit = vi.fn();
    const controller = installSeekController(track, {
      preview: vi.fn(),
      commit,
    });

    track.emit("pointerdown", pointerEvent(12));
    controller.cancel();
    track.emit("pointerup", pointerEvent(12));

    expect(commit).not.toHaveBeenCalled();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(12);
    expect(controller.isDragging()).toBe(false);
  });

  it.each(["pointercancel", "lostpointercapture"])(
    "%s clears the owned pointer without committing",
    (eventType) => {
      const track = fakeTrack();
      const commit = vi.fn();
      const controller = installSeekController(track, {
        preview: vi.fn(),
        commit,
      });

      track.emit("pointerdown", pointerEvent(13));
      track.emit(eventType, pointerEvent(13));
      track.emit("pointerup", pointerEvent(13));

      expect(commit).not.toHaveBeenCalled();
      expect(controller.isDragging()).toBe(false);
    },
  );

  it("retains ownership when pointer capture fails", () => {
    const track = fakeTrack();
    track.setPointerCapture.mockImplementation(() => {
      throw new Error("capture unavailable");
    });
    const preview = vi.fn();
    const commit = vi.fn();
    const controller = installSeekController(track, { preview, commit });

    track.emit("pointerdown", pointerEvent(17));
    track.emit("pointermove", pointerEvent(17));
    track.emit("pointerup", pointerEvent(17));

    expect(preview).toHaveBeenCalledTimes(2);
    expect(commit).toHaveBeenCalledOnce();
    expect(controller.isDragging()).toBe(false);
  });

  it("cancels from the document when pointer capture fails outside the track", () => {
    const track = fakeTrack();
    track.setPointerCapture.mockImplementation(() => {
      throw new Error("capture unavailable");
    });
    const commit = vi.fn();
    const controller = installSeekController(track, {
      preview: vi.fn(),
      commit,
    });

    track.emit("pointerdown", pointerEvent(18));
    const outsideUp = pointerEvent(18);
    track.emitOutside("pointerup", outsideUp);

    expect(commit).not.toHaveBeenCalled();
    expect(controller.isDragging()).toBe(false);
    expect(track.ownerDocument.removeEventListener).toHaveBeenCalled();
  });

  it.each(["blur", "visibilitychange"])(
    "%s cancels an active pointer without committing",
    (eventType) => {
      const track = fakeTrack();
      const commit = vi.fn();
      const controller = installSeekController(track, {
        preview: vi.fn(),
        commit,
      });
      track.emit("pointerdown", pointerEvent(31));

      if (eventType === "blur") {
        track.emitWindow("blur");
      } else {
        track.ownerDocument.visibilityState = "hidden";
        track.emitOutside("visibilitychange", {});
      }
      track.emit("pointerup", pointerEvent(31));

      expect(commit).not.toHaveBeenCalled();
      expect(track.releasePointerCapture).toHaveBeenCalledOnce();
      expect(controller.isDragging()).toBe(false);
    },
  );

  it("cancels a captured pointer released outside the track bounds", () => {
    const track = fakeTrack();
    const commit = vi.fn();
    const controller = installSeekController(track, {
      preview: vi.fn(),
      commit,
    });

    track.emit("pointerdown", pointerEvent(35, { clientX: 50, clientY: 10 }));
    track.emit("pointerup", pointerEvent(35, { clientX: 101, clientY: 10 }));

    expect(commit).not.toHaveBeenCalled();
    expect(track.releasePointerCapture).toHaveBeenCalledOnce();
    expect(controller.isDragging()).toBe(false);
  });

  it("replacing an installed controller cancels it and removes its listeners", () => {
    const track = fakeTrack();
    const firstPreview = vi.fn();
    const firstCommit = vi.fn();
    const first = installSeekController(track, {
      preview: firstPreview,
      commit: firstCommit,
    });
    track.emit("pointerdown", pointerEvent(41));

    const secondPreview = vi.fn();
    const secondCommit = vi.fn();
    const second = installSeekController(track, {
      preview: secondPreview,
      commit: secondCommit,
    });
    track.emit("pointerdown", pointerEvent(42));
    track.emit("pointerup", pointerEvent(42));

    expect(first.isDragging()).toBe(false);
    expect(firstCommit).not.toHaveBeenCalled();
    expect(firstPreview).toHaveBeenCalledOnce();
    expect(secondPreview).toHaveBeenCalledOnce();
    expect(secondCommit).toHaveBeenCalledOnce();
    expect(track.removeEventListener).toHaveBeenCalled();
    expect(track.ownerDocument.defaultView.removeEventListener).toHaveBeenCalled();
    expect(second.isDragging()).toBe(false);
  });

  it("clears ownership and releases capture when the initial preview throws", () => {
    const track = fakeTrack();
    const previewError = new Error("preview failed");
    const controller = installSeekController(track, {
      preview: () => { throw previewError; },
      commit: vi.fn(),
    });

    const down = pointerEvent(18);
    expect(() => track.emit("pointerdown", down)).toThrow(previewError);
    expect(down.preventDefault).toHaveBeenCalledOnce();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(18);
    expect(controller.isDragging()).toBe(false);
  });

  it("clears ownership and releases once when an owned move preview throws", () => {
    const track = fakeTrack();
    const previewError = new Error("move preview failed");
    const preview = vi.fn()
      .mockImplementationOnce(() => {})
      .mockImplementationOnce(() => { throw previewError; });
    const commit = vi.fn();
    const controller = installSeekController(track, { preview, commit });
    track.releasePointerCapture.mockImplementation((pointerId) => {
      track.emit("lostpointercapture", pointerEvent(pointerId));
    });
    track.emit("pointerdown", pointerEvent(20));

    expect(() => track.emit("pointermove", pointerEvent(20))).toThrow(previewError);
    track.emit("pointerup", pointerEvent(20));

    expect(controller.isDragging()).toBe(false);
    expect(track.releasePointerCapture).toHaveBeenCalledOnce();
    expect(track.releasePointerCapture).toHaveBeenCalledWith(20);
    expect(commit).not.toHaveBeenCalled();
  });

  it("clears state and releases capture when commit throws, preserving the commit error", () => {
    const track = fakeTrack();
    const commitError = new Error("commit failed");
    track.releasePointerCapture.mockImplementation(() => {
      throw new Error("release failed");
    });
    const controller = installSeekController(track, {
      preview: vi.fn(),
      commit: () => { throw commitError; },
    });

    track.emit("pointerdown", pointerEvent(19));

    expect(() => track.emit("pointerup", pointerEvent(19))).toThrow(commitError);
    expect(track.releasePointerCapture).toHaveBeenCalledWith(19);
    expect(controller.isDragging()).toBe(false);
  });
});
