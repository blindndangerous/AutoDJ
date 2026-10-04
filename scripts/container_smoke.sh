#!/usr/bin/env bash
# Build the image and smoke-test the Compose service: LAN mode on the host
# network, pairing, the health check, folder ownership, the CPU-only torch
# build, and stream mode switched on through config.toml.
set -euo pipefail

temp_parent="${RUNNER_TEMP:-/tmp}"
temp_parent="${temp_parent%/}"
if [[ -z "$temp_parent" || "$temp_parent" != /* || ! -d "$temp_parent" || ! -w "$temp_parent" ]]; then
  echo "RUNNER_TEMP must name an existing writable absolute directory" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"
if [[ -e config.toml ]]; then
  echo "config.toml already exists; the smoke test writes its own, so run it from a clean checkout" >&2
  exit 1
fi

smoke_root=""
compose_touched=false
base_url="http://127.0.0.1:8080"

emit_failure_diagnostics() {
  timeout --signal=TERM --kill-after=5s 15s \
    docker compose logs --no-color --tail 200 >&2 || true
  timeout --signal=TERM --kill-after=5s 10s \
    docker inspect --format '{{json .State}}' autodj >&2 || true
}

bounded_compose_down() {
  timeout --signal=TERM --kill-after=5s 30s \
    docker compose down --volumes --remove-orphans
}

cleanup() {
  exit_code=$?
  cleanup_exit_code=0
  trap - EXIT
  if [[ "$compose_touched" == true && "$exit_code" -ne 0 ]]; then
    emit_failure_diagnostics
  fi
  if [[ "$compose_touched" == true ]]; then
    bounded_compose_down || cleanup_exit_code=$?
  fi
  rm -f config.toml
  if [[ -n "${smoke_root:-}" && -d "$smoke_root" && "$smoke_root" == "$temp_parent"/autodj-smoke.* ]]; then
    rm -rf -- "$smoke_root" || cleanup_exit_code=$?
  fi
  if [[ "$exit_code" -ne 0 ]]; then
    exit "$exit_code"
  fi
  exit "$cleanup_exit_code"
}
trap cleanup EXIT

wait_until_healthy() {
  local status=""
  for _attempt in $(seq 1 60); do
    status="$(
      docker inspect autodj --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
        2>/dev/null || true
    )"
    case "$status" in
      healthy) return 0 ;;
      unhealthy)
        echo "Container became unhealthy" >&2
        return 1
        ;;
    esac
    sleep 1
  done
  echo "Container did not become healthy (status: $status)" >&2
  return 1
}

smoke_root="$(mktemp -d -- "$temp_parent/autodj-smoke.XXXXXXXX")"
chmod 0700 "$smoke_root"

# Folders owned by the invoking user, and the container running as that user:
# the documented way to use host folders without chown.
export AUTODJ_MUSIC_DIR="$smoke_root/music"
export AUTODJ_INDEX_DIR="$smoke_root/index"
export AUTODJ_MODEL_DIR="$smoke_root/models"
AUTODJ_UID="$(id -u)"
AUTODJ_GID="$(id -g)"
export AUTODJ_UID AUTODJ_GID
mkdir -p "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
chmod 0755 "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
unset AUTODJ_ACCESS_TOKEN

docker compose config >/dev/null
docker compose build --pull
compose_touched=true
docker compose up -d
wait_until_healthy

test "$(docker inspect autodj --format '{{.HostConfig.NetworkMode}}')" = "host"
test "$(docker compose exec -T autodj id -u)" = "$AUTODJ_UID"
test "$(docker compose exec -T autodj id -g)" = "$AUTODJ_GID"
for dir in /music /index /models; do
  test "$(docker compose exec -T autodj stat -c '%u:%g:%a' "$dir")" = "$AUTODJ_UID:$AUTODJ_GID:755"
done
docker compose exec -T autodj sh -ceu 'touch /index/.write-test; rm /index/.write-test'
docker compose exec -T autodj sh -ceu 'touch /models/.write-test; rm /models/.write-test'
# The server secret is created on first start and saved in the index folder.
test -s "$AUTODJ_INDEX_DIR/.access-token"
# Tag reading needs mutagen in the image; torch must be the CPU build with no CUDA wheels.
docker compose exec -T autodj /opt/venv/bin/python -c '
import importlib.metadata
import mutagen, scipy, torch, torchaudio
assert torch.version.cuda is None, torch.version.cuda
names = [dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()]
cuda = [name for name in names if name.startswith(("nvidia-", "cuda-")) or name == "triton"]
assert not cuda, cuda
'

# LAN mode hides library details until a browser pairs.
test "$(curl --fail --silent --show-error "$base_url/healthz")" = '{"status":"ok"}'
curl --silent --output /dev/null --write-out '%{http_code}' "$base_url/api/status" | grep -qx '401'

cookie_jar="$smoke_root/autodj.cookies"
pairing_code="$(docker compose exec -T autodj autodj devices pairing-code)"
if [[ ! "$pairing_code" =~ ^[0-9]{8}$ ]]; then
  echo "Container returned an invalid pairing code" >&2
  exit 1
fi
curl --fail --silent --show-error \
  --header "Origin: $base_url" \
  --header "Content-Type: application/json" \
  --cookie-jar "$cookie_jar" \
  --data-binary @- \
  "$base_url/api/pair" >/dev/null <<JSON
{"code":"$pairing_code","device_name":"Container smoke browser"}
JSON
curl --fail --silent --show-error --cookie "$cookie_jar" "$base_url/api/status" >/dev/null
health_payload="$(curl --fail --silent --show-error --cookie "$cookie_jar" "$base_url/healthz")"
python3 - "$health_payload" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
tracks = payload.get("tracks") if isinstance(payload, dict) else None
if (
    not isinstance(payload, dict)
    or payload.get("status") != "ok"
    or not isinstance(tracks, int)
    or isinstance(tracks, bool)
    or tracks != 0
):
    raise SystemExit(f"unexpected health payload: {payload!r}")
PY

# Stream mode comes from config.toml, which the container reads from this folder.
printf '[stream]\nenabled = true\n' > config.toml
docker compose up -d --force-recreate
wait_until_healthy
stream_secret="$(cat "$AUTODJ_INDEX_DIR/.stream-secret")"

# The smoke library has no tracks, so the stream only carries the encoded silence the bus sends
# while idle. Assert that the route, secret and encoder wiring answer: 200, audio/mpeg, and ICY
# metadata headers. A check of real music frames is left to a real library.
stream_headers="$smoke_root/autodj-stream-headers.txt"
curl --silent --show-error --max-time 5 \
  --dump-header "$stream_headers" --output /dev/null \
  --header "Icy-MetaData: 1" \
  "$base_url/stream/${stream_secret}.mp3" || true
grep -Eiq '^HTTP/[0-9.]+ 200' "$stream_headers"
grep -Eiq '^content-type: *audio/mpeg' "$stream_headers"
grep -Eiq '^icy-name:' "$stream_headers"
grep -Eiq '^icy-metaint:' "$stream_headers"
