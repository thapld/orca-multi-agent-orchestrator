#!/bin/zsh
# orca-dispatch-task.sh: Script hỗ trợ giao nhanh task cho worker trong child worktree
# Sử dụng: ./orca-dispatch-task.sh <task-title> <agent> "<spec-description>"

set -e

if [[ "$1" == "-h" || "$1" == "--help" || "$1" == "help" || $# -lt 2 ]]; then
  echo "🐋 [Orca Dispatch] Điều phối và giao việc cho worker trong child worktree độc lập"
  echo "Sử dụng:"
  echo "  orca-dispatch <task-title> [role|agent] \"<spec-description>\""
  echo "  orca-dispatch <task-title> \"<spec-description>\" (mặc định giao cho role developer)"
  echo ""
  echo "Ví dụ:"
  echo "  orca-dispatch \"feature-auth\" dev \"Thêm API login JWT vào src/auth.ts\""
  echo "  orca-dispatch \"test-auth\" qa \"Viết test suite kiểm thử auth API\""
  echo "  orca-dispatch \"audit-sec\" security \"Kiểm tra lỗ hổng OWASP và leak token\""
  exit 0
fi

TASK_TITLE="$1"
if [[ $# -eq 2 ]]; then
  SPEC="$2"
  REQ_AGENT="dev"
else
  REQ_AGENT="$2"
  SPEC="$3"
fi

# Tự động lấy agent của dev/qa/sec từ orca-team nếu truyền role name
ROLE_QUERY="${REQ_AGENT:-dev}"
CONFIG_AGENT=""
if command -v orca-team &>/dev/null; then
  CONFIG_AGENT=$(orca-team get "$ROLE_QUERY" 2>/dev/null || true)
fi

AGENT="${CONFIG_AGENT:-${REQ_AGENT:-antigravity}}"
if [[ "$REQ_AGENT" =~ ^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot)$ ]]; then
  AGENT="$REQ_AGENT"
fi

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
TIMEOUT_MS="${TIMEOUT_MS:-300000}"

# Lấy model và reasoning effort nếu đã được cấu hình trong orca-team
MODEL_ARGS=()
if command -v orca-team &>/dev/null; then
  DEV_MODEL=$(orca-team get "$ROLE_QUERY" --field model 2>/dev/null || true)
  DEV_EFFORT=$(orca-team get "$ROLE_QUERY" --field effort 2>/dev/null || true)
  if [[ -n "$DEV_MODEL" && "$DEV_MODEL" != "null" && "$DEV_MODEL" != "(default)" ]]; then
    MODEL_ARGS+=(--model "$DEV_MODEL")
    if [[ -n "$DEV_EFFORT" && "$DEV_EFFORT" != "null" && "$DEV_EFFORT" != "-" ]]; then
      MODEL_ARGS+=(--effort "$DEV_EFFORT")
    fi
  fi
fi

echo "🐋 [Orca Dispatch] Đang khởi tạo task: '$TASK_TITLE' cho agent: $AGENT ${MODEL_ARGS[*]}..."

# Tự động tiêm Hiến pháp Vai trò Cấp Principal (Chống vượt rào & Chống báo sạch giả) nếu SPEC chưa có
if [[ "$SPEC" != "[ROLE:"* ]]; then
  case "${ROLE_QUERY:l}" in
    devops|infra|sre|ops)
      SPEC="[ROLE: PRINCIPAL DEVOPS & SRE] Nhiệm vụ: ${SPEC}. Chuẩn mực: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO: Chỉ sửa tầng Infra/Docker/K8s/DB/CI; CẤM tự ý sửa business logic của Dev (nếu container crash do lỗi code Dev, phải xuất container log và báo failed/escalation). BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: (1) Nếu build/deploy code mới của Dev, phải git merge --no-edit <nhánh-dev> trước để tránh build nhầm code cũ, (2) CẤM chỉ sửa file YAML/Dockerfile rồi báo xong — phải chạy build/rollout thật, xác nhận pod Running (1/1 Ready, 0 Restarts, không CrashLoopBackOff), log sạch lỗi và git commit."
      ;;
    sec|security|bug|auditor)
      SPEC="[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: ${SPEC}. Chuẩn mực: Kiểm toán độc lập (Red-Team) từng dòng git diff theo OWASP Top 10 (SQLi, Injection, Broken Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự ý sửa code tính năng của Dev. BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: Diff đúng commit mới của Dev; CẤM phán chung chung ('Code an toàn') — phải liệt kê rõ file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets] và chốt rõ SECURITY_VERDICT: PASS hoặc BLOCK."
      ;;
    qa|test|tester|sdet)
      SPEC="[ROLE: PRINCIPAL QA & SDET] Nhiệm vụ: ${SPEC}. Chuẩn mực: Tư duy bẻ gãy hệ thống (Adversarial Testing) — phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB) và 100% Acceptance Criteria. CẤM VƯỢT RÀO: Chỉ viết/chạy test và nghiệm thu; CẤM TUYỆT ĐỐI tự sửa code business logic của Dev để ép test xanh; CẤM xóa assert hoặc mock giả luôn trả về true. BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: (1) Phải git merge --no-edit <nhánh-dev> để lấy code mới trước khi test (chống test nhầm nhánh cũ!), (2) CẤM duyệt bằng mắt — phải chạy lệnh test thật đạt EXIT_CODE=0, Tests Run > 0, Failed = 0, (3) Xuất log thật và chốt QA_VERDICT: PASS hoặc FAIL."
      ;;
    ui|ux|frontend|design)
      SPEC="[ROLE: PRINCIPAL UI/UX ARCHITECT] Nhiệm vụ: ${SPEC}. Chuẩn mực: Tinh chỉnh chuẩn xác CSS/DOM qua Orca Design Mode, đảm bảo Responsive (Mobile/Tablet/Desktop), Accessibility (a11y), Loading/Empty/Error states. CẤM VƯỢT RÀO: Chỉ sửa tầng UI/Frontend; CẤM sửa backend core/DB schema. BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: Build/typecheck EXIT_CODE=0, không lỗi Console, kiểm tra snapshot/screenshot thực tế và git commit kèm Commit Hash."
      ;;
    *)
      SPEC="[ROLE: PRINCIPAL FULL-STACK DEV] Nhiệm vụ: ${SPEC}. Chuẩn mực: Clean Architecture, type-safe 100%, xử lý triệt để edge cases (null, timeout, thread-safety/concurrency, backward-compat), không để TODO/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code ứng dụng & unit test của mình; CẤM sửa K8s/Docker/CI của DevOps; CẤM xóa/skip/comment-out (@Ignore, .skip) test cũ đang fail để lách lỗi. BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: (1) Chạy compile/build & unit test thực tế đạt EXIT_CODE=0, (2) git add & git commit sạch sẽ vào nhánh worktree (git status rỗng), (3) Báo rõ Branch, Commit Hash và log test."
      ;;
  esac
fi

# 0. Đảm bảo có Run đang bound trong Orca
CHECK_RUN=$($ORCA_BIN orchestration task-list --json 2>&1 || true)
if echo "$CHECK_RUN" | grep -q "No Run is bound"; then
  echo "ℹ️  Chưa có Run orchestration active. Đang tự động tạo Run..."
  $ORCA_BIN orchestration run-create --objective "Orchestration Run: $TASK_TITLE" --json >/dev/null 2>&1 || true
fi

# 1. Tạo task trong Orca
TASK_JSON=$($ORCA_BIN orchestration task-create \
  --task-title "$TASK_TITLE" \
  --spec "$SPEC" \
  --json 2>&1) || true

TASK_ID=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') is False:
        sys.exit(1)
    res = d.get('result', d)
    tid = res.get('id') or (res.get('task', {}).get('id') if isinstance(res.get('task'), dict) else '')
    print(tid or '')
except Exception:
    sys.exit(1)
" <<< "$TASK_JSON" 2>/dev/null || echo "$TASK_JSON" | grep -o '"id": *"[^"]*"' | tail -n 1 | cut -d'"' -f4)

if [[ -z "$TASK_ID" ]]; then
  echo "❌ Không thể tạo task trong Orca. Kết quả: $TASK_JSON"
  exit 1
fi

echo "✅ Đã tạo task ID: $TASK_ID"

# 2. Khởi chạy worker
# Kiểm tra nếu git repo chưa có commit thì fallback dùng worktree current
HAS_GIT_COMMIT=true
if ! git rev-parse HEAD &>/dev/null; then
  HAS_GIT_COMMIT=false
fi

if [[ "$HAS_GIT_COMMIT" == "true" ]]; then
  SAFE_SLUG=$(echo "$TASK_TITLE" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-')
  [[ -z "$SAFE_SLUG" ]] && SAFE_SLUG="task"
  SHORT_ID=$(echo "$TASK_ID" | tail -c 7)
  WORKTREE_NAME="wt-${SAFE_SLUG}-${SHORT_ID}"

  echo "🚀 Đang khởi chạy worker trong worktree: $WORKTREE_NAME..."

  START_RECEIPT=$($ORCA_BIN orchestration worker-start \
    --task "$TASK_ID" \
    --worktree new-child \
    --name "$WORKTREE_NAME" \
    --agent "$AGENT" \
    "${MODEL_ARGS[@]}" \
    --setup run \
    --timeout-ms "$TIMEOUT_MS" \
    --json 2>&1) || true
else
  echo "⚠️  Git repository chưa có commit đầu tiên. Đang khởi chạy worker trên workspace hiện tại..."
  START_RECEIPT=$($ORCA_BIN orchestration worker-start \
    --task "$TASK_ID" \
    --worktree current \
    --agent "$AGENT" \
    "${MODEL_ARGS[@]}" \
    --timeout-ms "$TIMEOUT_MS" \
    --json 2>&1) || true
fi

DISPATCH_ID=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') is False:
        sys.exit(1)
    res = d.get('result', d)
    did = res.get('dispatchId') or res.get('id') or ''
    print(did)
except Exception:
    sys.exit(1)
" <<< "$START_RECEIPT" 2>/dev/null || echo "$START_RECEIPT" | grep -o '"dispatchId": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

TERM_HANDLE=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    res = d.get('result', d)
    print(res.get('agentTerminalHandle') or res.get('terminalHandle') or '')
except Exception:
    print('')
" <<< "$START_RECEIPT" 2>/dev/null || echo "$START_RECEIPT" | grep -o '"agentTerminalHandle": *"[^"]*"' | head -n 1 | cut -d'"' -f4)

if [[ -z "$DISPATCH_ID" ]]; then
  echo "❌ Khởi chạy worker thất bại: $START_RECEIPT"
  exit 1
fi

echo "🎉 Worker đã được khởi chạy thành công!"
echo "   • Worktree: $WORKTREE_NAME"
echo "   • Dispatch: $DISPATCH_ID"
[[ -n "$TERM_HANDLE" ]] && echo "   • Terminal: $TERM_HANDLE"
