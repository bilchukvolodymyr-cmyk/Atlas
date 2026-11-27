#!/usr/bin/env bash
# Atlas reads Gmail (READ ONLY) on this Mac: a second LaunchAgent next to com.atlas.folder.
# Uses the Claude Code CLI (Claude subscription + claude.ai Gmail connector). Safe to run again.
set -euo pipefail
HOME_DIR="$HOME/Atlas"; APP="$HOME_DIR/app"
PLIST="$HOME/Library/LaunchAgents/com.atlas.mail.plist"
PY="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$HOME/Library/LaunchAgents/com.atlas.folder.plist" 2>/dev/null || command -v python3)"
CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
[ -f "$HOME_DIR/mail.json" ] || cat > "$HOME_DIR/mail.json" <<J
{"enabled": true, "interval_minutes": 15, "backfill_days": 7,
 "tool_prefix": "mcp__claude_ai_Gmail__", "search_model": "haiku", "triage_model": "sonnet"}
J
chmod 600 "$HOME_DIR/mail.json"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.atlas.mail</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>atlas.mail</string>
    <string>--db</string><string>$HOME_DIR/atlas.db</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$CLAUDE"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME_DIR/logs/mail.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/logs/mail.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/com.atlas.mail" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
launchctl print "gui/$(id -u)/com.atlas.mail" | grep -E "state =" || true
echo "mail log: $HOME_DIR/logs/mail.log   ledger: $HOME_DIR/mail.db"
