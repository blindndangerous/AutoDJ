#!/usr/bin/env bash
set -euo pipefail

temp_parent="${RUNNER_TEMP:-/tmp}"
temp_parent="${temp_parent%/}"
if [[ -z "$temp_parent" || "$temp_parent" != /* || ! -d "$temp_parent" || ! -w "$temp_parent" ]]; then
  echo "RUNNER_TEMP must name an existing writable absolute directory" >&2
  exit 1
fi

smoke_root=""
compose_touched=false
lan_phase_active=false

emit_lan_failure_logs() {
  timeout --signal=TERM --kill-after=5s 15s \
    docker compose --profile lan logs --no-color --tail 200 autodj-lan >&2 || true
}

bounded_compose_down() {
  timeout --signal=TERM --kill-after=5s 30s \
    docker compose --profile lan down --volumes --remove-orphans
}

cleanup() {
  exit_code=$?
  cleanup_exit_code=0
  trap - EXIT
  if [[ "$lan_phase_active" == true && "$exit_code" -ne 0 ]]; then
    emit_lan_failure_logs
  fi
  if [[ "$compose_touched" == true ]]; then
    bounded_compose_down || cleanup_exit_code=$?
  fi
  if [[ -d "${smoke_root:-}" && -O "$smoke_root" && "$smoke_root" == "$temp_parent"/autodj-smoke.* ]]; then
    rm -rf -- "$smoke_root" || {
      removal_exit_code=$?
      if [[ "$cleanup_exit_code" -eq 0 ]]; then
        cleanup_exit_code=$removal_exit_code
      fi
    }
  fi
  if [[ "$exit_code" -ne 0 ]]; then
    exit "$exit_code"
  fi
  exit "$cleanup_exit_code"
}
trap cleanup EXIT

smoke_root="$(mktemp -d -- "$temp_parent/autodj-smoke.XXXXXXXX")"
chmod 0700 "$smoke_root"
if [[ "$smoke_root" != "$temp_parent"/autodj-smoke.* || ! -O "$smoke_root" ]]; then
  echo "mktemp returned an unexpected or unowned smoke directory" >&2
  exit 1
fi

export AUTODJ_MUSIC_DIR="$smoke_root/music"
export AUTODJ_INDEX_DIR="$smoke_root/index"
export AUTODJ_MODEL_DIR="$smoke_root/models"
mkdir -p "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
chmod 0755 "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
sudo chown 10001:10001 "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"

unset AUTODJ_ACCESS_TOKEN AUTODJ_LAN_HOST AUTODJ_LAN_ORIGIN
docker compose config >/dev/null
docker compose --profile lan config >/dev/null
docker compose build --pull
lan_negative_log="$smoke_root/autodj-lan-negative.log"
compose_touched=true
set +e
timeout 15s docker compose --profile lan run --rm --no-deps autodj-lan \
  >"$lan_negative_log" 2>&1
lan_exit_code=$?
set -e
if [[ "$lan_exit_code" -eq 0 ]]; then
  echo "LAN service unexpectedly started without authentication/origin inputs" >&2
  exit 1
fi
if [[ "$lan_exit_code" -eq 124 ]]; then
  echo "LAN validation timed out instead of rejecting incomplete security inputs" >&2
  cat "$lan_negative_log" >&2
  exit 1
fi
grep -Eiq 'requires|access[ _-]token|allowed[ _-](host|origin)|invalid.*origin' \
  "$lan_negative_log"
docker compose up -d
health_payload="$(curl --fail --retry 30 --retry-delay 1 --retry-connrefused \
  http://127.0.0.1:8080/healthz)"
python3 - "$health_payload" <<'PY'
# HEALTH_VALIDATOR_START
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
# HEALTH_VALIDATOR_END
PY

test "$(docker compose exec -T autodj id -u)" = "10001"
test "$(docker compose exec -T autodj id -g)" = "10001"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /music)" = "10001:10001:755"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /index)" = "10001:10001:755"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /models)" = "10001:10001:755"
docker compose exec -T autodj sh -ceu 'touch /index/.write-test; rm /index/.write-test'
docker compose exec -T autodj sh -ceu 'touch /models/.write-test; rm /models/.write-test'

host_ip="$(docker inspect autodj --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostIp}}')"
test "$host_ip" = "127.0.0.1"

bounded_compose_down
export AUTODJ_LAN_BIND_ADDRESS=127.0.0.1
export AUTODJ_ACCESS_TOKEN
AUTODJ_ACCESS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AUTODJ_LAN_HOST=radio.local
export AUTODJ_LAN_ORIGIN=http://radio.local:8080
lan_phase_active=true
docker compose --profile lan up -d autodj-lan

lan_health_status=""
for _attempt in $(seq 1 30); do
  lan_health_status="$(
    docker inspect autodj-lan --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
      2>/dev/null || true
  )"
  case "$lan_health_status" in
    healthy) break ;;
    unhealthy)
      exit 1
      ;;
  esac
  sleep 1
done
if [[ "$lan_health_status" != healthy ]]; then
  echo "Authenticated LAN container did not become healthy (status: $lan_health_status)" >&2
  exit 1
fi

curl --fail --silent --show-error \
  --header "Host: $AUTODJ_LAN_HOST" \
  http://127.0.0.1:8080/healthz >/dev/null

lan_cookie_jar="$smoke_root/autodj-lan.cookies"
curl --fail --silent --show-error \
  --header "Host: $AUTODJ_LAN_HOST" \
  --header "Origin: $AUTODJ_LAN_ORIGIN" \
  --header "Content-Type: application/json" \
  --cookie-jar "$lan_cookie_jar" \
  --data-binary @- \
  http://127.0.0.1:8080/api/login >/dev/null <<JSON
{"token":"$AUTODJ_ACCESS_TOKEN"}
JSON
curl --fail --silent --show-error \
  --header "Host: $AUTODJ_LAN_HOST" \
  --cookie "$lan_cookie_jar" \
  http://127.0.0.1:8080/api/status >/dev/null
