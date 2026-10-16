# Install AutoDJ

This guide installs AutoDJ on Windows, Linux or macOS, or in Docker on a server or NAS. It then
shows how to open AutoDJ from other devices on your network.

## Choose how to install

- **Windows**: the easiest way. One command installs everything AutoDJ needs.
- **Linux or macOS**: the same launcher; you install two tools first. On macOS, analysis always
  runs on the processor, not the graphics card.
- **Docker**: for a home server or NAS that runs containers. Analysis runs on the processor.

Whichever you choose, AutoDJ runs on your own computer. Your music is never uploaded anywhere.

## What you need

- A computer that stays on while the station plays.
- Your music on that computer, or on a network share it can open.
- Several gigabytes of free disk space for AutoDJ and its analysis model. The first setup
  downloads several gigabytes.
- An internet connection for the first setup and the first analysis. After that, AutoDJ works
  offline.
- Optional: an NVIDIA or a recent AMD graphics card, which makes song analysis much faster.

## Windows

1. Install [Git for Windows](https://git-scm.com/downloads).
2. Open PowerShell in the folder where you want AutoDJ, and run:

   ```powershell
   git clone https://github.com/blindndangerous/AutoDJ
   cd AutoDJ
   .\autodj.cmd
   ```

3. The first time, AutoDJ checks for two tools: uv, which installs Python and AutoDJ's packages,
   and FFmpeg, which plays MP3 and M4A files and makes the stream. If either is missing, it asks:
   "Download and install ... for your Windows account with WinGet? [y/N]". Type `y` and press
   Enter. The default answer is No.
4. Guided setup starts. See [First start](#first-start).

Later, start AutoDJ with the same command, `.\autodj.cmd`. Use it for every other command too,
such as `.\autodj.cmd doctor`. You never need to activate anything first.

## Linux and macOS

1. Install [Git](https://git-scm.com/downloads), [uv](https://docs.astral.sh/uv/getting-started/installation/)
   and [FFmpeg](https://ffmpeg.org/download.html). Most Linux systems have Git and FFmpeg in
   their package manager.
2. In a terminal, run:

   ```bash
   git clone https://github.com/blindndangerous/AutoDJ
   cd AutoDJ
   ./autodj
   ```

3. Guided setup starts. See [First start](#first-start).

Later, start AutoDJ with `./autodj`, and use it for every other command, such as
`./autodj doctor`.

## First start

The first start runs guided setup. It says "Welcome to AutoDJ. Let's detect your hardware and
set up your music library." Then it:

1. Detects your processor and graphics card, and recommends CPU, NVIDIA or AMD. It explains its
   choice.
2. Asks for a web port, `Local web port [8080]:`. Press Enter to keep 8080 unless another
   program uses it.
3. Shows the installation plan and asks `Apply this setup plan? (y/n) [n]:`. Type `y`.
4. Installs Python and AutoDJ's packages, then checks that the processor or graphics card really
   works.
5. Offers to test importing three tracks. You can answer No; you will add music in the page.

When setup finishes, AutoDJ starts. It prints the address of its web page, normally
`http://localhost:8080`. Open that address in a browser on the same computer. AutoDJ does not
open a browser by itself unless you start it with `--open`.

To install without starting AutoDJ, run `.\autodj.cmd setup` on Windows or `./autodj setup`
elsewhere. [Set up AutoDJ](../setup.md) explains every setup option, such as choosing CPU on
purpose.

Your library starts empty. Next, [add your music](add-your-music.md).

### Graphics cards

Setup picks a graphics card automatically when it finds a supported one:

- NVIDIA cards on Windows and Linux, with a driver that supports CUDA 13.0.
- Recent AMD cards and laptop graphics on Windows and Linux. [AMD GPU support](../windows-amd.md)
  lists them.

Everything else uses the processor. That works; it is only slower. A graphics card speeds up
song analysis, not playback.

## Docker and NAS

AutoDJ ships a `compose.yaml` file for Docker Compose. In Docker, AutoDJ runs in LAN mode:
other devices on your network can open it, and each browser pairs once with a code.

1. On the server, clone AutoDJ:

   ```bash
   git clone https://github.com/blindndangerous/AutoDJ
   cd AutoDJ
   ```

2. Create a file named `.env` beside `compose.yaml`, with your own paths:

   ```dotenv
   AUTODJ_MUSIC_DIR=/volume1/music
   AUTODJ_INDEX_DIR=./index
   AUTODJ_MODEL_DIR=./models
   AUTODJ_UID=1000
   AUTODJ_GID=1000
   ```

   - `AUTODJ_MUSIC_DIR` is your music folder. Inside the container it is `/music`.
   - `AUTODJ_INDEX_DIR` is where AutoDJ keeps its library, settings and logs.
   - `AUTODJ_MODEL_DIR` is where the analysis model is downloaded.
   - `AUTODJ_UID` and `AUTODJ_GID` are the user and group that own those folders. Find them with
     `id -u` and `id -g`. Without them, the container runs as user 10001, which may not be
     allowed to write your folders.

3. Create the folders and start AutoDJ:

   ```bash
   mkdir -p index models
   docker compose up -d --build
   ```

4. Find the address and the pairing code:

   ```bash
   docker compose logs autodj
   ```

   AutoDJ prints the addresses to open, such as `http://radio.example.com:8080`, and an 8-digit pairing code.
5. Open one of the addresses in a browser and type the code.
6. In **Library tools**, add the folder `/music`.

The container restarts by itself after a crash or a reboot. It uses the server's own network, so
the addresses are the server's own names and addresses.

Some things to know:

- Settings that live in files, such as `[station] output` or HTTPS certificates, go in
  `config.toml` and `config.local.toml` beside `compose.yaml`. Give paths inside that folder
  as relative paths, such as `ssl_certfile = "certs/fullchain.pem"`. Restart with
  `docker compose up -d` after a change.
- The image includes FFmpeg, and its analysis runs on the processor.
- The music folder is mounted read-only, so AutoDJ cannot write tag edits into your files.
- To use a [beets](https://beets.io/) library, set `AUTODJ_BEETS_PATH` to the folder that holds
  beets' `library.db`, and set `beets_db = "/beets/library.db"` in the `[library]` section of
  `config.local.toml`.
- Stereo Tool is not in the image. Download it from Settings, Sound; it is saved in the index
  folder.
- To get a new pairing code later, run
  `docker compose exec autodj autodj devices pairing-code`.

### A NAS without a graphics card

Analysis on a NAS processor is slow. Another computer with a graphics card can analyse new songs
for it over the network: create a token in Settings, **Analysis workers**, then run
`autodj worker` on that computer. It downloads each song from the NAS, so it needs no copy of
your music. See [Using a GPU machine](../operations.md#using-a-gpu-machine).

## Use AutoDJ from other devices

By default, AutoDJ answers only on the computer it runs on. To open it from a phone, tablet or
another computer on your network, start it in LAN mode:

- Windows: `.\autodj.cmd serve --lan`
- Linux and macOS: `./autodj serve --lan`

AutoDJ prints its network addresses. While no browser is paired, it also prints an 8-digit pairing code.

1. On the other device, open one of the addresses.
2. Type the pairing code. That browser stays paired; it does not need the code again.
3. For the next device, get a new code: run `autodj devices pairing-code`, or press **Show
   pairing code** in Settings, **Profiles and access** on a browser that is already paired.

Each code pairs one device and changes every five minutes. To make LAN mode permanent, add this
to `config.toml` and restart AutoDJ:

```toml
[server]
lan = true
```

Settings, **Profiles and access** lists the paired devices with **Rename** and **Revoke**.
**Sign out this browser** ends the pairing of the browser you are using.

If another device cannot connect, check the firewall on the AutoDJ computer. Windows may show a
Windows Security prompt the first time; allow access on private networks. On Linux, open the
port, for example `sudo ufw allow 8080/tcp`.

Listeners do not pair. Anyone who can reach the address can listen to the stream; see
[Streaming and relays](streaming-and-relays.md).

### HTTPS

Over plain HTTP from another device, four transition effects (gate stutter, bitcrusher, freeze
and glitch) play as a plain crossfade in that browser, because browsers allow them only on HTTPS
pages. HTTPS also protects pairing codes on the network.

To use HTTPS, give AutoDJ a certificate and its private key in `config.toml`:

```toml
[server]
ssl_certfile = "certs/radio.pem"
ssl_keyfile = "certs/radio-key.pem"
```

Every device must trust the certificate. [HTTPS on your home network](../operations.md#https-on-your-home-network)
shows how to make one.

## Move an existing station to a new computer

Install AutoDJ fresh on the new computer, then restore a backup from the old one. Do not copy the
`.uv` folder between computers. [Moving an existing station](../setup.md#moving-an-existing-station)
has the steps.

## Update AutoDJ

Back up first with `autodj backup`. The [upgrade checklist](../operations.md#upgrade-checklist)
has the full steps until the Backups and updates guide is written.

## Related pages

- [Add your music](add-your-music.md)
- [Troubleshooting](troubleshooting.md#installing-and-starting)
- [Settings reference: Server](../settings-reference.md#server)
