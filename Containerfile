# Manifest-list digests reviewed 2026-08-02 via registry Docker-Content-Digest headers.
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

FROM node:24.6.0-bookworm-slim@sha256:9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff AS frontend
WORKDIR /build
COPY package.json package-lock.json pyproject.toml vite.config.js ./
COPY src/autodj/static ./src/autodj/static
RUN npm ci --ignore-scripts --no-audit --no-fund && npm run build

FROM python-base AS package
COPY src ./src
COPY --from=frontend /build/src/autodj/static_dist ./src/autodj/static_dist
RUN uv export --frozen --only-group build --no-emit-project --format requirements-txt --output-file /tmp/build-constraints.txt \
    && uv build --wheel --out-dir /tmp/dist --build-constraints /tmp/build-constraints.txt --require-hashes

FROM python-base AS runtime
WORKDIR /app
COPY --from=package /tmp/dist /tmp/dist
RUN uv pip install --python /opt/venv/bin/python --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist \
    && groupadd --gid 10001 autodj \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin autodj \
    && install -d -o 10001 -g 10001 /app/.cache /index /models

ENV HOME=/home/autodj \
    XDG_CACHE_HOME=/app/.cache \
    HF_HOME=/models/huggingface \
    AUTODJ_HOST=0.0.0.0 \
    AUTODJ_PORT=8080 \
    AUTODJ_LIBRARY_MUSIC_DIR=/music \
    AUTODJ_INDEX_DIR=/index \
    AUTODJ_MODEL_DIR=/models

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD ["/opt/venv/bin/python", "-c", "import ipaddress, os, re, urllib.request; raw = os.environ['AUTODJ_HOST'].strip(); host = raw[1:-1] if raw.startswith('[') and raw.endswith(']') else raw; port = int(os.environ['AUTODJ_PORT']); assert 1 <= port <= 65535; ipv6 = ':' in host; assert (ipv6 and ipaddress.ip_address(host).version == 6) or (not ipv6 and re.fullmatch(r'(?!-)(?:[A-Za-z0-9-]{1,63}\\.)*[A-Za-z0-9-]{1,63}', host) and all(not label.endswith('-') for label in host.split('.'))); probe = '[::1]' if ipv6 else '127.0.0.1'; urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://{probe}:{port}/healthz', timeout=2).read()"]
ENTRYPOINT ["/opt/venv/bin/autodj"]
CMD ["serve", "--no-playback"]
