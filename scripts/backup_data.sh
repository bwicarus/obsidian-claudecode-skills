#!/usr/bin/env bash
# 每日数据备份 —— 只备份**不可再生**数据,跳过可由源头重建的大缓存。
# 大厂做法:SQLite 用在线 .backup(WAL 安全,不锁库不撕裂)、归档原子落地、保留滚动多份。
# 由 systemd timer bwicarus-backup.timer 每日触发。手动:bash scripts/backup_data.sh
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/bwicarus/backups}"
WEBAPP_DATA="${WEBAPP_DATA:-/home/bwicarus/webapp/data}"
STATE_DIR="${STATE_DIR:-/home/bwicarus/claude/state}"
KEEP="${KEEP:-14}"                  # 保留最近 N 天
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# ── 1) webapp/data:账号/token/用户数据(不可再生) ──
if [ -d "$WEBAPP_DATA" ]; then
  mkdir -p "$STAGE/webapp-data"
  # SQLite 在线一致备份(.backup 走 backup API,WAL 下安全,不会拷到半截事务)
  find "$WEBAPP_DATA" -maxdepth 5 -name '*.db' | while read -r db; do  # maxdepth 5:含 users/<u>/private/fitness.db
    rel="${db#"$WEBAPP_DATA"/}"
    mkdir -p "$STAGE/webapp-data/$(dirname "$rel")"
    sqlite3 "$db" ".backup '$STAGE/webapp-data/$rel'" || cp -a "$db" "$STAGE/webapp-data/$rel"
  done
  # 其余文件(dashboard/history/private 的 json、模板 html/css/js),排除 DB 与 WAL 边车
  rsync -a --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
        "$WEBAPP_DATA/" "$STAGE/webapp-data/" 2>/dev/null || \
    cp -a "$WEBAPP_DATA/." "$STAGE/webapp-data/"
fi

# ── 2) claude/state:学习状态小文件(vocab 追踪 / QA 历史 / 配额),排除可再生大缓存 ──
if [ -d "$STATE_DIR" ]; then
  mkdir -p "$STAGE/study-state"
  tar -C "$STATE_DIR" \
    --exclude='pdf-page-img' --exclude='backup-pdfs' --exclude='model3d' \
    --exclude='book-preprocess' --exclude='pdf-compressed' --exclude='kg' \
    --exclude='pdf-search.db' --exclude='dict-cache' --exclude='pdf-char-cache' \
    --exclude='google-vision-ocr' --exclude='pdf-text-index' --exclude='mokuro-ocr' \
    --exclude='grammar-cache' \
    -cf "$STAGE/study-state/state-small.tar" . 2>/dev/null || true
fi

# ── 3) 打包 + 原子落地(先写 .part 再 mv → 不会留半截归档) ──
OUT="$BACKUP_DIR/bwicarus-backup-$STAMP.tar.gz"
tar -C "$STAGE" -czf "$OUT.part" . && mv "$OUT.part" "$OUT"
echo "✅ 备份完成: $OUT ($(du -h "$OUT" | cut -f1))"

# ── 4) 保留最近 KEEP 份,删更旧的 ──
ls -1t "$BACKUP_DIR"/bwicarus-backup-*.tar.gz 2>/dev/null | tail -n +"$((KEEP+1))" | while read -r old; do
  rm -f "$old" && echo "🗑  删除旧备份: $old"
done
