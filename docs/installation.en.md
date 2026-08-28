# Installation (English)

This guide installs **Telegram Guest Agent** as a separate Telegram Guest Mode sidecar. It works with Hermes Runs and with a generic OpenAI-compatible Chat Completions harness.

## 1. Prerequisites

- A Telegram bot created with `@BotFather`, with **Guest Mode** enabled in BotFather's Mini App.
- The numeric Telegram user ID that is allowed to use the bot.
- Git and Bash for the supplied installation scripts.
- Docker Engine with Docker Compose v2 and the Docker Buildx plugin (recommended production path), or Python 3.12 for direct execution.
- A harness endpoint and bearer key.

Use a dedicated bot token. Do not point another long-polling process at this token.

Verify host dependencies before cloning:

```bash
git --version
docker version
docker compose version
docker buildx version
```

The Docker path installs no Python packages on the host: the runtime uses the standard library and is built into the supplied image.

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
HERMES_POLL_INTERVAL=1
GUEST_PROGRESS_ENABLED=1
GUEST_PROGRESS_MIN_INTERVAL=1.0
GUEST_PROGRESS_HEARTBEAT_INTERVAL=4.0
```

`GUEST_OWNER_ID` is mandatory and must be a positive integer. There is deliberately no default owner.

For Docker Desktop, `host.docker.internal` reaches a harness running on the host. The supplied Compose configuration adds the same hostname on modern Linux Docker. If your harness is another service in the same Compose network, use its service hostname and port instead.

## 3. Choose a harness mode

### Hermes Runs (recommended)

Set `HERMES_USE_RUNS=1` when the harness supports:

```text
POST /v1/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events  # optional SSE progress stream
```

The start request receives `model`, `instructions`, `input`, and a stable `session_id`. For a valid reply to the guest answer, it also receives the bounded `conversation_history` buffer. This makes reply context reliable even when a Runs implementation treats `session_id` as an execution or memory scope rather than a transcript lookup key. This is the best option for long tool-using work and durable harness-side conversation state.

`HERMES_POLL_INTERVAL` controls how soon a completed Run is delivered. The default of `1` second is a responsive production setting; the gateway enforces a floor of `0.5` seconds. Increase it only when your harness needs fewer status requests more than it needs lower delivery latency.

When the optional SSE endpoint is available, the gateway replaces «Думаю…» with fixed public phases derived from real lifecycle events. Supported actions have distinct start, working, completion, and failure text. Long-running actions rotate through safe working phrases every `GUEST_PROGRESS_HEARTBEAT_INTERVAL` seconds (`4` by default, bounded to `2–30`). Terminal previews are used only in memory to distinguish broad actions such as tests, builds, scripts, repository checks, or file search.

The gateway never forwards the raw tool name, arguments, event preview, command, URL, file name, local path, partial model output, or model reasoning. `GUEST_PROGRESS_MIN_INTERVAL` limits Telegram edits to one per second by default and is bounded to `0.5–10` seconds. Set `GUEST_PROGRESS_ENABLED=0` to keep the static placeholder. If SSE is unavailable, Run polling and final delivery still work normally.

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

In both modes, the gateway keeps the last six prompt/answer turns in memory for each active reply session. That history expires with `GUEST_PENDING_ANCHOR_TTL` (120 seconds by default) and is lost on gateway restart; it is never written into `state.json`.

Chat Completions has no standard tool-lifecycle stream. In this mode the gateway keeps the static placeholder instead of showing guessed activity.

## 4. Start and verify

Run the check first:

```bash
./run-docker.sh --check
```

It verifies the Telegram bot and attempts a non-fatal harness health check. Then start polling:

```bash
./run-docker.sh
```

or, without the helper:

```bash
docker compose up --build
```

`init-env.sh` records the host UID/GID in `.env`, and `run-docker.sh` supplies the same values for older configs. This lets the unprivileged container write its bind-mounted runtime state without loosening file permissions.

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

For Docker Compose, a host-side harness must write any public output file under `<repository>/runtime/guest-media-cache`. The gateway sees that mount as `/sandbox/inbound`. `run-docker.sh` derives the absolute host path from the active checkout on every start, so moving or replacing the deployment cannot leave a stale media bridge. Set the path explicitly only when starting with raw `docker compose` or direct Python:

```dotenv
GUEST_OWNER_MEDIA_ENABLED=1
GUEST_OWNER_MEDIA_ALLOWED_DIRS=/sandbox/inbound
GUEST_MEDIA_HOST_DIR=/absolute/path/to/telegram-guest-agent/runtime/guest-media-cache
```

When the harness returns a path below `GUEST_MEDIA_HOST_DIR`, the gateway maps it back into `/sandbox/inbound`, verifies it is an allowed regular file, uploads it to the owner DM first, then reuses the returned Telegram `file_id` in the public reply where Telegram supports that media type. Paths outside the allowlist are refused, and local paths are redacted from public output. For direct Python execution, set both paths to an explicitly allowlisted local directory instead.

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
./run-docker.sh --check
docker compose up -d --build
docker compose logs -f telegram-guest-agent
```

Do not delete `runtime/` while the service is running: it contains the queue and may contain undelivered updates.
