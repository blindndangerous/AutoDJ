# Operator glossary

Use these meanings when reading controls, lessons and reports. For procedures,
start at the [documentation home](README.md).

## Station

An independent programme with its own settings, queue, history and playout on
one server. Music and its analysis are shared. See [station ownership](explanation/stations.md).

## Library

The shared catalog of registered audio and its metadata, analysis and song cue
points. Import registers files; it does not move them. See [add music](guides/add-your-music.md).

## Basic and advanced analysis

Basic analysis prepares playable records and measurements. Advanced analysis
adds sound representations used by similarity picking; a playable song may lack
these. See [analysis operations](operations.md#indexing-and-dj-analysis).

## Pick mode

The method used to choose automatic songs: Similarity, Random walk, Least
recently played or Clockwheel. See [selection](explanation/selection.md#each-pick-mode-answers-a-different-question).

## Seed

The song used as a starting reference for similarity choices. Anchoring can keep
that reference instead of following each new song. See [Song picking settings](settings-reference.md#playback).

## Category

A named song pool defined by conditions or a saved list. A category does not
start playback by itself. See [build your sound](guides/build-your-sound.md).

## Clockwheel

A repeating ordered list of picking steps. It must be saved, active and selected
as the pick mode to drive automatic rotation. See [clockwheels](operations.md#clockwheels).

## Rotation

The pattern of automatic choices over time, shaped by picking settings,
clockwheels and rules. A rotation test simulates choices; it does not play them.
See [testing the rotation](operations.md#testing-the-rotation).

## Queue

Ordered deliberate choices waiting to play before ordinary automatic picks.
Changing pick mode does not empty it. See [queue priority](explanation/selection.md#the-queue-comes-before-automatic-picking).

## Show

A saved ordered programme of songs that can be queued or started by a scheduled
event. See [shows](operations.md#shows).

## Repeat rules

Restrictions on how soon songs, artists and other configured relationships may
recur. Automatic paths may relax some rules when choices run out. See
[repeat rules and exceptions](contributor/track-selection.md#repeat-avoidance-and-discovery).

## Discovery

Occasional automatic choices from more distant-sounding eligible songs, adding
variety to similarity selection. See [selection](explanation/selection.md).

## Cue points

Saved positions such as Start, Intro, Outro, Segue and End used to describe or
control song timing. Shared music cue points affect every station using the
song. See [song structure and landmarks](operations.md#song-structure-and-landmarks).

## Segue

A handover to the next song. The saved Segue point is a position in the ending
song used by saved-point playback. See [playback settings](settings-reference.md#playback).

## Voice liner

A saved clip, such as an ID or jingle, available to the station's liner features.
It differs from a one-off booked voice track. See [recorded liner lesson](tutorials/recorded-liners.md).

## Voice track and break

A voice track is a DJ take recorded in the browser and explicitly booked for an
open song change. A break can also arrive from an announcer program. See
[voice tracking](operations.md#voice-tracking-from-the-browser).

## Open song change

The upcoming transition still accepting a booked break, after the next song
because the next transition is already prepared. Its displayed song pair and
booking deadline can change. See [voice tracking lesson](tutorials/voice-tracking.md).

## Talk-up and ducking

A talk-up places speech over an intro, aiming to finish before the vocal.
Ducking lowers the music under speech. Standalone placement plays the break
between songs. See [voice tracking placement](operations.md#voice-tracking-from-the-browser).

## Preview

An audition or forecast, depending on context. **Listen back** auditions a local
voice take; **Hour preview** forecasts the programme. Neither by itself books
new audio. See [listening controls](explanation/playback.md#listening-controls-and-programme-controls).

## Stream and relay

A stream is the station's encoded programme for listeners. A relay sends it to
another streaming destination. Local listening volume does not change broadcast
level. See [radio streaming](operations.md#radio-stream-sonos-vlc-and-other-players).

## Soft and Hard timing

Soft generally waits for a song to finish; Hard generally runs at the due time
and can interrupt. Shows, ads and stream actions have specific exceptions. See
[scheduling timing](explanation/scheduling.md#soft-and-hard-express-priorities).

## Fixed and predicted

In Hour preview, fixed rows represent already committed choices at that moment;
predicted rows estimate later choices. Neither prevents future operator edits.
See [forecast evidence](explanation/scheduling.md#fixed-and-predicted-are-different-kinds-of-evidence).

## Backtiming and hour fitting

Backtiming estimates how song changes land against fixed times. Hour fitting
prefers useful lengths among eligible songs. They help plan timing without
guaranteeing it. See [Hour preview](operations.md#hour-preview-and-backtiming).

## DMCA mode

Optional selection and public-display restrictions intended to help operate
within certain limits. It is not a legal compliance guarantee. See
[DMCA operations](operations.md#dmca-mode).
