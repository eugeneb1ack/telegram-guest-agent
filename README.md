# Telegram Guest Agent

<p align="center">
  <img src="assets/telegram-guest-agent-cover.png" alt="Telegram Guest Agent — manga-style Guest Agent flow" width="100%">
</p>

Owner-only Telegram Guest Mode gateway for Hermes and compatible AI harnesses. It accepts a guest request, gives the agent a isolated, durable reply context, and returns a safe Telegram response without exposing the harness, local files, or credentials.

**Install:** [English](docs/installation.en.md) · [Русский](docs/installation.ru.md)

## What it does

- Polls `guest_message` updates with a dedicated Telegram bot token, avoiding `getUpdates` conflicts with a main bot.
- Fails closed: only the numeric `GUEST_OWNER_ID` may invoke the agent.
- Starts a new session for every standalone mention; an explicit mention replying to a registered bot answer resumes that exact branch, including after other branches or gateway restarts.
- Uses Hermes Runs with a stable `session_id`, matching `X-Hermes-Session-Key` and authoritative branch history. Standard Chat Completions endpoints receive the same bounded transcript in `messages`.
- Replaces the initial placeholder with live, privacy-safe activity such as «Открываю страницу…» or «Запускаю тесты…» when Hermes reports a real tool event.
- Supplies the latest Bot-API-visible profile photo as tool input when an explicit avatar task targets the exact author of a replied message or a `text_mention`. Plain `@username` targets are routed to the guarded Userbot operation instead of being guessed.
- Persists the delivery queue before acknowledging an update, so long agent runs do not block polling and survive a sidecar restart.
- Downloads inbound media into a sandbox, caps its size, and sanitizes local paths in every public answer.
- Preserves the original Telegram `chat_id`, `message_id`, and `sender_id` beside voice/audio media so an installed `userbot` skill can use Telegram-native MTProto transcription instead of silently falling back to Whisper.
- Stages explicitly allowed local output media through the owner DM before reusing the returned Telegram `file_id` in a public rich reply.
- Uses Bot API 10.3 rich message blocks when available, including compact tables, expandable quotations, and embedded documents; falls back to ordinary text if Telegram rejects the rich payload.

## Session behaviour

```text
@bot + no reply                  -> new branch A
reply to A's answer + @bot        -> continue A
@bot + no reply                  -> new branch B
reply to an old A answer + @bot   -> return to A
reply to another person + @bot   -> new branch C, with that quoted message
reply without @bot               -> ignored
```

Each branch is scoped to the bot, chat, topic and invoking owner. Other users
cannot invoke the agent or append messages to its history. Only an explicit
Telegram mention in the current message can start work: an ordinary reply,
an unrelated mention or a bot command alone produces no answer or reaction.
`GUEST_BOT_USERNAME` is required for username mentions; an unset value never
turns off the mention gate. The same gate applies to restored queued requests.
Only the current owner request and its immediate reply target enter the payload; nested replies
and surrounding chat history are excluded. An unknown or expired bot answer
starts a fresh branch with its visible text as a quote, never the latest branch.
Inline answer IDs are decoded using Telegram's known 20/24-byte formats and
validated against the source peer. Unsupported IDs fail closed.

Branches expire after **30 days of inactivity** (`GUEST_SESSION_TTL=2592000`).
The gateway retains at most 200 inactive/active recent branches plus in-flight
work, and 10,000 answer anchors. Each transcript holds the initial exchange and
up to 23 recent exchanges, bounded to 120,000 characters (30,000 per message).
This is a bounded conversation window, not an unlimited archive. Anchors and
history are persisted atomically in the private `runtime/state.json` (0600).
The retention policy governs gateway state; it does not delete Hermes logs or
archives. Requests in one branch run sequentially; other branches may run in
parallel.

Hermes Runs receives authoritative history even for a fresh session, preventing
implicit restoration of an unrelated harness transcript. The bootstrap disables
shared profile memory and external memory providers for the dedicated guest
profile; persona, skills and tool access remain profile-owned. Existing installs
must apply the memory settings described in the installation guides.

When upgrading from the older 120-second routing, ambiguous legacy anchors are
not imported. Queued requests survive, but start isolated sessions. An answer
sent before this upgrade can supply quoted text, not reconstruct missing history.

## Quick start

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
./setup-hermes-profile.sh --clone-from default
$EDITOR .env  # add only the Telegram token, owner ID, and bot username
./run-docker.sh
```

The Hermes bootstrap creates a dedicated profile, gives its API server the full
`hermes-cli` capability bundle, generates a separate bearer key, connects the
sidecar, and exposes only the profile's generated-image cache through a
read-only media mount. It never writes credentials, persona, or skills into
the repository. Omit `--clone-from default` for a clean profile with bundled
skills, or clone from another local profile to preserve its custom
skills/persona/provider authentication. The complete setup for Hermes or
another harness is in the [English installation guide](docs/installation.en.md)
and [Russian installation guide](docs/installation.ru.md).

Run a connectivity check before enabling polling:

```bash
./run-docker.sh --check
```

## Harness contract

The gateway is intentionally narrow transport glue. Keep persona, tools, and policy in the harness profile, not in this repository.

| Mode | Required endpoint | Context contract |
| --- | --- | --- |
| `HERMES_USE_RUNS=1` | `POST /v1/runs`, `GET /v1/runs/{run_id}`; optional `GET /v1/runs/{run_id}/events` | Receives a stable `session_id`, matching `X-Hermes-Session-Key`, and authoritative `conversation_history`; the SSE endpoint adds live activity updates. |
| `HERMES_USE_RUNS=0` | OpenAI-style `POST /v1/chat/completions` | Receives a normal `messages` array; this gateway supplies the same bounded local reply history. |

Both modes use the durable bounded history described above. `GUEST_PENDING_ANCHOR_TTL` is obsolete and no longer affects routing. Both modes expect a bearer token and return ordinary text. Chat Completions responses must contain `choices[0].message.content`.

### Telegram-native voice transcription

`GUEST_TELEGRAM_NATIVE_STT_REQUIRED=1` is the safe default for Telegram voice,
audio, and video-note speech tasks. The gateway puts the source chat, message,
and sender IDs in the same `media_context` section as the downloaded media and
adds a system-level routing rule: load the harness profile's `userbot` skill,
use its canonical Telethon module and Telegram's native MTProto transcription,
and accept only a complete provenance-matched result. Whisper, Ollama, ffmpeg,
and external STT are not permitted as silent fallbacks. If the profile has no
working userbot integration, the agent must report that limitation instead of
inventing or substituting a transcript.

This project does not package Telegram user sessions or Telethon credentials.
Install the userbot runtime and skill separately, then clone that configured
Hermes profile with `setup-hermes-profile.sh --clone-from ...`. Generic
harnesses that deliberately own a different transcription policy can set
`GUEST_TELEGRAM_NATIVE_STT_REQUIRED=0` explicitly.

### Generated-media completion contract

For a generated image or file, a successful harness tool call is not yet a
completed Telegram task. The Runs request names the exact host-side
`GUEST_HARNESS_MEDIA_DIR`; the harness must copy or export the final artifact
there, verify that the file exists, and return a standalone
`MEDIA:/absolute/path/inside/that/directory` line with its human-readable
answer. The sidecar stages that file in the owner's DM, reuses Telegram's
returned `file_id`, and appends the media block to the final rich article.
Paths from a workspace, project, or temporary directory remain blocked and
redacted even if the underlying generation tool succeeded.

For an explicit avatar/profile-photo request, the sidecar calls
`getUserProfilePhotos` only after an exact user ID is available from the
replied message or a `text_mention`. The largest current photo is downloaded
through the inbound media bridge and exposed in
`media_context.person_targets` as tool input. A plain `@username` contains no
Bot API user ID, so the harness must use the guarded Userbot
`download_profile_photo` operation. If the first answer to an explicit media
task has no verified allowlisted artifact, the sidecar performs one corrective
run in the same isolated session. It never loops or silently chooses a
similarly named user.

Generated general files are staged through the same private owner-DM bridge as
images and embedded as Bot API 10.3 `document` blocks. Markdown tables use the
new compact cell layout. An explicit `<blockquote expandable>` block is kept as
a native collapsible quotation; ordinary quotations remain ordinary, so the
renderer does not unexpectedly hide content.

## Live activity status

In Hermes Runs mode, the gateway consumes the structured SSE lifecycle stream and updates the existing Telegram placeholder only when the harness reports actual activity. Each supported action has distinct start, working, completion, and failure phases. Fixed public categories cover planning, skills, tool discovery, browser navigation and interaction, internet search and extraction, command-line work, scripts, tests, code checks, builds, repository work, file search and reading, code changes, documents, media, data, context, task plans, communication, automation, external tools, and subagents.

Long operations rotate through safe working phrases every `GUEST_PROGRESS_HEARTBEAT_INTERVAL` seconds, so a terminal command or browser action does not appear frozen. Terminal previews are inspected only in memory to select a broad category such as tests, build, script, repository, or file search. Raw tool names, arguments, previews, commands, URLs, file names, local paths, partial model output, and model reasoning are never copied into Telegram. Unknown tools use varied generic start/working/completion phrases.

Updates are coalesced and rate-limited by `GUEST_PROGRESS_MIN_INTERVAL`; set `GUEST_PROGRESS_ENABLED=0` to disable them. If the SSE endpoint is unavailable, final-answer polling continues normally and the placeholder stays unchanged. Chat Completions mode has no tool-event contract, so it deliberately keeps «Думаю…» instead of inventing activity.

## Privacy and security model

- Do not reuse the Telegram token of another polling bot.
- `.env`, `runtime/`, `state.json`, generated media, and logs are ignored by Git. Never commit or publish them.
- `runtime/state.json` contains accepted owner requests, their explicit reply targets, answers and queued work. The gateway writes it atomically with owner-only file permissions where supported; the Docker helper runs as the host UID/GID so `runtime/` may remain `0700` and state files `0600`.
- Inbound media is untrusted and lands in `/sandbox/inbound` in Docker. The container is non-root, read-only, drops Linux capabilities, uses `no-new-privileges`, and has a constrained temporary filesystem.
- The Docker runner derives the host side of the media bridge from the active checkout on every start; Compose uses that exact path for both the bind mount and the path given to the harness, preventing stale media paths after a deployment move.
- Local files may be staged only from `GUEST_OWNER_MEDIA_ALLOWED_DIRS`. Everything else is rejected. Public responses redact `MEDIA:`, `file://`, Windows paths, and sensitive POSIX paths.
- Generated harness files are accepted only from the explicitly configured `GUEST_HARNESS_MEDIA_DIR`. Compose mounts that one directory read-only at `/sandbox/harness-output`; it does not mount the Hermes profile, credentials, skills, or history. Accepted files are sent to the owner DM first, then their Telegram `file_id` is embedded in the guest rich article.
- Telegram-native transcription receives only source provenance through the prompt. Telethon credentials, account environment files, and session files stay in the separate userbot runtime and must never be mounted into this container or committed here.
- Rotate credentials if they ever appear in a terminal capture, issue, commit, or public message.

## Development

The project has no third-party Python runtime dependency.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q
python3 -m py_compile guest_gateway.py rich_renderer.py
bash -n init-env.sh setup-hermes-profile.sh run-docker.sh install-launchagent.sh run.sh
```

The test suite covers queue durability, context routing, Runs payloads and live activity privacy, Chat Completions reply history, rich-message fallback, media constraints, and path sanitization.

## Repository layout

| Path | Purpose |
| --- | --- |
| `guest_gateway.py` | Telegram polling, durable queue, context resolution, harness clients, media and delivery safeguards. |
| `rich_renderer.py` | Conservative Markdown/Rich HTML to Telegram rich-block renderer. |
| `compose.yaml` / `Dockerfile` | Hardened container deployment. |
| `setup-hermes-profile.sh` | Creates or connects a dedicated full-tool Hermes profile without committing secrets. |
| `.env.example` | Safe configuration template with placeholders only. |
| `docs/` | English and Russian installation guides. |
| `assets/` | README artwork only; never runtime or user media. |

## macOS service (optional)

`install-launchagent.sh` writes a per-user launchd job that starts `run-docker.sh`. It is optional and assumes the repository lives at `$HOME/Documents/telegram-guest-agent` unless `APP_DIR` is set. Docker Desktop works directly; the runner automatically uses Colima only when its socket already exists.

## License

This project is released under the [MIT License](LICENSE).
