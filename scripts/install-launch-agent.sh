#!/bin/sh
# Install (or reinstall) the TooGee launch agent: starts TooGee at login and
# keeps it running. Logs: ~/Library/Logs/TooGee/toogee.log
#
#   scripts/install-launch-agent.sh
#   scripts/uninstall-launch-agent.sh      # to remove it
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
LABEL=com.toogee.assistant
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[ -x "$REPO/.venv/bin/python" ] || { echo "No .venv yet -- see the README's setup steps." >&2; exit 1; }
[ -f "$REPO/.env" ] || { echo "No .env yet -- copy .env.example to .env and fill it in." >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/TooGee"
chmod +x "$REPO/scripts/run.sh"
sed -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" "$REPO/launchd/$LABEL.plist.in" > "$DEST"
plutil -lint "$DEST" >/dev/null

# Replace a running copy, if any, then start it.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed $DEST"
echo "TooGee is starting; follow it with:  tail -f ~/Library/Logs/TooGee/toogee.log"
