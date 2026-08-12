#!/bin/sh
set -eu

PORT="${JELLYFIN_USAGE_PORT:-8092}"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx tailscale; then
  docker exec tailscale tailscale serve --bg --http="${PORT}" "http://jellyfin-usage:8080"
else
  if ! tailscale serve --bg --http="${PORT}" "http://127.0.0.1:${PORT}"; then
    cat >&2 <<EOF

Tailscale denied Serve configuration for the current user.
Run this once on the host, then retry this script:

  sudo tailscale set --operator=$USER

Or configure the route directly with sudo:

  sudo tailscale serve --bg --http=${PORT} http://127.0.0.1:${PORT}

EOF
    exit 1
  fi
fi

echo "Jellyfin usage tracker should now be reachable through Tailscale on HTTP port ${PORT}."
