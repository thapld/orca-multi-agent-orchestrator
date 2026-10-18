#!/bin/zsh
# orca-pr-review.sh: Script hỗ trợ mở nhanh worktree thẩm định GitHub PR độc lập
# Sử dụng: ./orca-pr-review.sh <pr-number> [agent]

set -e

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
  echo "🐋 [Orca PR Review] Mở hoặc tái sử dụng worktree thẩm định GitHub Pull Request độc lập"
  echo "Sử dụng:"
  echo "  orca-review-pr <pr-number> [role|agent]"
  echo ""
  echo "Ví dụ:"
  echo "  orca-review-pr 123"
  echo "  orca-review-pr 123 claude"
  exit 0
fi

if [[ $# -lt 1 ]]; then
  echo "❌ Thiếu số PR. Sử dụng: orca-review-pr <pr-number> [role|agent]" >&2
  exit 1
fi
if [[ $# -gt 2 ]]; then
  echo "❌ Thừa tham số ($# tham số). Sử dụng: orca-review-pr <pr-number> [role|agent]" >&2
  exit 1
fi

PR_NUM="$1"
if ! [[ "$PR_NUM" =~ ^[1-9][0-9]*$ ]]; then
  echo "❌ Số PR phải là số nguyên dương (ví dụ: 123). Giá trị nhận được: '$PR_NUM'" >&2
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
ROLE_OR_AGENT="${2:-pr}"
ROLE_OR_AGENT="${ROLE_OR_AGENT:l}"
KNOWN_AGENTS_RE='^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot|gemini|muse|codebuddy|omp)$'
KNOWN_ROLES_RE='^((coord|coordinator|leader|lead|architect|orchestrator|dev|developer|developers|coder|fullstack|backend|doc|docs|fix|devops|devopsspecialist|infra|sre|ops|sec|security|bughunter|bug|auditor|qa|test|tester|sdet|ui|ux|frontend|uispecialist|design|bench|benchmark|perf|performance|benchspecialist|re|reverse|rea|respecialist|critic|research|researcher|challenger|advocate|criticspecialist|pr|prreviewer|pr-review|reviewer|issue|issueresolver|issue-fix|hotfix)(-worker)?(-[0-9]+)?)$'
if ! [[ "$ROLE_OR_AGENT" =~ $KNOWN_AGENTS_RE || "$ROLE_OR_AGENT" =~ $KNOWN_ROLES_RE ]]; then
  echo "❌ Role hoặc agent không hợp lệ: '${2:-pr}'. Hãy dùng role hợp lệ hoặc tên agent CLI." >&2
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
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get pr 2>/dev/null || true)
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
WORKTREE_NAME="review-pr-$PR_NUM"

GOAL_ENABLED="false"
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  GOAL_ROLE_QUERY="pr"
  [[ "$ROLE_OR_AGENT" =~ $KNOWN_ROLES_RE ]] && GOAL_ROLE_QUERY="$ROLE_OR_AGENT"
  GOAL_CFG=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$GOAL_ROLE_QUERY" --field goal 2>/dev/null || true)
  [[ "$GOAL_CFG" == "true" ]] && GOAL_ENABLED="true"
fi
PR_PROMPT="[ROLE: PRINCIPAL PR REVIEWER] Nhiệm vụ: Thẩm định toàn diện Pull Request #${PR_NUM}. Chuẩn mực: (0) Đồng bộ đúng commit mới nhất của PR trước khi review (worktree có thể đã cũ từ lần review trước): chạy 'gh pr checkout ${PR_NUM} --force' hoặc 'git fetch origin pull/${PR_NUM}/head && git checkout --detach FETCH_HEAD' và ghi rõ commit hash đã review, (1) Đọc toàn bộ line-by-line diff qua 'orca file open-changed --mode diff --worktree active' và 'git diff', (2) Kiểm tra kiến trúc, Clean Code, OWASP Top 10, Race Conditions, Memory/Resource Leaks, (3) BẮT BUỘC chạy thực tế test suite và linter trong worktree PR (CẤM duyệt suông bằng mắt!). CẤM VƯỢT RÀO: Không tự ý sửa code tính năng của PR khi chưa được yêu cầu. BẰNG CHỨNG BẮT BUỘC: Xuất báo cáo review chi tiết từng file:line, kết quả chạy test thực tế (EXIT_CODE, số test pass/fail) và chốt PR_VERDICT: APPROVE hoặc REQUEST_CHANGES."

# Kiểm tra xem worktree định danh review-pr-<PR_NUM> đã tồn tại trong repo chưa (tránh đẻ worktree trùng lặp -2, -3 kể cả khi đang ở detached HEAD hoặc nhánh PR)
git worktree prune >/dev/null 2>&1 || true
EXISTING_WT_PATH=$(git worktree list --porcelain 2>/dev/null | awk -v target="$WORKTREE_NAME" '
  /^worktree / { wt = substr($0, 10) }
  /^branch /   { br = substr($0, 8) }
  /^$/ {
    n = length(target)
    if ((length(wt) > n && (substr(wt, length(wt) - n) == ("/" target) || substr(wt, length(wt) - n) == ("." target))) || br == ("refs/heads/" target)) { print wt; wt = ""; br = ""; exit }
    wt = ""; br = ""
  }
  END {
    n = length(target)
    if (wt != "" && ((length(wt) > n && (substr(wt, length(wt) - n) == ("/" target) || substr(wt, length(wt) - n) == ("." target))) || br == ("refs/heads/" target))) { print wt }
  }
')

if [[ -n "$EXISTING_WT_PATH" && -d "$EXISTING_WT_PATH" ]]; then
  echo "♻️  [Orca PR Review] Tái sử dụng worktree định danh hiện có: $WORKTREE_NAME ($EXISTING_WT_PATH)..."
  DISPATCH_SCRIPT="${0:A:h}/orca-dispatch-task.sh"
  [[ ! -x "$DISPATCH_SCRIPT" ]] && DISPATCH_SCRIPT="$(command -v orca-dispatch 2>/dev/null || true)"
  if [[ -n "$DISPATCH_SCRIPT" && -x "$DISPATCH_SCRIPT" ]]; then
    exec "$DISPATCH_SCRIPT" "$WORKTREE_NAME" "${2:-pr}" "$PR_PROMPT"
  else
    echo "❌ Không tìm thấy 'orca-dispatch-task.sh' (hoặc 'orca-dispatch') để tái sử dụng worktree '$WORKTREE_NAME'." >&2
    exit 1
  fi
fi

if [[ "$GOAL_ENABLED" != "false" ]]; then
  PR_PROMPT="${PR_PROMPT} CHẾ ĐỘ /goal (LÀM TRIỆT ĐỂ 100% DELIVERABLES): Tự lập checklist đối chiếu từng yêu cầu với bằng chứng thực tế (file diff, build/test EXIT_CODE=0, runtime log); khi chạy Jest bắt buộc kèm --forceExit chống treo open handles; khi lệnh chạy ngầm (task-XX) CẤM gọi manage_task(status) lặp lại mỗi vài giây gây phình turns; CẤM dừng nửa chừng hoặc bỏ sót bất kỳ hạng mục nào; nếu chạy trong phiên Orchestration có Preamble thì nối gộp 'orca orchestration send ... --type handoff ... && orca orchestration send ... --type worker_done --outcome succeeded ...' trong cùng 1 lệnh shell, sau khi hoàn tất 100% mới xuất thẻ <!-- GOAL_COMPLETE --> ở câu trả lời cuối cùng để đóng Goal."
fi

echo "🐋 [Orca PR Review] Đang khởi tạo worktree thẩm định PR #$PR_NUM bằng agent: $AGENT..."

SETUP_POLICY="skip"
[[ -f "$REPO_ROOT/package.json" ]] && SETUP_POLICY="run"

RECEIPT=$("$ORCA_BIN" worktree create \
  --repo "path:$REPO_ROOT" \
  --name "$WORKTREE_NAME" \
  --pr "$PR_NUM" \
  --no-parent \
  --agent "$AGENT" \
  --prompt "$PR_PROMPT" \
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
  echo "❌ Khởi tạo worktree cho PR #$PR_NUM thất bại: $RECEIPT"
  exit 1
fi

echo "✅ Đã tạo thành công worktree review PR: $WT_NAME"
echo "👉 Mã nguồn PR #$PR_NUM đã được checkout tự động kèm chỉ dẫn Principal PR Reviewer."
