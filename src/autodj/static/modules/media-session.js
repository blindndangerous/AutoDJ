// Media Session API: OS media keys, lock-screen art, and the
// notification-shade transport pill on Chromium / WebKit / Firefox.

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

// Wire OS media-key actions.  Caller passes the play handler so this
// module stays decoupled from playbackEnabled / unlockAndPlay state
// owned by the audio-engine module.
export function installMediaActionHandlers({ onPlay, onPauseOrSkipNext } = {}) {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.setActionHandler("play", () => {
    if (typeof onPlay === "function") onPlay();
    else fetch("/api/pause", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    fetch("/api/pause", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("nexttrack", () => {
    if (typeof onPauseOrSkipNext === "function") onPauseOrSkipNext();
    else fetch("/api/skip", { method: "POST" });
  });
  navigator.mediaSession.setActionHandler("previoustrack", null);
}
