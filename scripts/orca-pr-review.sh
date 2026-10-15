#!/bin/zsh
# orca-pr-review.sh: Script hỗ trợ mở nhanh worktree thẩm định GitHub PR độc lập
# Sử dụng: ./orca-pr-review.sh <pr-number> [agent]

set -e

PR_NUM="${1:?Thiếu số PR. Dùng: $0 <pr-number> [agent]}"
CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  CONFIG_AGENT=$(orca-team get pr 2>/dev/null || true)
fi
AGENT="${2:-${CONFIG_AGENT:-claude}}"
WORKTREE_NAME="review-pr-$PR_NUM"

echo "🐋 [Orca PR Review] Đang khởi tạo worktree thẩm định PR #$PR_NUM bằng agent: $AGENT..."

RECEIPT=$(/usr/local/bin/orca worktree create \
  --name "$WORKTREE_NAME" \
  --pr "$PR_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --setup run \
  --json)

WT_NAME=$(echo "$RECEIPT" | grep -o '"name": *"[^"]*"' | head -n 1 | cut -d'"' -f4)
echo "✅ Đã tạo thành công worktree review PR: ${WT_NAME:-$WORKTREE_NAME}"
echo "👉 Mã nguồn PR #$PR_NUM đã được checkout tự động để bắt đầu review line-by-line diff."
