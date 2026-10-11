Station test results, 10 October 2026.

The local `station-playout` branch is ready for listening tests. Open [AutoDJ](http://127.0.0.1:8081) and use Listen. The server is running the Plague 2024 clock with Smooth processing, the 1.5-second fade, zero ducking and continuous playback enabled. Nothing has been pushed and no pull request has been created.

The import found 1,518 playable songs across the nine music folders beneath `C:\station\throwback`. Drops and Sweepers were added without subfolders. There are ten files in those two liner folders and two existing imaging copies in the upload folder. Stingers 2–5 have full overlap; the sweeper finishes before the next song starts. Fourteen exact file matches received the agreed cues.

Gold's 211 existing matching vectors were preserved exactly. The old index files and browser settings were backed up in `index\station-backup-20261010T091123Z`. Browser settings also have a `web_state.json.v1.bak` copy. Restart checks confirmed that folders, checkbox choices, cues, playback settings and the clock position were retained.

The checks covered:

- 600 Random walk selections from the real library, with no violations of the existing 50-song and 8-artist repeat windows. All nine music folders were represented, including songs without advanced analysis.
- A complete simulated PAL cycle: 20 music slots and seven liner slots, with the correct folders and selection rules. Live playback followed Top, Gold, a Drops stinger and 00a. The station kept playing with no listeners.
- Queue add, remove and reorder, Play now, early Skip, and a recorded Central Cee → Plague → White Town handoff. The final 147-second MP3 capture had finite audio, no clipped samples and no measured silent stretches of 20 milliseconds or more after startup. The processor reported no errors throughout that repeat run.
- A forced processor restart in a separate real-time audio test. It recovered through the limited bypass without measured silent blocks after startup.
- Chrome checks for folder controls, search, the point editor, keyboard focus, point provenance and the separate preview control. Previewing was not used to move the station.

The [recorded station output](.cache/station-live-check/central-plague-whitetown.mp3) is available locally. It includes the live Smooth processor, without Stereo Tool. The stream capture has a short joining delay; this is not a pause between songs. Its decoder logged one MP3 reservoir warning when joining the stream, then decoded the complete recording.

Background audio preparation and advanced analysis are still running. The library already plays without waiting for them. At the final runtime check, matching data had increased from 211 to 242 songs. An uncached Smooth selection can take longer to start; the first cold test took about 42 seconds to prepare its opening handoff, while the repeat used the cache.

The final Python suite passed 3,627 tests, with 18 skipped and 95.56% coverage. The JavaScript suite passed 803 tests. Ruff, formatting, mypy, Vulture, dependency checks, Bandit and ESLint passed. The separate JavaScript dead-code scan still reports existing local reference/build files; the new station modules were not among its findings.

Linux, an overnight run and the full Firefox/WebKit browser audit have not been tested here. The bundled Playwright browsers were unavailable; the browser smoke used installed Chrome. Missing album artwork returned an existing 404 but did not prevent point editing. Automatically suggested cues and the common liner level still need your ears, particularly on unfamiliar songs and uncompressed voice tracks. The two sample voice tracks were not in the registered folders and have not been added to the station rotation.

The EQ follow-up fixes the large tonal change caused by even a tiny slider movement. At 100% in all three bands, the repaired EQ reproduces the input exactly. A 1% change to any band changes the measured response by less than 0.1 dB across the tested frequencies, from 40 Hz to 16 kHz. Slider changes ramp over 20 milliseconds, and the filters keep their memory current at 100%. Boosted audio reaches the existing master compressor and limiter without clipping inside the EQ first; output protection remains in place. No controls or compressor settings changed.

All 295 tests in the player, mixer and stereo test files passed, including new checks for the frequency response, slider ramps, separate stereo channels and boosted audio reaching the master. Ruff, formatting and targeted type checks passed. These checks ran offline while the station kept playing. Loading the repair requires a server restart, which must wait for the user's approval; listening to the repaired EQ is still to come.
