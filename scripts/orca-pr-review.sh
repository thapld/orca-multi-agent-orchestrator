#!/bin/zsh
# orca-pr-review.sh: Script hỗ trợ mở nhanh worktree thẩm định GitHub PR độc lập
# Sử dụng: ./orca-pr-review.sh <pr-number> [agent]

set -e

PR_NUM="${1:?Thiếu số PR. Dùng: $0 <pr-number> [agent]}"
if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
  echo "❌ Số PR phải là số nguyên dương (ví dụ: 123). Giá trị nhận được: '$PR_NUM'"
  exit 1
fi

CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  CONFIG_AGENT=$(orca-team get pr 2>/dev/null || true)
fi
AGENT="${2:-${CONFIG_AGENT:-claude}}"
WORKTREE_NAME="review-pr-$PR_NUM"

echo "🐋 [Orca PR Review] Đang khởi tạo worktree thẩm định PR #$PR_NUM bằng agent: $AGENT..."

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"

RECEIPT=$($ORCA_BIN worktree create \
  --name "$WORKTREE_NAME" \
  --pr "$PR_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --setup run \
  --json 2>&1) || true

WT_NAME=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') is False:
        sys.exit(1)
    res = d.get('result', d)
    name = res.get('name') or (res.get('worktree', {}).get('name') if isinstance(res.get('worktree'), dict) else '')
    print(name or '')
except Exception:
    sys.exit(1)
" <<< "$RECEIPT" 2>/dev/null || true)

if [[ -z "$WT_NAME" ]]; then
  echo "❌ Khởi tạo worktree cho PR #$PR_NUM thất bại: $RECEIPT"
  exit 1
fi

echo "✅ Đã tạo thành công worktree review PR: $WT_NAME"
echo "👉 Mã nguồn PR #$PR_NUM đã được checkout tự động để bắt đầu review line-by-line diff."
