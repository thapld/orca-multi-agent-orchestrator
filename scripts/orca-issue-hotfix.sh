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
AGENT="${2:-${CONFIG_AGENT:-antigravity}}"
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

ISSUE_PROMPT="[ROLE: PRINCIPAL ISSUE RESOLVER & BUGFIX ENGINEER] Nhiệm vụ: Xử lý triệt để GitHub Issue #${ISSUE_NUM}. Chuẩn mực bắt buộc: (1) Đọc kỹ mô tả Issue và tìm đúng nguyên nhân gốc rễ (Root Cause Analysis), (2) BẮT BUỘC viết Reproduction Test tái hiện đúng lỗi (chạy test xác nhận FAIL trước khi sửa), (3) Sửa mã nguồn tối thiểu, chuẩn kiến trúc cho đến khi Reproduction Test chuyển sang PASS, (4) Chạy toàn bộ regression test suite đảm bảo EXIT_CODE=0 và không làm vỡ tính năng cũ. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ để lách lỗi; CẤM refactor lan man ngoài phạm vi Issue. BẰNG CHỨNG BẮT BUỘC: Commit sạch với thông điệp 'fix(#${ISSUE_NUM}): <mô tả>' và xuất đầy đủ Commit Hash + log chạy test."

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"

RECEIPT=$($ORCA_BIN worktree create \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
  --agent "$AGENT" \
  --prompt "$ISSUE_PROMPT" \
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
echo "👉 Mã nguồn Issue #$ISSUE_NUM đã được kéo về tự động kèm chỉ dẫn Principal Issue Resolver."
