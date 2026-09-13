#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${GUEST_AGENT_APP_DIR:-$(cd "$(dirname "$0")" && pwd -P)}"
PROFILE="telegram-guest-agent"
CLONE_FROM=""
PORT="8644"
MODEL=""
CDP_URL=""
ALLOW_PRIVATE_URLS=0
REUSE=0
ROTATE_KEY=0
START_GATEWAY=1

usage() {
  cat <<'USAGE'
Usage: ./setup-hermes-profile.sh [options]

Create and connect a dedicated Hermes profile for Telegram Guest Agent.

Options:
  --profile NAME              Profile name (default: telegram-guest-agent)
  --clone-from NAME           Clone persona, credentials, and installed skills
                              from an existing local Hermes profile
  --port PORT                 Dedicated Hermes API port (default: 8644)
  --model MODEL               Override the cloned/default Hermes model
  --cdp-url URL               Existing Chrome/CloakBrowser CDP endpoint
  --allow-private-urls        Permit private/fake-IP DNS resolutions for this
                              profile; intended for VPN/proxy 198.18.0.0/15 DNS
  --reuse                     Configure an already existing profile
  --rotate-key                Generate a new profile API key
  --no-start                  Configure without installing/starting its gateway
  -h, --help                  Show this help

The script never prints API keys and writes them only to owner-readable .env
files. It mounts only <profile>/cache/images into the sidecar, never the full
Hermes profile.
USAGE
}

while (($#)); do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || { echo "--profile requires a value" >&2; exit 2; }
      PROFILE="$2"
      shift 2
      ;;
    --clone-from)
      [[ $# -ge 2 ]] || { echo "--clone-from requires a value" >&2; exit 2; }
      CLONE_FROM="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "--port requires a value" >&2; exit 2; }
      PORT="$2"
      shift 2
      ;;
    --model)
      [[ $# -ge 2 ]] || { echo "--model requires a value" >&2; exit 2; }
      MODEL="$2"
      shift 2
      ;;
    --cdp-url)
      [[ $# -ge 2 ]] || { echo "--cdp-url requires a value" >&2; exit 2; }
      CDP_URL="$2"
      shift 2
      ;;
    --allow-private-urls)
      ALLOW_PRIVATE_URLS=1
      shift
      ;;
    --reuse)
      REUSE=1
      shift
      ;;
    --rotate-key)
      ROTATE_KEY=1
      shift
      ;;
    --no-start)
      START_GATEWAY=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$PROFILE" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  echo "Profile name must contain only lowercase letters, digits, dashes, or underscores" >&2
  exit 2
}
[[ "$PORT" =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535)) || {
  echo "Port must be an integer between 1024 and 65535" >&2
  exit 2
}
if [[ -n "$CLONE_FROM" && ! "$CLONE_FROM" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "Clone source must be a valid Hermes profile name" >&2
  exit 2
fi
if [[ -n "$CDP_URL" && ! "$CDP_URL" =~ ^https?://127\.0\.0\.1:[0-9]+/?$ && ! "$CDP_URL" =~ ^wss?:// ]]; then
  echo "--cdp-url must be a loopback HTTP(S) discovery URL or a WS(S) endpoint" >&2
  exit 2
fi

command -v hermes >/dev/null 2>&1 || {
  echo "Hermes CLI is required but was not found on PATH" >&2
  exit 1
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required for the gateway health check" >&2
  exit 1
}

umask 077
cd "$APP_DIR"
if [[ ! -f .env ]]; then
  ./init-env.sh
fi

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  local found=0
  tmp="$(mktemp "${file}.tmp.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "${line%%=*}" == "$key" ]]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
      found=1
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$file"
  if ((found == 0)); then
    printf '\n%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

read_env_value() {
  local file="$1"
  local key="$2"
  awk -v wanted="$key" '
    index($0, wanted "=") == 1 {
      value = substr($0, length(wanted) + 2)
    }
    END { print value }
  ' "$file"
}

profile_exists=0
if hermes profile show "$PROFILE" >/dev/null 2>&1; then
  profile_exists=1
fi
if ((profile_exists == 1 && REUSE == 0)); then
  echo "Hermes profile '$PROFILE' already exists; rerun with --reuse to configure it" >&2
  exit 1
fi
if ((profile_exists == 0)); then
  create_args=(profile create "$PROFILE" --description "Dedicated owner-only Telegram Guest Mode harness profile")
  if [[ -n "$CLONE_FROM" ]]; then
    create_args+=(--clone-from "$CLONE_FROM")
  fi
  hermes "${create_args[@]}"
fi

profile_config="$(hermes -p "$PROFILE" config path | tail -n 1)"
[[ -f "$profile_config" ]] || {
  echo "Hermes did not return a valid config path for '$PROFILE'" >&2
  exit 1
}
profile_dir="$(cd "$(dirname "$profile_config")" && pwd -P)"
profile_env="$(hermes -p "$PROFILE" config env-path | tail -n 1)"
[[ -f "$profile_env" ]] || {
  touch "$profile_env"
  chmod 600 "$profile_env"
}

api_key="$(read_env_value "$profile_env" API_SERVER_KEY)"
if ((profile_exists == 0 || ROTATE_KEY == 1)) || ((${#api_key} < 32)); then
  command -v openssl >/dev/null 2>&1 || {
    echo "openssl is required to generate a strong Hermes API key" >&2
    exit 1
  }
  api_key="$(openssl rand -hex 32)"
fi
set_env_value "$profile_env" API_SERVER_KEY "$api_key"

hermes -p "$PROFILE" config set platforms.api_server.enabled true
hermes -p "$PROFILE" config set platforms.api_server.extra.host 0.0.0.0
hermes -p "$PROFILE" config set platforms.api_server.extra.port "$PORT"
# Use the profile's complete CLI capability bundle. Runtime check functions
# still hide unavailable tools, while custom skills and configured MCP/plugin
# toolsets remain profile-owned instead of being duplicated in this project.
hermes -p "$PROFILE" config set platform_toolsets.api_server '["hermes-cli"]'
# Guest branches supply their own history. A cloned profile's persistent
# memories must not be injected into every public guest conversation.
hermes -p "$PROFILE" config set memory.memory_enabled false
hermes -p "$PROFILE" config set memory.user_profile_enabled false
hermes -p "$PROFILE" config set memory.provider ''
if [[ -n "$MODEL" ]]; then
  hermes -p "$PROFILE" config set model.default "$MODEL"
fi
if [[ -n "$CDP_URL" ]]; then
  hermes -p "$PROFILE" config set browser.cdp_url "$CDP_URL"
fi
if ((ALLOW_PRIVATE_URLS == 1)); then
  hermes -p "$PROFILE" config set browser.allow_private_urls true
fi
hermes -p "$PROFILE" config check

profile_model="$(hermes -p "$PROFILE" config get model.default | tail -n 1)"
harness_media_dir="$profile_dir/cache/images"
mkdir -p "$harness_media_dir"
chmod 700 "$harness_media_dir"

set_env_value "$APP_DIR/.env" HERMES_API_URL "http://host.docker.internal:${PORT}/v1/chat/completions"
set_env_value "$APP_DIR/.env" HERMES_API_KEY "$api_key"
set_env_value "$APP_DIR/.env" HERMES_USE_RUNS 1
set_env_value "$APP_DIR/.env" GUEST_HARNESS_MEDIA_DIR "$harness_media_dir"
if [[ -n "$profile_model" ]]; then
  set_env_value "$APP_DIR/.env" HERMES_MODEL "$profile_model"
fi

if ((START_GATEWAY == 1)); then
  if ((profile_exists == 1)); then
    # Reinstalling an already loaded launchd/systemd service can unload the
    # supervisor and leave Hermes running only as a detached fallback process.
    # Restart the existing service first; install only when it has never been
    # registered on this machine.
    if ! hermes -p "$PROFILE" gateway restart; then
      hermes -p "$PROFILE" gateway install --force --start-now
    fi
  else
    hermes -p "$PROFILE" gateway install --force --start-now
  fi
  healthy=0
  for _ in {1..20}; do
    if curl --silent --show-error --fail \
      --header "Authorization: Bearer ${api_key}" \
      "http://127.0.0.1:${PORT}/health" >/dev/null; then
      healthy=1
      break
    fi
    sleep 1
  done
  if ((healthy == 0)); then
    echo "Hermes profile was configured, but its API did not become healthy on port $PORT" >&2
    echo "Inspect it with: hermes -p $PROFILE gateway status" >&2
    exit 1
  fi
fi

echo "Hermes profile '$PROFILE' is connected to Telegram Guest Agent."
echo "Profile tools: full hermes-cli bundle; skills and policy remain owned by Hermes."
echo "Generated media: read-only bridge from the profile cache/images directory."
echo "Next: set GUEST_BOT_TOKEN and GUEST_OWNER_ID in .env, then run ./run-docker.sh --check."
