#!/usr/bin/env bash
set -euo pipefail

STATUS_FILE="${1:-/home/bwicarus/claude/references/reader-collaboration-status.md}"
EVENT_DIR="${2:-/home/bwicarus/claude/references/.reader-status-events}"
EVENT_LOG="$EVENT_DIR/events.tsv"
SNAPSHOT_FILE="$EVENT_DIR/last-status.md"

mkdir -p "$EVENT_DIR"
touch "$EVENT_LOG"

status_dir="$(dirname "$STATUS_FILE")"
status_name="$(basename "$STATUS_FILE")"
last_hash=""

record_event() {
  local current_hash now event_id diff_file
  [[ -f "$STATUS_FILE" ]] || return 0
  current_hash="$(sha256sum "$STATUS_FILE" | awk '{print $1}')"
  [[ "$current_hash" == "$last_hash" ]] && return 0
  last_hash="$current_hash"
  now="$(TZ=Asia/Tokyo date '+%Y-%m-%d %H:%M:%S %Z')"
  event_id="$(date '+%Y%m%d-%H%M%S')-$$"
  diff_file="$EVENT_DIR/$event_id.diff"
  if [[ -f "$SNAPSHOT_FILE" ]]; then
    diff -u "$SNAPSHOT_FILE" "$STATUS_FILE" > "$diff_file" || true
  else
    cp "$STATUS_FILE" "$diff_file"
  fi
  cp "$STATUS_FILE" "$SNAPSHOT_FILE"
  printf '%s\t%s\t%s\n' "$now" "$current_hash" "$diff_file" >> "$EVENT_LOG"
  : > "$EVENT_DIR/pending"
  if tmux has-session -t reader-coordinator 2>/dev/null; then
    tmux send-keys -t reader-coordinator:0.0 -l -- "收到共享状态文件更新事件。新增内容在 $diff_file；请读取完整共享状态文件并按当前用户任务规则决定是否向现有 Codex 或 Claude 会话下达下一步中文指令。" 
    tmux send-keys -t reader-coordinator:0.0 C-m
  fi
}

record_event

while true; do
  inotifywait -m -q -e close_write,moved_to --format '%f' "$status_dir" 2>/dev/null |
  while IFS= read -r changed; do
    [[ "$changed" == "$status_name" ]] || continue
    sleep 0.25
    record_event
  done
  sleep 1
done
