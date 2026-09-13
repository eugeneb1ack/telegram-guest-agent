# Installation (English)

This guide installs **Telegram Guest Agent** as a separate Telegram Guest Mode sidecar. It works with Hermes Runs and with a generic OpenAI-compatible Chat Completions harness.

## 1. Prerequisites

- A Telegram bot created with `@BotFather`, with **Guest Mode** enabled in BotFather's Mini App.
- The numeric Telegram user ID that is allowed to use the bot.
- Git and Bash for the supplied installation scripts.
- Docker Engine with Docker Compose v2 and the Docker Buildx plugin (recommended production path), or Python 3.12 for direct execution.
- For the automated Hermes path: a working `hermes` CLI, `curl`, and `openssl`.
- For Telegram-native voice summaries: a separately installed and authorized Telethon userbot runtime, its serialized runner, and an enabled `userbot` skill in the selected Hermes profile.
- For another harness: an endpoint, model name, and bearer key.

Use a dedicated bot token. Do not point another long-polling process at this token.

Verify host dependencies before cloning:

```bash
git --version
docker version
docker compose version
docker buildx version
hermes --version
curl --version
openssl version
```

The Docker path installs no Python packages on the host: the runtime uses the standard library and is built into the supplied image.

## 2. Clone and create the local environment

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
```

`.env` is owner-readable and ignored by Git. Never paste its contents into an
issue, commit, terminal recording, or support message.

## 3. Create a dedicated Hermes harness profile

The repository can create and connect the profile instead of asking you to
assemble its API configuration by hand.

To preserve the same persona, provider authentication, installed custom
skills, plugins, and policies as your normal Hermes profile, clone it locally:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --clone-from default \
  --port 8644
```

The clone stays under the local Hermes home. Nothing from it is copied into
this repository or Git. The script replaces the cloned API-server key with a
new profile-specific key.

For a clean profile with only Hermes' bundled skills and default persona:

```bash
./setup-hermes-profile.sh --profile telegram-guest-agent --port 8644
```

Finish provider authentication in that profile if the clean profile does not
already have a working model. You can also select a model explicitly:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --model your-model-name \
  --port 8644
```

The bootstrap performs the following scoped changes:

- creates a dedicated Hermes profile, or refuses to modify an existing one
  unless `--reuse` is supplied;
- enables its authenticated Runs/Chat Completions API on the selected port;
- gives `api_server` the complete `hermes-cli` tool bundle, so the profile's
  skills, browser, terminal, web, files, memory, plugins, and available MCP
  tools are resolved by Hermes normally;
- generates a strong API key without printing it and writes the matching
  connection values into this repository's ignored `.env`;
- exposes only `<profile>/cache/images` to the Telegram container through a
  read-only mount, allowing generated images to be sent to the owner DM and
  embedded by Telegram `file_id` in the rich guest article;
- installs and starts only that profile's Hermes gateway.

To configure an existing profile deliberately:

```bash
./setup-hermes-profile.sh --profile telegram-guest-agent --reuse
```

Add `--rotate-key` only when you intend to replace the profile's current API
key. Add `--no-start` when a separate supervisor will install/start Hermes.

### Telegram-native voice summaries through Userbot

The repository does not contain or install Telegram API credentials, account
environment files, or Telethon sessions. If the guest agent must summarize a
Telegram voice, audio file, or video note through Telegram's native
transcription, prepare the userbot integration before cloning the Hermes
profile:

- the userbot runtime has an authorized account and one serialized session
  owner;
- `scripts/userbotrun.py`, `modules/transcribe_audio_native.py`, and
  `modules/summarize_chat_native.py` exist in that runtime;
- the Hermes source profile has an enabled `userbot` skill that routes exact
  message IDs through those modules;
- the Hermes terminal policy can reach the runtime without copying its session
  or credentials into this repository or container.

Verify operation discovery locally without opening a second Telegram session:

```bash
cd /path/to/telethon-userbot
venv/bin/python scripts/userbot_module_registry.py \
  --query 'transcribe one exact Telegram voice message using native Telegram transcription' \
  --json
hermes -p telegram-guest-agent skills list
```

The registry should select `transcribe_audio_native.py`, and the Hermes list
should include `userbot`. For a single replied voice, the skill should invoke
the exact-ID transcription module through `userbotrun.py`. For a whole-dialog
summary, it should use `summarize_chat_native.py --do-summary`. Only a result
with `complete=true` and matching chat/message/sender provenance may be
summarized.

The sidecar default is:

```dotenv
GUEST_TELEGRAM_NATIVE_STT_REQUIRED=1
```

With this setting, the gateway copies the original `chat_id`, `message_id`,
and `sender_id` into the media section sent to the harness and adds a mandatory
system instruction. Whisper, Ollama, ffmpeg, and other external/local STT
routes are forbidden for a Telegram speech-summary task when those IDs are
available. If native transcription is unavailable or incomplete, the agent
must report the limitation and stop. Set the value to `0` only when a generic
harness deliberately owns and documents another transcription policy.

### CloakBrowser, VPNs, and fake-IP DNS

An existing Chrome or CloakBrowser CDP endpoint can be attached explicitly:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --clone-from default \
  --cdp-url http://127.0.0.1:9242 \
  --allow-private-urls
```

Use `--allow-private-urls` only when your trusted VPN/proxy resolves public
domains into private or benchmark ranges such as `198.18.0.0/15`. Without the
flag, Hermes correctly fails closed and may reject those public domains as
internal addresses. The flag applies only to the dedicated profile; Hermes
continues to hard-block cloud metadata/link-local credential endpoints. It
also permits other private-network browsing from that profile, so do not
enable it on an untrusted or publicly callable harness.

The API server binds to `0.0.0.0` so the Docker sidecar can reach it through
`host.docker.internal`. Keep the port firewalled to trusted local networks and
never expose it publicly even though bearer authentication is required.

## 4. Configure Telegram

After the Hermes bootstrap, edit `.env` and set the Telegram values. The
Hermes URL, key, model, Runs mode, and generated-media directory are already
filled in:

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

## 5. Choose a harness mode

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

For Hermes, prefer `setup-hermes-profile.sh` so the endpoint, full tool bundle,
API key, and generated-media bridge remain consistent. Persona, skills, tools,
and policy stay in that profile; this sidecar only carries Telegram transport
context.

### Generic OpenAI-compatible Chat Completions

Set `HERMES_USE_RUNS=0` when the harness supports only:

```text
POST /v1/chat/completions
```

The endpoint must accept `model`, `messages`, `stream: false`, bearer authentication, and return:

```json
{"choices":[{"message":{"content":"answer text"}}]}
```

Both modes persist branch history and answer anchors atomically in private `runtime/state.json` (0600), surviving restarts. Branches expire after 30 days of inactivity (`GUEST_SESSION_TTL=2592000`). The initial exchange and up to 23 recent exchanges are retained, bounded to 120,000 characters total and 30,000 per message. Limits are 200 recent branches plus in-flight work and 10,000 message anchors. `GUEST_PENDING_ANCHOR_TTL` is obsolete. Retention covers gateway state, not Hermes archives.

Chat Completions has no standard tool-lifecycle stream. In this mode the gateway keeps the static placeholder instead of showing guessed activity.

For a non-Hermes harness, skip `setup-hermes-profile.sh` and set
`HERMES_API_URL`, `HERMES_API_KEY`, `HERMES_MODEL`, and `HERMES_USE_RUNS`
manually. If it returns local generated files, set `GUEST_HARNESS_MEDIA_DIR`
to one dedicated output directory. Compose mounts only that directory
read-only. The harness must emit `MEDIA:/absolute/path/to/file` or a Markdown
image using that path; all other local paths remain redacted.

Tool completion alone is not delivery completion. Before returning its final
answer, the harness must copy/export the final artifact into
`GUEST_HARNESS_MEDIA_DIR`, verify that the regular file exists there, and place
the `MEDIA:` reference on its own line. The sidecar first stages the file in the
owner's DM and then embeds the returned Telegram `file_id` into the rich
article. A file left in a project, workspace, or temporary directory is
intentionally rejected even when the generation tool itself succeeded.

For avatar work, `GUEST_PROFILE_PHOTO_ENABLED=1` lets the sidecar call
`getUserProfilePhotos` when an exact `user_id` is already present on the
replied message author or a `text_mention` entity. The largest available photo
is passed to Hermes as a local reference file. A plain `@username` has no Bot
API user ID, so the harness routes it to the guarded Userbot
`download_profile_photo` operation and never guesses a similarly named user.
`GUEST_TOOL_RECOVERY_ENABLED=1` allows exactly one corrective harness attempt
when an explicit media task finishes without a verified allowlisted artifact.

## 6. Start and verify

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

For Hermes, also confirm the dedicated profile before enabling polling:

```bash
hermes profile show telegram-guest-agent
hermes -p telegram-guest-agent gateway status
hermes -p telegram-guest-agent config get platform_toolsets.api_server
```

The last command should include `hermes-cli`. A real end-to-end test should
then ask the guest agent to list/load one installed skill and browse a harmless
public page. If generated-media support matters, ask it to generate one small
image and verify both deliveries: the original file in the owner DM and the
image block in the guest rich article.

## 7. Test session semantics

1. `@bot` without a reply starts a new branch.
2. Reply to a specific bot answer **with another `@bot` mention** to continue that branch.
3. A new standalone mention starts a different branch. Replying to an old answer returns to its branch until expiry.
4. Replying to another person with `@bot` starts a new branch with only that message as quoted source. Subsequent owner/bot conversation stays in that branch.
5. Unmentioned messages and other callers are ignored. Surrounding chat, nested replies and other topics are excluded.

Branches are scoped to bot, chat, topic and owner. Unknown or expired answers provide only quoted text for a fresh branch. Ambiguous anchors from the old version are not imported; queued work survives. Turns in one branch execute sequentially.

Runs receives matching `session_id` and `X-Hermes-Session-Key` plus explicit `conversation_history`. An empty window uses a system boundary to prevent implicit Hermes history hydration.

The dedicated guest profile must disable shared automatic memory so `MEMORY.md` and `USER.md` do not enter every branch. The bootstrap configures this automatically. For an existing **guest** profile:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: ''
```

The memory files are retained. Other profiles, including trainer, do not need changes. Restart only the guest profile gateway after changing its configuration.

## Media configuration

Inbound Telegram files are downloaded under `GUEST_MEDIA_CACHE_DIR` and capped by `GUEST_MEDIA_MAX_BYTES`. Docker overrides the container path to `/sandbox/inbound`.

Inbound and generated media use separate bridges. Inbound Telegram files stay
in `<repository>/runtime/guest-media-cache` and are writable only where the
download path needs it. A harness-generated output directory is mounted
read-only at `/sandbox/harness-output`. The Hermes bootstrap sets it to the
dedicated profile's `cache/images`; a generic harness can set it manually:

```dotenv
GUEST_OWNER_MEDIA_ENABLED=1
GUEST_OWNER_MEDIA_ALLOWED_DIRS=/sandbox/inbound
GUEST_MEDIA_HOST_DIR=/absolute/path/to/telegram-guest-agent/runtime/guest-media-cache
GUEST_HARNESS_MEDIA_DIR=/absolute/path/to/the/harness/output-directory
```

When the harness returns a path below either configured host root, the gateway
maps it to the corresponding container mount, verifies that it is an allowed
regular file within the size limit, uploads it to the owner DM first, then
reuses the Telegram `file_id` in the public rich reply. Paths outside those
roots are refused and redacted. The generated-output mount does not expose the
profile's `.env`, persona, skills, memory, or history. For direct Python
execution, set `GUEST_HARNESS_MEDIA_HOST_DIR`,
`GUEST_HARNESS_MEDIA_CACHE_DIR`, and `GUEST_OWNER_MEDIA_ALLOWED_DIRS`
explicitly because Compose normally supplies the container-side values.

With Bot API 10.3, general files are also embedded as native `document`
blocks, Markdown tables use compact cell spacing, and an explicit
`<blockquote expandable>` HTML block becomes a collapsible quotation. Normal
quotations are not collapsed automatically.

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
