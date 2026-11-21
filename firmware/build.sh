#!/bin/sh
# Build and flash the Wili firmware for the FreeWili OG.
#
#   firmware/build.sh build        # build both CPUs (default)
#   firmware/build.sh flash        # build, then flash via the main CPU
#   firmware/build.sh bootloader   # ONE-TIME per board: install the display bootloader
#   firmware/build.sh test         # the toolkit's host unit tests
#
# The firmware is an app for Free-Wili's wiliOGbsp toolkit, which is a git
# submodule pinned to the commit it was developed against. This script
#   1. initialises the submodule,
#   2. applies patches/wiliOGbsp-wili.patch (small driver additions: speaker
#      stream mode and an async LCD blit) unless already applied,
#   3. links apps/wili into the toolkit's apps/ folder,
#   4. runs the toolkit's own `fw` task runner.
# Needs the Pico SDK 2.3.0 layout under ~/.pico-sdk -- see the README.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
BSP="$HERE/wiliOGbsp"
PATCH="$HERE/patches/wiliOGbsp-wili.patch"
CMD=${1:-build}

if [ ! -f "$BSP/AGENTS.md" ]; then
    git -C "$REPO" submodule update --init firmware/wiliOGbsp
fi

if git -C "$BSP" apply --reverse --check "$PATCH" 2>/dev/null; then
    : # already applied
else
    git -C "$BSP" apply "$PATCH"
    echo "applied $(basename "$PATCH")"
fi

if [ ! -e "$BSP/apps/wili" ]; then
    ln -s ../../apps/wili "$BSP/apps/wili"
fi

# fw.py only needs pyserial; prefer the project's venv.
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY=python3

case "$CMD" in
    build)      exec "$PY" "$BSP/tools/fw.py" build wili ;;
    flash)      "$PY" "$BSP/tools/fw.py" build wili && exec "$PY" "$BSP/tools/fw.py" flash wili ;;
    bootloader) exec "$PY" "$BSP/tools/fw.py" bootloader ;;
    test)       exec "$PY" "$BSP/tools/fw.py" test ;;
    *) echo "usage: $0 [build|flash|bootloader|test]" >&2; exit 2 ;;
esac
