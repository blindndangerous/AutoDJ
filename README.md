# AutoDJ

AutoDJ runs a music station from your own files. It can choose the next song by sound, follow a clockwheel and rotation categories, or play a queue you build. It also supports beatmatched sets with cues, tempo, key, and transition effects. You can listen in the browser, stream to a network player, or relay a station to Icecast or Shoutcast.

Music stays on your computer or server. A new library starts empty, so you choose which folders AutoDJ can read. The web page handles most daily work: add music, build a clockwheel, set repeat rules, adjust sound, schedule events, and manage stations.

## Start in five minutes

On Windows, install [Git](https://git-scm.com/downloads), then run in PowerShell:

```powershell
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
.\autodj.cmd
```

On Linux or macOS, install [Git](https://git-scm.com/downloads), [uv](https://docs.astral.sh/uv/getting-started/installation/), and [FFmpeg](https://ffmpeg.org/download.html), then run:

```bash
git clone https://github.com/blindndangerous/AutoDJ
cd AutoDJ
./autodj
```

The first run guides you through setup. Windows can offer to install missing tools. Open the web address printed in the terminal, usually `http://localhost:8080`. In **Library tools**, add a music folder. Then open **Now Playing** and start playback. You can queue songs in **Queue & Search** while the library finishes analysis.

For a server or NAS, use the [Docker and NAS steps](docs/guides/install.md#docker-and-nas). To reach the page from another device, use [LAN mode and browser pairing](docs/guides/install.md#use-autodj-from-other-devices).

## Build your station

- [Install AutoDJ](docs/guides/install.md) on Windows, Linux, macOS, Docker, or a NAS.
- [Add your music](docs/guides/add-your-music.md) and decide whether to run advanced analysis.
- [Build your sound](docs/guides/build-your-sound.md) with categories, clockwheels, repeat rules, and transitions.
- [Plan a schedule](docs/guides/schedule.md) for shows, ads, announcements, and time checks.
- [Go live](docs/guides/going-live.md) with a DJ, listener requests, and voice tracks.
- [Stream and relay a station](docs/guides/streaming-and-relays.md) to listeners or a hosted server.
- [Run several stations](docs/guides/several-stations.md) from one shared music library.
- [Back up and update](docs/guides/backups-and-updates.md) a station.
- [Use the keyboard and a screen reader](docs/guides/keyboard-and-screen-reader.md).
- [Troubleshoot AutoDJ](docs/guides/troubleshooting.md) when setup, playback, or streaming fails.
- [Look up settings](docs/settings-reference.md) for values, defaults, and the controls shown in the page.

Advanced analysis finds songs that sound alike and estimates key, energy, and tempo. Similarity picking and discovery use it. Without it, songs still play in queues, categories, and clockwheels; random and least recently played picking also work. Analysis can take hours on a large library, especially on a processor. You can start using the station before it finishes.

AutoDJ can also use beat grids, intro and outro points, harmonic mixing, and beatmatching for DJ style transitions. These features are optional. The [sound guide](docs/guides/build-your-sound.md) explains when they help.

You can use AutoDJ just for beatmatching and its library tools without running an automatic station.

## Tour of the web page

**Now Playing** controls playback and shows the next song. **Queue & Search** finds songs and builds a queue. **Track info** shows cues, song structure, and the **Tags** card. **History** lists past plays. **Carts** plays short audio on demand, and **Voice tracks** records breaks. **Ads** manages spots and reports. **Library tools** imports music, categories, clockwheels, and shows. **Settings** holds sound, song picking, schedule, streaming, access, and station controls. Some tabs appear only when their feature is available. See the [keyboard and screen reader guide](docs/guides/keyboard-and-screen-reader.md) for navigation.

## For administrators

The [install guide](docs/guides/install.md) covers Windows, Linux, Docker, and NAS storage. [Operations](docs/operations.md) has backup, restore, upgrade, network, and command details. Run `autodj doctor` to check an installation. Use **Settings**, **Profiles and access** for paired browsers and **Settings**, **Analysis workers** for machine tokens. Do not publish the control page without access controls.

## For contributors

Start with [Contributing](CONTRIBUTING.md) and the [architecture decision index](docs/adr/README.md). Coding agents should read [AGENTS.md](AGENTS.md) before changing the repository.

## Credits and licensing

AutoDJ is copyright © 2026 blindndangerous, jage9, and contributors. It is licensed under [AGPL version 3 or later](LICENSE). If you change AutoDJ and offer it over a network, that licence requires you to offer the changed source. Releases up to and including 0.19.1 remain available under MIT. AutoDJ is provided as is, without warranty; use it at your own risk.

Third-party credits:

- [MuQ model weights](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter) use CC BY-NC 4.0 and require attribution. AutoDJ downloads them separately and never redistributes them. Commercial use requires permission or a compatible, separately licensed model. See [Model selection](docs/model-selection.md).
- The base install and container include [mutagen](https://mutagen.readthedocs.io/) under GPL-2.0-or-later and LGPL-licensed soxr through [librosa](https://librosa.org/). Librosa itself uses ISC.
- [FAISS](https://github.com/facebookresearch/faiss) powers vector search. [FastAPI](https://fastapi.tiangolo.com/) serves the web page.
- Cue import reads [Mixxx](https://mixxx.org/), [Rekordbox](https://rekordbox.com/), and [Traktor](https://www.native-instruments.com/en/products/traktor/) libraries, and [Serato](https://serato.com/) cue tags.
- [Stereo Tool](https://www.thimeo.com/stereo-tool/) has separate terms. AutoDJ does not include it; each installation downloads it after you accept those terms.

## Contributors

- [blindndangerous](https://github.com/blindndangerous): project vision, library design, requirements, web UI direction, accessibility decisions, testing on a 10,000-track library, and release decisions.
- [jage9](https://github.com/jage9): station playout, folder libraries, cue editing, clockwheel rotation, play history, Smooth and Stereo Tool processing, doctor and launcher repairs, Windows AMD and MuQ work, keyboard shortcuts, the accessibility audit, CI, and testing.
- Claude (Anthropic): implementation across MuQ and librosa analysis, FAISS picking, audio mixing and effects, FastAPI and WebSocket controls, the web page, and tests.
- Codex (OpenAI): reviews, automated verification, model research, and documentation corrections.

If you contribute, add your name and the work you contributed.

AutoDJ and this documentation were developed with AI assistance. See [Contributing](CONTRIBUTING.md) to help.
