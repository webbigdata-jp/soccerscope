#!/usr/bin/env bash
#
# run_daily.sh - Soccer streaming data collection pipeline (run every morning at 10:00 from Task Scheduler)
# Location: /home/dev1/haishin/soccer/run_daily.sh
#
set -Eeuo pipefail

# --- Add uv and related tools to PATH (required because .bashrc is not loaded in logged-off/non-interactive sessions) ---
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
# Load the env file created by the standard uv installer, if it exists
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"

PROJECT_DIR="/home/dev1/haishin/soccer/git"
PIPELINE_DIR="$PROJECT_DIR/pipeline"
LOG_DIR="/home/dev1/haishin/soccer/logs"     # Keep the log directory outside Git management
cd "$PIPELINE_DIR"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"
log() { echo "[$(date '+%F %T')] $*"; }

# Record where the script failed (set -e exits immediately afterward)
trap 'rc=$?; log "ERROR: failed at line ${LINENO} (exit ${rc}). Stopping the remaining steps."' ERR

log "===== START daily pipeline ====="
log "log file: $LOG_FILE"


# Helper that logs each command before running it
run() {
  log ">>> $*"
  "$@"
}

run uv run phase2_collect_video_ids.py
run uv run phase3_fetch_metadata.py
run uv run phase7_calc_buzz_score.py
run uv run phase4_fetch_comments.py
run uv run 1_embed_videos.py
run uv run 2_load_to_mongo.py
run uv run 3_analyze_comments.py
run uv run 4_load_comment_analysis.py
run uv run build_stats_page.py

# --- git ---
cd "$PROJECT_DIR"

# --- git ---
log ">>> git add / commit / push"
cd "$PROJECT_DIR"
git add -A
if git diff --cached --quiet; then
  log "No changes to commit. Skipping commit/push."
else
  git commit -m "add data $(date '+%F %T')"
  git push
  log "push completed."
fi

# Clean up logs older than 30 days
find "$LOG_DIR" -name '*.log' -type f -mtime +30 -delete 2>/dev/null || true

log "===== DONE ====="
