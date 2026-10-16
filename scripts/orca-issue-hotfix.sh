#!/bin/zsh
# orca-issue-hotfix.sh: Script hỗ trợ mở nhanh worktree sửa GitHub Issue song song
# Sử dụng: ./orca-issue-hotfix.sh <issue-number> [agent]

set -e

if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" || $# -lt 1 ]]; then
  echo "🐋 [Orca Hotfix] Mở hoặc tái sử dụng worktree xử lý GitHub Issue độc lập"
  echo "Sử dụng:"
  echo "  orca-hotfix <issue-number> [role|agent]"
  echo ""
  echo "Ví dụ:"
  echo "  orca-hotfix 42"
  echo "  orca-hotfix 42 kimi"
  exit 0
fi

ISSUE_NUM="$1"
if ! [[ "$ISSUE_NUM" =~ ^[0-9]+$ ]]; then
  echo "❌ Số Issue phải là số nguyên dương (ví dụ: 42). Giá trị nhận được: '$ISSUE_NUM'"
  exit 1
fi

ORCA_TEAM_BIN="$(command -v orca-team 2>/dev/null || echo "${0:A:h}/orca-team")"
ROLE_OR_AGENT="${2:-issue}"
CONFIG_AGENT=""
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  CONFIG_AGENT=$("$ORCA_TEAM_BIN" get "$ROLE_OR_AGENT" 2>/dev/null || true)
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" get issue 2>/dev/null || true)
  fi
  [[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]] && CONFIG_AGENT=""
fi
if [[ "$ROLE_OR_AGENT" =~ ^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot|gemini|muse|codebuddy|omp)$ ]]; then
  AGENT="$ROLE_OR_AGENT"
else
  AGENT="${CONFIG_AGENT:-antigravity}"
fi
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

ISSUE_PROMPT="[ROLE: PRINCIPAL ISSUE RESOLVER & BUGFIX ENGINEER] Nhiệm vụ: Xử lý triệt để GitHub Issue #${ISSUE_NUM}. Chuẩn mực bắt buộc: (1) Đọc kỹ mô tả Issue và tìm đúng nguyên nhân gốc rễ (Root Cause Analysis), (2) BẮT BUỘC viết Reproduction Test tái hiện đúng lỗi (chạy test xác nhận FAIL trước khi sửa), (3) Sửa mã nguồn tối thiểu, chuẩn kiến trúc cho đến khi Reproduction Test chuyển sang PASS, (4) Chạy toàn bộ regression test suite đảm bảo EXIT_CODE=0 và không làm vỡ tính năng cũ. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ để lách lỗi; CẤM refactor lan man ngoài phạm vi Issue. BẰNG CHỨNG BẮT BUỘC: Commit sạch với thông điệp 'fix(#${ISSUE_NUM}): <mô tả>' và xuất đầy đủ Commit Hash + log chạy test."

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

# Kiểm tra xem worktree định danh hotfix-issue-<ISSUE_NUM> đã tồn tại trong repo chưa (tránh đẻ worktree trùng lặp -2, -3)
EXISTING_WT_PATH=$(git worktree list 2>/dev/null | sed -n "s/^\([^ ]*\).*\[${WORKTREE_NAME}\].*/\1/p" | head -n 1)

if [[ -n "$EXISTING_WT_PATH" ]]; then
  echo "♻️  [Orca Hotfix] Tái sử dụng worktree định danh hiện có: $WORKTREE_NAME ($EXISTING_WT_PATH)..."
  DISPATCH_SCRIPT="${0:A:h}/orca-dispatch-task.sh"
  if [[ -x "$DISPATCH_SCRIPT" ]]; then
    exec "$DISPATCH_SCRIPT" "$WORKTREE_NAME" "${2:-issue}" "$ISSUE_PROMPT"
  fi
fi

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

RECEIPT=$($ORCA_BIN worktree create \
  --repo "path:$REPO_ROOT" \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
  --no-parent \
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
    wt = res.get('worktree') if isinstance(res.get('worktree'), dict) else res
    # Orca 'worktree create --json' trả về result.worktree {displayName, path, id, ...} (không có trường 'name')
    name = wt.get('displayName') or wt.get('name') or wt.get('path') or wt.get('id') or ''
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
