# AMD GPU support

AMD uses the same [setup flow](setup.md) and launcher as every other installation:

```powershell
.\autodj.cmd setup
.\autodj.cmd serve
```

On Linux, use `./autodj`. Setup detects the graphics card, installs the matching ROCm
PyTorch packages, and verifies a GPU calculation before saving the runtime. No separate AMD
launcher or manual settings file is required. The driver must already be installed.

## Automatic detection

The Windows/Linux x86-64 installer maps explicit Radeon model names to device-specific
packages from AMD's [PyTorch installer](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html).
It uses ROCm 7.14.1 with PyTorch 2.12.0, torchvision 0.27.0, and torchaudio 2.11.0.
The model mappings live in `scripts/setup_hardware.py`; package recipes live in
`scripts/setup-profiles.json`.

Detection covers selected RX 9000 and RX 7000 desktop cards, Radeon PRO cards, and Radeon
700M, 800M, and 8000S integrated graphics. This is an explicit model list, not a promise that
every card in those series works. Unknown AMD adapters and combinations requiring different
device packages receive a CPU recommendation with an explanation. Existing compatible
environments can be adopted with `setup --backend amd --environment PATH`.

Package availability and a matching architecture are sufficient to offer setup; the actual GPU
test decides whether setup succeeds. Check AMD's installation documentation for OS and driver
requirements. A failing GPU test leaves the previous runtime selection intact and reports the
problem. Fix the driver or explicitly choose `setup --backend cpu`.

The Radeon 860M (`gfx1152`) has been tested locally with a real GPU calculation. Other mapped
models have detection and installation-plan tests, but have not all been physically tested.

## Everyday use

```powershell
.\autodj.cmd doctor
.\autodj.cmd index
.\autodj.cmd serve
```

The launcher remembers the verified environment, including for imports started from the web UI.
It does not replace AMD packages with the project's CPU lockfile. Existing configurations,
model downloads, and indexes are preserved. To preview setup, run `setup --dry-run`.

The indexer displays the device name and `ROCm GPU`. PyTorch exposes AMD through its `torch.cuda`
API; that name does not mean an NVIDIA card is being used. MuQ embeddings use the GPU, while
audio decoding and DJ analysis still use the CPU. Incremental indexing skips unchanged tracks;
switching devices alone does not require rebuilding the library.

Initial inference can be slower while MIOpen compiles kernels. The launcher retains its database
and kernel cache inside the selected environment, following AMD's
[MIOpen cache settings](https://rocm.docs.amd.com/projects/MIOpen/en/develop/conceptual/tuningdb.html).

## Dependency notes

Keep the project's Transformers dependency current. AutoDJ adapts MuQ's configuration and
final-layer output; downgrading Transformers is not required.

The tested AMD wheel requires `setuptools<82`, preventing installation of the setuptools 83 fix
for [PYSEC-2026-3447](https://osv.dev/vulnerability/PYSEC-2026-3447). That advisory concerns Unicode
filename exclusions while building source distributions on macOS; ordinary Windows inference
does not exercise that operation. A compatible patched AMD wheel is needed to resolve the
dependency finding. See [PyTorch's tracking issue](https://github.com/pytorch/pytorch/issues/187188).
