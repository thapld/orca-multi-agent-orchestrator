#!/bin/zsh
# orca-dispatch-task.sh: Script hỗ trợ giao nhanh task cho worker trong child worktree
# Sử dụng: ./orca-dispatch-task.sh <task-title> <agent> "<spec-description>"

set -e

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "${1:-}" == "help" ]]; then
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

if [[ $# -lt 2 ]]; then
  echo "❌ Thiếu tham số. Sử dụng: orca-dispatch <task-title> [role|agent] \"<spec-description>\"" >&2
  exit 1
fi
if [[ $# -gt 3 ]]; then
  echo "❌ Thừa tham số ($# tham số). Hãy bọc mô tả nhiệm vụ (<spec-description>) trong dấu ngoặc kép \"...\"." >&2
  exit 1
fi

KNOWN_AGENTS_RE='^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot|gemini|muse|codebuddy|omp)$'
KNOWN_ROLES_RE='^((coord|coordinator|leader|lead|architect|orchestrator|dev|developer|developers|coder|fullstack|backend|doc|docs|fix|devops|devopsspecialist|infra|sre|ops|sec|security|bughunter|bug|auditor|qa|test|tester|sdet|ui|ux|frontend|uispecialist|design|bench|benchmark|perf|performance|benchspecialist|re|reverse|rea|respecialist|critic|research|researcher|challenger|advocate|criticspecialist|pr|prreviewer|pr-review|reviewer|issue|issueresolver|issue-fix|hotfix)(-worker)?(-[0-9]+)?)$'

TASK_TITLE="$1"
if [[ $# -eq 2 ]]; then
  if [[ "${2:l}" =~ $KNOWN_ROLES_RE || "${2:l}" =~ $KNOWN_AGENTS_RE ]]; then
    echo "❌ Thiếu mô tả nhiệm vụ (<spec-description>) cho role/agent '$2'. Sử dụng: orca-dispatch \"$TASK_TITLE\" $2 \"<spec-description>\"" >&2
    exit 1
  fi
  SPEC="$2"
  if [[ "${TASK_TITLE:l}" =~ ^review-pr-[0-9]+$ ]]; then
    REQ_AGENT="pr"
  elif [[ "${TASK_TITLE:l}" =~ ^hotfix-issue-[0-9]+$ ]]; then
    REQ_AGENT="issue"
  elif [[ "${TASK_TITLE:l}" =~ ^(dev|devops|qa|sec|bench|re|ui|critic|pr|issue)-worker(-[0-9]+)?$ ]]; then
    REQ_AGENT="${TASK_TITLE:l}"
  else
    REQ_AGENT="dev"
  fi
else
  REQ_AGENT="${2:l}"
  SPEC="$3"
fi

if [[ -z "${TASK_TITLE//[[:space:]]/}" ]]; then
  echo "❌ Tên task (<task-title>) không được để trống." >&2
  exit 1
fi
if [[ -z "${SPEC//[[:space:]]/}" ]]; then
  echo "❌ Mô tả nhiệm vụ (<spec-description>) không được để trống." >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "❌ Thư mục hiện tại không nằm trong Git repository." >&2
  exit 1
fi

GIT_COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [[ -n "$GIT_COMMON_DIR" && "${GIT_COMMON_DIR:t}" == ".git" ]]; then
  REPO_ROOT="${GIT_COMMON_DIR:h}"
else
  REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
fi

# Tự động lấy agent của dev/qa/sec/devops/bench/re/critic từ orca-team
ORCA_TEAM_BIN="$(command -v orca-team 2>/dev/null || echo "${0:A:h}/orca-team")"

if [[ "$REQ_AGENT" =~ $KNOWN_AGENTS_RE ]]; then
  if [[ "${TASK_TITLE:l}" =~ ^review-pr-[0-9]+(-[0-9]+)?$ ]]; then
    ROLE_QUERY="pr"
  elif [[ "${TASK_TITLE:l}" =~ ^hotfix-issue-[0-9]+(-[0-9]+)?$ ]]; then
    ROLE_QUERY="issue"
  elif [[ "${TASK_TITLE:l}" =~ ^(dev|devops|qa|sec|bench|re|ui|critic|pr|issue)-worker(-[0-9]+)?$ ]]; then
    ROLE_QUERY="${TASK_TITLE:l}"
  else
    ROLE_QUERY="dev"
  fi
elif [[ "$REQ_AGENT" =~ $KNOWN_ROLES_RE ]]; then
  ROLE_QUERY="$REQ_AGENT"
else
  echo "❌ Role hoặc agent không hợp lệ: '$2'. Hãy dùng role hợp lệ (dev, devops, critic, sec, qa, bench, re, ui, pr, issue) hoặc tên agent CLI." >&2
  exit 1
fi
EFF_ROLE_QUERY="$ROLE_QUERY"
CONFIG_AGENT=""
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$ROLE_QUERY" 2>/dev/null || true)
  if [[ "$CONFIG_AGENT" == "none" ]] && ! [[ "$REQ_AGENT" =~ $KNOWN_AGENTS_RE ]] && ! [[ "$ROLE_QUERY" =~ ^(pr|prreviewer|pr-review|pr-worker|pr-review-worker|reviewer|issue|issueresolver|issue-fix|issue-worker|issue-fix-worker|hotfix|fix)(-worker)?(-[0-9]+)?$ ]]; then
    echo "❌ Vai trò '$ROLE_QUERY' hiện đang tắt (disabled) trong orca-team.json. Hãy bật bằng: orca-team set $ROLE_QUERY --enable" >&2
    exit 1
  fi
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    EFF_ROLE_QUERY="dev"
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get dev 2>/dev/null || true)
  fi
  if [[ -z "$CONFIG_AGENT" || "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    EFF_ROLE_QUERY="coord"
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get coord 2>/dev/null || true)
  fi
fi
[[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]] && CONFIG_AGENT=""

if [[ "$REQ_AGENT" =~ $KNOWN_AGENTS_RE ]]; then
  AGENT="$REQ_AGENT"
else
  AGENT="${CONFIG_AGENT:-antigravity}"
fi

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
TIMEOUT_MS="${TIMEOUT_MS:-300000}"

# Lấy model và reasoning effort nếu đã được cấu hình trong orca-team (chỉ áp dụng khi AGENT khớp với CONFIG_AGENT của role)
MODEL_ARGS=()
if [[ -x "$ORCA_TEAM_BIN" && "$AGENT" == "$CONFIG_AGENT" ]]; then
  DEV_MODEL=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$EFF_ROLE_QUERY" --field model 2>/dev/null || true)
  DEV_EFFORT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$EFF_ROLE_QUERY" --field effort 2>/dev/null || true)
  if [[ "$AGENT" =~ ^(antigravity|claude|codex|cursor|muse|codebuddy|omp)$ ]] && [[ -n "$DEV_MODEL" && "$DEV_MODEL" != "null" && "$DEV_MODEL" != "(default)" ]]; then
    MODEL_ARGS+=(--model "$DEV_MODEL")
    if [[ -n "$DEV_EFFORT" && "$DEV_EFFORT" != "null" && "$DEV_EFFORT" != "none" && "$DEV_EFFORT" != "None" && "$DEV_EFFORT" != "-" && "$AGENT" != "omp" ]]; then
      EFF_NORM="${DEV_EFFORT:l}"
      ALLOW_EFFORT=false
      if [[ "$AGENT" == "claude" ]]; then
        [[ "${DEV_MODEL:l}" != *haiku* && "$EFF_NORM" =~ ^(low|medium|high|xhigh|max)$ ]] && ALLOW_EFFORT=true
      elif [[ "$AGENT" == "cursor" ]]; then
        [[ ( "$DEV_MODEL" == "gpt-5.3-codex" || "$DEV_MODEL" == "claude-opus-4-8" ) && "$EFF_NORM" =~ ^(low|medium|high)$ ]] && ALLOW_EFFORT=true
      elif [[ "$AGENT" == "antigravity" || "$AGENT" == "codebuddy" ]]; then
        [[ "$EFF_NORM" =~ ^(xhigh|max|ultra)$ ]] && EFF_NORM="high"
        [[ "$EFF_NORM" == "minimal" ]] && EFF_NORM="low"
        [[ "$EFF_NORM" =~ ^(low|medium|high)$ ]] && ALLOW_EFFORT=true
      elif [[ "$AGENT" == "codex" || "$AGENT" == "muse" ]]; then
        [[ "$EFF_NORM" =~ ^(minimal|low|medium|high|xhigh|max|ultra)$ ]] && ALLOW_EFFORT=true
      fi
      if [[ "$ALLOW_EFFORT" == true ]]; then
        MODEL_ARGS+=(--effort "$EFF_NORM")
      fi
    fi
  fi
fi

echo "🐋 [Orca Dispatch] Đang khởi tạo task: '$TASK_TITLE' cho agent: $AGENT ${MODEL_ARGS[*]}..."

# 0. Đảm bảo có Run đang bound trong Orca trước khi truy vấn worker-list / tạo task
CHECK_RUN=$("$ORCA_BIN" orchestration task-list --brief --json 2>&1 || true)
if [[ "$CHECK_RUN" == *"No Run is bound"* || "$CHECK_RUN" == *"no_active_sender_terminal"* || "$CHECK_RUN" == *"no_bound_run"* ]]; then
  echo "ℹ️  Chưa có Run orchestration active. Đang tự động tạo Run..."
  if ! "$ORCA_BIN" orchestration run-create --objective "Orchestration Run: $TASK_TITLE" >/dev/null 2>&1; then
    echo "❌ Không thể tạo Run trong Orca." >&2
    exit 1
  fi
fi

# Xác định định danh worktree cố định theo từng vai trò & số lượng slot song song (Concurrency)
ROLE_WORKTREE="dev-worker"
EXPLICIT_SLOT=false
ROLE_BASE_KEY="${ROLE_QUERY:l}"
ROLE_EXPLICIT_NUM=""
if [[ "$ROLE_BASE_KEY" =~ ^([a-z0-9_-]+?)(-worker)?-([0-9]+)$ ]]; then
  ROLE_BASE_KEY="${match[1]}"
  ROLE_EXPLICIT_NUM="${match[3]}"
elif [[ "$ROLE_BASE_KEY" =~ ^([a-z0-9_-]+?)-worker$ ]]; then
  ROLE_BASE_KEY="${match[1]}"
fi
if [[ "${TASK_TITLE:l}" =~ ^(review-pr-[0-9]+(-[0-9]+)?|hotfix-issue-[0-9]+(-[0-9]+)?|(dev|devops|qa|sec|bench|re|ui|critic|pr|issue)-worker(-[0-9]+)?)$ ]]; then
  ROLE_WORKTREE="${TASK_TITLE:l}"
  EXPLICIT_SLOT=true
else
  case "$ROLE_BASE_KEY" in
    devops|devopsspecialist|infra|sre|ops) ROLE_WORKTREE="devops-worker" ;;
    sec|security|bughunter|bug|auditor) ROLE_WORKTREE="sec-worker" ;;
    qa|test|tester|sdet) ROLE_WORKTREE="qa-worker" ;;
    ui|ux|frontend|uispecialist|design) ROLE_WORKTREE="ui-worker" ;;
    bench|benchmark|perf|performance|benchspecialist) ROLE_WORKTREE="bench-worker" ;;
    re|reverse|rea|respecialist) ROLE_WORKTREE="re-worker" ;;
    critic|research|researcher|challenger|advocate|criticspecialist) ROLE_WORKTREE="critic-worker" ;;
    pr|prreviewer|pr-review|reviewer) ROLE_WORKTREE="pr-worker" ;;
    issue|issueresolver|issue-fix|hotfix|fix) ROLE_WORKTREE="issue-worker" ;;
    *) ROLE_WORKTREE="dev-worker" ;;
  esac
  if [[ -n "$ROLE_EXPLICIT_NUM" ]]; then
    EXPLICIT_SLOT=true
    if (( ROLE_EXPLICIT_NUM >= 2 )); then
      ROLE_WORKTREE="${ROLE_WORKTREE}-${ROLE_EXPLICIT_NUM}"
    fi
  fi
fi

# Tự động chọn Worktree Slot đang rảnh (<role>-worker) hoặc tự đẻ slot mới (<role>-worker-2, <role>-worker-3...) nếu bận
ROLE_GOAL_MODE="false"
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  RAW_GOAL=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$ROLE_QUERY" --field goal 2>/dev/null || true)
  [[ "$RAW_GOAL" == "true" ]] && ROLE_GOAL_MODE="true"
  SLOT_QUERY="$ROLE_QUERY"
  [[ "$EXPLICIT_SLOT" == true ]] && SLOT_QUERY="$ROLE_WORKTREE"
  CHOSEN_SLOT=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" worktree-arg "$SLOT_QUERY" --slot-only 2>/dev/null || true)
  [[ -n "$CHOSEN_SLOT" ]] && ROLE_WORKTREE="$CHOSEN_SLOT"
fi

GOAL_MODE_CLAUSE=" CHẾ ĐỘ /goal (LÀM TRIỆT ĐỂ 100% DELIVERABLES): Tự lập checklist đối chiếu từng yêu cầu với bằng chứng thực tế (file diff, build & test EXIT_CODE=0, runtime log); khi chạy Jest phải kèm --forceExit để chống treo open handles và khi lệnh chuyển sang background (task-XX) CẤM gọi manage_task(status) lặp liên tục 2-3s mỗi lần (hãy dừng gọi tool để hệ thống tự đánh thức khi xong hoặc dùng schedule >=20s); CẤM dừng nửa chừng hoặc bỏ sót bất kỳ hạng mục nào; chỉ sau khi đã hoàn tất 100% nhiệm vụ + gộp lệnh gửi bản tin báo cáo (--type handoff) && gửi --type worker_done về Coordinator mới được xuất thẻ <!-- GOAL_COMPLETE --> ở câu trả lời cuối cùng để đóng Goal."

# Tự động tiêm Hiến pháp Vai trò Cấp Principal (Chống vượt rào, Chống báo sạch giả & Chế độ /goal)
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  SPEC_ROLE_ARG="$ROLE_QUERY"
  [[ "$ROLE_WORKTREE" =~ -(worker-[0-9]+)$ ]] && SPEC_ROLE_ARG="$ROLE_WORKTREE"
  INJECTED_SPEC=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" spec "$SPEC_ROLE_ARG" -- "$SPEC" 2>/dev/null || true)
  if [[ -n "$INJECTED_SPEC" ]]; then
    SPEC="$INJECTED_SPEC"
  fi
fi
if [[ "$SPEC" != "[ROLE:"* ]]; then
  CLEAN_SPEC="${SPEC%.}"
  MERGE_DEVOPS_UPSTREAM="orca-team merge-upstream --role devops --task-id <task_id> --dispatch-id <dispatch_id>"
  MERGE_SEC_CMD="orca-team merge-upstream --role sec --task-id <task_id> --dispatch-id <dispatch_id>"
  MERGE_QA_CMD="orca-team merge-upstream --role qa --task-id <task_id> --dispatch-id <dispatch_id>"
  MERGE_BENCH_CMD="orca-team merge-upstream --role bench --task-id <task_id> --dispatch-id <dispatch_id>"
  case "$ROLE_BASE_KEY" in
    devops|devopsspecialist|infra|sre|ops)
      SPEC="[ROLE: PRINCIPAL DEVOPS/SRE] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO: Chỉ sửa Infra/Docker/K8s/DB/CI; CẤM sửa business logic của Dev; CẤM tự dịch ngược hay cat/strings/grep file binary (phải báo Coordinator giao re-worker); CẤM đặt backtick (\`) trong nháy kép (\"...\") của bash/python -c. RÀ SOÁT RUNTIME LOGS & BÁO CÁO (giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble; CẤM truyền --run vào check): (1) Đầu ca chạy orca orchestration check [--ack <DELIVERY_ID>] --terminal <worker_handle> --json và đồng bộ nhánh thuộc cùng Run kèm xem Plan/Peer Directory bằng ${MERGE_DEVOPS_UPSTREAM} (CẤM tự gõ vòng lặp merge dev-worker*), (2) CẤM chỉ nhìn Pod 1/1 Ready hay /health 200 lúc mới bật! Bắt buộc chờ qua chu kỳ background worker/@Scheduled/consumer và quét TOÀN BỘ Runtime Logs từ lúc start (kubectl/docker logs KHÔNG dùng --since ngắn) trên MỌI container (App + DB + Kafka/Broker + Sidecar), (3) Nếu có BẤT KỲ dòng ERROR/FATAL/PANIC/Exception/SQL/SSL/Auth 401-403/health=ERROR (hoặc vướng quá 15 turns) ➔ dừng đào bới và trình Coordinator: orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type escalation --subject 'Runtime Log Error' --body '<Chi tiết pod & log lỗi>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome failed --subject 'Worker Failed: ${ROLE_WORKTREE}' --body 'Runtime Log Error' --json; khi sạch lỗi 100%, gộp trong 1 lệnh (&&): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'Infra Ready: ${ROLE_WORKTREE}' --body '<Pod/Port/Log sạch 100%>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'Infra Ready & Runtime Logs 100% Clean' --json."
      ;;
    sec|security|bughunter|bug|auditor)
      SPEC="[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Soi git diff theo OWASP Top 10 (SQLi, Injection, Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự sửa code tính năng của Dev. GIAO TIẾP (CẤM truyền --run vào check): Đầu ca chạy ${MERGE_SEC_CMD} để đồng bộ đúng nhánh thuộc Run hiện tại và xem Pinned Plan/Peer Directory (CẤM tự gõ vòng lặp merge dev-worker*); nếu phát hiện lỗ hổng (SECURITY_VERDICT: BLOCK), trình ngay lên Coordinator (có thể gửi kèm P2P --to dispatch:<DEV_ID> --type handoff --subject 'SEC_BLOCK' nếu Dev đang live): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type escalation --subject 'Security Vulnerability Found' --body 'SEC_BLOCK: <file:line & PoC>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome failed --subject 'Worker Failed: ${ROLE_WORKTREE}' --body 'SECURITY_VERDICT: BLOCK' --json; khi an toàn 100%, liệt kê file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets] và gộp trong 1 lệnh (&&): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'Security Audit Passed: ${ROLE_WORKTREE}' --body 'SECURITY_VERDICT: PASS | Đã kiểm toán [OWASP, Concurrency, Leaks, Secrets] trên file:line...' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'SECURITY_VERDICT: PASS' --json."
      ;;
    qa|test|tester|sdet)
      SPEC="[ROLE: PRINCIPAL QA/SDET — KIỂM SOÁT CHẤT LƯỢNG & TIẾN ĐỘ 100% PLAN] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Thay mặt Leader kiểm soát chất lượng và kiểm đếm 100% tiến độ Plan ([P1..Pn]), phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB) và Acceptance Criteria. CẤM VƯỢT RÀO & CẤM CHE GIẤU LỖI: Chỉ viết/chạy test & kiểm toán Plan; CẤM sửa business logic của Dev; CẤM xóa/nới lỏng assert; CẤM dùng --since ngắn cắt log lỗi hoặc bỏ sót pod DB/Kafka! GIAO TIẾP, P2P & BÁO CÁO (giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble; CẤM truyền --run vào check): (1) Chạy orca orchestration check [--ack <DELIVERY_ID>] --terminal <worker_handle> --json (từ lần 2 bắt buộc kèm --ack <DELIVERY_ID>) và ${MERGE_QA_CMD} để đồng bộ các nhánh thuộc Run hiện tại + đọc Pinned Master Plan [P1..Pn] & Peer Directory (CẤM tự chạy vòng lặp merge dev-worker*), (2) KIỂM ĐẾM 100% PLAN-TO-CODE & TEST: Chạy git diff <base_branch>...HEAD (nhánh gốc in ra từ merge-upstream, vd main hoặc develop) đối chiếu 1-1 từng mục [P1..Pn] trong Plan với code thực tế, chạy test thật (Jest kèm --forceExit; không poll manage_task liên tục) VÀ quét TOÀN BỘ Runtime Logs (kubectl/docker logs từ lúc start trên MỌI container App + DB + Broker), (3) Nếu Dev thiếu BẤT KỲ mục nào trong Plan (INCOMPLETE_PLAN) hoặc test/Runtime Logs có lỗi ERROR/Exception/FATAL/SQL/SSL ➔ (a) Nếu dev-worker đang LIVE (P2P mode), bắn --to dispatch:<DEV_DISPATCH_ID> (hoặc --to '@all') kèm --type handoff --subject 'QA_REJECT vòng <n>/3' (P2P bắt buộc dùng --type handoff, CẤM dùng --type escalation với --to) để Dev sửa ngay (tối đa 3 vòng); (b) Nếu dev-worker đã SETTLED hoặc quá 3 vòng vẫn lỗi, trình ngay lên Coordinator: orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type escalation --subject 'QA Plan/Test Failure' --body 'INCOMPLETE_PLAN / TEST_FAIL: <Bảng mục [P1..Pn] thiếu hoặc log lỗi>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome failed --subject 'Worker Failed: ${ROLE_WORKTREE}' --body 'QA_VERDICT: FAIL' --json! (4) Chỉ khi 100% hạng mục [P1..Pn] DONE + test/log sạch 100% ➔ gộp gửi QA_FINAL và --type worker_done trong cùng 1 lệnh (&&): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'QA_FINAL: Plan 100% Verified (${ROLE_WORKTREE})' --body 'QA_VERDICT: PASS | Bảng Đối Chiếu Tiến Độ Plan: [P1..Pn] 100% DONE (kèm file:line, EXIT_CODE=0 & runtime logs sạch)' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'QA_VERDICT: PASS (100% Plan Verified)' --json."
      ;;
    ui|ux|frontend|uispecialist|design)
      SPEC="[ROLE: PRINCIPAL UI/UX] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Hoàn thiện 100% hạng mục UI/UX [P1..Pn] được giao (qa-worker sẽ đối chiếu git diff 1-1 với Plan ở Wave 2), CSS/DOM chuẩn, Responsive, a11y, Loading/Empty/Error states. CẤM VƯỢT RÀO: Chỉ sửa UI/Frontend; CẤM sửa backend/DB schema. GIAO TIẾP (CẤM truyền --run vào check): Chạy orca-team merge-upstream --role ui --task-id <task_id> --dispatch-id <dispatch_id> (đồng bộ nhánh gốc main/develop và xem Pinned Plan/Peer Directory), Build/typecheck EXIT_CODE=0, không lỗi Console, git commit vào ${ROLE_WORKTREE} và gộp trong 1 lệnh (&&): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'UI Ready: ${ROLE_WORKTREE} (<commit_hash>)' --body '<Tóm tắt hạng mục UI [P1..Pn] đã xong, Commit/Routes>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'UI Ready: ${ROLE_WORKTREE} (<commit_hash>)' --json."
      ;;
    bench|benchmark|perf|performance|benchspecialist)
      SPEC="[ROLE: PRINCIPAL BENCHMARK & PERF] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Có warm-up, dùng công cụ chuẩn (k6/wrk/JMH/pytest-benchmark/go test -bench/EXPLAIN ANALYZE), đo Before vs After. CẤM VƯỢT RÀO: Chỉ viết/chạy benchmark & phân tích bottleneck; CẤM tự sửa business logic hoặc bỏ validate để ăn gian điểm số! GIAO TIẾP (CẤM truyền --run vào check): (1) Chạy ${MERGE_BENCH_CMD} để đồng bộ đúng các nhánh thuộc Run này trước khi đo, (2) Xuất bảng số liệu: Concurrency, Throughput (RPS/EPS), Latency (p50/p95/p99), Error Rate (0%), CPU/RAM; nếu nghẽn cổ chai: orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type escalation --subject 'Perf Bottleneck Found' --body '<file:line/query gây nghẽn>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome failed --subject 'Worker Failed: ${ROLE_WORKTREE}' --body 'PERF_VERDICT: BOTTLENECK_FOUND' --json; khi đạt SLA: orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'Benchmark Passed: ${ROLE_WORKTREE}' --body 'PERF_VERDICT: PASS | <Bảng RPS/EPS, p50/p95/p99, Error Rate 0%>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'PERF_VERDICT: PASS' --json."
      ;;
    re|reverse|rea|respecialist)
      SPEC="[ROLE: PRINCIPAL REVERSE ENGINEER (REA)] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Kích hoạt đúng bộ 3 skill dịch ngược: (i) reverse-engineer-anything & công cụ rea (open_binary, inspect_artifact, analyze_javascript_application, inspect_web_network_capture...), (ii) reverse-clone-website (~/.gemini/config/skills/reverse-clone-website/SKILL.md & scripts probe.mjs, capture.mjs, js-slice.mjs, dump-computed.mjs, diff-computed.mjs) khi dịch ngược/clone 1:1 website hoặc web app đang chạy, (iii) game-reverse-reconstruct khi dịch ngược/tái tạo app native hoặc game (APK/XAPK/IPA). CẤM VƯỢT RÀO: Read-Only với business logic dự án (CẤM tự viết/sửa code app của dev-worker/ui-worker, CẤM copy code decompile rối rắm hoặc lộ secret vào repo); nếu sinh file Đặc tả Clean-Room, dữ liệu capture/ hoặc asset gốc sạch trong worktree thì phải git commit gọn gàng vào ${ROLE_WORKTREE} trước khi gửi handoff để dev-worker/ui-worker đồng bộ qua orca-team merge-upstream; CẤM đoán mò thiếu Evidence từ REA/CDP. BÁO CÁO: Xuất Evidence (Target SHA-256/Path/URL, REA/CDP tool, Variant Gates Desktop 1440 vs Mobile 390, Symbol/Offset/Decompiled Module/Navigation Graph/Protocol contract) và gộp trong 1 lệnh (&&): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'REA Clean-Room Spec' --body 'RE_VERDICT: EVIDENCE_COMPLETE | <Tóm tắt Spec 1:1>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'RE_VERDICT: EVIDENCE_COMPLETE' --json."
      ;;
    critic|research|researcher|challenger|advocate|criticspecialist)
      SPEC="[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC & SYSTEM ENGINEER] Nhiệm vụ: ${CLEAN_SPEC} (Spec tách riêng Triệu chứng + bằng chứng thô và Giả thuyết/Kế hoạch của Leader). Chuẩn Quân Sư & System Engineering: (1) Tra cứu Cẩm nang System Engineering qua orca-team kb (~/.orca/knowledge/system-engineer/INDEX.md) và CHỈ mở 1–2 chuyên đề liên quan (orca-team kb 01..05), (2) Lập giả thuyết & thiết kế ĐỘC LẬP từ bằng chứng thô TRƯỚC khi đọc Kế hoạch của Leader (chống anchoring), kiểm chứng bằng file:line, git log/diff, DB/logs, (3) Thẩm định Plan của Leader theo 5 trục: [a] Đủ 100% hạng mục [P1..Pn] + Disjoint File Ownership? [b] Đúng tầng App vs Infra/DB? [c] Idempotency/Concurrency/Locking an toàn? [d] Tương thích ngược DB/API & Index/Timeout chuẩn? [e] Tiêu chí định lượng để qa-worker kiểm đếm 1-1? CẤM VƯỢT RÀO: Read-Only 100% với code app & infra; CẤM hùa theo Leader hoặc gửi thẳng CONSENSUS_REACHED/worker_done ở lượt đầu mà bỏ qua orca orchestration ask (sẽ bị orca-command-guard chặn cứng!). TRANH BIỆN 2 CHIỀU BẮT BUỘC 2 PHA VỚI LEADER (giữ nguyên --from/--task-id/--dispatch-id từ Preamble): • PHA 1 (BẮT BUỘC Ở VÒNG 1 — GỬI ask THẨM ĐỊNH CHÉO): Chạy DUY NHẤT lệnh orca orchestration ask --from <worker_handle> --question 'CRITIC_CHALLENGE vòng <n>/3: <Bằng chứng file:line + 5 trục System Eng> | <Điểm cần bổ sung/chỉnh sửa hoặc câu hỏi kiểm chứng ranh giới file Disjoint Ownership & vai trò dev/devops/qa/sec cho [P1..Pn]>' --timeout-ms 900000 --json (timeout thì chạy lại kèm --resume <message_id>), lặp tối đa 3 vòng; • PHA 2 (CHỈ SAU KHI ask NHẬN ĐƯỢC reply TỪ LEADER): (a) Đạt đồng thuận 100% ➔ orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'Critic Consensus' --body 'CRITIC_VERDICT: CONSENSUS_REACHED | <Master Plan [P1..Pn] & root-cause + file:line>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'CRITIC_VERDICT: CONSENSUS_REACHED' --json; (b) Quá 3 vòng bất đồng ➔ gửi --type handoff --subject 'Critic Deadlock' --body 'CRITIC_VERDICT: HYPOTHESIS_CHALLENGED | <2 phương án + bằng chứng>' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'CRITIC_VERDICT: HYPOTHESIS_CHALLENGED' --json."
      ;;
    pr|prreviewer|reviewer)
      SPEC="[ROLE: PRINCIPAL PR REVIEWER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Đồng bộ commit mới nhất của PR, đọc git diff, kiểm tra kiến trúc, OWASP Top 10, Race Conditions, Memory Leaks và chạy test suite + linter thực tế. CẤM VƯỢT RÀO: Read-Only. BẰNG CHỨNG: Báo cáo file:line, EXIT_CODE test và chốt PR_VERDICT: APPROVE hoặc REQUEST_CHANGES kèm gửi --type worker_done --outcome succeeded."
      ;;
    issue|issueresolver|hotfix|fix)
      SPEC="[ROLE: PRINCIPAL ISSUE RESOLVER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: (1) Tìm Root Cause, (2) Viết Reproduction Test xác nhận FAIL trước khi sửa, (3) Sửa code tối thiểu tới khi PASS, (4) Chạy regression test EXIT_CODE=0. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ. BẰNG CHỨNG: Commit sạch kèm Commit Hash + log chạy test và gửi --type worker_done --outcome succeeded."
      ;;
    *)
      SPEC="[ROLE: PRINCIPAL DEV] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Hoàn thiện 100% mọi hạng mục [P1..Pn] trong Nhiệm vụ & Pinned Master Plan (qa-worker sẽ đối chiếu git diff 1-1 với Plan ở Wave 2, thiếu mục nào sẽ bắn QA_REJECT/INCOMPLETE_PLAN), Clean Architecture, type-safe, xử lý edge cases (null, timeout, concurrency), không TODO/stub/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code app & unit test (__tests__/*, *.test.*, *.spec.*) thuộc phạm vi file được giao (cấm đụng module của slot khác); tự định vị package trong monorepo không cần ask; CẤM sửa K8s/Docker của DevOps; CẤM xóa/skip (@Ignore, .skip) test cũ. GIAO TIẾP & P2P (giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble; CẤM truyền --run vào check): (1) Chạy orca-team merge-upstream --role dev --task-id <task_id> --dispatch-id <dispatch_id> (đồng bộ nhánh gốc main/develop và xem Pinned Plan/Peer Directory) và orca orchestration check [--ack <DELIVERY_ID>] --terminal <worker_handle> --json (từ lần check thứ 2 bắt buộc kèm --ack <DELIVERY_ID> của lần trước); nếu vướng kiến trúc liên role thì hỏi qua orca orchestration ask --from <worker_handle> --question '...' --json, (2) Build & Unit Test EXIT_CODE=0 (Jest kèm --forceExit; khi chạy background task-XX CẤM poll manage_task liên tục) và git commit sạch vào nhánh ${ROLE_WORKTREE}, (3) Nếu chạy P2P song song với QA/Sec, gửi --to '@all' (hoặc --to dispatch:<PEER_ID>) kèm --type handoff --subject 'DEV_READY v<n>' để QA/Sec merge & test ngay (sửa tối đa 3 vòng); khi hoàn tất ca, gộp gửi handoff và --type worker_done trong CÙNG 1 LỆNH (&&, KHÔNG dùng --to '@all' ở lệnh cuối): orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type handoff --subject 'Dev Handoff: ${ROLE_WORKTREE} (<commit_hash>)' --body 'Tóm tắt hạng mục [P1..Pn] đã xong, commit, file/API/schema thay đổi' --json && orca orchestration send --from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id> --type worker_done --outcome succeeded --subject 'Worker Done: ${ROLE_WORKTREE}' --body 'Completed ${ROLE_WORKTREE} (<commit_hash>)' --json."
      ;;
  esac
fi
SPEC="${SPEC//\(dev-worker \/ dev-worker-N\)/(${ROLE_WORKTREE})}"
SPEC="${SPEC//<branch_name>/${ROLE_WORKTREE}}"
[[ "$ROLE_WORKTREE" == ui-worker-* ]] && SPEC="${SPEC//vào nhánh ui-worker/vào nhánh ${ROLE_WORKTREE}}"
while [[ "$SPEC" == *" /goal" || "$SPEC" == *"/goal/goal" ]]; do
  SPEC="${SPEC%/goal}"
  SPEC="${SPEC%[[:space:]]}"
done
if [[ "$ROLE_GOAL_MODE" != "false" && "$SPEC" != *"CHẾ ĐỘ /goal"* ]]; then
  if [[ "$SPEC" == *[.!?] ]]; then
    SPEC="${SPEC}${GOAL_MODE_CLAUSE}"
  else
    SPEC="${SPEC}.${GOAL_MODE_CLAUSE}"
  fi
fi

# 1. Tạo task trong Orca
TASK_JSON=$("$ORCA_BIN" orchestration task-create \
  --task-title "$TASK_TITLE" \
  --spec "$SPEC" \
  --json 2>&1) || true

TASK_ID=$(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if not isinstance(d, dict) or d.get("ok") is False:
        sys.exit(0)
    res = d.get("result") if isinstance(d.get("result"), dict) else d
    tid = (res.get("task", {}).get("id") if isinstance(res.get("task"), dict) else None) or res.get("id") or ""
    print(tid if tid != "local" else "")
except Exception:
    pass
' <<< "$TASK_JSON" 2>/dev/null || true)

if [[ -z "$TASK_ID" ]]; then
  [[ -x "$ORCA_TEAM_BIN" ]] && "$ORCA_TEAM_BIN" --path "$REPO_ROOT" worktree-arg "$ROLE_WORKTREE" --release >/dev/null 2>&1 || true
  echo "❌ Không thể tạo task trong Orca. Kết quả: $TASK_JSON"
  exit 1
fi

echo "✅ Đã tạo task ID: $TASK_ID"

# 2. Khởi chạy worker (Tái sử dụng Worktree định danh của Role & Gom nhóm Terminal Tabs)
HAS_GIT_COMMIT=true
if ! git rev-parse HEAD &>/dev/null; then
  HAS_GIT_COMMIT=false
fi

if [[ "$HAS_GIT_COMMIT" == "true" ]]; then
  WORKTREE_NAME="$ROLE_WORKTREE"
  git worktree prune >/dev/null 2>&1 || true
  EXISTING_WT_PATH=$(git worktree list --porcelain 2>/dev/null | awk -v target="$ROLE_WORKTREE" '
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
    echo "🚀 Tái sử dụng worktree định danh '$ROLE_WORKTREE' ($EXISTING_WT_PATH) — Mở & gom nhóm Terminal Tab mới..."
    START_RECEIPT=$("$ORCA_BIN" orchestration worker-start \
      --task "$TASK_ID" \
      --worktree "path:$EXISTING_WT_PATH" \
      --agent "$AGENT" \
      "${MODEL_ARGS[@]}" \
      --timeout-ms "$TIMEOUT_MS" \
      --json 2>&1) || true
  else
    SETUP_POLICY="skip"
    [[ -f "$REPO_ROOT/package.json" ]] && SETUP_POLICY="run"
    echo "🚀 Khởi tạo worktree định danh lần đầu cho role: '$ROLE_WORKTREE'..."
    START_RECEIPT=$("$ORCA_BIN" orchestration worker-start \
      --task "$TASK_ID" \
      --worktree new-child \
      --name "$ROLE_WORKTREE" \
      --agent "$AGENT" \
      "${MODEL_ARGS[@]}" \
      --setup "$SETUP_POLICY" \
      --timeout-ms "$TIMEOUT_MS" \
      --json 2>&1) || true
  fi
else
  WORKTREE_NAME="current"
  echo "⚠️  Git repository chưa có commit đầu tiên. Đang khởi chạy worker trên workspace hiện tại..."
  START_RECEIPT=$("$ORCA_BIN" orchestration worker-start \
    --task "$TASK_ID" \
    --worktree current \
    --agent "$AGENT" \
    "${MODEL_ARGS[@]}" \
    --timeout-ms "$TIMEOUT_MS" \
    --json 2>&1) || true
fi

DISPATCH_ID=$(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if not isinstance(d, dict) or d.get("ok") is False:
        sys.exit(0)
    res = d.get("result") if isinstance(d.get("result"), dict) else d
    disp = res.get("dispatch") if isinstance(res.get("dispatch"), dict) else {}
    did = res.get("dispatchId") or disp.get("id") or res.get("id") or ""
    print(did if did != "local" else "")
except Exception:
    pass
' <<< "$START_RECEIPT" 2>/dev/null || true)

TERM_HANDLE=$(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    if not isinstance(d, dict) or d.get("ok") is False:
        sys.exit(0)
    res = d.get("result") if isinstance(d.get("result"), dict) else d
    wrk = res.get("worker") if isinstance(res.get("worker"), dict) else {}
    trm = res.get("terminal") if isinstance(res.get("terminal"), dict) else {}
    print(res.get("agentTerminalHandle") or res.get("terminalHandle") or wrk.get("agentTerminalHandle") or trm.get("handle") or "")
except Exception:
    pass
' <<< "$START_RECEIPT" 2>/dev/null || true)

if [[ -z "$DISPATCH_ID" ]]; then
  [[ -x "$ORCA_TEAM_BIN" ]] && "$ORCA_TEAM_BIN" --path "$REPO_ROOT" worktree-arg "$ROLE_WORKTREE" --release >/dev/null 2>&1 || true
  echo "❌ Khởi chạy worker thất bại: $START_RECEIPT"
  exit 1
fi

# Orca trả về dispatchId kể cả khi worker-start KHÔNG thành công (state != 'ready', CLI exit code 1)
START_STATE_INFO=$(python3 -c "
import sys, json
try:
    res = json.loads(sys.stdin.read()).get('result', {})
    state = res.get('state') or ''
    if state and state != 'ready':
        print(state)
        if res.get('lastError'):
            print('   • Lỗi (' + str(res.get('failedStage') or 'start') + '): ' + str(res.get('lastError')))
        for c in res.get('nextCommands') or []:
            print('   • Lệnh gợi ý: ' + str(c))
except Exception:
    pass
" <<< "$START_RECEIPT" 2>/dev/null || true)
if [[ -n "$START_STATE_INFO" ]]; then
  [[ -x "$ORCA_TEAM_BIN" ]] && "$ORCA_TEAM_BIN" --path "$REPO_ROOT" worktree-arg "$ROLE_WORKTREE" --release >/dev/null 2>&1 || true
  echo "❌ Worker dispatch $DISPATCH_ID chưa sẵn sàng (state: ${START_STATE_INFO%%$'\n'*})"
  [[ "$START_STATE_INFO" == *$'\n'* ]] && echo "${START_STATE_INFO#*$'\n'}"
  exit 1
fi

echo "🎉 Worker đã được khởi chạy thành công!"
echo "   • Worktree: $WORKTREE_NAME"
echo "   • Dispatch: $DISPATCH_ID"
if [[ -n "$TERM_HANDLE" ]]; then
  echo "   • Terminal: $TERM_HANDLE"
fi

