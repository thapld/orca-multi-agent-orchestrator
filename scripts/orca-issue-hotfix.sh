#!/bin/zsh
# orca-issue-hotfix.sh: Script hỗ trợ mở nhanh worktree sửa GitHub Issue song song
# Sử dụng: ./orca-issue-hotfix.sh <issue-number> [agent]

set -e

ISSUE_NUM="${1:?Thiếu số Issue. Dùng: $0 <issue-number> [agent]}"
CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  CONFIG_AGENT=$(orca-team get issue 2>/dev/null || true)
fi
AGENT="${2:-${CONFIG_AGENT:-kimi}}"
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

RECEIPT=$(/usr/local/bin/orca worktree create \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --setup run \
  --json)

WT_NAME=$(echo "$RECEIPT" | grep -o '"name": *"[^"]*"' | head -n 1 | cut -d'"' -f4)
echo "✅ Đã tạo thành công worktree độc lập: ${WT_NAME:-$WORKTREE_NAME}"
echo "👉 Mã nguồn Issue #$ISSUE_NUM đã được kéo về tự động và cô lập trong worktree."
