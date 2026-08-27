#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
if [[ -e .env ]]; then
  echo ".env already exists; refusing to overwrite it" >&2
  exit 1
fi

cp .env.example .env
chmod 600 .env
echo "Created .env. Set GUEST_BOT_TOKEN, GUEST_OWNER_ID, HERMES_API_URL, HERMES_API_KEY, and HERMES_MODEL before starting the service."
