# Learn to run a station

Build a practice station, then choose lessons for the work you want to learn.
Use a fresh station that is not broadcasting to listeners. The examples reuse
the default practice station and its category and clockwheel, both named
`Practice`. The station's displayed name may still be AutoDJ; the first lesson
does not rename it. Each lesson states
which earlier work it needs and what it changes.

For instructions on a particular job, use the [task guides](../README.md#do-a-task).
For a value or default, use the [settings reference](../settings-reference.md).

## Start with a simple station

1. [Build your first station](first-station.md). Import a small folder, hear a
   queued song, and run a category-based clockwheel. Start with AutoDJ installed,
   an empty station using Browser output, and at least five music files on the
   AutoDJ computer. Advanced analysis is optional.
2. [Shape a rotation with two categories](two-categories.md). Divide Practice
   into two song pools and alternate them. Predict the pool for the next pick,
   then inspect the result. Begin with the first lesson's working station and
   music that can be divided into two nonempty pools.
3. [Schedule and verify a short show](scheduled-show.md). Prepare a show,
   schedule it, and observe the handoff back to automatic picking. Begin with
   the practice rotation and check the lesson's output-mode and time prerequisites.

- [Listen to and export a shortened programme review](programme-review.md). Review a saved show privately, inspect transitions, and keep matching MP3/WAV exports.

## Explore analysis and recorded speech

- [Use advanced analysis for similarity picking](similarity-picking.md). Analyze
  suitable songs, observe similarity choices, and compare them with your category
  rotation. This requires model setup and analysis time; musical outcomes vary.
- [Record and place a browser voice track](voice-tracking.md). Record a short
  break, preview it privately, then explicitly send it to the station. Read its
  microphone, output-mode and browser prerequisites first.

## Learn mixing and station sound

- [Build a short beatmatched set](beatmatched-set.md). Inspect song timing and
  compare a transition with beatmatching off and on. Use the lesson's suitable
  tracks, timing analysis, and FFmpeg. The lesson temporarily uses Radio stream
  output and listens in the same browser; it needs no second device.
- [Add your station's voice](recorded-liners.md). Import a recorded liner,
  preview it, and hear it in rotation. Bring a short audio clip you may use;
  no speech provider is needed.
- [Compare speech voices and publish a liner](speech-liner.md). Use an existing
  speech connection, compare samples, save a liner, and separately assign it to
  rotation. This branch requires a configured provider and may incur charges.
  Learn recorded liners first.

## Reach listeners and work live

- [Stream your station to a second device](stream-second-device.md). Change to
  Radio stream output, listen on a trusted local network, and restore Browser
  output. Begin with the first lesson's station, FFmpeg, and a second device.
- [Handle a listener request](listener-requests.md). Submit a request from the
  listener page, approve it, and observe playback. Follow the lesson's streaming
  prerequisites; an accepted request is not a promise of an exact start time.
- [Take over live and return to automation](live-handoff.md). Connect a DJ
  source, verify takeover, disconnect, and observe automatic playout resume.
  Prepare the practice stream and the source program named in the lesson.

## Protect and extend your setup

- [Sign in with Authentik and Caddy](sso-authentik-caddy.md). Protect the
  practice server with SSO, verify access and browser ownership, and restore
  native access. Requires an existing Authentik installation, Caddy, a domain
  with working HTTPS, administrator access and two test accounts.
- [Back up and recover the practice station](backup-recovery.md). Inspect a
  backup and restore into an isolated destination. Begin with the saved practice
  setup and space for a separate recovery copy. Keep music and credentials safe
  separately; a station backup does not include everything.
- [Run two independent stations](two-stations.md). Reuse the shared music library
  while keeping rotation and settings separate. Begin with the practice station;
  use the lesson's distinct second-station ID and output requirements.

## Choose a path

For an automatic station, work through the first three lessons, then add recorded
liners and streaming. For DJ mixing, move from the first lesson to the beatmatched
set. Recovery is useful before changing a setup you want to keep. The live and
speech lessons introduce extra equipment or services; their prerequisites are
part of the lesson, not requirements for the whole series.

For definitions and background, use the [glossary](../glossary.md) and
[operator explanations](../README.md#understand-autodj). Before relying on the
practice settings, follow [Prepare a station for regular use](../guides/prepare-station.md).

## Write another tutorial

Use the [author template](author-template.md) and the
[contributor guidance](../../CONTRIBUTING.md#writing-documentation).
Keep the Practice example consistent across lessons. If proposing a future
lesson, give its outcome and prerequisites and mark it planned until its file
exists. Do not link to an empty lesson.

AutoDJ and this page were developed with AI assistance.
