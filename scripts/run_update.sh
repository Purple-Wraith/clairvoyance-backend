#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Clairvoyance v5.0 — automated data refresh wrapper
#
# CRON SCHEDULE (Mountain Time — TZ=America/Denver):
#   Full refresh 5× daily at 08:00, 12:00, 16:00, 20:00, 00:00 MT
#   Live-window  (17:00–23:00 MT) — self-terminating live score loop
#
#   crontab -e  (add these lines):
#   TZ=America/Denver
#   0 8,12,16,20,0 * * * /Users/reeseoliver/clairvoyance-backend/scripts/run_update.sh --push >> /Users/reeseoliver/clairvoyance-backend/logs/cron.log 2>&1
#   0 17          * * * /Users/reeseoliver/clairvoyance-backend/scripts/run_update.sh --mode live --push >> /Users/reeseoliver/clairvoyance-backend/logs/live.log 2>&1
#
# Manual runs:
#   ./scripts/run_update.sh                         # full refresh, no push
#   ./scripts/run_update.sh --push                  # full refresh + push
#   ./scripts/run_update.sh --mode live --push      # live-window mode
#   ./scripts/run_update.sh --mode props --push     # Linemate props only
#   ./scripts/run_update.sh --sport nhl --push      # single sport
#   ./scripts/run_update.sh --no-reference --push   # skip slow Reference sites
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/clairvoyance_update.py"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/update_$(date +%Y%m%d).log"
PYTHON="${CLAIRVOYANCE_PYTHON:-python3}"

mkdir -p "$LOG_DIR"

# ── activate venv if present ─────────────────────────────────────────────────
for VENV in "$REPO_DIR/.venv" "$REPO_DIR/venv"; do
  if [[ -f "$VENV/bin/activate" ]]; then
    source "$VENV/bin/activate"
    PYTHON="$VENV/bin/python3"
    break
  fi
done

# ── install / upgrade deps once per day ──────────────────────────────────────
STAMP="$LOG_DIR/.deps_$(date +%Y%m%d)"
if [[ ! -f "$STAMP" ]]; then
  echo "[$(date +%H:%M:%S)] Installing dependencies…" | tee -a "$LOG_FILE"
  $PYTHON -m pip install -q -r "$REPO_DIR/scripts/requirements.txt" 2>&1 | tail -5 | tee -a "$LOG_FILE"
  touch "$STAMP"
fi

# ── load .env (if present) ───────────────────────────────────────────────────
if [[ -f "$REPO_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$REPO_DIR/.env"
  set +a
fi

# ── run ───────────────────────────────────────────────────────────────────────
echo ""                                                              | tee -a "$LOG_FILE"
echo "════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"
echo "[$(date +%H:%M:%S)] Clairvoyance v7.0  args: $*"            | tee -a "$LOG_FILE"
echo "════════════════════════════════════════════════════════════" | tee -a "$LOG_FILE"

cd "$REPO_DIR"
$PYTHON "$SCRIPT" "$@" 2>&1 | tee -a "$LOG_FILE"

echo "[$(date +%H:%M:%S)] Done." | tee -a "$LOG_FILE"

# ── rotate old logs ───────────────────────────────────────────────────────────
find "$LOG_DIR" -name "update_*.log" -mtime +14 -delete 2>/dev/null || true
find "$LOG_DIR" -name "live_*.log"   -mtime +7  -delete 2>/dev/null || true
find "$LOG_DIR" -name ".deps_*"      -mtime +1  -delete 2>/dev/null || true
