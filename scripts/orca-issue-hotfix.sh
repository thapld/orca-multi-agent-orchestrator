#!/bin/zsh
# orca-issue-hotfix.sh: Script hỗ trợ mở nhanh worktree sửa GitHub Issue song song
# Sử dụng: ./orca-issue-hotfix.sh <issue-number> [agent]

set -e

ISSUE_NUM="${1:?Thiếu số Issue. Dùng: $0 <issue-number> [agent]}"
if ! [[ "$ISSUE_NUM" =~ ^[0-9]+$ ]]; then
  echo "❌ Số Issue phải là số nguyên dương (ví dụ: 42). Giá trị nhận được: '$ISSUE_NUM'"
  exit 1
fi

CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  CONFIG_AGENT=$(orca-team get issue 2>/dev/null || true)
fi
AGENT="${2:-${CONFIG_AGENT:-kimi}}"
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"

RECEIPT=$($ORCA_BIN worktree create \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
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
  echo "❌ Khởi tạo worktree cho Issue #$ISSUE_NUM thất bại: $RECEIPT"
  exit 1
fi

echo "✅ Đã tạo thành công worktree độc lập: $WT_NAME"
echo "👉 Mã nguồn Issue #$ISSUE_NUM đã được kéo về tự động và cô lập trong worktree."
