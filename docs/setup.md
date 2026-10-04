# Set up AutoDJ

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone AutoDJ.
The launcher obtains Python 3.14 when needed. Node.js is not required to run the application.
Install [FFmpeg](https://ffmpeg.org/download.html) for MP3 indexing, AAC/M4A decoding, and radio streaming.

On Windows:

```powershell
.\autodj.cmd setup
.\autodj.cmd serve
```

On Linux or macOS:

```sh
./autodj setup
./autodj serve
```

Starting `serve`, `doctor`, or another command interactively before setup also starts the setup
flow, then continues your original command after setup succeeds. Running the launcher without
arguments starts `serve`. Use the same launcher for all commands; there is no separate AMD command.

Setup detects the operating system and GPU, recommends a supported AMD or NVIDIA runtime or CPU,
asks for your music folder and port, and shows the installation plan before changing anything.
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

## Options

Use `setup --help` to see all options. Common examples (replace `./autodj` with `./autodj.cmd` on Windows):

```sh
./autodj setup --dry-run
./autodj setup --music-dir /path/to/music --port 8080 --yes
./autodj setup --backend cpu
./autodj setup --backend amd --environment .uv/existing-amd
./autodj setup --test-import
```

`--dry-run` inspects hardware and prints a plan without installing or writing files. `--yes` accepts
the setup plan for unattended installation; a new installation also needs `--music-dir`. Optional
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
