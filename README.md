# Telegram Guest Agent

<p align="center">
  <img src="assets/telegram-guest-agent-cover.png" alt="Telegram Guest Agent — manga-style Guest Agent flow" width="100%">
</p>

Owner-only Telegram Guest Mode gateway for Hermes and compatible AI harnesses. It accepts a guest request, gives the agent a stable short-lived reply context, and returns a safe Telegram response without exposing the harness, local files, or credentials.

**Install:** [English](docs/installation.en.md) · [Русский](docs/installation.ru.md)

## What it does

- Polls `guest_message` updates with a dedicated Telegram bot token, avoiding `getUpdates` conflicts with a main bot.
- Fails closed: only the numeric `GUEST_OWNER_ID` may invoke the agent.
- Starts a new session for every standalone invocation; a reply to a recent guest answer continues its session for a configurable TTL (120 seconds by default).
- Uses Hermes Runs when available, passing a stable `session_id`. For a standard OpenAI-compatible Chat Completions endpoint, it keeps a bounded, in-memory six-turn transcript for the same reply window.
- Replaces the initial placeholder with live, privacy-safe activity such as «Открываю страницу…» or «Запускаю тесты…» when Hermes reports a real tool event.
- Persists the delivery queue before acknowledging an update, so long agent runs do not block polling and survive a sidecar restart.
- Downloads inbound media into a sandbox, caps its size, and sanitizes local paths in every public answer.
- Stages explicitly allowed local output media through the owner DM before reusing the returned Telegram `file_id` in a public rich reply.
- Uses Bot API rich message blocks when available and falls back to ordinary text if Telegram rejects the rich payload.

## Session behaviour

```text
new @bot request ──> new session
reply to recent guest answer ──> same session, until TTL expires
new @bot request again ──> a different new session
```

The reply relationship is the boundary. The agent never carries history from one standalone request into another just because they are in the same chat.

Hermes Runs is the preferred transport: the harness receives a stable `session_id` and can own long-lived conversation state. Chat Completions mode is compatible with ordinary OpenAI-style endpoints, but its local transcript is deliberately small, process-local, and cleared on gateway restart. It is designed for short reply continuations, not a durable chat archive.

## Quick start

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
$EDITOR .env
./run-docker.sh
```

Set at least `GUEST_BOT_TOKEN`, `GUEST_OWNER_ID`, `HERMES_API_URL`, `HERMES_API_KEY`, and `HERMES_MODEL` in `.env`. The complete setup for Hermes or another harness is in the [English installation guide](docs/installation.en.md) and [Russian installation guide](docs/installation.ru.md).

Run a connectivity check before enabling polling:

```bash
./run-docker.sh --check
```

## Harness contract

The gateway is intentionally narrow transport glue. Keep persona, tools, and policy in the harness profile, not in this repository.

| Mode | Required endpoint | Context contract |
| --- | --- | --- |
| `HERMES_USE_RUNS=1` | `POST /v1/runs`, `GET /v1/runs/{run_id}`; optional `GET /v1/runs/{run_id}/events` | Receives a stable `session_id` and, for a valid reply, the bounded `conversation_history`; the SSE endpoint adds live activity updates. |
| `HERMES_USE_RUNS=0` | OpenAI-style `POST /v1/chat/completions` | Receives a normal `messages` array; this gateway supplies the same bounded local reply history. |

Both modes retain up to six request/answer pairs for a valid reply session. The buffer expires with `GUEST_PENDING_ANCHOR_TTL`, is cleared when the sidecar restarts, and is never written to `state.json`. Both modes expect a bearer token and return ordinary text. Chat Completions responses must contain `choices[0].message.content`.

## Live activity status

In Hermes Runs mode, the gateway consumes the structured SSE lifecycle stream and updates the existing Telegram placeholder only when the harness reports actual activity. Each supported action has distinct start, working, completion, and failure phases. Fixed public categories cover planning, skills, tool discovery, browser navigation and interaction, internet search and extraction, command-line work, scripts, tests, code checks, builds, repository work, file search and reading, code changes, documents, media, data, context, task plans, communication, automation, external tools, and subagents.

Long operations rotate through safe working phrases every `GUEST_PROGRESS_HEARTBEAT_INTERVAL` seconds, so a terminal command or browser action does not appear frozen. Terminal previews are inspected only in memory to select a broad category such as tests, build, script, repository, or file search. Raw tool names, arguments, previews, commands, URLs, file names, local paths, partial model output, and model reasoning are never copied into Telegram. Unknown tools use varied generic start/working/completion phrases.

Updates are coalesced and rate-limited by `GUEST_PROGRESS_MIN_INTERVAL`; set `GUEST_PROGRESS_ENABLED=0` to disable them. If the SSE endpoint is unavailable, final-answer polling continues normally and the placeholder stays unchanged. Chat Completions mode has no tool-event contract, so it deliberately keeps «Думаю…» instead of inventing activity.

## Privacy and security model

- Do not reuse the Telegram token of another polling bot.
- `.env`, `runtime/`, `state.json`, generated media, and logs are ignored by Git. Never commit or publish them.
- `runtime/state.json` can contain queued Telegram update payloads. The gateway writes it atomically with owner-only file permissions where supported; the Docker helper runs as the host UID/GID so `runtime/` may remain `0700` and state files `0600`.
- Inbound media is untrusted and lands in `/sandbox/inbound` in Docker. The container is non-root, read-only, drops Linux capabilities, uses `no-new-privileges`, and has a constrained temporary filesystem.
- The Docker runner derives the host side of the media bridge from the active checkout on every start; Compose uses that exact path for both the bind mount and the path given to the harness, preventing stale media paths after a deployment move.
- Local files may be staged only from `GUEST_OWNER_MEDIA_ALLOWED_DIRS`. Everything else is rejected. Public responses redact `MEDIA:`, `file://`, Windows paths, and sensitive POSIX paths.
- Rotate credentials if they ever appear in a terminal capture, issue, commit, or public message.

## Development

The project has no third-party Python runtime dependency.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q
python3 -m py_compile guest_gateway.py rich_renderer.py
bash -n init-env.sh run-docker.sh install-launchagent.sh run.sh
```

The test suite covers queue durability, context routing, Runs payloads and live activity privacy, Chat Completions reply history, rich-message fallback, media constraints, and path sanitization.

## Repository layout

| Path | Purpose |
| --- | --- |
| `guest_gateway.py` | Telegram polling, durable queue, context resolution, harness clients, media and delivery safeguards. |
| `rich_renderer.py` | Conservative Markdown/Rich HTML to Telegram rich-block renderer. |
| `compose.yaml` / `Dockerfile` | Hardened container deployment. |
| `.env.example` | Safe configuration template with placeholders only. |
| `docs/` | English and Russian installation guides. |
| `assets/` | README artwork only; never runtime or user media. |

## macOS service (optional)

`install-launchagent.sh` writes a per-user launchd job that starts `run-docker.sh`. It is optional and assumes the repository lives at `$HOME/Documents/telegram-guest-agent` unless `APP_DIR` is set. Docker Desktop works directly; the runner automatically uses Colima only when its socket already exists.

## License

This project is released under the [MIT License](LICENSE).
