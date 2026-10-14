#!/bin/zsh
# orca-issue-hotfix.sh: Script hỗ trợ mở nhanh worktree sửa GitHub Issue song song
# Sử dụng: ./orca-issue-hotfix.sh <issue-number> [agent]

set -e

ISSUE_NUM="${1:?Thiếu số Issue. Dùng: $0 <issue-number> [agent]}"
AGENT="${2:-kimi}"
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

RECEIPT=$(/usr/local/bin/orca worktree create \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
  --agent "$AGENT" \
  --setup run \
  --json)

echo "✅ Đã tạo thành công worktree: $WORKTREE_NAME"
echo "👉 Mã nguồn Issue #$ISSUE_NUM đã được kéo về tự động và cô lập trong worktree."
echo "$RECEIPT"
