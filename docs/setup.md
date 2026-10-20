# Set up AutoDJ

For architecture and verification context, see the [ADR index](adr/README.md)
and [Proposed setup/model decision](adr/0012-separate-runtime-setup-from-model-execution.md).
The setup/model record documents current implementation for review; it
does not announce new hardware support.

Install Git and clone AutoDJ; the [README quick start](../README.md#start-in-five-minutes) has the commands.
For optional speech generation, configure existing provider connections in the
[speech studio](operations.md#speech-studio). AutoDJ does not install speech models
or provision endpoints, and saved generated audio plays without provider access.
The launcher obtains Python 3.14 when needed. Node.js is not required to run the application.

On Windows:

```powershell
.\autodj.cmd
```

On first start, Windows offers to install missing [uv](https://docs.astral.sh/uv/getting-started/installation/)
and [FFmpeg](https://ffmpeg.org/download.html) through WinGet. If WinGet is unavailable, the
launcher prints the manual installation instructions. It reuses tools already on PATH.

On Linux or macOS, install uv and FFmpeg first, then run:

```sh
./autodj
```

Starting `serve`, `doctor`, or another command interactively before setup also starts the setup
flow, then continues your original command after setup succeeds. Running the launcher without
arguments starts `serve`. Use the same launcher for all commands; there is no separate AMD command.

Setup detects the operating system and GPU, recommends a supported AMD or NVIDIA runtime or CPU,
asks for a web port, and shows the installation plan before changing anything. A new installation
starts with an empty library; add music folders from the Library page after starting AutoDJ.
It installs the selected PyTorch build before the application so it does not download a CPU build
just to replace it with a GPU build. GPU packages are pinned by the installation profiles; the
application's other dependency requirements are resolved during setup.

Setup verifies an actual tensor calculation and audio-library imports before remembering the
runtime. Unsupported hardware or unavailable hardware detection gets a clearly reported CPU
recommendation. A supported GPU whose driver/runtime fails the calculation is reported as a setup
failure; retry after fixing the driver or choose `setup --backend cpu`. No automatic GPU installation
can guarantee support for every driver, operating system release, or graphics card.

Existing configuration files, music, indexes, and model files are retained. Doctor runs in read-only
mode during setup. An old index or another doctor finding is reported without starting an expensive
rebuild or preventing a verified runtime from being remembered. Run `doctor` afterwards to review
its repair offers. Model downloads and indexing are separate actions unless `--test-import` is supplied.

## CPU-only installation

No backend flag is needed on a fresh CPU-only computer. Setup detects the hardware and saves
the CPU choice. Use `setup --backend cpu` only to deliberately force CPU, such as replacing a
previously saved GPU choice. You do not need an AMD environment or GPU drivers for CPU mode.

Use the launcher for ordinary commands. You do not need to activate an environment or run
`uv sync` separately.

## No-model installation

Use this mode when the server needs the web UI, basic library imports, browser
playback or server streams, but will not run the music embedding model locally:

```sh
./autodj setup --no-model --yes
./autodj serve
```

On Windows use `.\autodj.cmd` in place of `./autodj`. The saved selection is
`.uv/setup-no-model`; later launcher calls reuse it. No MuQ, PyTorch or
Transformers packages or model weights are installed. CPU audio processing still
uses NumPy, librosa and FFmpeg; no-model does not mean dependency-free. Set
`UV_NO_CACHE=1` during setup to avoid retaining a second installer cache.
Fresh no-model setup omits sound-card output packages. Browser playback and
server streaming are included; server sound-card output needs the `play` extra
and platform audio support. Existing sound-card packages are not removed.

Basic imports and previously saved analysis remain usable. `autodj index` imports
playable songs successfully and reports pending analysis without loading a model
or downloading weights. Automatic local MuQ analysis is disabled; explicit
advanced-analysis and worker commands explain how to enable it.
Queue, random, least-recently-played and category/clockwheel operation do not
require local model weights. Similarity needs compatible saved or remote vectors.

A remote model-capable worker can supply new analysis only when the server
already has a compatible model cache at its configured `model_dir` or
`manual_path`. The server uses that copy to validate the worker's exact weight
identity without loading the model. A fresh no-model installation cannot
bootstrap this cache with `autodj index`; preserve an existing compatible cache
or use a model-capable runtime to obtain it before returning to no-model mode.
See [remote worker requirements](guides/operations/gpu-worker.md#3-install-autodj-on-the-gpu-machine).

The ordinary setup still installs the complete CPU/GPU-capable runtime. To add
local model analysis later, run `./autodj setup --backend cpu` or the appropriate
supported GPU setup. Switching runtimes preserves the old environment; review
its disk use before installing another one. Model weights are downloaded only
when analysis is requested. `--no-model` cannot be combined with GPU setup or
`--test-import`.

For direct package installs, the base package is now the no-model serving set;
the `index` extra adds model analysis, `play` adds sound-card output, and `all`
adds both. Prefer guided CPU setup to select CPU wheels explicitly. The container
continues to install the `index` extra and its pinned CPU model runtime.
For a released wheel, use the [release-artifact install command](guides/install.md#release-artifacts)
with no extra for base serving, `[index]` for model analysis, `[play]` for
sound-card output without the model, or `[all]` for both. Keep the selected extra
when upgrading that wheel.

## Finding uv and FFmpeg

AutoDJ finds these programs on PATH, the operating system's list of executable directories.
FFmpeg handles common music formats and streaming. The Windows launcher includes registered
user and machine PATH entries in its own process, so tools installed by WinGet can be used
without opening a new terminal. This does not rewrite your PATH settings.

First-run setup offers to install missing tools with WinGet package IDs `astral-sh.uv` and
`Gyan.FFmpeg` for the current user. Starts that find an installed runtime do not install tools.
The prompt defaults to No. Use `.\autodj.cmd setup --yes` to approve these downloads and
the setup plan without prompts. This also accepts the packages' installation agreements.
For a manual installation, add the folder containing each executable to PATH.

## Moving an existing station

Install a fresh checkout on the destination; leave `.venv`, `.uv`, and their saved hardware
selection on the old computer. Use the same AutoDJ version on both machines for backup/restore.

1. Copy `config.toml` and `config.local.toml` if present, original music and external liner
   folders, and any configured external tools such as Stereo Tool. Preserve file paths where
   possible; changing drive letters requires updating saved paths, not just the configuration.
2. Run `.\autodj.cmd setup` on Windows, or `./autodj setup` on Linux/macOS. Decline the
   optional test import and leave the destination server stopped until after restoring.
3. On Windows, create a backup with `.\autodj.cmd backup station.zip`, copy it to the new
   checkout, then run `.\autodj.cmd restore station.zip` there. On Linux/macOS, use `./autodj`.
   See [Backup](operations.md#backup) and [Restore and validate](operations.md#restore-and-validate)
   for details. For the final handoff, stop the old server and analysis jobs before taking
   the final backup. Use `--force` only when intentionally replacing existing destination data.
4. Run `doctor`, check folder availability and the restored station settings, then start
   `serve` on the destination. Pair its browser again and update listening devices to its address.

The backup preserves catalog analysis and cues, so moving to CPU does not itself require
reanalyzing tracks when their files remain available at the recorded paths. Original audio,
configuration, model downloads, recordings, external tools,
and relay credentials are not included in the backup; transfer what you need separately.
Keep the old installation available until you have verified the new one.

## Options

Use `setup --help` to see all options. Common examples (replace `./autodj` with `./autodj.cmd` on Windows):

```sh
./autodj setup --dry-run
./autodj setup --port 8080 --yes
./autodj setup --backend cpu
./autodj setup --backend amd --environment .uv/existing-amd
./autodj setup --test-import
```

`--dry-run` inspects hardware and prints a plan without installing or writing files; bootstrap
Python or uv must already be available. `--yes` accepts the setup plan for unattended
installation; on Windows it also installs missing uv and FFmpeg through WinGet. If WinGet
is unavailable, install these tools beforehand. Help and `--dry-run` never install tools,
even with `--yes`. Optional
`--test-import` and `--serve` actions still require their flags in unattended mode. `--environment`
validates and adopts an existing installation without replacing its packages. Rerunning setup uses
the remembered runtime rather than creating a duplicate.

See [AMD GPU support](windows-amd.md) for the supported AMD families and driver requirements.
NVIDIA setup uses the configured CUDA wheel channel on Windows/Linux x86-64; CPU is the fallback on
other platforms, subject to Python package availability.

## Runtime storage and development

Setup records its verified choice in ignored `.uv/setup.json`; users do not need to edit this file.
The ordinary installed `autodj` and `python -m autodj` entry points honor it too. A missing or invalid
saved runtime produces an error instead of silently switching to another installation.

The normal launchers do not sync GPU packages against the CPU dependency lock. `uv run autodj`
also honors the saved runtime, but uv first creates or syncs its own `.venv`; use the root launcher
to avoid that duplicate. For development, `uv sync --frozen --all-extras` remains available and
`AUTODJ_RUNTIME=current` bypasses the saved selection. `AUTODJ_GPU=0` explicitly forces CPU.
