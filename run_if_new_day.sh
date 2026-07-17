#!/bin/bash
# Runs scout.py once per calendar day, triggered by Claude Code's SessionStart hook.
DIR="/Users/zhongyuxuan/Desktop/job-scout"
STAMP="$DIR/.last_session_run"
TODAY=$(date +%Y-%m-%d)

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$TODAY" ]; then
    exit 0
fi

echo "$TODAY" > "$STAMP"
cd "$DIR" || exit 1
/opt/homebrew/bin/python3 scout.py >> logs/scout.log 2>> logs/scout.err.log
