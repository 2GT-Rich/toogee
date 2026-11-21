#!/bin/sh
# Stop TooGee and remove its launch agent. Logs in ~/Library/Logs/TooGee are kept.
set -eu
LABEL=com.toogee.assistant
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed the TooGee launch agent."
