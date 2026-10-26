#!/usr/bin/env bash
# Switch Bull Desk's local Docker computers on or off. Expected state on this box: off.
# Usage: bull-desk-computers.sh status | on --i-understand-docker-socket | off
set -euo pipefail

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$STACK_DIR/.env"
OVERLAY="$STACK_DIR/docker-compose.vps.yml"
PROJECT=bull-desk
PROFILE=docker-computers
ON_VALUE=docker # docs/self-host-sandbox-providers.md: local Docker computers
OFF_VALUE=docker # NOT "none": the app schema has no "none" kind, so threads fail with HTTP 500. Off = no supervisor running, computers fail closed.

compose() {
  docker compose --env-file "$ENV_FILE" \
    -f "$STACK_DIR/infra/compose/docker-compose.yml" -f "$OVERLAY" "$@"
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# Reads only SANDBOX_PROVIDER; never prints anything else from .env.
env_provider() {
  sed -n 's/^SANDBOX_PROVIDER=//p' "$ENV_FILE" | tail -n 1
}

set_provider() {
  local value=$1
  if grep -q '^SANDBOX_PROVIDER=' "$ENV_FILE"; then
    sed -i "s/^SANDBOX_PROVIDER=.*/SANDBOX_PROVIDER=${value}/" "$ENV_FILE"
  else
    printf 'SANDBOX_PROVIDER=%s\n' "$value" >>"$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
}

service_state() {
  local state
  state="$(docker ps -a \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1" \
    --format '{{.State}}' | head -n 1)"
  printf '%s' "${state:-absent}"
}

# SANDBOX_PROVIDER as the running api actually sees it.
effective_provider() {
  local id
  id="$(docker ps -q \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=api" | head -n 1)"
  [[ -n "$id" ]] || {
    printf 'api not running'
    return
  }
  docker inspect "$id" --format '{{range .Config.Env}}{{println .}}{{end}}' |
    sed -n 's/^SANDBOX_PROVIDER=//p' | head -n 1
}

warn_if_not_passed_through() {
  if ! grep -q 'SANDBOX_PROVIDER' "$OVERLAY"; then
    printf 'warning: %s does not pass SANDBOX_PROVIDER to api/worker; base compose hardcodes "docker",\n' \
      "$(basename "$OVERLAY")" >&2
    printf '         so the .env value is ignored until the overlay sets it (see DEPLOY-NOTES.md).\n' >&2
  fi
}

cmd_status() {
  [[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE"
  printf 'SANDBOX_PROVIDER (.env):    %s\n' "$(env_provider)"
  printf 'SANDBOX_PROVIDER (api):     %s\n' "$(effective_provider)"
  printf 'supervisor container:       %s\n' "$(service_state supervisor)"
  printf 'computer image service:     %s\n' "$(service_state computer)"
  printf 'bot computers running:      %s\n' "$(docker ps -q --filter 'label=rakazo.managed=true' | wc -l | tr -d ' ')"
  warn_if_not_passed_through
}

cmd_on() {
  [[ "${1:-}" == "--i-understand-docker-socket" ]] ||
    die "refusing: pass --i-understand-docker-socket to turn computers on"
  grep -q '^SANDBOX_SUPERVISOR_TOKEN=..*' "$ENV_FILE" ||
    die "SANDBOX_SUPERVISOR_TOKEN is empty in .env"
  cat <<'EOF'
RISK: the supervisor mounts /var/run/docker.sock, which is root on this host.
Anything that escapes a bot computer or compromises the supervisor controls every
container here, including the trading services. Run 'off' as soon as you are done.
EOF
  set_provider "$ON_VALUE"
  compose --profile "$PROFILE" up -d
  warn_if_not_passed_through
  cmd_status
}

cmd_off() {
  set_provider "$OFF_VALUE"
  compose --profile "$PROFILE" stop supervisor computer
  compose --profile "$PROFILE" rm -f supervisor computer
  compose up -d
  warn_if_not_passed_through
  cmd_status
}

case "${1:-}" in
  status) cmd_status ;;
  on)
    shift
    cmd_on "$@"
    ;;
  off) cmd_off ;;
  *) die "usage: $(basename "$0") status | on --i-understand-docker-socket | off" ;;
esac
