#!/bin/zsh
# orca-pr-review.sh: Script hỗ trợ mở nhanh worktree thẩm định GitHub PR độc lập
# Sử dụng: ./orca-pr-review.sh <pr-number> [agent]

set -e

PR_NUM="${1:?Thiếu số PR. Dùng: $0 <pr-number> [agent]}"
AGENT="${2:-claude}"
WORKTREE_NAME="review-pr-$PR_NUM"

echo "🐋 [Orca PR Review] Đang khởi tạo worktree thẩm định PR #$PR_NUM bằng agent: $AGENT..."

RECEIPT=$(/usr/local/bin/orca worktree create \
  --name "$WORKTREE_NAME" \
  --pr "$PR_NUM" \
  --agent "$AGENT" \
  --setup run \
  --json)

echo "✅ Đã tạo thành công worktree: $WORKTREE_NAME"
echo "👉 Mã nguồn PR #$PR_NUM đã được checkout tự động để bắt đầu review line-by-line diff."
echo "$RECEIPT"
