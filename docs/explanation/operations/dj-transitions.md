# How drop mixing and key shift work

Understand when server-side drop mixing and key shift apply, their fallback rules and their processing cost.

[Operations index](../../operations.md) · [Settings reference](../../settings-reference.md)

## Drop mixing and key shift

Two `djmix` options in a station's `station.json` (also under DJ mixing in the web Settings)
change how the server mix joins songs. Both are off by default and both apply only when the
server does the mixing, in stream mode or with server audio.

```json
{
  "djmix": {
    "beatmatch": true,
    "drop_mix": true,
    "key_shift": true
  }
}
```

`drop_mix` brings the next song in so that its drop lands on the downbeat that starts the ending
song's next section, counted in whole phrases (`phrase_bars`) from that song's drop or breakdown.
The crossfade ends on that downbeat: the new song's build-up plays under the old one, and the
drop comes in at full level as the old song finishes fading out. With the `auto` transition
effect a noise riser builds up to the drop; an effect you chose by name is kept, and any riser,
siren or horn layer ends on the drop. It needs `beatmatch` on and both songs' tempos and beat
grids trusted, so the beats are locked together. Drops come from cues you set in Rekordbox,
Traktor, Serato or Mixxx (a cue of type drop, or a hot cue named "Drop"); the drop AutoDJ detects
itself is used only when it is unmistakable, the second after it at least 18 dB louder than the
second before it and the level holding for three seconds after. Breakdowns count as a starting
point only when you marked them. A pair mixes the usual way when the new song would have to start
more than 90 seconds in, when the ending song would stop more than 32 seconds earlier than usual,
or when there is less than 4 seconds of build-up. The overlap is the usual crossfade length, at
most 30 seconds. A drop mix is not moved or shortened by `vocal_guard`: the build-up before a
drop rarely has singing, and moving the crossfade would take the drop off the downbeat. Look for
"Drop mix:" lines in the log to see which pairs used it.

`key_shift` plays the next song one or two semitones higher or lower during the crossfade when
its key clashes with the ending song's but the shifted key would mix (by the Camelot rule, or by
your `harmonic_mode` when that is on, so an energy boost pick is not undone). The tempo does not
change. After the crossfade the song slides back to its own key over `beatmatch_glide_bars` bars,
at least 4 seconds, so from then on it plays in its true key. The `auto` transition effect treats
a shifted pair as matching keys. Songs with an unknown key, or a clash that needs a bigger shift,
mix unchanged. "Key shift:" lines in the log name the songs it moved.

Both run the phase vocoder over the overlap and the glide only, never the whole song. A key shift
also resamples that stretch first and locks the phases of each partial together, which roughly
doubles the work of a beatmatched glide: on a desktop PC about 0.75 seconds of processor time per
transition with a 6-second crossfade and a 6-second glide, against about 0.4 seconds for
beatmatch alone. It is done ahead of time while the previous song plays.
