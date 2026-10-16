#!/bin/zsh
# orca-pr-review.sh: Script hỗ trợ mở nhanh worktree thẩm định GitHub PR độc lập
# Sử dụng: ./orca-pr-review.sh <pr-number> [agent]

set -e

PR_NUM="${1:?Thiếu số PR. Dùng: $0 <pr-number> [agent]}"
if ! [[ "$PR_NUM" =~ ^[0-9]+$ ]]; then
  echo "❌ Số PR phải là số nguyên dương (ví dụ: 123). Giá trị nhận được: '$PR_NUM'"
  exit 1
fi

ORCA_TEAM_BIN="$(command -v orca-team 2>/dev/null || echo "${0:A:h}/orca-team")"
CONFIG_AGENT=""
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  CONFIG_AGENT=$("$ORCA_TEAM_BIN" get pr 2>/dev/null || true)
  [[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]] && CONFIG_AGENT=""
fi
AGENT="${2:-${CONFIG_AGENT:-antigravity}}"
WORKTREE_NAME="review-pr-$PR_NUM"

PR_PROMPT="[ROLE: PRINCIPAL PR REVIEWER] Nhiệm vụ: Thẩm định toàn diện Pull Request #${PR_NUM}. Chuẩn mực: (1) Đọc toàn bộ line-by-line diff qua 'orca file open-changed --mode diff --worktree active' và 'git diff', (2) Kiểm tra kiến trúc, Clean Code, OWASP Top 10, Race Conditions, Memory/Resource Leaks, (3) BẮT BUỘC chạy thực tế test suite và linter trong worktree PR (CẤM duyệt suông bằng mắt!). CẤM VƯỢT RÀO: Không tự ý sửa code tính năng của PR khi chưa được yêu cầu. BẰNG CHỨNG BẮT BUỘC: Xuất báo cáo review chi tiết từng file:line, kết quả chạy test thực tế (EXIT_CODE, số test pass/fail) và chốt PR_VERDICT: APPROVE hoặc REQUEST_CHANGES."

echo "🐋 [Orca PR Review] Đang khởi tạo worktree thẩm định PR #$PR_NUM bằng agent: $AGENT..."

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"

RECEIPT=$($ORCA_BIN worktree create \
  --name "$WORKTREE_NAME" \
  --pr "$PR_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --prompt "$PR_PROMPT" \
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
echo "👉 Mã nguồn PR #$PR_NUM đã được checkout tự động kèm chỉ dẫn Principal PR Reviewer."
