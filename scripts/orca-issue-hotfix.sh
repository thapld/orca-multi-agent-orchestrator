#!/bin/zsh
# orca-issue-hotfix.sh: Script hỗ trợ mở nhanh worktree sửa GitHub Issue song song
# Sử dụng: ./orca-issue-hotfix.sh <issue-number> [agent]

set -e

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  echo "🐋 [Orca Hotfix] Mở hoặc tái sử dụng worktree xử lý GitHub Issue độc lập"
  echo "Sử dụng:"
  echo "  orca-hotfix <issue-number> [role|agent]"
  echo ""
  echo "Ví dụ:"
  echo "  orca-hotfix 42"
  echo "  orca-hotfix 42 kimi"
  exit 0
fi

if [[ $# -lt 1 ]]; then
  echo "❌ Thiếu số Issue. Sử dụng: orca-hotfix <issue-number> [role|agent]" >&2
  exit 1
fi
if [[ $# -gt 2 ]]; then
  echo "❌ Thừa tham số ($# tham số). Sử dụng: orca-hotfix <issue-number> [role|agent]" >&2
  exit 1
fi

ISSUE_NUM="$1"
if ! [[ "$ISSUE_NUM" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ Số Issue phải là số nguyên dương (ví dụ: 42). Giá trị nhận được: '$ISSUE_NUM'" >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "❌ Thư mục hiện tại không nằm trong Git repository." >&2
  exit 1
fi

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
GIT_COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [[ -n "$GIT_COMMON_DIR" && "${GIT_COMMON_DIR:t}" == ".git" ]]; then
  REPO_ROOT="${GIT_COMMON_DIR:h}"
else
  REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
fi

ORCA_TEAM_BIN="$(command -v orca-team 2>/dev/null || echo "${0:A:h}/orca-team")"
ROLE_OR_AGENT="${2:-issue}"
ROLE_OR_AGENT="${ROLE_OR_AGENT:l}"
KNOWN_AGENTS_RE='^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot|gemini|muse|codebuddy|omp)$'
KNOWN_ROLES_RE='^((coord|coordinator|leader|lead|architect|orchestrator|dev|developer|developers|coder|fullstack|backend|doc|docs|fix|devops|devopsspecialist|infra|sre|ops|sec|security|bughunter|bug|auditor|qa|test|tester|sdet|ui|ux|frontend|uispecialist|design|bench|benchmark|perf|performance|benchspecialist|re|reverse|rea|respecialist|critic|research|researcher|challenger|advocate|criticspecialist|pr|prreviewer|pr-review|reviewer|issue|issueresolver|issue-fix|hotfix)(-worker)?(-[0-9]+)?)$'
if ! [[ "$ROLE_OR_AGENT" =~ $KNOWN_AGENTS_RE || "$ROLE_OR_AGENT" =~ $KNOWN_ROLES_RE ]]; then
  echo "❌ Role hoặc agent không hợp lệ: '${2:-issue}'. Hãy dùng role hợp lệ hoặc tên agent CLI." >&2
  exit 1
fi
CONFIG_AGENT=""
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$ROLE_OR_AGENT" 2>/dev/null || true)
  if [[ "$CONFIG_AGENT" == "none" ]] && ! [[ "$ROLE_OR_AGENT" =~ $KNOWN_AGENTS_RE ]] && ! [[ "$ROLE_OR_AGENT" =~ ^(pr|prreviewer|pr-review|pr-worker|pr-review-worker|reviewer|issue|issueresolver|issue-fix|issue-worker|issue-fix-worker|hotfix|fix)(-worker)?(-[0-9]+)?$ ]]; then
    echo "❌ Vai trò '$ROLE_OR_AGENT' hiện đang tắt (disabled) trong orca-team.json. Hãy bật bằng: orca-team set $ROLE_OR_AGENT --enable" >&2
    exit 1
  fi
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get issue 2>/dev/null || true)
  fi
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get dev 2>/dev/null || true)
  fi
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get coord 2>/dev/null || true)
  fi
  [[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]] && CONFIG_AGENT=""
fi
if [[ "$ROLE_OR_AGENT" =~ $KNOWN_AGENTS_RE ]]; then
  AGENT="$ROLE_OR_AGENT"
else
  AGENT="${CONFIG_AGENT:-antigravity}"
fi
WORKTREE_NAME="hotfix-issue-$ISSUE_NUM"

GOAL_ENABLED="true"
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  GOAL_ROLE_QUERY="issue"
  [[ "$ROLE_OR_AGENT" =~ $KNOWN_ROLES_RE ]] && GOAL_ROLE_QUERY="$ROLE_OR_AGENT"
  GOAL_CFG=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$GOAL_ROLE_QUERY" --field goal 2>/dev/null || true)
  [[ "$GOAL_CFG" == "false" ]] && GOAL_ENABLED="false"
fi
ISSUE_PROMPT="[ROLE: PRINCIPAL ISSUE RESOLVER & BUGFIX ENGINEER] Nhiệm vụ: Xử lý triệt để GitHub Issue #${ISSUE_NUM}. Chuẩn mực bắt buộc: (0) Đồng bộ code mới nhất (git merge --no-edit main nếu worktree tái sử dụng từ lần sửa trước), (1) Đọc kỹ mô tả Issue và tìm đúng nguyên nhân gốc rễ (Root Cause Analysis), (2) BẮT BUỘC viết Reproduction Test tái hiện đúng lỗi (chạy test xác nhận FAIL trước khi sửa), (3) Sửa mã nguồn tối thiểu, chuẩn kiến trúc cho đến khi Reproduction Test chuyển sang PASS, (4) Chạy toàn bộ regression test suite đảm bảo EXIT_CODE=0 và không làm vỡ tính năng cũ. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ để lách lỗi; CẤM refactor lan man ngoài phạm vi Issue. BẰNG CHỨNG BẮT BUỘC: Commit sạch với thông điệp 'fix(#${ISSUE_NUM}): <mô tả>' và xuất đầy đủ Commit Hash + log chạy test."

# Kiểm tra xem worktree định danh hotfix-issue-<ISSUE_NUM> đã tồn tại trong repo chưa (tránh đẻ worktree trùng lặp -2, -3)
git worktree prune >/dev/null 2>&1 || true
EXISTING_WT_PATH=$(git worktree list --porcelain 2>/dev/null | awk -v target="$WORKTREE_NAME" '
  /^worktree / { wt = substr($0, 10) }
  /^branch /   { br = substr($0, 8) }
  /^$/ {
    n = length(target)
    if ((length(wt) > n && substr(wt, length(wt) - n) == ("/" target)) || br == ("refs/heads/" target)) { print wt; wt = ""; br = ""; exit }
    wt = ""; br = ""
  }
  END {
    n = length(target)
    if (wt != "" && ((length(wt) > n && substr(wt, length(wt) - n) == ("/" target)) || br == ("refs/heads/" target))) { print wt }
  }
')

if [[ -n "$EXISTING_WT_PATH" && -d "$EXISTING_WT_PATH" ]]; then
  echo "♻️  [Orca Hotfix] Tái sử dụng worktree định danh hiện có: $WORKTREE_NAME ($EXISTING_WT_PATH)..."
  DISPATCH_SCRIPT="${0:A:h}/orca-dispatch-task.sh"
  [[ ! -x "$DISPATCH_SCRIPT" ]] && DISPATCH_SCRIPT="$(command -v orca-dispatch 2>/dev/null || true)"
  if [[ -n "$DISPATCH_SCRIPT" && -x "$DISPATCH_SCRIPT" ]]; then
    exec "$DISPATCH_SCRIPT" "$WORKTREE_NAME" "${2:-issue}" "$ISSUE_PROMPT"
  else
    echo "❌ Không tìm thấy 'orca-dispatch-task.sh' (hoặc 'orca-dispatch') để tái sử dụng worktree '$WORKTREE_NAME'." >&2
    exit 1
  fi
fi

if [[ "$GOAL_ENABLED" != "false" ]]; then
  ISSUE_PROMPT="${ISSUE_PROMPT} CHẾ ĐỘ /goal (LÀM TRIỆT ĐỂ 100% DELIVERABLES): Tự lập checklist đối chiếu từng yêu cầu với bằng chứng thực tế (file diff, build/test EXIT_CODE=0, runtime log); khi chạy Jest bắt buộc kèm --forceExit chống treo open handles; khi lệnh chạy ngầm (task-XX) CẤM gọi manage_task(status) lặp lại mỗi vài giây gây phình turns; CẤM dừng nửa chừng hoặc bỏ sót bất kỳ hạng mục nào; nếu chạy trong phiên Orchestration có Preamble thì nối gộp 'orca orchestration send ... && orca orchestration worker-done ...' trong cùng 1 lệnh shell, sau khi hoàn tất 100% mới xuất thẻ <!-- GOAL_COMPLETE --> ở câu trả lời cuối cùng để đóng Goal."
fi

echo "🐋 [Orca Hotfix] Đang khởi tạo worktree song song cho Issue #$ISSUE_NUM bằng agent: $AGENT..."

SETUP_POLICY="skip"
[[ -f "$REPO_ROOT/package.json" ]] && SETUP_POLICY="run"

RECEIPT=$("$ORCA_BIN" worktree create \
  --repo "path:$REPO_ROOT" \
  --name "$WORKTREE_NAME" \
  --issue "$ISSUE_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --prompt "$ISSUE_PROMPT" \
  --setup "$SETUP_POLICY" \
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
