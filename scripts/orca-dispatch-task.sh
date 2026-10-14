#!/bin/zsh
# orca-dispatch-task.sh: Script hỗ trợ giao nhanh task cho worker trong child worktree
# Sử dụng: ./orca-dispatch-task.sh <task-title> <agent> "<spec-description>"

set -e

TASK_TITLE="${1:?Thiếu tiêu đề task. Dùng: $0 <title> <agent> <spec>}"
AGENT="${2:-kimi}"
SPEC="${3:?Thiếu mô tả spec. Dùng: $0 <title> <agent> <spec>}"

echo "🐋 [Orca Dispatch] Đang khởi tạo task: '$TASK_TITLE' cho agent: $AGENT..."

# 1. Tạo task trong Orca
TASK_JSON=$(/usr/local/bin/orca orchestration task-create \
  --task-title "$TASK_TITLE" \
  --spec "$SPEC" \
  --json)

TASK_ID=$(echo "$TASK_JSON" | grep -o '"id": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

if [[ -z "$TASK_ID" ]]; then
  echo "❌ Không thể tạo task trong Orca. Kết quả: $TASK_JSON"
  exit 1
fi

echo "✅ Đã tạo task ID: $TASK_ID"

# 2. Khởi chạy worker trong child worktree
WORKTREE_NAME="task-$(echo "$TASK_TITLE" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')"

echo "🚀 Đang khởi chạy worker trong worktree: $WORKTREE_NAME..."

START_RECEIPT=$(/usr/local/bin/orca orchestration worker-start \
  --task "$TASK_ID" \
  --worktree new-child \
  --name "$WORKTREE_NAME" \
  --agent "$AGENT" \
  --setup run \
  --timeout-ms 300000 \
  --json)

echo "🎉 Worker đã được khởi chạy thành công!"
echo "$START_RECEIPT"
