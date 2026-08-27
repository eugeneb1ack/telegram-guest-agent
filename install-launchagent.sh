#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/Documents/telegram-guest-agent}"
PLIST="$HOME/Library/LaunchAgents/io.telegram-guest-agent.plist"
LABEL="io.telegram-guest-agent"

mkdir -p "$APP_DIR/runtime" "$APP_DIR/runtime/guest-media-cache" "$HOME/Library/LaunchAgents"
chmod 700 "$APP_DIR/runtime" "$APP_DIR/runtime/guest-media-cache"
chmod +x "$APP_DIR/run-docker.sh"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$APP_DIR/run-docker.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/runtime/guest-gateway.out.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/runtime/guest-gateway.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL" || true
echo "$PLIST"
