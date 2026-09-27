// Media Session API: OS media keys, lock-screen art, and the
// notification-shade transport pill on Chromium / WebKit / Firefox.

import { requestJsonBestEffort } from "./api-client.js";

export function updateMediaSession(s) {
  if (!("mediaSession" in navigator)) return;
  const t = s.current_track;
  if (!t) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    return;
  }
  navigator.mediaSession.metadata = new MediaMetadata({
    title:  t.title || "",
    artist: t.artist || "",
    album:  t.album || "",
    artwork: [{
      src: "/api/art?path=" + encodeURIComponent(t.path),
      sizes: "512x512",
      type: "image/jpeg",
    }],
  });
  navigator.mediaSession.playbackState = s.is_paused ? "paused" : "playing";
  if (s.duration && s.elapsed != null) {
    try {
      navigator.mediaSession.setPositionState({
        duration: s.duration,
        position: Math.min(s.elapsed, s.duration),
        playbackRate: 1.0,
      });
    } catch (_) { /* not supported on every browser */ }
  }
}

// Wire OS media-key actions.  Caller passes the play and pause handlers
// so this module stays decoupled from playbackEnabled / unlockAndPlay
// state owned by the audio-engine module.  Each returns true when it
// handled the key (stream mode toggles Listen here); otherwise the
// server's pause toggle runs.
export function installMediaActionHandlers({
  isEnabled = () => true,
  onPlay,
  onPause,
  onPauseOrSkipNext,
  onRequestError,
} = {}) {
  if (!("mediaSession" in navigator)) return;
  if (typeof onRequestError !== "function") {
    throw new TypeError("installMediaActionHandlers requires onRequestError");
  }
  const fallback = (url) => requestJsonBestEffort(
    url, { method: "POST" }, onRequestError,
  );
  const pauseToggle = (callback) => async () => {
    if (!isEnabled()) return;
    let handled;
    try {
      handled = typeof callback === "function" ? await callback() : false;
    } catch (errorValue) {
      onRequestError(errorValue);
      return;
    }
    if (handled !== true) void fallback("/api/pause");
  };
  navigator.mediaSession.setActionHandler("play", pauseToggle(onPlay));
  navigator.mediaSession.setActionHandler("pause", pauseToggle(onPause));
  navigator.mediaSession.setActionHandler("nexttrack", async () => {
    if (!isEnabled()) return;
    if (typeof onPauseOrSkipNext !== "function") {
      void fallback("/api/skip");
      return;
    }
    try {
      await onPauseOrSkipNext();
    } catch (errorValue) {
      onRequestError(errorValue);
    }
  });
  navigator.mediaSession.setActionHandler("previoustrack", null);
}
