#!/bin/bash
# Clairvoyance Engine v7.0 — Cron Schedule Setup
# Run: bash scripts/setup_cron.sh
# Installs refresh schedule into current user's crontab

SCRIPT_DIR="$(cd "$(dirname "$0")"; pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="$(which python3)"
LOG="$ROOT/logs/cron.log"
LIVE_LOG="$ROOT/logs/live.log"

mkdir -p "$ROOT/logs"

CRON_LINES="TZ=America/Denver
# Clairvoyance v7.0 — full refresh at 09:00, 15:00, 23:00 MT
0 9,15,23 * * * cd $ROOT && bash scripts/run_update.sh --push >> $LOG 2>&1
# Clairvoyance v7.0 — live window 16:00 MT (runs until 23:00 self-terminates)
0 16 * * * cd $ROOT && bash scripts/run_update.sh --mode live --push >> $LIVE_LOG 2>&1"

echo "Current crontab:"
crontab -l 2>/dev/null || echo "(empty)"
echo ""
echo "Installing Clairvoyance v7.0 cron schedule..."

(crontab -l 2>/dev/null \
  | grep -v "clairvoyance_update.py" \
  | grep -v "run_update.sh" \
  | grep -v "# Clairvoyance"; \
  echo "$CRON_LINES") | crontab -

echo "Done. New crontab:"
crontab -l
echo ""
echo "Schedule (all Mountain Time):"
echo "  09:00 MT — Full refresh (all sports, push to GitHub)"
echo "  15:00 MT — Full refresh (all sports, push to GitHub)"
echo "  16:00 MT — Live window starts (2-min intervals, self-terminates at 23:00)"
echo "  23:00 MT — Full refresh (all sports, push to GitHub)"
