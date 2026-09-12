# Experimental Windows AMD GPU setup

The default Windows `uv` environment installs the PyTorch build in `uv.lock`, which uses the CPU.
AutoDJ's indexer can use an AMD GPU when PyTorch has ROCm support, but this project does not
install AMD's ROCm packages by default. This guide creates a separate `.uv/amd` environment and
launcher; it leaves the standard `.venv`, `pyproject.toml`, and `uv.lock` alone.

This setup was tested on an AMD Ryzen AI 7 PRO 350 with Radeon 860M integrated graphics (`gfx1152`),
Python 3.14.6, PyTorch 2.12.0+rocm7.14.1, torchvision 0.27.0+rocm7.14.1, and torchaudio
2.11.0+rocm7.14.1. That is one tested configuration, not a general compatibility guarantee. Check
AMD's [ROCm 7.14.1 compatibility matrix](https://rocm.docs.amd.com/en/docs-7.14.1/compatibility/compatibility-matrix.html)
for supported Windows versions and drivers. AMD's [PyTorch install page](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html)
lists the device-specific wheel command below.

## Install

From a PowerShell prompt in the repository root, create an isolated environment and install AMD's
ROCm PyTorch wheels for `gfx1152`:

```powershell
uv venv --python 3.14.6 --seed .uv/amd
& .\.uv\amd\Scripts\python.exe -m pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ `
  "torch[device-gfx1152]==2.12.0+rocm7.14.1" `
  "torchvision[device-gfx1152]==0.27.0+rocm7.14.1" `
  "torchaudio==2.11.0+rocm7.14.1"
uv pip install --python .uv/amd/Scripts/python.exe -e ".[all]"
```

The AMD wheel command follows the versions and index on AMD's install page. For another GPU, select
the matching `device-gfx*` extra and supported versions from that page. The launcher uses MIOpen
caches under `.uv/amd/miopen/` so they stay with this environment.

If you use AutoDJ's web UI, install and build its frontend once:

```powershell
npm ci
npm run build
```

## Check the GPU and run AutoDJ

Confirm that PyTorch sees the Radeon device and reports its HIP runtime:

```powershell
& .\.uv\amd\Scripts\python.exe -c "import torch; print('available:', torch.cuda.is_available()); print('HIP:', torch.version.hip); print('device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```

Start indexing or the web server through the repository launcher:

```powershell
.\scripts\autodj-amd.cmd doctor
.\scripts\autodj-amd.cmd index --limit 50 --no-enrich --no-analyse
.\scripts\autodj-amd.cmd serve
```

Open the address printed by `serve` (by default `http://127.0.0.1:8080`). Use this launcher for web-based imports too:
the usual `uv run autodj serve` starts the standard environment, which uses CPU PyTorch on Windows.
The launcher can use a different environment for testing by setting `AUTODJ_AMD_ENV`; a relative
path is resolved from the repository root.

AMD uses the same automatic GPU selection, embedding pipeline, and GPU-eligible analysis steps as
NVIDIA. PyTorch's ROCm build exposes these through `torch.cuda`, so the indexer currently prints
`CUDA (GPU)` for AMD too. If no usable GPU is detected, AutoDJ falls back to CPU. To force CPU for
one PowerShell session, set `$env:AUTODJ_GPU = "0"`; remove it with `Remove-Item Env:AUTODJ_GPU`
to restore automatic selection.

Existing indexed tracks do not need to be re-embedded. Normal incremental indexing skips unchanged
files on either device. GPU processing improves throughput rather than embedding quality; small
floating-point differences are possible. Omit `--no-enrich --no-analyse` for the full maintenance
pipeline. `--workers` controls audio prefetch threads in the same way as other GPU installations.

The first inference can be much slower while MIOpen initializes and compiles kernels. The launcher
keeps its user database and kernel cache in the environment directory using AMD's documented
[MIOpen cache settings](https://rocm.docs.amd.com/projects/MIOpen/en/develop/conceptual/tuningdb.html).

On the tested laptop, one small repeated inference benchmark ran about 1.9x faster on GPU than CPU
after warmup. This is a smoke-test result; full index time also depends on audio decoding, model
loading, enrichment, and the particular GPU and driver.
