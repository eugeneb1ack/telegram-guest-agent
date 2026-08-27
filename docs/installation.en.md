# Installation (English)

This guide installs **Telegram Guest Agent** as a separate Telegram Guest Mode sidecar. It works with Hermes Runs and with a generic OpenAI-compatible Chat Completions harness.

## 1. Prerequisites

- A Telegram bot created with `@BotFather`, with **Guest Mode** enabled in BotFather's Mini App.
- The numeric Telegram user ID that is allowed to use the bot.
- Docker Engine with Docker Compose v2 (recommended), or Python 3.12 for direct execution.
- A harness endpoint and bearer key.

Use a dedicated bot token. Do not point another long-polling process at this token.

## 2. Configure the agent

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
```

Edit `.env` and set the required values:

```dotenv
GUEST_BOT_TOKEN=123456:replace-with-your-token
GUEST_OWNER_ID=123456789
GUEST_BOT_USERNAME=your_guest_bot_username

HERMES_API_URL=http://host.docker.internal:8643/v1/chat/completions
HERMES_API_KEY=replace-with-your-harness-key
HERMES_MODEL=your-model-name
HERMES_USE_RUNS=1
```

`GUEST_OWNER_ID` is mandatory and must be a positive integer. There is deliberately no default owner.

For Docker Desktop, `host.docker.internal` reaches a harness running on the host. The supplied Compose configuration adds the same hostname on modern Linux Docker. If your harness is another service in the same Compose network, use its service hostname and port instead.

## 3. Choose a harness mode

### Hermes Runs (recommended)

Set `HERMES_USE_RUNS=1` when the harness supports:

```text
POST /v1/runs
GET  /v1/runs/{run_id}
```

The start request receives `model`, `instructions`, `input`, and a stable `session_id`. This is the best option for long tool-using work and durable harness-side conversation state.

For Hermes, point `HERMES_API_URL` and `HERMES_API_KEY` to the API of the dedicated profile you want the guest agent to use. Persona, tools, and policy stay in that profile; this sidecar only carries Telegram transport context.

### Generic OpenAI-compatible Chat Completions

Set `HERMES_USE_RUNS=0` when the harness supports only:

```text
POST /v1/chat/completions
```

The endpoint must accept `model`, `messages`, `stream: false`, bearer authentication, and return:

```json
{"choices":[{"message":{"content":"answer text"}}]}
```

The gateway keeps the last six prompt/answer turns in memory for each active reply session. That history expires with `GUEST_PENDING_ANCHOR_TTL` (120 seconds by default) and is lost on gateway restart; it is never written into `state.json`.

## 4. Start and verify

Run the check first:

```bash
docker compose run --rm --no-deps telegram-guest-agent --check
```

It verifies the Telegram bot and attempts a non-fatal harness health check. Then start polling:

```bash
./run-docker.sh
```

or, without the helper:

```bash
docker compose up --build
```

The container restarts unless stopped. Follow its output with:

```bash
docker compose logs -f telegram-guest-agent
```

## 5. Test session semantics

1. Invoke the guest bot with an explicit `@your_guest_bot_username` mention or command. This starts a fresh session.
2. Reply to the resulting guest answer and invoke the bot again. The reply is eligible to continue the short-lived session.
3. Start another standalone invocation. It receives a new session and cannot inherit the earlier history.

If a Telegram Guest Mode client delivers a plain reply separately, the gateway temporarily anchors it; the next explicit invocation in that reply scope consumes the anchor.

## Media configuration

Inbound Telegram files are downloaded under `GUEST_MEDIA_CACHE_DIR` and capped by `GUEST_MEDIA_MAX_BYTES`. Docker overrides the container path to `/sandbox/inbound`.

If the harness generates a local file that should become a public Telegram attachment, set the following variables deliberately:

```dotenv
GUEST_OWNER_MEDIA_ENABLED=1
GUEST_OWNER_MEDIA_ALLOWED_DIRS=/absolute/path/visible/to/the-harness
GUEST_MEDIA_HOST_DIR=/absolute/path/visible/to/the-harness
```

The bot uploads an allowed file to the owner DM first, then reuses the returned Telegram `file_id` in the public reply where Telegram supports that media type. Paths outside the allowlist are refused, and local paths are redacted from public output.

## Direct Python execution (optional)

For local development only, create a protected `.env`, then run:

```bash
python3 guest_gateway.py --check
python3 guest_gateway.py --poll
```

Docker is the recommended production boundary for untrusted media.

## Updating

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs -f telegram-guest-agent
```

Do not delete `runtime/` while the service is running: it contains the queue and may contain undelivered updates.
