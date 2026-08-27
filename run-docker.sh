#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

mkdir -p runtime runtime/guest-media-cache
chmod 700 runtime runtime/guest-media-cache

# Preserve the old single-file state location during an upgrade, but use the
# mounted runtime directory for all subsequent atomic state writes.
if [[ -f state.json && ! -f runtime/state.json ]]; then
  cp state.json runtime/state.json
fi

# Docker Desktop needs no override. Colima users get a convenient local default
# without forcing that runtime on Linux or other Docker installations.
if [[ -z "${DOCKER_HOST:-}" && -S "$HOME/.colima/default/docker.sock" ]]; then
  export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
fi

command -v docker >/dev/null 2>&1 || {
  echo "docker is not installed or not on PATH" >&2
  exit 1
}

exec docker compose -f compose.yaml up --build --no-color
