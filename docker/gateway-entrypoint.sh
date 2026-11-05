#!/usr/bin/env bash
# Entrypoint of the hyper gateway image: Ignition 8.3 and gateway-agent in one container.
#
# Every boot:   gateway-agent bootstrap prepare   (API key, GatewayAgent level, permissions)
# First boot:   Ignition is started once so it creates its config, stopped, then
#               gateway-agent bootstrap finalize grants the agent API access.
# Then:         the agent runs under a restart loop, and Ignition's own entrypoint takes over
#               as the container's main process (tini is PID 1 and reaps both).
set -euo pipefail

readonly AGENT=/opt/gateway-agent/venv/bin/gateway-agent
readonly IGNITION_ENTRYPOINT=/usr/local/bin/docker-entrypoint.sh
readonly STATE="${AGENT_STATE_DIR:-/var/lib/gateway-agent}"

log() { echo "[gateway $(date -u +%H:%M:%S)] $*"; }

mkdir -p "$STATE"
chmod 700 "$STATE"

# Ignition commissions itself from these on first start. With no password given, one is
# generated and kept in the agent's state volume (docker exec ... cat it to sign in).
if [[ -z "${GATEWAY_ADMIN_PASSWORD:-}" && -z "${GATEWAY_ADMIN_PASSWORD_FILE:-}" ]]; then
  pw="$STATE/gateway-admin-password"
  if [[ ! -s "$pw" ]]; then
    (umask 077 && head -c 24 /dev/urandom | base64 | tr -d '/+=\n' > "$pw")
    log "generated the Ignition admin password ($pw)"
  fi
  export GATEWAY_ADMIN_PASSWORD_FILE="$pw"
fi

set +e
"$AGENT" bootstrap prepare
rc=$?
set -e
if [[ $rc -eq 3 ]]; then
  log "first boot: starting Ignition once so it creates its configuration"
  "$IGNITION_ENTRYPOINT" "$@" &
  ign=$!
  for _ in $(seq 1 200); do
    if curl -fsS -m 3 http://127.0.0.1:8088/StatusPing 2>/dev/null | grep -q '"RUNNING"'; then
      break
    fi
    if ! kill -0 "$ign" 2>/dev/null; then
      log "Ignition exited during first boot"; exit 1
    fi
    sleep 3
  done
  log "first boot: stopping Ignition to finish setup"
  kill -TERM "$ign"
  wait "$ign" || true
  "$AGENT" bootstrap finalize
elif [[ $rc -ne 0 ]]; then
  log "bootstrap failed (exit $rc)"; exit "$rc"
fi

# The agent: restarted if it ever exits; stops with the container.
(
  trap 'exit 0' TERM INT
  while true; do
    "$AGENT" run || true
    log "gateway-agent exited; restarting in 5 s"
    sleep 5
  done
) &

log "starting Ignition"
exec "$IGNITION_ENTRYPOINT" "$@"
