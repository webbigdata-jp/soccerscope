#!/usr/bin/env bash
#
# run_daily.sh - soccer 配信データ収集パイプライン（毎朝10時にタスクスケジューラから実行）
# 配置先: /home/dev1/haishin/soccer/run_daily.sh
#
set -Eeuo pipefail

# --- uv などにPATHを通す（ログオフ/非対話セッションでは .bashrc が読まれないため必須） ---
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
# uv 標準インストーラが置く env があれば読み込む
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"

# 変更後
PROJECT_DIR="/home/dev1/haishin/soccer/git"
PIPELINE_DIR="$PROJECT_DIR/pipeline"
LOG_DIR="/home/dev1/haishin/soccer/logs"     # ログ置き場はgit管理外のまま据え置き
...
cd "$PIPELINE_DIR"
...
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
cd "$GIT_DIR"
git add -A
if git diff --cached --quiet; then
  log "コミット対象の変更なし。commit/push をスキップ。"
else
  git commit -m "add data $(date '+%F %T')"
  git push
  log "push 完了。"
fi

# 30日より古いログを掃除
find "$LOG_DIR" -name '*.log' -type f -mtime +30 -delete 2>/dev/null || true

log "===== DONE ====="
