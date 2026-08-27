# Security policy

## Supported version

Only the current `main` branch is supported.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose Telegram messages, media, local paths, tokens, or harness access. Use GitHub's private security advisory flow for this repository, include a minimal reproduction, and redact all credentials and private conversation content.

Do not attach `.env`, `runtime/`, `state.json`, logs containing user messages, downloaded media, or Telegram bot tokens to a report.

## Security boundaries

- `GUEST_OWNER_ID` is mandatory and owner-only authorization is fail-closed.
- Telegram media is untrusted and should run through the supplied Docker sandbox.
- The runtime queue may contain private incoming payloads. Keep it local and protected.
- Only explicitly configured media directories can be uploaded from the harness host.
