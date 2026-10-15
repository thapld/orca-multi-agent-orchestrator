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

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
TIMEOUT_MS="${TIMEOUT_MS:-300000}"

# Lấy model và reasoning effort nếu đã được cấu hình trong orca-team
MODEL_ARGS=()
if command -v orca-team &>/dev/null; then
  DEV_MODEL=$(orca-team get "$ROLE_QUERY" --field model 2>/dev/null || true)
  DEV_EFFORT=$(orca-team get "$ROLE_QUERY" --field effort 2>/dev/null || true)
  if [[ -n "$DEV_MODEL" && "$DEV_MODEL" != "null" && "$DEV_MODEL" != "(default)" ]]; then
    MODEL_ARGS+=(--model "$DEV_MODEL")
    if [[ -n "$DEV_EFFORT" && "$DEV_EFFORT" != "null" && "$DEV_EFFORT" != "-" ]]; then
      MODEL_ARGS+=(--effort "$DEV_EFFORT")
    fi
  fi
fi

echo "🐋 [Orca Dispatch] Đang khởi tạo task: '$TASK_TITLE' cho agent: $AGENT ${MODEL_ARGS[*]}..."

# 1. Tạo task trong Orca
TASK_JSON=$($ORCA_BIN orchestration task-create \
  --task-title "$TASK_TITLE" \
  --spec "$SPEC" \
  --json 2>&1) || true

TASK_ID=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') is False:
        sys.exit(1)
    res = d.get('result', d)
    tid = res.get('id') or (res.get('task', {}).get('id') if isinstance(res.get('task'), dict) else '')
    print(tid or '')
except Exception:
    sys.exit(1)
" <<< "$TASK_JSON" 2>/dev/null || echo "$TASK_JSON" | grep -o '"id": *"[^"]*"' | tail -n 1 | cut -d'"' -f4)

if [[ -z "$TASK_ID" ]]; then
  echo "❌ Không thể tạo task trong Orca. Kết quả: $TASK_JSON"
  exit 1
fi

echo "✅ Đã tạo task ID: $TASK_ID"

# 2. Khởi chạy worker trong child worktree
SAFE_SLUG=$(echo "$TASK_TITLE" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')
[[ -z "$SAFE_SLUG" ]] && SAFE_SLUG="task"
SHORT_ID=$(echo "$TASK_ID" | tail -c 7)
WORKTREE_NAME="wt-${SAFE_SLUG}-${SHORT_ID}"

echo "🚀 Đang khởi chạy worker trong worktree: $WORKTREE_NAME..."

START_RECEIPT=$($ORCA_BIN orchestration worker-start \
  --task "$TASK_ID" \
  --worktree new-child \
  --name "$WORKTREE_NAME" \
  --agent "$AGENT" \
  "${MODEL_ARGS[@]}" \
  --setup run \
  --timeout-ms "$TIMEOUT_MS" \
  --json 2>&1) || true

DISPATCH_ID=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') is False:
        sys.exit(1)
    res = d.get('result', d)
    did = res.get('dispatchId') or res.get('id') or ''
    print(did)
except Exception:
    sys.exit(1)
" <<< "$START_RECEIPT" 2>/dev/null || echo "$START_RECEIPT" | grep -o '"dispatchId": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

TERM_HANDLE=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    res = d.get('result', d)
    print(res.get('agentTerminalHandle') or res.get('terminalHandle') or '')
except Exception:
    print('')
" <<< "$START_RECEIPT" 2>/dev/null || echo "$START_RECEIPT" | grep -o '"agentTerminalHandle": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

if [[ -z "$DISPATCH_ID" ]]; then
  echo "❌ Khởi chạy worker thất bại: $START_RECEIPT"
  exit 1
fi

echo "🎉 Worker đã được khởi chạy thành công!"
echo "   • Worktree: $WORKTREE_NAME"
echo "   • Dispatch: $DISPATCH_ID"
[[ -n "$TERM_HANDLE" ]] && echo "   • Terminal: $TERM_HANDLE"
