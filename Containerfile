# AutoDJ container image — Podman-native, Docker-compatible.
#
# Runs `autodj serve` (CPU-only) so users can `podman compose up` from
# a fresh clone.  Indexing remains a host task because GPU access
# would require nvidia-container-toolkit setup; mount the resulting
# index/ folder into the container for playback.
#
# Build:
#   podman build -t autodj:latest -f Containerfile .
# Run (compose handles the volume mounts):
#   podman compose up
# Or standalone:
#   podman run --rm -p 8080:8080 \
#     -v ./music:/music:ro -v ./index:/index \
#     autodj:latest

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv installs deps faster than pip + handles the lockfile we already ship.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# System deps for soundfile / librosa / ffmpeg-style decoding.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libsndfile1 \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps before copying source so layer cache survives
# unrelated source edits.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source.  Static assets ride along as a Python package
# resource so `autodj serve` finds them.
COPY src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8080

# Defaults — override via compose / -e on the command line.
ENV AUTODJ_HOST=0.0.0.0 \
    AUTODJ_PORT=8080 \
    AUTODJ_INDEX_DIR=/index

ENTRYPOINT ["uv", "run", "autodj"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
