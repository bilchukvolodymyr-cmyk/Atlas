#!/usr/bin/env bash
# Atlas on a MacBook, folder mode (iCloud Drive). No server, no open ports, no payment:
# Claude is called through the Claude Code CLI logged in with your Claude subscription.
# Run in Terminal:  bash deploy/mac/install.sh      (safe to run again)
set -euo pipefail

HOME_DIR="$HOME/Atlas"
APP="$HOME_DIR/app"
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
BOX="$ICLOUD/Atlas"
PLIST="$HOME/Library/LaunchAgents/com.atlas.folder.plist"
REPO="${ATLAS_REPO:-https://github.com/bilchukvolodymyr-cmyk/atlas.git}"

PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "Need Python 3.11+ (e.g. brew install python@3.13)"; exit 1; }
CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || { echo "Claude Code CLI not found (npm i -g @anthropic-ai/claude-code, then: claude login)"; exit 1; }
[ -d "$ICLOUD" ] || { echo "iCloud Drive folder not found: turn on iCloud Drive in System Settings"; exit 1; }

mkdir -p "$HOME_DIR/logs" "$BOX/inbox" "$BOX/outbox" "$HOME/Library/LaunchAgents"
if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone -q "$REPO" "$APP"; fi

echo "== checking that Claude Code answers with your subscription"
echo '{"items": []}' | "$CLAUDE" -p --tools "" --output-format json "Reply with the JSON object you were given, nothing else." >/dev/null \
  || { echo "claude -p failed: run 'claude' once and log in"; exit 1; }

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.atlas.folder</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>atlas.folder</string>
    <string>--dir</string><string>$BOX</string>
    <string>--db</string><string>$HOME_DIR/atlas.db</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$CLAUDE"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/atlas.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/atlas.log</string>
</dict></plist>
PL

launchctl bootout "gui/$(id -u)/com.atlas.folder" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
launchctl print "gui/$(id -u)/com.atlas.folder" | grep -E "state =" || true
echo "Atlas watches: $BOX/inbox   database: $HOME_DIR/atlas.db   log: $HOME_DIR/logs/atlas.log"
