#!/bin/sh
# Run the TooGee assistant -- what the launch agent starts. Also fine by hand.
#
# launchd starts jobs with a bare PATH, so put Homebrew back (node is needed
# for the Kokoro voice) and run from the repo so .env and tts/ are found.
# Keeps the previous log as toogee.log.1 once the current one passes 5 MB.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED=1

LOG_DIR="$HOME/Library/Logs/TooGee"
LOG="$LOG_DIR/toogee.log"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5000000 ]; then
    mv -f "$LOG" "$LOG.1"
fi

exec "$REPO/.venv/bin/python" -W ignore "$REPO/wili_assistant.py" "$@"
