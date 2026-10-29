# Inspect and navigate song landmarks

Read song structure in Track info and use the landmark shortcuts to preview or inspect what comes next.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Song structure and landmarks

A sighted DJ reads a song's shape from its waveform. AutoDJ gives the same picture as text: a
song's **landmarks** are its intro, the start of the singing, drops, breakdowns, build-ups, the
outro and the segue, plus every 32-beat phrase and the hot and memory cues named in DJ software.
They come from what AutoDJ already knows about the song, merged into one list:

- the cue points in Track info (Start, Intro, Outro and Segue), whoever set them;
- the DJ analysis: detected drops, breakdowns and phrases, the beat grid (for bar numbers), and the
  cues imported from Mixxx, Rekordbox, Serato or Traktor. A hot cue named "Drop 2" or "Breakdown"
  counts as one. Cues that only time a mix (first and last downbeat) are left out;
- the synced lyrics: where the singing starts.

Marks less than a second apart are one landmark. Of two marks of the same kind, one set by hand
wins over DJ software, then lyrics, then analysis; two kinds that fall together share a name, such
as "Drop and vocals". Bars count from the first beat of the beat grid, so bar 1 is the first
detected beat; a song with no beat grid has no bar numbers. Drops, breakdowns, phrases and bars
appear once a library scan has analysed the song, while **Find beats and song structure** is on
in Library tools (it is on by default).

## In Track info

Track info lists the song's structure under **Song structure**, one landmark per list item in time
order, for example:

- Intro, 0:00 to 0:32, bars 1 to 17
- Vocals from 0:45, bar 23
- Drop, 1:35, bar 49
- Breakdown, 2:40, bar 81
- Outro, 3:50 to 4:10, bars 116 to 126

Times are numeric on screen and to a screen reader alike, as elsewhere in AutoDJ. Phrases, unnamed cues and the end of the intro (its item already says
where it ends) are not listed; the bracket keys still stop on them, and Shift+U says when the intro
ends.

## Keys

- **Right bracket** and **Left bracket** go to the next and previous landmark and say it. Each
  answer starts with what it is about: "Preview:" or "On air:".
  - While Track info shows, they move the cue point preview, whether it is playing or paused, and
    say "Preview: Drop, 1:35, bar 49". **Use preview position** then copies that
    time into a cue point.
  - On any other tab they speak about the song on air, from where it is playing: "On air: Drop,
    1:35, bar 49, in 0:12" (or "0:08 ago" for Left bracket). They do not
    move it, because listeners would hear the jump.
  - To have them move the song on air too, turn on **Bracket keys seek the song on air** under
    Settings, Browser settings, Preferences, Keyboard. It is off by default and saved in this browser only.
    Seeking is not possible in stream mode, and a seek names the song it is for: when another song
    is playing by the time it reaches the server, nothing moves and the page says "The song on air
    has changed, so it was not moved."
  - Left bracket goes back to the start of the landmark you are in, or to the one before when you
    are within a second of it.
- **Shift+U** says what is coming next in the song on air, with the seconds and, when the tempo is
  known, the bars: "On air: Drop in 0:12, 6 bars", "On air: Outro starts in 0:30, 15 bars". After the last landmark it says how long the song has left. With several stations it
  starts with the station's name.

Each key says its answer once, politely, and never moves focus. They are single-key shortcuts, so
**Enable keyboard shortcuts** (Settings, Browser settings, Preferences) turns them off with the rest. Neither
bracket nor Shift+U is used by the browsers or by NVDA and JAWS outside browse mode; in browse
mode, use focus mode, or press NVDA+F2 or JAWS Insert+3 first to pass the key through. On a
keyboard layout that types brackets with AltGr, AltGr and the bracket key work; Ctrl+Alt with the
bracket keys of a US layout is left to the browser and the screen reader.

The song on air is looked up afresh on every key press, and the server keeps each song's answer
until its cue points, its analysis or its lyrics (the audio file or its `.lrc` sidecar) change.

The landmarks are read through the station the page controls, at `GET /api/landmarks?path=...`
or `/api/stations/<slug>/landmarks?path=...`, for a song in the library only.
