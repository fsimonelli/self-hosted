#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"
PROTON_SERVICE="${PROTON_SERVICE:-protonvpn}"
WEBUI_PORT="${QBITTORRENT_WEBUI_PORT:-8080}"

dc=(docker compose)
if [[ -f "$ENV_FILE" ]]; then
  dc+=(--env-file "$ENV_FILE")
fi
dc+=(-f "$COMPOSE_FILE")

run_dc() {
  "${dc[@]}" "$@"
}

if ! run_dc config --services | grep -qx "$PROTON_SERVICE"; then
  echo "CRITICAL: service '$PROTON_SERVICE' not found in compose stack"
  exit 2
fi

if ! run_dc ps "$PROTON_SERVICE" >/dev/null 2>&1; then
  echo "CRITICAL: cannot query Docker Compose runtime (daemon access or stack state issue)"
  exit 2
fi

forwarded_port="$(run_dc exec -T "$PROTON_SERVICE" sh -lc 'cat /tmp/gluetun/forwarded_port 2>/dev/null || true' | tr -d '\r\n[:space:]')"
if [[ -z "$forwarded_port" || ! "$forwarded_port" =~ ^[0-9]+$ ]]; then
  echo "CRITICAL: Gluetun forwarded port is missing or invalid"
  exit 2
fi

preferences_json="$(run_dc exec -T "$PROTON_SERVICE" sh -lc "wget -qO- --timeout=10 http://127.0.0.1:${WEBUI_PORT}/api/v2/app/preferences" 2>/dev/null || true)"
if [[ -z "$preferences_json" ]]; then
  echo "CRITICAL: cannot query qBittorrent preferences on 127.0.0.1:${WEBUI_PORT}"
  echo "Hint: verify qBittorrent WebUI is up and 'Bypass authentication for clients on localhost' is enabled."
  exit 2
fi

listen_port="$(printf '%s' "$preferences_json" | sed -n 's/.*"listen_port":\([0-9][0-9]*\).*/\1/p' | head -n1)"
if [[ -z "$listen_port" || ! "$listen_port" =~ ^[0-9]+$ ]]; then
  echo "CRITICAL: could not parse qBittorrent listen_port from preferences API"
  exit 2
fi

if [[ "$forwarded_port" != "$listen_port" ]]; then
  echo "CRITICAL: port mismatch (gluetun=$forwarded_port qbittorrent=$listen_port)"
  exit 2
fi

echo "OK: ProtonVPN/Gluetun and qBittorrent are in sync (port $forwarded_port)"
