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

PROJECT_DIR="/home/dev1/haishin/soccer/git"
PIPELINE_DIR="$PROJECT_DIR/pipeline"
LOG_DIR="/home/dev1/haishin/soccer/logs"     # ログ置き場はgit管理外のまま据え置き
cd "$PIPELINE_DIR"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d_%H%M%S).log"
log() { echo "[$(date '+%F %T')] $*"; }

# 失敗したらどこで落ちたか記録（set -e により直後に終了する）
trap 'rc=$?; log "ERROR: 失敗 line ${LINENO} (exit ${rc})。以降の処理を中止します。"' ERR

log "===== START daily pipeline ====="
log "log file: $LOG_FILE"


# 実行コマンドをログに出しつつ走らせるヘルパ
run() {
  log ">>> $*"
  "$@"
}


# --- git ---
cd "$PROJECT_DIR"

# --- git ---
log ">>> git add / commit / push"
cd "$PROJECT_DIR"
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
