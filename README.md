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

The first run guides you through setup. Windows can offer to install missing tools. Open the web address printed in the terminal, usually `http://127.0.0.1:8080`. In **Library tools**, add a music folder. Then open **Now Playing** and start playback. You can queue songs in **Queue & Search** while the library finishes analysis.

For a server or NAS, use the [Docker and NAS steps](docs/guides/install.md#docker-and-nas). To reach the page from another device, use [LAN mode and browser pairing](docs/guides/install.md#use-autodj-from-other-devices).

## Build your station

- [Add your music](docs/guides/add-your-music.md) and decide whether to run advanced analysis.
- [Build your sound](docs/guides/build-your-sound.md) with categories, clockwheels, repeat rules, and transitions.
- [Stream and relay a station](docs/guides/streaming-and-relays.md) to listeners or a hosted server.
- [Run several stations](docs/guides/several-stations.md) from one shared music library.
- [Troubleshoot AutoDJ](docs/guides/troubleshooting.md) when setup, playback, or streaming fails.
- [Look up settings](docs/settings-reference.md) for values, defaults, and the controls shown in the page.

Advanced analysis finds songs that sound alike and estimates key, energy, and tempo. Similarity picking and discovery use it. Without it, songs still play in queues, categories, and clockwheels; random and least recently played picking also work. Analysis can take hours on a large library, especially on a processor. You can start using the station before it finishes.

AutoDJ can also use beat grids, intro and outro points, harmonic mixing, and beatmatching for DJ style transitions. These features are optional. The [sound guide](docs/guides/build-your-sound.md) explains when they help.

## For administrators

The [install guide](docs/guides/install.md) covers Windows, Linux, Docker, and NAS storage. [Operations](docs/operations.md) has backup, restore, upgrade, network, and command details. Run `autodj doctor` to check an installation. Use **Settings**, **Profiles and access** for paired browsers and **Settings**, **Analysis workers** for machine tokens. Do not publish the control page without access controls.

## Licence and contributors

AutoDJ is copyright © 2026 blindndangerous, jage9, and contributors. It is licensed under [AGPL version 3 or later](LICENSE). If you change AutoDJ and offer it over a network, that licence requires you to offer the changed source. AutoDJ comes without warranty; check your stream, backups, and licensing obligations before relying on it.

The default MuQ model downloads separately. Its [weights are licensed CC BY-NC 4.0](https://huggingface.co/OpenMuQ/MuQ-large-msd-iter), which forbids commercial use without separate permission. [Model selection](docs/model-selection.md) explains the supported model. Stereo Tool is separate software under [Thimeo's terms](https://www.thimeo.com/stereo-tool/) and downloads through **Settings**, **Sound** after you accept its terms.

AutoDJ and this documentation were developed with AI assistance. Human maintainers set requirements, review changes, and make release decisions. Claude contributed implementation across the application; Codex contributed reviews, verification, and documentation. See [Contributing](CONTRIBUTING.md) to help.
