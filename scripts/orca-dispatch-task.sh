#!/bin/zsh
# orca-dispatch-task.sh: Script hỗ trợ giao nhanh task cho worker trong child worktree
# Sử dụng: ./orca-dispatch-task.sh <task-title> <agent> "<spec-description>"

set -e

TASK_TITLE="${1:?Thiếu tiêu đề task. Dùng: $0 <title> [agent|role] <spec>}"

if [[ $# -eq 2 ]]; then
  SPEC="$2"
  REQ_AGENT=""
else
  REQ_AGENT="${2:-}"
  SPEC="${3:?Thiếu mô tả spec. Dùng: $0 <title> [agent|role] <spec>}"
fi

# Tự động lấy agent của dev từ orca-team nếu không truyền agent cụ thể
CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  ROLE_QUERY="${REQ_AGENT:-dev}"
  CONFIG_AGENT=$(orca-team get "$ROLE_QUERY" 2>/dev/null || true)
fi

AGENT="${CONFIG_AGENT:-${REQ_AGENT:-kimi}}"
if [[ -n "$REQ_AGENT" && "$REQ_AGENT" =~ ^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot)$ ]]; then
  AGENT="$REQ_AGENT"
fi

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

DISPATCH_ID=$(echo "$START_RECEIPT" | grep -o '"dispatchId": *"[^"]*"' | head -n 1 | cut -d'"' -f4)
TERM_HANDLE=$(echo "$START_RECEIPT" | grep -o '"agentTerminalHandle": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

echo "🎉 Worker đã được khởi chạy thành công!"
echo "   • Worktree: $WORKTREE_NAME"
[[ -n "$DISPATCH_ID" ]] && echo "   • Dispatch: $DISPATCH_ID"
[[ -n "$TERM_HANDLE" ]] && echo "   • Terminal: $TERM_HANDLE"
