# Behavior and Accessibility Remediation Design

**Status:** Approved direction

**Goal:** Correct confirmed playback, selection, persistence, browser-state, HTTP-media, keyboard, and screen-reader defects without adding unrelated DJ effects.

## Selection and analysis correctness

Key estimation will replace binary rotated pitch sets with distinct weighted major and minor profiles. Synthetic chroma fixtures must distinguish representative major and minor keys and preserve `-1` for insufficient evidence.

Librosa tempo result will be returned with extra metadata and assigned when trusted tags or beets metadata do not provide BPM. Unknown BPM under configured hard range will be excluded rather than silently admitted; UI will explain unknown-value exclusion.

Smart shuffle will query FAISS using negated normalized vector so it retrieves globally distant candidates. Genre, harmonic, and other hard filters will progressively expand search until valid matches are found or entire index is exhausted. Preference relaxation will be explicit and observable, never silent.

## Persistent state

Runtime state restoration will use typed, field-specific readers rather than casting every DJ-mix value to boolean. Saved schema will version its payload. Every field emitted by settings serialization must either restore with validation or be intentionally marked session-only.

Round-trip tests will cover harmonic mode, queue seed behavior, beat/key synchronization, prefetch, silence detection, liner settings, null-clearing behavior, and unknown future fields.

## Media serving

Audio file reads will not block asyncio event loop. Starlette threadpool-backed synchronous iterators or bounded `asyncio.to_thread` reads will provide full and ranged streaming with disconnect cancellation.

Range parsing will implement standard single-range forms: `start-end`, `start-`, and `-suffixLength`. Unsatisfiable or multi-range requests return 416 with correct `Content-Range`.

ALAC path will check ffmpeg availability before constructing streaming response. Missing ffmpeg selects documented raw fallback before response headers are sent.

## Frontend interaction

Hotkeys will ignore events from native interactive controls unless shortcut is explicitly compatible. Buttons retain Space/Enter activation; range controls retain arrows; tablist retains APG navigation. Global transport shortcuts remain available from noninteractive Now Playing content.

Voice-liner counter will call imported `bumpLinerTrackCount` on distinct track transitions. Repeated state messages for same track must not increment count.

Seek dragging will suppress progress rendering only. `pointerup`, `pointercancel`, and `lostpointercapture` clear drag state. Other WebSocket state continues applying during drag.

Lyrics requests will carry track path plus monotonically increasing request generation and AbortController. Stale responses cannot replace current lyrics.

Fetch helpers will validate transport status and application payload, restore disabled controls in `finally`, announce failures accurately, and reconcile optimistic queue changes after error.

## Accessibility

Now-playing album, BPM, key, and energy remain persistent browseable text. Decorative visual badges may stay hidden from accessibility tree to avoid duplicate speech.

Cue points receive persistent summary associated with seek control. Summary includes count and useful cue labels/times, updates on track change, and remains browseable after live announcement clears.

Liner delete controls expose action plus filename. Removing final queue item moves focus to stable queue status or heading. Status/error messages use appropriate polite or assertive live regions without duplicate announcements.

Connected-status text and meaningful control boundaries will meet WCAG 2.2 AA contrast. Reduced-motion preference disables smooth lyrics scrolling and nonessential transitions.

## Module boundaries

Changes will avoid a wholesale frontend rewrite. Touched behavior may move from `app.js` into focused existing modules or new small modules when doing so creates a direct test seam. Audio-engine internals remain stable unless required by confirmed defect.

## Error handling

- No eligible selection returns a clear selection error rather than violating hard filters.
- Invalid saved fields log one warning and retain configured default.
- Failed HTTP actions restore operability and announce failure, never success.
- Stale async frontend results are discarded silently; current request owns visible status.
- Streaming disconnect closes file/process resources promptly.

## Testing

- Synthetic major/minor profiles, BPM fallback, >200-track smart shuffle, and progressively expanded filter fixtures.
- Full serialized-state round trips for every persisted setting.
- Range-request integration tests including suffixes and missing ffmpeg.
- DOM tests dispatching keys on buttons, ranges, tabs, text fields, and static content.
- Distinct-track liner cadence, pointer cancellation, stale lyrics, failed fetch, optimistic queue rollback, and focus-restoration tests.
- Automated semantic, keyboard, live-region, reduced-motion, and contrast assertions.
- NVDA/browser validation when automation is available; otherwise record assistive-technology validation limitation without claiming human testing.

## Out of scope

New transition effects, cloud lyrics, social features, and visual redesign are deferred until correctness and accessibility gates pass.
