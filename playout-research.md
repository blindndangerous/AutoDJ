**Radio automation and playout: findings and recommended paths**

Research date: 9 October 2026; cue-data research, sound tests, timing corrections and segue trials: 10 October 2026. Local baseline: AutoDJ 0.19.1 in this checkout. This covers official documentation, selected upstream source, local code, the available WSL environment, and offline AutoDJ rendering proofs using the supplied tracks. The user prefers round seven's B source preparation over A; Stereo Tool adds welcome smoothing. The [latest round-nine examples](local/playout-round9/index.html) use one **1.5-second exponential outgoing music fade, with 0.5s before the next item and 1.0s underneath**, across music, voice, ID and jingle handoffs. They retain White Town's corrected Cue In and the tighter Pink entry, and advance Kendrick's segue to **4:30.6**. Segue anchors music; outro allows an earlier voice start. Rivendell has not been installed or auditioned. “Documented” means a capability is described by its authors; “needs proof” means the exact requested behavior has not been established.

**Working constraints: manually supplied cue points, free playout software, and headless playout with an accessible browser interface.** A paid audio-processor license is now open for consideration; paid playout packages remain excluded. Assume the intro/outro and protected-vocal boundaries will be entered or imported. Automatic vocal detection is not a prerequisite. Linux remains preferred. The first selection criterion is the quality and programmability of music/ID/voice transitions; the clock can be our own control script. The requested interface is browser-based operation, rather than a requirement that every administration task use a terminal.

**Current recommendation: make a small radio-transition proof in AutoDJ, with Rivendell as the strongest existing broadcast-system alternative.** AutoDJ already has server mixing and browser controls, making it a useful foundation for the clarified workflow. The core rolling clock is comparatively straightforward once the engine exposes usable queue and playback controls. Native scheduling limitations are secondary to adding/removing future items, reading playback state, submitting transition timing, and distinguishing queued items from material actually aired.

The practical evaluation order is:

1. **Continue tuning the AutoDJ radio-transition proof.** Round seven adds separate voice compression, music leveling, the second voice track, revised cues, and a continuous sequence. Compare these before adding the clock. The current preference is steady music under speech, with voice processing and gain set before a shared processor. This is an experiment order; the final engine choice depends on audio and live-control results.
2. **Trial Rivendell's headless engine and supported interfaces.** It already supplies a headless player, offline log renderer, command-line log editing, and HTTP cue/transition controls. A third-party browser frontend also merits inspection. Its built-in rotation policy can be bypassed if the integration succeeds.
3. **Use Liquidsoap if the engine work in AutoDJ or integration work in Rivendell becomes disproportionate.** It offers programmable Linux audio infrastructure; our cue rules, browser workflow, and clock would still need integration.

RadioDJ remains a free Windows audition candidate, but this review has not established a suitable headless/browser workflow for it. Select the audio foundation on actual transition results, then attach the clock. StationPlaylist, mAirList, RadioBOSS, and paid PlayIt Live modules are excluded.

| Path | Main benefit | Main work or tradeoff |
| --- | --- | --- |
| Extend AutoDJ | Existing server mixer, browser interface, library, and transition code under our control | Add explicit speech-aware timing, verified cues, durable airplay history, and reliable recovery |
| Rivendell plus browser controls and our clock | Existing Linux broadcast playout with headless and offline modes | Prove live log refresh, automatic speech placement, and browser accessibility |
| Liquidsoap plus our controls and clock | Programmable Linux audio infrastructure | Author the three-item timing logic and connect the browser/library workflow |

**The requirements need a few precise definitions.** A radio segue can bring the new song in near its normal level while the outgoing song fades. Applying the same long fade-in to every incoming song can cause the energy dip you want to avoid. The appropriate entry point, outgoing fade shape, available headroom, and the actual recordings matter more than a single global “crossfade seconds” setting. Liquidsoap's documented custom transitions include fading only the outgoing source, as well as two-sided fades and other choices. [Liquidsoap crossfading](https://www.liquidsoap.info/doc-2.4.5/crossfade)

For this project, use the following small set of cue fields. These are proposed requirements, not an implemented schema or a claim that every candidate uses these exact field names.

| Field | Purpose |
| --- | --- |
| Cue In / Start | First position permitted to play; skips unwanted leading material |
| Intro End / Vocal Start / Post | First protected vocal; the usable talk-up duration is this position minus Cue In |
| Outro Start / Vocal End | End of the last protected vocal, leaving a clear closing region for speech; does not itself start a fade |
| Segue / Mix Point / Next Start | Preferred point for starting the next event; the outgoing file may continue underneath |
| Cue Out / End | Final playback boundary, including any wanted effect or reverb tail |
| Optional Fade In End / Fade Out Start | Deliberate playback envelopes, independent of the vocal and segue markers; no fade is a valid choice |
| Speech Start / End, for IDs and voice | Protect spoken words separately from leading silence, production beds, and effect tails |
| Allowed music-under region, for IDs and voice | Describes where another music source may run beneath this item; the supplied IDs permit the whole item |

Manual or imported markers are the working assumption. Ensure the chosen engine interprets an intro marker as the intended first-vocal boundary and an outro marker as the permitted talk-over region; DJ mix markers can mean something different. Preserve a conservative fallback for a missing marker, but do not make automatic detection a development project.

Use the established separation between cut start/end, segue/next-start, fade points, and talk/intro timing. Rivendell documents separate Cut, Segue, Fade, Talk, and Hook markers; RadioDJ documents start, intro, outro, end, and custom fades. There is no single shared naming convention, so an importer must map meanings rather than copy labels blindly. An outro cue does not require a long fade, and a segue cue does not have to truncate the outgoing file. In the supplied Plague jingle, the latest next-event cue is 5.3 seconds while reverb continues to Cue Out at approximately 8.809 seconds. Central Cee's Intro End at 0:02, Outro Start at 1:23, and Segue at 1:28 are another example of why these markers must remain separate. [Rivendell marker definitions](https://opsguide.rivendellaudio.org/html/sect.rdlibrary.library_maintenance.html), [RadioDJ cue features](https://www.radiodj.ro/radiodj-features)

**Separate outro and segue points are established playout practice.** The four familiar start/intro/outro/end labels do not exhaust the available markers. mAirList explicitly defines Outro as the vocal ending and Start Next as the next song's automatic start, with Fade Out and Cue Out also separate. RadioBOSS has Intro/Outro for voice tracking and a separate Mix point for the next track. These paid products are cited only to establish the convention, not added to the shortlist. The two positions may coincide; they need not. Central Cee's 1:23 vocal ending and 1:28 preferred segue require both. Segue timing is also an editorial preference that can change with the next item, so a shared cue would be a useful default rather than an immutable property. [mAirList cue definitions](https://wiki.mairlist.com/tutorials:general:getting-started:v6_3:v6_3_playlist), [RadioBOSS Track Tool](https://manual.djsoft.net/radioboss/en/track_tool.htm), [RadioBOSS Mix definition](https://manual.djsoft.net/radioboss/en/crossfades.htm)

Store positions as integer milliseconds from the original decoded file start, with display durations derived from those positions. A missing cue is unknown, not zero: zero can be a genuine cold vocal start. Associate markers with the exact file version and record whether they were manually confirmed, imported, or estimated. Keep item type, cold/fade ending style, measured LUFS/true peak, and an operator gain trim alongside the cues. Music also needs artist/category identity for rotation. Voice links need an explicit full-play requirement and any intended neighboring songs. Per-play timing and fade overrides belong to the scheduled event, so an ID's quick cut does not overwrite the music track's reusable outro. Hook regions and beat grids can remain optional until their corresponding features are needed.

Our fallback is explicit: ordinary music/ID transitions use **Segue → Outro → End**. A voice link has two independent positions: its outgoing talk region uses **Outro → Segue → End**, while **the next song waits for Segue → End**. The usable earliest voice start is the earlier of that talk marker and the planned music segue. If the segue cuts the song before its outro, there is no outgoing talk window: speech can start with the incoming song instead of delaying the handoff to the old outro. Back-time the full retained voice toward the incoming vocal, clamping its start between that usable earliest point and the planned segue. A short voice may start with the next song and finish early in its intro; it must never start after that song begins or pull music forward to an earlier outro. If the voice cannot fit, start at the usable earliest point and delay music enough to protect the incoming vocal. This is our chosen policy, not a universal default across products.

For a voice transition, first derive `earliest_voice = min(selected_talk_marker, planned_music_segue)`. Then `voice_start = max(earliest_voice, min(planned_music_segue, planned_music_segue + usable_intro - clearance - voice_duration))`; `actual_music_start = max(planned_music_segue, voice_start + voice_duration + clearance - usable_intro)`. Compute these in audio frames. The stored outro remains unchanged: it describes the source, while the scheduling bound describes this playback. The shared renderer still rejects contradictory explicit timing windows; its caller now supplies the correctly bounded window.

Fade policy is separate from voice placement. The user's latest choice is **1.5 seconds total, with 0.5 seconds before the next event enters and 1.0 second underneath it**, using a slow-start, fast-finish exponential curve. One outgoing-music envelope serves song, ID and jingle entries. In a voice link it follows the actual incoming music entry; the complete speech remains back-timed and protected. Incoming items have no added fade-in, and speech does not duck the music. Fades stop at the real file end if there is insufficient tail. Plague retains its produced reverb after its 5.3-second next-song cue, and voice/ID recordings retain their full selected content. The user prefers the 0.25-second ID overlap into Pink's cold opening; full voice links retain positive vocal clearance. Cue In removes unwanted silent lead before timing the overlap, and usable intro is absolute Intro End minus Cue In.

**No broad, free shared database of these radio cues was verified.** The user clarifies that most supplied music uses regular radio edits, with Iyaz the custom mix. Standard edits are sensible lookup candidates; the immediate obstacle is finding a service that actually exposes vocal and segue timestamps, not merely identifying the recording. MusicBrainz documents recording identity, duration and ISRC fields, while AcoustID returns fingerprint matches and associated recording metadata. Their documented interfaces do not supply first-vocal, last-vocal or radio-segue positions. This is a finding about the services checked, not proof that no specialist or private catalogue exists. [MusicBrainz data inventory](https://musicbrainz.org/doc/MusicBrainz_Database), [AcoustID API](https://acoustid.org/webservice)

Station databases and embedded metadata are practical sources when available. For example, Myriad's import documentation maps APE Intro/Segue fields and WAV CART timing markers into its library. Such a format can transfer cues already prepared for a file; it does not establish an online catalogue for arbitrary songs. The best immediate policy remains importing existing cues and keeping a local store of verified markers, including the supplied Iyaz timing. [Myriad embedded cue import](https://help.broadcastradio.com/support/solutions/articles/101000554387-supported-embedded-metadata-when-importing-into-myriad-playout-v6)

**The TM/Promo Only check found a useful commercial metadata source, but no public cue lookup.** TM Studios' GoldDisc supplies music with an importable CSV including intro/extro data; its FAQ also specifies fade points and cold/fade ending information. Access is sold with its radio-station music service. This is a credible import route if we obtain a matching release and its metadata, not a free database for the files already here. [TM GoldDisc](https://tmstudios.com/golddisc/)

The public [TM CHR catalogue](https://cdn.tmstudios.com/wp-content/uploads/2026/07/TM-GoldDisc-CHR-Library-July-2026.pdf) includes Ms. Jackson, Boy's a Liar and Roxanne, but does not expose their cue timestamps or establish an exact edit match. Promo Only advertises clean, intro and quick edits through its subscription service; the checked public pages did not expose reusable vocal/segue timestamps. No vendor cue values were imported into these examples. The local, user-supplied markers remain authoritative; a title or radio-edit label alone is insufficient for copying another file's timing. [Promo Only Top 40](https://www.promoonly.com/products/subscriptions/top-40), [Mainstream Radio](https://www.promoonly.com/products/subscriptions/mainstream-radio)

An open project worth watching is Mixxx's August 2026 CMRT work: it describes fingerprinting and mastering/offset matching as groundwork for future sharing of beat grids and cue points through a broader DJ-Intelligence effort. It is relevant to this exact idea, but the announcement does not establish an operational public radio-cue lookup. No audio was uploaded to a matching service. [Mixxx CMRT project](https://mixxx.org/news/2026-08-24-gsoc-2026-cmrt-canonical-master-release-track/)

**A quick cue-preparation path can bypass AutoDJ's expensive analysis.** Existing `detect_intro_start` in [dj_meta.py](src/autodj/dj_meta.py) detects the first sample above a silence threshold, while `detect_intro_outro` uses short-window energy. Neither recognizes the first or last sung word. The normal `analyse_audio` path also performs beat/phrase work, and library indexing has additional work; those are unnecessary when preparing known radio cues. A lightweight path can suggest audible start/end and tail boundaries and offer short auditions for confirming vocal markers. The two-file probe below demonstrates the cheap boundary scan; a complete cue-editor workflow and dependable vocal detection remain unimplemented.

**New-track probe: White Town and Jordan Knight.** Both files were absent from the round-seven inventory, so both were checked. The latest tag reads, 44.1kHz decodes and RMS measurements together took about 1.1 seconds, excluding Python startup/imports. No beat analysis, embeddings or library indexing ran. Neither file has embedded cue markers, synced lyrics, or a sibling LRC file. Their BPM values below come from tags, not a beat estimate. [Measurements](local/playout-cue-probe/findings.json), [probe notes](local/playout-cue-probe/README.md), [opening/ending auditions](local/playout-cue-probe/index.html)

| File | Decoded duration | First sound above -60 dBFS | Last active 100ms window at -60 dBFS | Ending evidence |
| --- | --- | --- | --- | --- |
| White Town, Your Woman | 4:17.097 | 0.260s | About 4:16.7 | Active level through most of the tail; a short final decline, not a demonstrated long fade |
| Jordan Knight, Give It To You (radio edit without waltz) | 3:48.754 | 0.084s | About 3:48.7 | Strong level close to file end; near-cold/short-tail candidate |

The user subsequently confirmed **White Town: intro 0:12, outro 4:17, nearly cold ending with a later fade point** and **Jordan Knight: intro 0:08**. These authoritative markers are stored separately in [confirmed-cues.json](local/playout-cue-probe/confirmed-cues.json), with file hashes so automatic reanalysis cannot replace them. White Town's decoded file ends at 4:17.097, so the unspecified later fade timestamp remains unset; no longer tail is invented. Jordan's outro and both preferred segues remain unspecified.

For safe playback, keep Jordan's Cue In at 0 and its full file end; White Town could skip approximately 0.20s of leading silence while retaining the onset, without shifting the original-file cue positions. The last-active values are threshold measurements, not approved hard cuts or preferred musical segues. The energy helper's 4.5s and 9s “intro end” results must not become talk-over permissions: those labels describe level changes, not detected words. White Town's metadata identifies “Radio Series March 1997” and 102 BPM; Jordan's identifies “Give It To You” and 134 BPM, but neither includes usable cue timing.

An additional local experiment downloaded only the official `openai/whisper-tiny.en` model to `playout-tools/models` and analyzed the first 45s and last 60s of each song, with no audio upload. Cached loading took 2.74s and CPU inference across the four excerpts took 107.11s; first setup also incurred download/import overhead. This particular Transformers pipeline attempt produced unreliable boundaries, including a Jordan ending timestamp beyond the excerpt. All proposed vocal cues were therefore rejected and saved as unknown. The result argues for manual/imported vocal markers in this prototype, not that automatic vocal analysis is impossible. The model's documented hallucination limits also require caution when treating recognized words as timing evidence. [Local diagnostics](local/playout-cue-probe/candidate-vocals.json), [official model card](https://huggingface.co/openai/whisper-tiny.en)

Reuse existing evidence first: [dj_cues_import.py](src/autodj/dj_cues_import.py) imports supported DJ-library cues, and [audio_meta.py](src/autodj/audio_meta.py) reads timed lyrics. The current player's `vocal_start_s` already uses lyric-derived sung spans through [vocals.py](src/autodj/vocals.py), with an energy-intro fallback. Imported cues and lyrics can refer to another edit or a different cue meaning; lyrics also need not mark the exact final syllable. They are suggestions until checked. Silence detection alone cannot establish a safe speech-over-music window, and an unverified estimate should not authorize overlap with a protected vocal.

**The two speech examples describe a small timeline planner.** Consider the ID's start as time zero and assume its speech lasts the full ten seconds. With a five-second instrumental intro, start the new song at about five seconds. The song's first vocal then arrives at ten seconds, as the ID finishes. With a 0.25-second clearance, the new song starts at 5.25 seconds.

More generally, relative to the ID's start:

`next_song_start >= speech_end + clearance - usable_intro_duration`

Also obey the ID's earliest allowed overlap and any restrictions from the outgoing song. “Usable intro” is measured from the chosen playback entry; a trimmed or time-stretched entry changes it. The complete ID file may extend beyond its last spoken word, so the production-tail policy must be explicit.

For a 30-second voice track and ten seconds of clear music on either side, this is a valid arrangement:

| Time from voice start | Outgoing song A | Voice track | Incoming song B |
| --- | --- | --- | --- |
| 0–10 seconds | Instrumental outro at steady level | Speaking | Not started |
| 10–20 seconds | Finished | Speaking; dry or over a separately chosen bed | Not started |
| 20–30 seconds | Finished | Speaking over B's intro | Instrumental intro |
| 30 seconds onward | Finished | Finished | First vocal begins |

There are only twenty seconds of instrumental cover in this example. The other ten seconds need dry speech, a suitable bed, or a different arrangement. Overlapping the two music windows with one another does not create extra safe speech time. Add the clearance margin when making the actual plan.

If a link cannot fit, the system should deliberately delay the incoming song, choose a shorter compatible ID, or use a dry break. It should not silently continue speech across a protected vocal. A produced ID with its own music may need different overlap rules from a dry announcer link.

**The clock can be a script that keeps a short queue filled.** It need not replace a mature playout engine's audio work. The following are bookkeeping rules for that script, not requirements for the engine's native scheduler:

- The clock contains category slots and timed anchors: for example, ID → current → recurrent → gold → voice link → current, with a resynchronization point at the hour.
- Resolve only a short useful horizon, such as the next few items. Allow a longer locked region when a presenter records a song-specific link.
- Apply track and artist separation against actual aired history and temporary reservations for upcoming items. Reservations prevent two pending slots from selecting the same otherwise-eligible song.
- Choose the least recently aired eligible track in the required category. Define a deterministic policy for never-played tracks and ties.
- Canceling an unplayed event releases its reservation and leaves its last-aired time unchanged. The song is eligible under the ordinary rules again; it need not be forced to play immediately next.
- Count a song that has actually started separately from one merely queued. Store start, stop, partial play, cancellation, and failure distinctly. Decide how partial plays affect rotation.
- Make forced overrides visible. If a small category cannot satisfy all separation rules, use an explicit fallback order instead of silently weakening them.

At a resynchronization point, release unplayed reservations and move to the appropriate clock position. Also decide whether the skipped *category slot* is discarded with the old hour or carried forward. Returning a song to eligibility and carrying its slot into the next hour are separate decisions.

Backtime from **effective on-air duration**, including overlaps, cue trims, voice-only intervals, and any tempo adjustment. Raw file durations will give the wrong answer. Replan after a request, a recorded link, a skip, or a changed marker. As an anchor approaches, select a fitting song or approved filler, omit an optional slot, or apply the chosen hard/soft timing policy. Dropping a four-minute song to correct a forty-second overrun otherwise creates a new underfill problem.

A hard anchor starts at its defined time even if something must end early. A soft anchor waits for an acceptable end, potentially within a tolerance window. Exact timing, never cutting songs, and arbitrary last-minute insertions cannot all be unconditional promises.

**The strongest existing Linux application is Rivendell.** Its official overview describes a complete GNU/Linux broadcast automation system with touchscreen-friendly controls. The project currently lists production release 4.5.0. It merits a real trial, particularly if the eventual live studio workflow matters. [Rivendell overview](https://rivendellaudio.com/), [official releases](https://github.com/ElvishArtisan/rivendell/releases)

Its VoiceTracker provides outgoing audio, the voice link, and incoming audio in an editable transition view. Operators can record or import links, move overlaps, adjust volume envelopes, and use Talk markers to align the end of speech to the incoming post. This is much closer to the requested three-item presentation than a generic playlist crossfade. The automatic placement of arbitrary IDs still needs testing with the particular marker rules proposed above. [Rivendell VoiceTracker](https://opsguide.rivendellaudio.org/html/sect.voicetracking.using_the_voicetracker_dialog.html)

Its timed-event behaviors include immediate starts, making a timed event next while bypassing intervening entries, and waiting up to a specified interval. Those provide useful hard/soft recovery primitives. RDLogManager builds dated logs from events, clocks, and grids; “as needed” log generation should not be confused with continuous per-slot selection. [Rivendell time types](https://opsguide.rivendellaudio.org/html/sect.rdlogedit.time_and_time_types.html), [RDLogManager events](https://opsguide.rivendellaudio.org/html/sect.rdlogmanager.events.html)

A source-level distinction remains relevant only if using its native selector: the inspected v4 `RDEventLine::GenerateMusicSchedEvent` filters candidates using recent scheduled selections and rules, then chooses randomly among the survivors. It writes the selection to `STACK_LINES` during generation. This is not least-recently-aired selection; a subsequently dropped item can still influence scheduling separation. Actual cut play counters are maintained separately. Our own clock can own this policy instead. [Rivendell selector source](https://github.com/ElvishArtisan/rivendell/blob/v4/lib/rdevent_line.cpp), [cut playback source](https://github.com/ElvishArtisan/rivendell/blob/v4/lib/rdcut.cpp)

The follow-up establishes a useful scripting route: RML's `PX` inserts a cart into the live log with an optional offset and transition type, and `AL` appends a log. The realtime PAD feed and bundled Python PyPAD module expose current/next information. This supports the proposed external clock architecture. [RML commands](https://opsguide.rivendellaudio.org/html/sect.rml.commands.html), [PAD/PyPAD](https://opsguide.rivendellaudio.org/html/chapter.pad.html)

The HTTP API provides more precise control than `PX` alone: `EditCut` accepts start/end, talk, segue, and fade markers in milliseconds; `SaveLog` accepts per-entry start/end, segue, fade, and ducking settings for individual plays. That is a documented route for supplying planned transitions without changing the permanent markers for every use of an ID. [Rivendell Web API specification](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/apis/web_api.xml)

The remaining integration proof is how saved changes reach an already playing log, including removals/reordering and the point beyond which an event is committed. Test the API together with RML's refresh/control commands. VoiceTracker's Hit Post remains an editor operation, so a custom planner may still need to calculate arbitrary ID/song offsets. API fields establish a control route, not proof that unattended fitting already works. [RML commands](https://opsguide.rivendellaudio.org/html/sect.rml.commands.html), [VoiceTracker](https://opsguide.rivendellaudio.org/html/sect.voicetracking.using_the_voicetracker_dialog.html)

**Rivendell can play without its desktop interface.** Current v4 source contains `rdvairplayd`, a headless virtual-log player using Qt's core application loop. It still connects to Rivendell's database, audio engine, and interprocess services; this is an existing server component rather than a proposed conversion of RDAirPlay's GUI. [Headless player source](https://github.com/ElvishArtisan/rivendell/blob/v4/rdvairplayd/rdvairplayd.cpp)

Its text and API facilities cover useful parts of the preparation workflow:

| Task | Existing facility | Boundary |
| --- | --- | --- |
| Import media | `rdimport` | Requires an initialized Rivendell installation and library groups |
| Create/edit a log | `rdclilogedit`, driven through standard input | Supports insertion/removal and PLAY/SEGUE/STOP; editing a stored log is separate from safely updating active playout |
| Set precise cut markers | HTTP `EditCut` | Requires appropriate user/group permissions; cut markers are shared library metadata |
| Set transition details for one play | HTTP `SaveLog` | Provides per-entry settings; live refresh semantics still need testing |
| Render a log to WAV | `rdrender --to-file=... --format=PCM24 LOGNAME` | Requires Rivendell configuration/services and media access, but permits listening without real-time sound-card capture |

[Import manual](https://opsguide.rivendellaudio.org/html/sect.utilities.rdimport.html), [CLI log editor manual](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/manpages/rdclilogedit.xml), [Web API specification](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/apis/web_api.xml), [renderer manual](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/manpages/rdrender.xml)

Two details matter for a fair test. `rdmarkerset` is a bulk automatic trim/segue tool, not an arbitrary timestamp editor; use `EditCut` for manually supplied markers. Also, `rdrender` explicitly does not implement hard-start scheduling semantics beyond using times to delimit rendering. It can audition a segue, but cannot prove top-of-hour recovery. Live daemon tests must follow. [Marker utility manual](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/manpages/rdmarkerset.xml), [renderer limitations](https://github.com/ElvishArtisan/rivendell/blob/v4/docs/manpages/rdrender.xml)

**A browser frontend already exists and should be evaluated before writing one.** The third-party [Rivendell Web Broadcast](https://github.com/olsson82/rivendellweb) project provides library/log management and browser voice recording. Its [cut editor](https://docs.rivendellwebsys.com/audiocart/) documents marker editing and keyboard shortcuts. However, its README flags development status, direct database changes alongside API calls, and an experimental multitrack editor. This makes an isolated trial appropriate. Full live virtual-log control and screen-reader accessibility have not been established; keyboard shortcuts alone do not prove accessibility. [Project status and features](https://github.com/olsson82/rivendellweb/blob/master/README.md)

My assessment: headless operation is not a reason to reject Rivendell. Its engine and API are credible building blocks for the requested browser workflow. The decision is whether integrating that workflow costs less than extending AutoDJ's existing one. A completely GUI-free bootstrap, the browser frontend, and the exact unattended speech transitions have not been tested here.

**Liquidsoap is the strongest engine candidate for a custom Linux path.** It provides programmable stream composition, metadata-dependent transitions, live sources, multiple outputs, and operator control. The official stable documentation reviewed here is 2.4.5. It supplies useful audio infrastructure, but it does not itself give us the station library editor, voice-track production workflow, or requested scheduling policy. [Liquidsoap capabilities](https://www.liquidsoap.info/doc-2.4.5/index.html)

Requests can carry cue metadata, and request sources can be supplied by an external decision function. That makes a Python scheduler feeding a Linux playout service plausible. Queue mutation, prefetch, and transition buffering must be tested on the selected version: material already rendered or buffered cannot simply be unscheduled by removing a database row. Some explanatory pages retain older API examples, so a prototype must check the installed operator reference. [Liquidsoap requests](https://www.liquidsoap.info/doc-2.4.5/requests), [request sources](https://www.liquidsoap.info/doc-2.4.5/request_sources), [current track-processing reference](https://www.liquidsoap.info/doc-2.4.5/reference/source-track-processing)

My assessment: Liquidsoap is the leading separate programmable engine to trial if AutoDJ's mixer changes or Rivendell's integration prove too costly. Supply the known cue points, calculate music/voice offsets, and let the engine execute the composition. AutoCue is optional under the manual-marker assumption. The transition planner is still custom work; validate request callbacks against the final mixed output before using them as the authoritative aired log.

**RadioDJ is the Windows candidate under the no-paid-software constraint.** The official feature list documents start/intro/outro/end cue points, automatic sweepers over track start/intro/end, voice tracking, and processing facilities. This is enough to justify an audition based on sound and overlap behavior. It does not establish the exact arrangement of a ten-second ID with a five-second intro, or unattended positioning of a thirty-second link across two songs. [RadioDJ features](https://www.radiodj.ro/radiodj-features)

Its author documents using marked intros for automatic sweepers in rotations. Test that existing behavior before proposing replacement code. Then check a supported way for an external script to add, cancel, and inspect queued items. Free availability does not establish that the engine source or all needed extension interfaces are available; that affects how far we can adapt it. [RadioDJ sweeper tutorial](https://www.radiodj.ro/getting-started-with-radiodj/page/2)

The earlier paid-product trial recommendations are withdrawn. PlayIt Live has a free base, but its paid voice-tracking/advanced modules are outside the current scope. [PlayIt feature/module list](https://www.playitsoftware.com/Features)

**AzuraCast remains useful, but its role needs to be deliberate.** Its playlist model includes general rotations, timed/frequency-based inserts, and priorities. Advanced Playlists allow custom Liquidsoap logic while retaining library management. That makes an integration possible; it does not verify the complete rolling clock or producer-aware speech policy. [AzuraCast playlists](https://azuracast.com/docs/user-guide/playlists/)

AzuraCast already offers master_me processing and AutoCue. The former is a useful free processing option; with supplied cue points, automatic cue detection is not central to the decision. [AzuraCast processing and AutoCue](https://azuracast.com/docs/help/optimizing/)

My assessment: keep AzuraCast if its station administration and delivery features are valuable. A custom playout source feeding its supported live/relay path is a candidate arrangement, or custom logic could run inside its Liquidsoap configuration. Establish one owner for selection, fades, metadata, and processing; test takeover/fallback and avoid applying a second crossfade or processor to an already mixed stream. That integration is proposed, not verified in this research. [AzuraCast developer APIs](https://www.azuracast.com/docs/developers/apis/)

**Other projects are worth understanding without expanding the initial shortlist.**

| Candidate | Relevant capability | Why it is secondary here |
| --- | --- | --- |
| LibreTime | Linux/web station scheduling, playlists, dynamic SmartBlocks, configurable adjacent fades | No documented automatic three-item, protected-vocal voice planner was established; that audio/control gap now matters more than its scheduling model. [Playlists](https://libretime.org/docs/user-manual/playlists/), [show scheduling](https://libretime.org/docs/user-manual/scheduling-shows/) |
| OpenBroadcaster | Linux-oriented server/player system with dynamic playlists, scheduled delivery, and touchscreen Live Assist | Relevant to the GUI/touchscreen search. Fine-grained scripted segue and voice-track timing remain unverified. [Official manual](https://www.openbroadcaster.com/wp-content/uploads/2026/01/openbroadcaster-manual-01042026.pdf), [OBPlayer source](https://github.com/openbroadcaster/obplayer) |
| Mixxx | Linux DJ interface, beat synchronization, cue-based Auto DJ | Useful for a live mix show or as a transition reference. Automatic speech placement across both songs and its external control path remain to be established. [Official DJing manual](https://manual.mixxx.org/2.5/en/chapters/djing_with_mixxx.html) |
| IDJC | Linux/JACK live presentation, decks, microphone and jingle facilities | A possible live console; automatic fitting of an arbitrary ID across outgoing and incoming music was not established. [Official project](https://sourceforge.net/projects/idjc/) |

“Unverified” is not “impossible.” These systems were screened against this particular specification. Documentation age, interface design, and the amount of integration required matter as much as a feature checklist.

**AutoDJ already contains valuable parts of the solution.** The source review found more relevant functionality than a basic continuity player description suggests:

| Existing area | Evidence in this checkout | Consequence |
| --- | --- | --- |
| Similarity and repeat avoidance | `Player._choose_next`, `PlayerState.record_played` in [player.py](src/autodj/player.py); [selection documentation](docs/track-selection.md) | Preserve the picker for optional musical ranking. It currently uses recent-item windows, not durable category-wide least-recently-aired rotation. |
| Server mixing and render-ahead | [render_ahead.py](src/autodj/render_ahead.py), [mixbus.py](src/autodj/mixbus.py) | A useful existing engine to test first. It renders ahead and hands the bus complete audio. |
| Lyric-aware timing | `vocal_start_s` at player.py:1915; [vocals.py](src/autodj/vocals.py) | First synced lyric is used, with detected intro-end fallback. This is not independent vocal recognition. |
| Liner talk-ups | [liner_scheduler.py](src/autodj/liner_scheduler.py):296 and :316; [existing tests](tests/unit/test_liner_talk_up.py) | Can end a fitting liner before a vocal, or cue it during an existing transition. Reuse this work. |
| Mixing and BPM work | [transitions.py](src/autodj/transitions.py); rendering in player.py | Existing beat and transition behavior provides a head start for optional mixes. |
| Interactive control | [README](README.md), browser queue and settings | Music can already be inserted/reordered. General voice-event insertion needs a richer event model. |
| Output processing | mixbus.py:571; ReplayGain handling in player.py | EQ, liner ducking, gain handling, and final sample clipping exist. This is not a complete broadcast dynamics chain. |

**Yes, AutoDJ can plausibly evolve into this system.** The first change should concentrate on deterministic timing for three items: outgoing music, speech, and incoming music. Use the existing decoders, transitions, mixer, library, and controls; make manually verified cues authoritative and allow the incoming song to wait when speech needs extra room. Imported voice files are enough for the first proof. Browser recording, the rolling clock, and further BPM policies can follow after the audio is right.

There is a particularly useful test seam already present: `MixBus.render_block()` advances audio without wall-clock pacing, and `MixBus.run()` drives that same method for live output. A small harness can save those blocks to WAV and report event offsets. This is existing internal capability, not an already implemented end-to-end radio-render command. Reuse the live rendering path in the proof so that a successful recording tests code we can actually deploy. [Mixer](src/autodj/mixbus.py), [block-render tests](tests/unit/test_mixbus.py)

For the accessible browser workflow, cue positions need labeled numeric fields, keyboard audition/set controls, and a textual description of the planned overlap. Queue actions need predictable focus and status announcements. A waveform can supplement these controls. Voice tracking should allow upload/record, replay, and numeric adjustment of placement. These are requirements for new work; this review has not audited the existing UI against them.

The distinctions that prevent treating this as finished radio automation are specific:

- **The talk-up fallback is permissive.** `_fire_locked` plays immediately when no suitable intro cue is available; the tests explicitly cover too-long and unknown-vocal cases. The strict requirement calls for a safe arrangement instead. Its current outgoing-tail scheduling works within an already selected overlap, rather than delaying the next song to accommodate arbitrary speech.
- **The current vocal evidence is limited, but the revised assumption supplies the solution.** Give manually entered/imported first/last protected-vocal markers precedence over lyric or energy estimates, and accept ID overlap permissions. Automatic detection need not be improved for this project.
- **The future transition can already be baked into the outgoing audio.** A queue edit can require rerendering; the existing refresh deliberately retains a fallback and can defer the change by a track. That is reasonable continuity behavior, but it constrains last-second clock correction.
- **Played history needs durable, output-aware semantics.** Recent selections are held in memory. The server's normal commit hook follows rendered-track handoff, although the incoming song may already have been audible inside the preceding render's overlap. The seed is recorded through a separate path before its later start callback. Reuse the hooks, but do not equate their present meaning with a precise first-audible-sample log.
- **Continuous output still needs proving.** RenderAhead documents silence if the next render is late, and hard jumps can wait for new rendering. A radio service needs prepared fallback material and a tested recovery policy.
- **Playback modes have different responsibilities.** Browser playback has its own audio engine and liner logic; the reviewed talk-up implementation is in the server path. Station automation should use one authoritative server output, with browser controls and monitoring.

The relevant code is concentrated in player.py:991, :1015, :1258, :1327, :1915; liner_scheduler.py:273, :296, :316; vocals.py:1; and render_ahead.py:20. These are investigation points for a future implementation, not code changes made by this report.

**The smallest useful custom build is a clock controller feeding the selected engine.** Reuse AutoDJ's library and interface where they help. Keep the audio engine responsible for executing transitions; the controller chooses the next events and supplies their known markers and timing.

A possible division of responsibility is:

```text
Library + verified cues + reusable clocks + recorded links
                            |
                 Rolling event planner
           reservations + durable actual-air history
                            |
              One server playout authority
         outgoing song / voice link / incoming song
                            |
             Shared output processing chain
                            |
                  Stream and/or sound card

Browser: control, audition, recording, marker editing, monitoring
```

The exact internal API should be chosen after the proof, not built as a general framework now. The important boundary is that the planner can change future events while the audio service protects material already committed to output.

Use typed events for music, IDs, voice links, and anchors. For a song-specific recorded link, retain its intended neighboring track identities. A later insertion must preserve that relationship or flag the link for review; otherwise the presenter can announce a song that no longer follows. Generic links can remain flexible. This is why completely uncommitted future music and traditional advance voice tracking need a defined compromise.

Expose a short, explicit commitment window in the UI. “Play next” should say when it can still replace the next event. Support immediate interruption through a prepared transition, with a clear policy for speech already on air. Avoid doing analysis, decoding, file downloads, or database waits inside the audio callback.

For the first scheduling version, use strict eligible-track filtering followed by least-recently-aired order. Keep AutoDJ's similarity/BPM/key scoring optional: it can break ties or influence a deliberately relaxed policy, but it must not silently replace strict least-recent selection. When a song cannot fit the approaching anchor, defer it for that window without changing its airplay timestamp.

Start the audio proof in AutoDJ, where the server and browser foundation already exist. Keep Rivendell as the alternative broadcast engine and Liquidsoap as the programmable engine option. If the current render-ahead design makes safe late edits or speech placement awkward, compare the cost of changing that design with using one of those engines. The clock is not the deciding factor.

**Compression should be a separate output decision.** I interpret “audio compression” as broadcast dynamics processing. Lossy encoding for delivery is a separate step. A sensible proposed chain is:

`separate music/voice gain normalization → music/voice mix without automatic ducking → broadcast processor → output encoding/device`

Set music and voice levels before mixing, with sufficient headroom. The user's preference is for voice to ride over steady music, avoiding the audible drop and recovery of automatic ducking. A common output compressor changes the combined signal; it cannot selectively bring buried speech forward. Use separate measured loudness targets and an operator voice trim for repeatable balance; peak matching alone is insufficient. The current proof adds 4.5 dB to the peak-adjusted voice without altering music levels. Assess the final encoded stream as well as the uncompressed bus. A processor cannot recover a poorly placed segue or determine which vocal should be protected.

**master_me is the default processor to audition under the no-paid-software constraint.** Its project documents Linux support and a chain including leveling, compression, and limiting. Start with a supplied preset and compare processed/unprocessed output at matched perceived loudness using the station's music and speech. The previous paid-processor recommendation is withdrawn; no paid processing feature is required by this proposal. [master_me project and signal chain](https://github.com/trummerschlunk/master_me)

**Stereo Tool is a relevant sound reference, without needing the old Tomass DLL.** Its documented AGC, multiband compressors and output peak control address the same broad broadcast-processing problem. That makes it a plausible way to pursue smooth, dense output, not a parameter-compatible recreation of Tomass. The user's latest target is slightly gentler than the remembered Tomass sound. The saved SAM chain also processes decks before the master DSP, so its sound cannot be attributed to the external limiter alone. Stereo Tool has free features, but its full feature set is paid and VST use requires a license; retain the no-paid-software constraint when choosing an actual dependency. No download or audition was performed for this comparison. [Stereo Tool processing features](https://www.thimeo.com/stereo-tool/), [licensing](https://www.thimeo.com/stereo-tool/licenses/)

Avoid cascading heavy processors merely because both the playout package and streamer offer them. If the destination is FM as well as streaming, plan separate destination processing where appropriate. Measure processor/output latency before judging time accuracy or live presenter monitoring. A :00 event on the originating bus reaches internet listeners later, with listener-dependent buffering.

**BPM mixing should remain an optional transition policy.** A detected BPM is only one input; reliable musical mixes also need a correct beat grid, phrase alignment, sensible tempo limits, and safe vocal placement. Radio clock accuracy and speech intelligibility should take priority. Mixxx's manual makes the distinction between tempo matching and beat alignment explicit. [Mixxx synchronization](https://manual.mixxx.org/2.5/en/chapters/djing_with_mixxx.html)

For compatible dance tracks, use the existing AutoDJ beat work in an opt-in mix block and place a short drop at a planned transition point. For awkward tempo changes or uncertain grids, fall back to a regular radio segue or an appropriate ID. Automatic hour-fitting speed adjustment in a broadcast product is not proof of DJ-style beatmatching.

**WSL is already available for Linux experiments.** Read-only inspection found an Ubuntu WSL2 distribution running Ubuntu 24.04.3 LTS, with systemd, Python 3, and FFmpeg. Ubuntu was started for that inspection. A second WSL2 distribution named `mb` is present and was left alone. `liquidsoap`, `rdvairplayd`, and `rdrender` were not on Ubuntu's PATH. No packages were installed or configuration changed. Availability of Ubuntu does not establish Rivendell package/build compatibility on that release.

Use WSL for a Linux engine trial and eventual Linux deployment checks. The first AutoDJ listening proof can write a WAV file without setting up WSL audio or an on-air stream. A successful file render proves the audible arrangement; a separately paced live run is needed to expose underruns, buffering, edits, and output latency. Judge production reliability on the intended deployment system.

**Latest auditions: round nine, one exponential handoff across the examples.** Earlier auditions used a linear fade. After preferring the longer 1.5-second option, the user requested a **0.5-second fade lead and 1.0-second overlap**, including transitions into IDs. The [new listening page](local/playout-round9/index.html) applies that outgoing-music envelope to ten familiar sequences, each in B and C, with the accepted source processing and compressor settings. Start with examples **1**, **6** and **9**; examples **2**, **3** and **5** check voice placement.

The bundled SAM help describes its exponential option as starting slowly and steepening near the end. Our explicit audition curve is `gain(t) = 1 - expm1(3*t)/expm1(3)` over normalized progress from zero to one. About 91% of pre-fade amplitude remains when the new item enters one-third of the way through. This is a concrete curve matching that described behavior; the saved `foc=2` code and SAM's exact formula have not been verified. The user recalls using exponential. Existing render calls still default to linear, preserving earlier experiments. [SAM curve description](local/playout-sam-reference/Cross_fading.htm)

Kendrick's segue moves from **4:31.1 to 4:30.6**, following the user's half-second-earlier correction for example 6. Its intro remains 0:04. White Town retains Cue In 0.25s, absolute Intro End 12s and outgoing segue 4:16. That leaves 11.75s of incoming intro, which is used when back-timing the complete long voice. Jordan's and Pink's outgoing files end too soon for a full one-second tail; their envelopes finish at the physical file end without fabricated audio. The common music fade also applies into the Plague jingle, whose marked 5.3-second exit and produced reverb remain intact.

The shared fade helper adds only an optional linear/exponential selection. **82 focused tests pass**, covering envelope shape, full-level incoming audio, delayed voice placement, vocal clearance, invalid input and clipping rejection. The new renderer retains full voice/ID/jingle audio and records exact fade/event positions and source hashes for verification. [Round-nine notes](local/playout-round9/README.md), [timings and measurements](local/playout-round9/results.json), [rendered-audio checks](local/playout-round9/validation.json)

Independent reconstruction verifies all **ten raw mixes**, including the complete retained voice/ID/jingle recordings, within 1.20e-7 PCM rounding error. All **20 final WAVs** pass stereo PCM24/44.1kHz, finite-sample, peak and static-gain checks. Maximum measured true peak is -1.2dBTP; B/C loudness differs by at most 0.01 LU within a pair. All 14 original and prepared source hashes and the processor calibration match. The listening page has 20 labeled native controls, and local links and completed-output overwrite protection pass. These checks establish file integrity and timing, while listening still determines whether the curve feels right.

**Preceding focused auditions: White Town's silent lead and SAM's fade range.** The [four-duration comparison](local/playout-whitetown-fades/index.html) uses Ms. Jackson → White Town, with B and C processing. White Town has about **0.25s of leading near-silence**, with sustained activity beginning near 0.26s. It occurs in both original and prepared audio. The preceding sample played from file time zero, so audible White Town arrived about 0.26s after its scheduled start, almost at the end of the 0.30s of fade then remaining. This directly explains the reported fade-first, music-later result.

The new comparison sets **Cue In to 0.25s**, retaining the onset and leaving Intro at its absolute file position **12.0s**. Its usable intro is therefore **11.75s**. Ms. Jackson's segue stays at 3:52. Each example starts the old fade at preview 7.85s and White Town at 8.00s: total fades are **0.45s** (control), **0.70s**, **1.00s**, and **1.50s**. Compression, source gains, linear curve and the 0.15s lead are held fixed. This is one duration comparison after correcting an ordinary start cue, not another song-specific scheduling rule. [Notes](local/playout-whitetown-fades/README.md), [exact timings](local/playout-whitetown-fades/results.json), [validation](local/playout-whitetown-fades/validation.json)

Independent source sums verify all **four raw mixes**, including the unaltered opening from Cue In, with maximum PCM rounding residual about 1.19e-7. Both original/prepared source hashes and the processor calibration match. All **eight final WAVs** pass stereo PCM24/44.1kHz, finite-sample, peak and static-gain checks; measured true peaks are at or below -1.2dBTP. B/C loudness differs by at most 0.01 LU within a pair, and comparison targets vary by only 0.03 LU across durations. **78 focused tests** pass, as do lint, format and native-player/link checks. Listening still selects the duration; these checks do not judge the sound.

**SAM's automatic fade behavior explains why its duration fields must be read together.** The supplied settings store `fs=700` and `fb=1500`; the user confirms milliseconds, giving a **0.7–1.5s minimum/maximum range**. The bundled SAM3 help says automatic mode finds a level drop near the ending, starts the next song and fades the outgoing audio according to the remaining time, bounded by min/max. When detection is too close to the end for the minimum duration, it begins fading earlier and starts the next song halfway through that minimum fade. Its example uses a three-second minimum and starts the next song 1.5s before the end. The ordinary Fade Out Time field is expressly ignored in auto-detected mode, so the saved `fot=5000` is not a five-second automatic fade. These are documented semantics, not a verified replay of this old installation. [Bundled help](local/playout-sam-reference/Cross_fading.htm)

The saved decks also enable removal of leading and trailing silence, with front/back thresholds of **-25/-32dB**. The official guide confirms separate controls for level detection, min/max fade duration, curves, and gap removal. Only fade/gap settings have been copied into [the reference snapshot](local/playout-sam-reference/fade-settings.json); numeric curve enums and the exact sign/reference of `ft=13` remain unverified. No SAM application or missing DSP plugin was run. This comparison adopts the known duration range and corrects Cue In; it does not claim to duplicate SAM's full detector or curve. [Spacial product guide, pp. 83–85](https://support.spacial.com/hc/en-us/article_attachments/214390087)

**Preceding auditions: the glide and voice handoff corrections.** The [voice and ID listening page](local/playout-round8-glide/index.html) contains six examples in B and C. Begin with **5** for White Town's voice handoff and **9** for the tighter cold Pink entry. **10** repeats the approved White Town ID transition as a control. Example 1's incoming Cue In is superseded by the focused comparison above. B and C keep the accepted processing; no compressor, source gain, or preset was retuned.

The outgoing music now fades for 0.45 seconds with its first 0.15 seconds before the incoming song. This implements the requested sequence: old song begins fading, new song enters at full level, old fade finishes. Voice links use that same short handoff at the actual music entry, including when a long voice delays it. They no longer retain a long overlapping outgoing tail. ID cuts and the jingle's produced tail remain separate policies.

White Town's approved segue is **4:16**, and Roxanne's intro was already correctly set to **0:09**. The late voice example came from the caller moving music to White Town's stored **4:17 outro**. The Christmas voice plays for **8.69195 seconds**, so it fits wholly within Roxanne's intro. The general correction keeps music at the segue and bounds the earliest voice start to it when the stored outro is later. Example 5 now starts voice and Roxanne together at 4:16, leaving **0.30805 seconds** before Roxanne's vocal. Neither stored cue was rewritten, and no song-specific scheduling branch was added.

The previous Blackstreet → Party ID → Pink example already had zero scheduled gap. A lightweight check found only about 0.05 seconds of near-silence at Pink's beginning; its sparse pickup can still sound loose. Example 9 now starts Pink **0.25 seconds before the full ID ends**, using the user's permitted cold-start overlap. This is a listening trial, not a claim that the opening is vocally clear. Blackstreet's fade to the ID is unchanged.

Reviewed segue values remain **Iyaz 3:10.5**, **Jordan Knight 3:48.5** (the final half-second correction), **White Town 4:16**, and **PinkPantheress 2:10**. Outkast remains at the 3:52 trial. Different corrections in opposite directions do not justify a universal detector offset: keep the quick energy suggestion, per-file listening correction, and one shared timing rule. [Reviewed cues](local/playout-segue-probe/reviewed-segues.json)

The focused transition, cue and mix-bus suite passes **78 tests**; lint and formatting checks pass. Exact timings, processing measurements and rendered-audio verification are linked from the [new notes](local/playout-round8-glide/README.md). These remain offline proofs; live AutoDJ was not changed.

**Initial round-eight auditions: separate music segue estimates, before the listening corrections above.** The user accepted the corrected voice placement but heard Ms. Jackson's music handoff arrive too late in its fade. It had no explicit segue, so the previous voice tests used its 242.207-second file end. A file boundary is a safe metadata fallback, but it can leave an unsuitable radio exit when the music has already become very quiet.

The new local probe measures stereo RMS in 0.25-, 0.5- and 1-second windows over each song's final minute. Original-file and prepared-file evidence are stored separately; normalization changes absolute levels, and the accepted leveler changes the fade shape somewhat. Sustained changes matter more than a single quiet syllable, drum gap or rest. This requires decoding and simple energy measurements, without transcription, beat analysis or library indexing. The results are **energy-based segue suggestions**, not vocal outro detection or beat/phrase alignment. [Probe notes](local/playout-segue-probe/notes.md), [original envelopes](local/playout-segue-probe/tail_energy_original.json), [prepared envelopes](local/playout-segue-probe/tail_energy_prepared.json)

| Song lacking a segue | Trial music segue | Evidence and limit |
| --- | --- | --- |
| Outkast, Ms. Jackson | **3:52.000** | Raw tail stays about 6 dB below its preceding reference from roughly 3:50.2 and 9 dB below from 3:53.2. The trial avoids the much quieter remainder and is 10.207 seconds earlier than the old file-end fallback. The **3:50 outro stays unchanged**. |
| Jordan Knight, Give It To You | **3:48.000** | Strong ending followed by a rapid terminal decay. This trial is near the start of that final drop; intro remains **0:08**. |
| White Town, Your Woman | **4:17.000** | Energy drops near 4:16.6, but the trial respects the supplied **4:17 outro** and **0:12 intro**. The exact later fade marker remains unset; the file ends at 4:17.097. |
| Kendrick, Not Like Us | **4:31.100** | Active audio nearly to the end, then near/digital silence; trial avoids waiting through the remaining file padding. |
| PinkPantheress, Boy's A Liar | **2:10.000** | Final drop into near silence, rather than an earlier rhythmic dip. |
| Iyaz, Replay | **3:11.000** | Rapid terminal decay after a strong passage around 3:10, before trailing silence. |
| Blackstreet, Don't Leave Me | **4:07.000** | In this supplied file the tail declines well before its final boundary. It stays roughly 6 dB below its preceding reference from 4:06.5 and 9 dB below from 4:09.5; the earlier trial avoids a weak handoff. |

All seven are provisional editorial selections from measured evidence, saved separately with file hashes in [trial-segues.json](local/playout-segue-probe/trial-segues.json). They do not replace any supplied outro or intro. Roxanne's 2:35 segue, Central Cee's 1:28 segue, and the jingle's 5.3-second next-song point remain unchanged. Unknown vocal outros stay unknown; the new voice examples use only outgoing tracks with supplied outro cues.

The [round-eight listening page](local/playout-round8/index.html) contains **nine examples, each in B and C**, exercising every proposed segue. Begin with Ms. Jackson → White Town for the music exit itself, then the short Christmas voice into Jordan Knight, then the full voice into White Town. In the short-voice example, speech starts at preview 9.208s, Jordan begins at 10s, speech ends at 17.9s and Jordan's vocal is at 18s. In the full-voice example, speech starts at 8s and ends at 26.283s; White Town deliberately waits until 14.383s so its vocal at 26.383s stays clear. This delay is required by the complete voice duration, even though the preferred music segue is earlier.

The other examples cover Jordan → All Day ID → Iyaz; White Town → Christmas voice → Roxanne; Kendrick → Party ID → Blackstreet; PinkPantheress → Plague → Jordan; Iyaz → All Day ID → White Town; and Blackstreet → Party ID → PinkPantheress's cold vocal. Plague starts Jordan at 5.3 seconds while retaining its original reverb. No source or processor tuning changed: both new songs receive the accepted B preparation, existing prepared songs are reused, v2 makeup remains 9.01 dB, and Stereo Tool uses the same preset. [Render notes](local/playout-round8/README.md), [exact timings and measurements](local/playout-round8/results.json)

Validation: **70 focused tests passed**. Independent source sums verified all nine mixes, the complete retained voice/ID content, unclipped summing, voice-start windows, vocal clearance, outgoing fades and jingle tail. All **18 final WAVs** are finite stereo PCM24 at 44.1kHz; maximum sample peak is approximately -1.20 dBFS, measured true peaks are at or below -1 dBTP, and paired B/C loudness differs by at most 0.01 LU. All 14 original and 14 prepared source hashes remain unchanged, as do manual intro/outro/end values. Lint/format and native audio label/link checks passed. The RMS probe was also checked against known opposite-phase stereo and silence. Listening still decides whether each estimated exit is musically appropriate. [Validation](local/playout-round8/validation.json)

**Accepted timing correction, before the new segue estimates: back-time voice within the outro-to-segue window, never after the next song starts.** The user identified an error in round-seven example 8: Roxanne's 2:24 outro was starting both the Christmas voice and Iyaz. The first correction held music until 2:35 but still started every voice at the outro. That is also superseded: align the voice toward the end of the available window, clamping its start between the outro and the next song's start. In example 8, the voice is shorter than Iyaz's intro, so both start at Roxanne's 2:35 segue. This intentionally leaves about 2.308 seconds between the voice ending and Iyaz's vocal. The [earlier timing listening page](local/playout-voice-backtimed/index.html) contains B/C versions of all four affected examples: 2, 8, 9 and the continuous program. Source preparation, the accepted B processor settings/makeup, and the Stereo Tool preset are reused unchanged.

| Corrected example | Voice starts in preview | Voice ends | Next song starts | Incoming vocal |
| --- | --- | --- | --- | --- |
| 8: Roxanne → Christmas voice → Iyaz | 19.000s | 27.692s | 19.000s | 30.000s |
| 2: Ms. Jackson → full voice → Iyaz | 12.825s | 31.107s | 20.207s | 31.207s |
| 9: Ms. Jackson → Christmas voice → Kendrick | 15.415s | 24.107s | 20.207s | 24.207s |

In those earlier examples Ms. Jackson had no separate segue, so its voice links waited for the decoded end at 242.207s instead of using the 230s outro as the music point. Round eight now tests the earlier 232s energy-based segue. The earlier continuous version lasts approximately 5:22.371. The other round-seven voice examples already delayed music past its planned point to clear longer speech, so their placement did not change in that timing correction.

The shared offline renderer now accepts a separate `liner_start_s` and opt-in `backtime_liner`; defaults preserve existing ID behavior. Tests cover a short voice capped at music start, a voice placed between outro and segue, a long link delaying the song, contradictory windows, and the no-segue fallback. **70 focused tests passed**, with lint and formatting checks also passing. Independent source sums verified all four corrected mixes, full voice content, unchanged music levels and the allowed voice-start window. All eight final B/C WAVs are stereo PCM24 at 44.1kHz, finite and without sample clipping. B/C loudness differs by at most 0.01 LU within each example; maximum measured sample peak is about -1.21 dBFS. All twelve source hashes match the accepted round. [Corrected timings](local/playout-voice-backtimed/results.json), [verification](local/playout-voice-backtimed/validation.json), [reproduction notes](local/playout-voice-backtimed/README.md), [reproduction script](local/playout-timing-fix/render.py)

**Accepted sound comparison: round seven, with source processing and the second voice track.** The [original round-seven listening page](local/playout-round7-final/index.html) retains ten short transitions and one continuous sequence, each with three processing versions. Examples 2, 8, 9 and 11 there have been superseded for timing by the correction above. The user clearly prefers B's source treatment and reports that C adds some smoothing without hurting it. All three versions have added dynamics processing; these are not raw-versus-processed labels.

| Version | Source preparation | Shared output processor |
| --- | --- | --- |
| A | Previous equal-peak source balance, with the existing 4.5 dB voice lift | Smooth v2 compressor and limiter |
| B | Music loudness matching and slow bounded leveling; separately compressed and louder voice; consistent ID gain | Same smooth v2 compressor and limiter |
| C | Exactly B's prepared mix | Initial free-feature Stereo Tool preset |

Music in B/C is matched to -22 LUFS before and after the leveler; the second match prevents the leveler from reintroducing whole-song loudness differences. The dynamic stage uses 500ms analysis frames, a 15-frame smoothing window, and a +4 dB maximum local boost. Voice gets a 70Hz high-pass filter and 3:1 compression, then a -17.5 LUFS source target; produced IDs target -19 LUFS. A further 6 dB of common headroom is reserved for mixing. These are internal source levels, not the intended transmission loudness. The voice compressor reduces the original voice file's measured crest factor from approximately 19.7 to 15.5 dB, allowing its quieter content to sit higher without the same peak increase. No sidechain duck is used.

An initial normalizer configuration exaggerated the Ms. Jackson fade. Its threshold interacted badly with the smoothing near the quiet ending, so it was rejected. With the corrected settings, the one-second RMS difference between 3:42 and 3:49 is approximately 4.2 dB, versus 6.6 dB in the source. The largest measured one-second gain movement from 3:20 through 3:58 is about 0.67 dB. These measurements establish a gentler gain curve, not a listening verdict or a removal of the recording's natural fade. The local gain cap limits how much quiet tails can be lifted. This is an offline, lookahead-based experiment; it is not yet a live AGC implementation. [FFmpeg dynamic normalization](https://ffmpeg.org/ffmpeg-filters.html#dynaudnorm)

V2 makeup gain is calibrated once per source-preparation variant using the full continuous program, then held fixed across all excerpts. Stereo Tool also uses one fixed preset. Most short examples have 22 seconds of hidden processing lead-in; the jingle begins at its file start. The continuous example runs about 5:15, retaining shared-processor state through Ms. Jackson → voice → Roxanne → Christmas voice → Central Cee → Party ID → Blackstreet. Final constant playback gain matches A/B/C within each example, allowing a quieter comparison level when necessary to retain true-peak headroom. It does not change the voice/music balance. Compare versions of the same example at the same player volume.

The current cue changes are:

| Asset | Round-seven timing |
| --- | --- |
| Kendrick | Intro / first protected vocal at 4.000s |
| Plague jingle | Outgoing point at 5.300s; next track starts there and the original reverb continues to 8.809s |
| Roxanne | Intro 9.000s; outro / earliest voice start 144.000s; separate preferred segue 155.000s |
| Iyaz | Intro 11.000s |
| Blackstreet | Intro 1.500s; used as an incoming track without guessing its musical ending |
| Central Cee | Intro 2.000s; outro 83.000s; segue 88.000s |
| Original voice | Entire 18.282608s recording retained |
| New `vt_christmas.wav` | Audition Cue In 0.400s; Cue Out 9.091950s, giving 8.691950s of played material |

The Christmas recording has about half a second of low room tone before clear activity. The suggested 0.400s cue leaves about 0.100s of lead-in. Its last nonzero source sample is at approximately 8.992s; the audition keeps another 0.100s and omits the remaining digital silence. Internal pauses and all subsequent nonzero source content are retained. These are local playback cues, not edits to the original WAV or automatically verified speech boundaries. The whole retained interval is protected when aligning the next song's intro.

**Stereo Tool is now an actual processing comparison.** The portable Windows CLI is version 11.05, obtained through the [official download page](https://www.thimeo.com/stereo-tool/download/) from `https://download.thimeo.com/stereo_tool_cmd.exe`. Its SHA-256 is `8847a03e1c33111c2dea07cea2e7f8e5f5b25cd2389b68874c668955b4c3231e`. The supplied preset enables AGC, the first five-band multiband compressor, and final limiting; FM emphasis and paid enhancement stages are disabled. A fixed AGC Drive setting of 4 brings our quiet mix into the processor's useful range. No license was purchased. The vendor distinguishes free features from licensed ones and states that the VST plugin always requires a license. This CLI audition does not establish the license tier needed for a future production preset. [Stereo Tool licensing](https://www.thimeo.com/stereo-tool/licenses/)

This first Stereo Tool setting is a comparison candidate, not a Tomass clone or a finished CHR preset. At its original Drive setting of 1, the test mix was barely changed, despite the AGC target shown in the log; that calibration was discarded. More drive alone is not a solution to masked speech. B versus C is intended to reveal whether the shared processor helps after source balance is improved. If Stereo Tool proves useful, evaluate the processing features and exact license against that accepted sound before buying anything.

The [round-seven renderer](scripts/render_radio_round7.py), [render notes](local/playout-round7-final/README.md), and [measurements and cue provenance](local/playout-round7-final/results.json) retain the processing settings and source hashes. `playout-round7` and `playout-round7-balanced` are intermediate calibration runs; use `playout-round7-final` for listening feedback. The original tracks, SAM settings, and live AutoDJ behavior remain unchanged.

Final checks: **57 focused tests passed**, and lint/format checks passed. Independent source-sum comparisons verified all 22 underlying A/B mixes, including full retained voice content, vocal clearance, outgoing fades, the natural jingle tail, and the continuous timeline. All **33 final WAVs** are finite stereo PCM24 at 44.1kHz, with equal frame counts within each comparison and no sample clipping. Measured true peaks are at or below -1.20 dBTP; A/B/C integrated-loudness differences within each example are at most 0.01 LU. Comparison levels range from -16.91 to -14.00 LUFS. Hashes confirm all twelve original media files are unchanged. Processor logs show the selected free stages without missing-license warnings, and the listening page has valid links and labeled native players without autoplay. [Mix checks](local/playout-round7-final/mix-validation.json), [file and page checks](local/playout-round7-final/validation.json)

These checks do not judge the sound, prove a live timing path, or establish screen-reader acceptance. The user has now selected B's source treatment and likes C's additional smoothing. Listening to the corrected timing and testing the real-time integration remain outstanding.

**Earlier v6 listening proofs: thirteen examples, no ducking, voice up 4.5 dB.** The [v6 raw listening page](local/playout-examples-v6/index.html) and [smoother v2 processing comparison](local/playout-processing-v2/index.html) remain available as historical controls. Their cue table below records that round's values, before the newer Kendrick, Plague and Roxanne changes. The [renderer](scripts/render_radio_examples.py) uses AutoDJ's `load_stereo`, `RenderedTrack`, `MixBus.render_block`, and native liner overlay with `duck_db=0`. A small offline driver prepares the music timing and feeds the bus. It bypasses the live `Player` selector and render-ahead worker, so this establishes a reusable audio path rather than an installed radio automation mode.

The version numbers describe two independent experiments, which has made the audition names confusing:

| Folder or label | What it demonstrates |
| --- | --- |
| `playout-examples-v6` | Earlier timing and fixed source gains, including +4.5 dB voice and the eleven-second Iyaz intro; no added dynamics processing |
| `playout-processing` | Earlier aggressive two-compressor/limiter trial on selected older mixes; rejected for audible pumping |
| `playout-processing-v2` | Smoother compressor/limiter trial on selected mixes; its voice-up examples use the v6 voice gain and revised Iyaz timing |
| `levelmatched` | The same processed result turned down to its corresponding raw mix's measured integrated loudness, to make the change in sound easier to compare |

“Raw” still includes the source recordings' own mastering, the harness's fixed gains and the chosen transition envelopes. It means no additional broadcast compressor or limiter. Round seven uses one page with the same timings and explicit processing labels, avoiding these independent version numbers.

The historical v6 markers and instructions were:

| Asset | Marker/policy used |
| --- | --- |
| Kendrick Lamar, Not Like Us | First protected vocal at 3.000s |
| PinkPantheress, Boy's A Liar | Cold vocal start at 0.000s; short IDs may overlap by the explicitly permitted 0.250s; the longer voice track must finish before the vocal |
| Outkast, Ms. Jackson | Outro reference at 230.000s (3:50); an ID cuts the outgoing song with a 0.450s fade; a voice track uses its available natural outro |
| IMAGING - ALL DAY | Decoded duration 4.832177s; music is permitted underneath throughout |
| IMAGING - WE'LL BRING THE PARTY | Decoded duration 5.563515s; music is permitted underneath throughout |
| Plague 25 1 | Revised segue at 5.600s; keep natural reverb through the decoded 8.809116s file end |
| vt_nfbco.wav | Entire 18.282608s file must play; fixed +4.5 dB after the harness's source peak adjustment; no automatic music ducking |
| Roxanne | First protected vocal at 9s; cold-ending segue at 155s (2:35); remaining file tail is retained |
| Central Cee, Doja | Intro 2s; corrected last vocal 83s (1:23); segue 88s (1:28) |
| Iyaz, Replay | Revised intro 11s (0:11); described as mostly cold ending, but no exact ending marker supplied; current test uses its intro |
| Blackstreet, Don't Leave Me | Newly supplied reference: intro approximately 1.5s and a cold ending; exact ending timestamp not supplied; measured for level comparison, not yet included in the thirteen renders |

No separate last-spoken-word markers were supplied for the imaging. The proof conservatively treats each ID's full decoded duration as protected speech, with the specific cold-start allowance above. The full voice file also receives 0.100s clearance. These policies can be refined without changing the source files.

Most excerpts begin with Outkast at 3:42. Roxanne's outgoing example begins at 2:27; Central Cee's begins at 1:20. All speech starts at eight seconds in the preview. Times below are positions within the preview, not the original songs:

| Listening example | Incoming song starts | ID/voice ends | First incoming vocal |
| --- | --- | --- | --- |
| [Outkast → Lamar, music segue](local/playout-examples-v6/01_outkast_kendrick.wav) | 8.000s | — | 11.000s |
| [Outkast → All Day ID → Lamar](local/playout-examples-v6/02_all_day_kendrick.wav) | 9.932s | 12.832s | 12.932s |
| [Outkast → Party ID → Lamar](local/playout-examples-v6/03_party_kendrick.wav) | 10.664s | 13.564s | 13.664s |
| [Outkast → All Day ID → PinkPantheress](local/playout-examples-v6/04_all_day_pink.wav) | 12.582s | 12.832s | 12.582s |
| [Outkast → Party ID → PinkPantheress](local/playout-examples-v6/05_party_pink.wav) | 13.314s | 13.564s | 13.314s |
| [Plague jingle → PinkPantheress](local/playout-examples-v6/06_plague_pink.wav) | 5.600s | — | 5.600s |
| [Outkast → full voice → Lamar](local/playout-examples-v6/07_voicetrack_kendrick.wav) | 23.383s | 26.283s | 26.383s |
| [Outkast → full voice → PinkPantheress](local/playout-examples-v6/08_voicetrack_pink.wav) | 26.383s | 26.283s | 26.383s |
| [Outkast → Party ID → Roxanne](local/playout-examples-v6/09_party_roxanne.wav) | 8.000s | 13.564s | 17.000s |
| [Roxanne cold end → full voice → Lamar](local/playout-examples-v6/10_roxanne_voicetrack_kendrick.wav) | 23.383s | 26.283s | 26.383s |
| [Outkast → Party ID → Central Cee](local/playout-examples-v6/11_party_central.wav) | 11.664s | 13.564s | 13.664s |
| [Central Cee at 1:28 → All Day ID → Roxanne](local/playout-examples-v6/12_central_all_day_roxanne.wav) | 8.000s | 12.832s | 17.000s |
| [Outkast → full voice → Iyaz, eleven-second intro](local/playout-examples-v6/13_voicetrack_iyaz.wav) | 15.383s | 26.283s | 26.383s |

The plain music segue fades only the outgoing song over two seconds. Short-ID transitions cut the outgoing song in 0.450s. Incoming songs have no fade-in. Voice-track transitions add no music fade or duck: the outgoing recording retains its natural ending. Outkast's tail ends at 20.207s in these previews. Lamar's shorter intro therefore leaves 3.175s of dry speech before its start; Iyaz enters while the tail is still playing, leaving no dry gap. Iyaz's revised eleven-second intro brings the song in two seconds earlier than the v5 test, while speech still clears the first vocal by 0.100s.

The raw files apply static per-file peak gain to 0.45 and then add 4.5 dB to the voice alone. This preserves mix headroom but is neither ReplayGain nor loudness matching. Boosting the original nearly full-scale voice WAV directly would be a different operation: here the extra gain follows the harness's initial attenuation. Original media are unchanged. Each excerpt ends with a 100ms preview fade, unrelated to the segue. [Exact timings and gains](local/playout-examples-v6/timing.json), [render notes](local/playout-examples-v6/README.md)

Validation: **46 focused tests passed**, comprising existing mix-bus tests and ten behavior checks for the audition harness. Ruff lint and formatting checks passed. Independent sample comparisons of the rendered WAVs verify the exact source sums, complete voice file, steady music under speech, vocal clearance, outgoing cuts, and preserved Plague tail. All thirteen WAVs are finite stereo PCM24 at 44.1 kHz without sample clipping. The listening page uses labeled native audio controls and no autoplay; a screen-reader/browser acceptance session is still needed. [Focused tests](tests/unit/test_radio_examples.py)

The v6 harness required liner starts on 20ms block boundaries. Round seven removes that restriction by padding a liner with the precise silence needed within its starting block; a regression test checks the non-aligned case. Arbitrary live cue scheduling still needs its own accuracy test: source inspection found that the existing deferred `LinerCue` path activates while a block is being rendered, which may place the liner earlier within that block than a caller expects. That live path was not changed or used to schedule these previews.

**Earlier processing findings: source balance first, then a shared processor.** AutoDJ's current mixer has gain/EQ/ducking and a sample clamp; it does not supply the complete broadcast compression chain being auditioned here. The installed Windows FFmpeg was sufficient for the earlier processing comparisons. Round seven additionally uses the portable Stereo Tool CLI.

The first [dense audition](local/playout-processing/index.html) used two serial broadband compressors and substantial makeup into a limiter, producing approximately -10.6 to -10.7 LUFS. The user heard useful smoothing but also audible wavering/pumping. That is grounds to reject this preset regardless of its passing peak and loudness measurements. It also confirms that a loudness target alone cannot specify the desired station sound.

The [v2 comparison](local/playout-processing-v2/index.html) uses one soft-knee, stereo-linked RMS compressor at 2:1, 30ms attack, and 400ms release, with a less-driven, four-times-oversampled limiter and a target near -13 LUFS. Limiter automatic level is disabled and lookahead latency is compensated. The user found v2 much smoother, with voice audibility the remaining problem. The latest comparisons retain these settings and instead lift the voice by 4.5 dB before mixing. The latest processed Party ID, Lamar voice link, and eleven-second-intro Iyaz link measure -13.00, -13.09, and -13.01 LUFS respectively, with true peaks no higher than -1.18 dBTP. Level-matched raw/processed controls help separate tonal and dynamics differences from loudness preference. Exact commands, measured values, and source versions are in the comparison folder. [FFmpeg compressor and limiter documentation](https://www.ffmpeg.org/ffmpeg-filters.html#acompressor)

**Listening conclusion from v6/v2: keep v2's smoother character, but improve voice treatment.** Even with the 4.5 dB lift, the user heard speech become squeezed or obscured over loud intros. That motivated round seven's separate voice compression, music loudness matching and bounded music leveling. Increasing the final compressor's drive alone cannot improve the voice-to-music ratio. Song-level ReplayGain or LUFS normalization improves source consistency but does not control individual words or undo a fade within a song.

The Ms. Jackson dip before the voice is substantially present in the supplied recording. In the eight-second lead-in, one-second RMS windows fall from -19.58 dBFS in the first second to -26.21 dBFS in the last, reaching -27.55 dBFS along the way. The corresponding v2 processed windows go from -14.27 to -19.78 dBFS. These are short-window energy measurements, not loudness judgments, but they establish that the harness did not introduce the decline: the raw lead-in is the source at fixed gain, with no fade or duck envelope. V2 reduces the first-to-last decline from about 6.6 to 5.5 dB, leaving an audible level-management question. A bounded, slower automatic gain stage or an earlier segue cue merits evaluation; a static whole-file gain cannot remove that decline.

Professional broadcast processing separates slow gain control, density control, and peak protection. Orban's technical material describes AGC followed by multiband compression and limiting, including how dominant bass can pull other frequencies down in a broadband processor. This is a useful architecture reference, not a paid-product recommendation. Our proposed free path is consistent music normalization/leveling, dedicated voice processing, then a shared processor such as master_me if the simple FFmpeg chain remains limiting. Preserve the user's preference for steady music under voice; any small gain reduction or spectral adjustment should be a deliberate audition choice. [Orban transmission-processing technical paper](https://www.orban.com/s/Broadcast-Transmission-Audio-Processing.pdf)

These are short-file auditions with static makeup calibrated per example, not proof of continuous program leveling or a finished CHR processor. A live chain must keep state across transitions, normalize different source types consistently, and be checked with the final encoder. If the required density still causes audible breathing after source balance is right, audition master_me's multiband processing; simply increasing broadband gain reduction is not the default next step.

**The supplied SAM Broadcaster configuration supports processing sources before mixing.** The reference is [SAMBC.core.xml](SAMBC/SAMBC.core.xml), whose metadata identifies SAM version 3.4.1. This is a saved configuration, not a verified recording of that installation's output. Inspection was limited to processing settings and preset files; SAM was not launched and no plugin was loaded.

| Saved location | What is enabled or referenced | Implication |
| --- | --- | --- |
| `SAM/Players/Player0`, `Player1`, `Player2` | Overall AGC enabled; Gated AGC, dual-band processor, stereo expander, bass EQ and clipper enabled; five-band processor disabled | Level and dynamics control can act on individual sources before their signals compete in the mix |
| `SAM/AudioPipe/Mixer/Channel/Filters/AGC` | Overall `Enabled=0`, despite enabled flags inside some saved sub-sections | The saved five-band settings here do not establish active mixer processing |
| Mixer `WinampDSP` | `Count=1`, referencing `dsp_TLV1.dll`, described as “Tomass' Limiter V1.0” | The DLL is absent, but `winamp.ini` contains a matching settings section; a second stored filename entry does not mean two active instances |
| `Player3` | Built-in AGC disabled; one DSP reference to `dsp_audiostocker3.dll`, described as “AudioStocker PRO” | Another referenced external processor is absent from the supplied folder |
| `Player4`, `Player5`, `VoiceFX/Record` | Built-in AGC disabled | Do not assume every source receives the same processing |

**The external limiter's settings were found in [winamp.ini](SAMBC/winamp.ini).** Its `[Tomass_Limiter]` section records:

| Stored control | Value |
| --- | --- |
| `Normalize` | `1` |
| `BassRel`, `HighRel` | `1000`, `1000` |
| `LowAttack`, `HighAttack` | `30`, `20` |
| `Process`, `Gate` | `125`, `60` |
| `Link`, `Ovd`, `Fbk` | `0`, `0`, `0` |
| `Expndr`, `Expdepth`, `Noclip`, `Noexpndr`, `Preemph` | All `0` |

This supplies a concrete external-processor reference, not just the built-in SAM presets. The normalization flag and separate bass/high timing controls suggest automatic level handling and frequency-dependent recovery are relevant to its sound. Exact units, scale conversions, the normalization algorithm, and the meanings of the inverse flags remain unverified; for example, `Gate=60` must not be treated as a +60 dB threshold. The plugin binary and a reference output recording are still missing. [SAMBC.ini](SAMBC/SAMBC.ini) also lists the Tomass and AudioStocker plugin names, but those inventory entries do not establish additional active instances or AudioStocker's processing parameters.

The enabled built-in deck chain stores `GatedAGC/GateLevel=-40` and `MaxGain=20`. Its dual-band section stores three processor groups per band. In both bands, `Proc0` has `Ratio=4`, `Threshold=-8`, `Attack=100`, `Attack2=500`, `Release=1000`, `Release2=2000`, and `Hold=12`; `Proc1` has `Ratio=2.5`, `Threshold=-30`, `Attack=3`, `Release=100`; `Proc2` stores `Ratio=0`, `Threshold=-3`, `Attack=50`, `Release=800`. Dual-band drive is `3`, bass EQ is `Freq=65` / `Gain=6`, and clipper level is approximately `-0.2`. These are raw configuration values: the precise old-version time units, secondary timing behavior, and meaning of `Ratio=0` have not been verified. They must not be copied verbatim into FFmpeg parameters. The saved music-player volumes also differ from other players, so these settings do not provide an independent gain calibration.

This is substantially different from v2's single compressor on an already combined signal. The likely useful design is gated gain riding plus band-specific control on each music source, a separately prepared voice signal, then shared peak protection. The settings make smoother compensation for a declining song plausible, but the missing external processors and lack of a SAM output capture prevent attributing its sound to one setting. The reference's bass boost, stereo expansion and VoiceFX auto-fade settings are not requirements for our implementation.

Preset filenames are not evidence of active processing. [Default.agc](SAMBC/AGCPresets/Default.agc) broadly resembles the deck chain but is not an exact match: its dual-band `Proc2` thresholds are approximately `-9.2` in band 0 and `-3` in band 1, compared with `-3` in both saved deck bands. [Rock Pop v1.agc](<SAMBC/AGCPresets/Rock Pop v1.agc>) enables both five-band and dual-band sections and contains very different timing/ratio values. It is not the chain selected by the saved deck flags. `rock.agc`, `jazz.agc` and `smoothjazz.agc` are byte-identical in this folder. Current Spacial guidance for the related SAM Cast processor advises against simultaneously running its dual- and five-band processors; the historical preset is therefore not an automatic template to copy. [Spacial processing documentation](https://support.spacial.com/hc/en-us/articles/229534747-Introduction-to-SAM-Cast-Sound-Processing-Features)

**Blackstreet confirms that peak matching is a weak basis for the next sound test.** Measurements of the supplied full files, using FFmpeg's integrated-loudness analysis, are:

| Source | Original integrated LUFS | Harness peak adjustment | Estimated LUFS after peak adjustment |
| --- | --- | --- | --- |
| Blackstreet, Don't Leave Me | -11.44 | -9.09 dB | -20.53 |
| Outkast, Ms. Jackson | -11.13 | -7.97 dB | -19.10 |
| Kendrick Lamar, Not Like Us | -9.16 | -7.39 dB | -16.55 |
| Supplied voice track | -17.21 | -6.41 dB | -23.62; approximately -19.12 after the additional 4.5 dB voice lift |

The final column is the measured source loudness plus the static gain, not a separately measured processed-file result. Blackstreet is about 2.3 LU below Lamar in the original files, but about 4 LU below after our equal-peak adjustment. The boosted voice also remains about 2.6 LU below the peak-adjusted Lamar track on this whole-file comparison. These averages do not predict every word or intro, but they explain why raising voice peaks and equalizing music peaks do not establish a consistent speech-to-music balance. Blackstreet's decoded duration is 258.456s; its user-supplied 1.5s intro is a useful short-intro test once source leveling is in place.

**Round seven implements the requested source-preparation experiment.** Replace peak-only music matching with measured loudness matching and adequate headroom; evaluate bounded, gated music gain riding; level/compress voice separately before its mix gain. Blackstreet and Lamar test quieter versus denser recordings, Iyaz tests the longer talk-up window, and the Ms. Jackson outro tests gain recovery. Preserve voice content, instrumental-window rules and the preference against pronounced music ducking. Blackstreet is used as an incoming track without guessing its unspecified musical end cue.

The new set includes a longer sequence with processor state retained across transitions, fixed processor settings across examples, and loudness-matched A/B/C controls. Source preparation is labeled separately from shared output processing. Aim for firm, smooth control slightly gentler than the user's Tomass reference. The implementation remains an offline experiment; no live application behavior changes.

After the sound is accepted, run the same sequence in real time and replace an editable next event. If AutoDJ needs substantial mixer changes to meet that test, reproduce the sequence with Rivendell's renderer/headless player and its API. Inspect Rivendell Web Broadcast before building overlapping browser features. Liquidsoap remains the next engine trial if neither path is economical.

| Test | Evidence required |
| --- | --- |
| Ordinary radio segue | Record a strong incoming entry while the outgoing song fades; assess energy, masking, and peaks on varied material |
| Ten-second ID / five-second intro | Next song begins at approximately five seconds into the ID, adjusted for clearance and permitted overlap |
| Thirty-second voice / ten-second tail and intro | Correct three-item placement; account explicitly for the uncovered ten seconds |
| Cold vocal or unknown marker | Safe dry/alternative arrangement; no automatic speech collision |
| Late insertion | Replace an editable next event, preserve linked speech context, and show the actual commitment boundary |
| Rolling category clock | Correct slot sequence, least-recent eligible selection, and track/artist separation across a restart |
| Dropped song | Unplayed event cancellation releases reservation; eligibility changes only through ordinary rules |
| Partial song | Log audible start and stop, and apply the chosen partial-play policy consistently |
| Hard/soft recovery | Produce a recording and event log showing anchor time, any skipped slots, and underfill handling |
| Tempo mix | Correct beat/phrase alignment and updated effective durations; clean fallback when unsuitable |
| Fault recovery | Missing file, late render, unavailable database/network, processor failure, encoder reconnect, and restart |
| Unattended/live control | Continues without a browser; safe takeover/return; separate audition output and usable keyboard operation |

For timing tests, state the measurement point and a proposed tolerance before running them. Test at the originating audio output first, then measure downstream delay. Simulate a long schedule for rule correctness and run an extended real-audio soak separately; neither test replaces the other.

A practical sequence is: prove sound and speech overlaps with known markers, prove the queue/control interface, add the rolling clock, then verify durable recovery and the production workflow. Add live assist and elaborate BPM behavior after those foundations pass. The initial engine proof needs no clockwheel UI or vocal-detection work.

**Questions that will materially change the ranking:**

1. What launch horizon is reasonable, and how much custom development is acceptable once the free audio engine is chosen?
2. What queue lookahead is useful to the operator while our script resolves the rolling clock?
3. Which anchors must be exact, what tolerance is acceptable elsewhere, and may music be faded early?
4. What genres dominate, how often should IDs/voice links occur, and how central is beat mixing?
5. Will voice tracks identify the previous/next song, or mostly contain reusable station presentation? How far ahead will they be recorded?
6. In which files or applications are the existing cue points stored, so we can import them instead of entering them twice?
7. Is the output internet-only, local sound-card audio, FM, or several destinations? What live console/microphone workflow is anticipated?
8. Which browser and screen reader should be used for acceptance testing of the requested accessible interface, and is remote multi-user operation needed?

My current choice is **a small AutoDJ radio-transition prototype, with headless Rivendell as the strongest existing-system alternative and Liquidsoap as the separate programmable engine option**. Manual markers and an accessible browser interface make extending AutoDJ credible. Rivendell's headless player, offline renderer, API, and third-party web tools make that alternative more capable than a desktop-only reading of its documentation suggests. Recorded transition results and safe live queue control should decide the foundation; attach the rolling clock afterward.

This work adds standalone rendering scripts, focused tests, and generated listening examples alongside this report. It does not yet integrate manual cues into the live player or implement the rolling clock. A portable Stereo Tool CLI was downloaded for round seven; no WSL packages or Rivendell installation were needed.

Not checked: listening acceptance of the shared exponential handoff, browser/screen-reader acceptance, Rivendell installation, or continuous-operation reliability. The user prefers the longer fade and tighter Pink entry. The new envelope follows SAM's documented shape without claiming its exact formula. The main integration risks remain safe edits to prepared audio and accurate cue/event scheduling in a running service; offline recordings do not establish those properties.

