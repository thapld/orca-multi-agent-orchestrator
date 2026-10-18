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
      SPEC="[ROLE: PRINCIPAL DEVOPS/SRE] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO & CHỐNG TREO (ANTI-RABBIT-HOLE): Chỉ sửa Infra/Docker/K8s/DB/CI; CẤM sửa business logic của Dev; CẤM TUYỆT ĐỐI tự ý dịch ngược hoặc đọc/cat/strings/grep trực tiếp file binary trong container (nếu cần giải phẫu binary phải báo Coordinator giao re-worker); CẤM đặt dấu backtick (\`) bên trong chuỗi nháy kép (\"...\") của bash/zsh/python -c vì shell sẽ thực thi command substitution gây biến rỗng và treo vòng lặp stdout! RÀ SOÁT RUNTIME LOGS & BÁO CÁO COORDINATOR (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Đầu ca kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json, CẤM truyền --run vào check vì gây lỗi consumer_fenced!) và chạy ${MERGE_DEVOPS_UPSTREAM} để xem Pinned Master Plan + Danh bạ Peer-to-Peer của Run và đồng bộ main cùng DUY NHẤT các nhánh Dev/UI thuộc cùng Run của Leader này (tuyệt đối CẤM dùng vòng lặp wildcard git for-each-ref refs/heads/dev-worker* sẽ merge nhầm nhánh của Leader khác; nếu còn dev-worker trong cùng Run chưa xong lệnh sẽ chặn lại!), (2) CẤM chỉ nhìn Pod Running (1/1 Ready) hay /health 200 lúc mới bật rồi báo xong! Bắt buộc chờ qua chu kỳ chạy thật của background worker/@Scheduled/consumer và quét TOÀN BỘ Runtime Logs từ lúc container start (kubectl logs/docker logs KHÔNG dùng --since ngắn cắt lỗi) trên TẤT CẢ các pod/container (App + DB + Kafka/Broker + Sidecar) cùng trạng thái DB, (3) Nếu phát hiện BẤT KỲ dòng ERROR, FATAL, PANIC, Exception, Stacktrace, lỗi SQL, SSLHandshakeException, Auth 401/403 hay health=ERROR nào lúc runtime (hoặc nếu vướng lỗi quá 15 turns chưa xử lý được ở tầng Infra/DB) ➔ BẮT BUỘC dừng đào bới và trình báo ngay lên Coordinator: orca orchestration send --from <worker_handle> --type escalation --subject 'Runtime Log Error' --body '<Chi tiết pod & log lỗi trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json (KHÔNG dùng --to '@all'!) rồi chờ chỉ đạo hoặc báo --type worker_done --outcome failed (CẤM báo succeeded khi runtime log còn lỗi!); khi sạch lỗi 100%, gộp gửi handoff và worker_done trong cùng 1 lệnh (&&): orca orchestration send --from <worker_handle> --type handoff --subject 'Infra Ready' --body '<Pod/Port/Log sạch 100%>' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'DevOps Done: ${ROLE_WORKTREE}' --body '<Pod/Port/Log sạch 100%>' --task-id <task_id> --dispatch-id <dispatch_id> --json."
      ;;
    sec|security|bughunter|bug|auditor)
      SPEC="[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Soi git diff theo OWASP Top 10 (SQLi, Injection, Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự sửa code tính năng của Dev. GIAO TIẾP P2P ZERO-TRUST & BẰNG CHỨNG BẮT BUỘC: Chạy ${MERGE_SEC_CMD} để xem Pinned Master Plan + Danh bạ Peer-to-Peer của Run và đồng bộ main cùng DUY NHẤT các nhánh thuộc cùng Run của Leader này để diff đúng commit mới (tuyệt đối CẤM dùng vòng lặp wildcard git for-each-ref refs/heads/dev-worker* tránh trộn code của Leader khác; nếu có xung đột merge chỉ giữ code từ nhánh được kiểm toán, CẤM tạo commit mới sửa code tính năng); nếu phát hiện lỗ hổng có thể nhắn P2P cho Dev sửa (orca orchestration send --from <worker_handle> --to '@all' --type handoff --subject 'SEC_BLOCK' --body '<file:line + PoC>' --task-id <task_id> --dispatch-id <dispatch_id> --json) và trình lên Coordinator bằng orca orchestration send --from <worker_handle> --type escalation --subject 'Security Vulnerability Found' --body '<file:line & PoC trình Coordinator ra quyết định>' --task-id <task_id> --dispatch-id <dispatch_id> --json; liệt kê rõ file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets], chốt SECURITY_VERDICT: PASS hoặc BLOCK và gửi --type worker_done --outcome succeeded (hoặc failed nếu BLOCK)."
      ;;
    qa|test|tester|sdet)
      SPEC="[ROLE: PRINCIPAL QA/SDET — PLAN COMPLETION & ZERO-TRUST AUDITOR] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: (A) KIỂM TOÁN 100% TIẾN ĐỘ PLAN (PLAN-TO-CODE & TEST AUDIT): QA chịu trách nhiệm kiểm soát chất lượng VÀ rà soát 100% từng hạng mục [P1..Pn] trong Pinned Master Plan / Task Spec đã giao, đối chiếu 1-1 với git diff thực tế của Dev/UI/DevOps (git diff main...HEAD), bắt lỗi thiếu file/hàm/endpoint/migration/edge-case hoặc còn TODO/FIXME/stub/mock giả; (B) KIỂM THỬ BẺ GÃY & RUNTIME LOGS: Phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB), chạy test thật và quét sạch Runtime Logs. CẤM VƯỢT RÀO & CẤM CHE GIẤU LỖI: Chỉ viết/chạy test & kiểm toán Plan; CẤM TUYỆT ĐỐI sửa business logic của Dev; CẤM xóa/nới lỏng assert; CẤM dùng --since ngắn để cắt bỏ log lỗi cũ hoặc bỏ sót không quét pod DB/Kafka! GIAO TIẾP P2P VỚI DEV & BÁO CÁO CUỐI VỀ COORDINATOR (BẮT BUỘC): (1) Kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json, CẤM truyền --run vào check vì gây lỗi consumer_fenced!) và chạy ${MERGE_QA_CMD} để xem Pinned Master Plan + Danh bạ Peer-to-Peer của Run và đồng bộ main cùng DUY NHẤT các nhánh Dev/UI/DevOps thuộc cùng Run của Leader này trước khi test (tuyệt đối CẤM dùng vòng lặp wildcard git for-each-ref refs/heads/dev-worker*!), (2) Đối chiếu 1-1 từng mục [P1..Pn] trong Plan với git diff, chạy lệnh test thật (Jest kèm --forceExit) VÀ kiểm toán trực tiếp TOÀN BỘ Runtime Logs (kubectl logs/docker logs từ lúc start trên MỌI container App + DB + Broker), (3) Nếu phát hiện thiếu bất kỳ mục [Pk] nào trong Plan, test fail, hoặc có lỗi ERROR/Exception/FATAL/SQL/SSL/health=ERROR trong Runtime Logs: nếu dev-worker vẫn đang LIVE, gửi P2P trả bài trực tiếp cho Dev sửa (orca orchestration send --from <worker_handle> --to '@all' --type handoff --subject 'QA_REJECT vòng <n>/3' --body 'Thiếu mục [Pk] hoặc lỗi tại <file:line/log>' --task-id <task_id> --dispatch-id <dispatch_id> --json, tối đa 3 vòng nội bộ team); đồng thời hoặc nếu quá 3 vòng/Dev đã đóng thì gửi escalation về Coordinator (orca orchestration send --from <worker_handle> --type escalation --subject 'QA Plan/Runtime Failure' --body '<Bảng kiểm đếm [P1..Pn] + log lỗi trình Coordinator>' --task-id <task_id> --dispatch-id <dispatch_id> --json) và chốt QA_VERDICT: FAIL! (4) Chỉ khi 100% các mục [P1..Pn] trong Plan đạt chuẩn + Test PASS + Runtime Log 0 lỗi, gộp trong 1 lệnh (&&): orca orchestration send --from <worker_handle> --type handoff --subject 'QA_FINAL: PLAN 100% & TESTS PASS' --body 'QA_VERDICT: PASS | Bảng đối chiếu [P1..Pn]: <mục -> file:line + commit + test> | Tests Run & Runtime Logs: CLEAN' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'QA Done: Plan 100% & Tests PASS' --body 'QA_VERDICT: PASS | [P1..Pn] 100% DONE' --task-id <task_id> --dispatch-id <dispatch_id> --json."
      ;;
    ui|ux|frontend|uispecialist|design)
      SPEC="[ROLE: PRINCIPAL UI/UX] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: CSS/DOM chuẩn xác, Responsive (Mobile/Tablet/Desktop), Accessibility (a11y), Loading/Empty/Error states. CẤM VƯỢT RÀO: Chỉ sửa UI/Frontend; CẤM sửa backend core/DB schema. GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Đồng bộ nhánh mới nhất (git merge --no-edit main), xem Pinned Master Plan qua orca-team plan show --task-id <task_id> --dispatch-id <dispatch_id>, Build/typecheck EXIT_CODE=0, không lỗi Console, git commit kèm Commit Hash vào nhánh ${ROLE_WORKTREE} và gộp gửi handoff + worker_done trong cùng 1 lệnh (&&): orca orchestration send --from <worker_handle> --type handoff --subject 'UI Ready: ${ROLE_WORKTREE} (<commit_hash>)' --body '<Branch/Commit/Routes + các mục [Pk] đã hoàn thành>' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'UI Done: ${ROLE_WORKTREE} (<commit_hash>)' --body '<Branch/Commit/Routes>' --task-id <task_id> --dispatch-id <dispatch_id> --json."
      ;;
    bench|benchmark|perf|performance|benchspecialist)
      SPEC="[ROLE: PRINCIPAL BENCHMARK & PERF] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Có bước warm-up, dùng công cụ chuẩn (k6/wrk/JMH/pytest-benchmark/go test -bench/EXPLAIN ANALYZE), đo Before vs After. CẤM VƯỢT RÀO: Chỉ viết/chạy benchmark & phân tích bottleneck; CẤM tự sửa business logic hoặc drop dữ liệu/bỏ validate để ăn gian điểm số! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: (1) Nhận chỉ đạo nhánh/môi trường từ Coordinator và chạy ${MERGE_BENCH_CMD} để xem Pinned Master Plan và đồng bộ main cùng DUY NHẤT các nhánh thuộc cùng Run của Leader này trước khi đo hiệu năng, (2) Xuất bảng số liệu thật: Concurrency, Throughput (req/s hoặc EPS), Latency (p50/p95/p99), Error Rate (0%), CPU/RAM; nếu phát hiện nghẽn cổ chai, gửi ngay orca orchestration send --from <worker_handle> --type escalation --subject 'Perf Bottleneck Found' --body '<file:line/query gây nghẽn trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt PERF_VERDICT: PASS hoặc BOTTLENECK_FOUND kèm gửi --type worker_done --outcome succeeded (hoặc failed)."
      ;;
    re|reverse|rea|respecialist)
      SPEC="[ROLE: PRINCIPAL REVERSE ENGINEER (REA)] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Bắt buộc kích hoạt skill reverse-engineer-anything (morluto/rea) & công cụ rea/rea-agents (open_binary, inspect_artifact, analyze_javascript_application, inspect_web_network_capture...). CẤM VƯỢT RÀO: (1) Read-Only đối với source code dự án (chỉ xuất Đặc tả Kỹ thuật Clean-Room, CẤM copy code decompile rối rắm vào repo), (2) CẤM dùng REA cho source code repo thông thường, (3) CẤM đoán mò hành vi hộp đen khi không có Evidence từ REA! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Xuất Hồ sơ Evidence thực tế (Target SHA-256/Path, REA tool đã chạy, Symbol/Offset/Decompiled Function/Struct/Protocol contract, Unknowns/Limitations), gộp gửi handoff + worker_done trong cùng 1 lệnh (&&): orca orchestration send --from <worker_handle> --type handoff --subject 'REA Clean-Room Spec' --body '<Tóm tắt Spec trình Coordinator duyệt và giao Dev>' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'REA Done' --body '<Tóm tắt Spec & Evidence>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt RE_VERDICT: EVIDENCE_COMPLETE hoặc BLOCKED_BY_UNKNOWN."
      ;;
    critic|research|researcher|challenger|advocate|criticspecialist)
      SPEC="[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC & SYSTEM ENGINEER] Nhiệm vụ: ${CLEAN_SPEC} (Spec tách riêng Triệu chứng/Bối cảnh + bằng chứng thô và Bản nháp Kế hoạch/Giả thuyết của Leader). Chuẩn: (1) Đóng vai trò Quân sư Kiến trúc & Phản biện Hệ thống (System Engineering Advisor): tham chiếu Bộ Tri Thức System Engineering chuẩn tại ~/.orca/knowledge/system-engineer/INDEX.md (hoặc knowledge/system-engineer/INDEX.md trong repo / chạy orca-team kb) gồm 5 trụ cột [Distributed Systems & Consensus, Data Storage/Indexing/Consistency, Reliability/SRE/Observability, Security & Zero-Trust, Performance/Capacity/Scalability]; (2) Lập giả thuyết/phương án ĐỘC LẬP từ bằng chứng thô TRƯỚC khi đọc kế hoạch của Leader (chống anchoring), kiểm chứng bằng file:line, git log/blame/diff, DB state, runtime logs; (3) Chỉ ra giả định sai, điểm mù System Engineering (race condition, N+1, idempotency, backpressure, timeout/retry storm, SPOF, blast radius), quy kết nhầm role (App vs Infra/DB) và đề xuất hiệu chỉnh từng mục [P1..Pn] trong Plan. CẤM VƯỢT RÀO: Read-Only 100% với code app & infra; CẤM hùa theo Leader khi chưa tự kiểm chứng; CẤM phản biện không kèm bằng chứng file:line/log. GIAO THỨC TRANH BIỆN 2 CHIỀU VỚI LEADER (BẮT BUỘC giữ nguyên --from cho ask; --from/--task-id/--dispatch-id cho send từ Preamble): (a) Cần chất vấn/hiệu chỉnh Plan ➔ orca orchestration ask --from <worker_handle> --question 'CRITIC_CHALLENGE vòng <n>/3: <bằng chứng file:line/log + góc nhìn System Engineering> | Đề xuất hiệu chỉnh Plan [P1..Pn]: <phương án>' --timeout-ms 900000 --json (lệnh chặn tới khi Leader reply; timeout thì chạy lại với --resume <message_id>), kiểm chứng phản hồi của Leader rồi lặp tối đa 3 vòng; (b) Khi cả hai thống nhất ➔ gộp trong cùng 1 lệnh (&&): orca orchestration send --from <worker_handle> --type handoff --subject 'Critic Consensus' --body 'CRITIC_VERDICT: CONSENSUS_REACHED | <Master Plan [P1..Pn] & root-cause/spec đã thống nhất + file:line>' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'Critic Consensus Reached' --body 'CRITIC_VERDICT: CONSENSUS_REACHED | <Tóm tắt Master Plan đã thống nhất>' --task-id <task_id> --dispatch-id <dispatch_id> --json; (c) Hết 3 vòng vẫn bất đồng ➔ gộp lệnh send handoff với --subject 'Critic Deadlock' --body 'CRITIC_VERDICT: HYPOTHESIS_CHALLENGED | Phương án Leader vs Critic + bằng chứng mỗi bên' && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'Critic Deadlock' --body 'CRITIC_VERDICT: HYPOTHESIS_CHALLENGED' --task-id <task_id> --dispatch-id <dispatch_id> --json."
      ;;
    pr|prreviewer|reviewer)
      SPEC="[ROLE: PRINCIPAL PR REVIEWER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Đồng bộ commit mới nhất của PR, đọc từng dòng diff (git diff), kiểm tra kiến trúc, OWASP Top 10, Race Conditions, Memory Leaks và BẮT BUỘC chạy test suite + linter thực tế (CẤM duyệt suông bằng mắt). CẤM VƯỢT RÀO: Read-Only (không tự sửa code PR khi chưa được yêu cầu). BẰNG CHỨNG BẮT BUỘC: Báo cáo file:line, EXIT_CODE test và chốt PR_VERDICT: APPROVE hoặc REQUEST_CHANGES kèm gửi --type worker_done --outcome succeeded."
      ;;
    issue|issueresolver|hotfix|fix)
      SPEC="[ROLE: PRINCIPAL ISSUE RESOLVER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: (1) Tìm Root Cause, (2) Viết Reproduction Test xác nhận FAIL trước khi sửa, (3) Sửa code tối thiểu tới khi Reproduction Test PASS, (4) Chạy regression test đạt EXIT_CODE=0. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ. BẰNG CHỨNG BẮT BUỘC: Commit sạch kèm Commit Hash + log chạy test và gửi --type worker_done --outcome succeeded."
      ;;
    *)
      SPEC="[ROLE: PRINCIPAL DEV] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Clean Architecture, type-safe 100%, xử lý edge cases (null, timeout, concurrency), không TODO/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code app & unit test (__tests__/*, *.test.*, *.spec.*) thuộc phạm vi module/file được giao (cấm đụng sang module của slot khác); chủ động định vị đường dẫn package trong monorepo (services/*, packages/*, apps/*) mà không dừng lại ask Coordinator; CẤM sửa K8s/Docker của DevOps; CẤM xóa/skip (@Ignore, .skip) test cũ đang fail. GIAO TIẾP P2P ZERO-TRUST & THẨM QUYỀN QUYẾT ĐỊNH (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Đồng bộ nhánh mới nhất (git merge --no-edit main), chạy orca-team plan show --task-id <task_id> --dispatch-id <dispatch_id> để đọc Bản Kế Hoạch Tổng Thể đã ghim (Pinned Master Plan [P1..Pn]) + Danh bạ Peer-to-Peer của Run, và kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json, CẤM truyền --run vào check vì gây lỗi consumer_fenced!); nếu cần quyết định kiến trúc/cấu hình liên role vượt ngoài Plan đã chốt, BẮT BUỘC hỏi Coordinator qua orca orchestration ask --from <worker_handle> --question '...' --json, (2) Build & Unit Test EXIT_CODE=0 (Jest bắt buộc kèm --forceExit) và git commit sạch vào nhánh worktree hiện tại (${ROLE_WORKTREE}), (3) Khi hoàn tất (hoặc cần phối hợp trực tiếp với QA/Sec/DevOps đang chạy song song), có thể nhắn P2P cho đồng đội qua --to '@all' hoặc --to dispatch:<PEER_ID>, và BẮT BUỘC gộp lệnh gửi handoff + worker_done về Coordinator trong CÙNG MỘT LỆNH SHELL nối bằng &&: orca orchestration send --from <worker_handle> --type handoff --subject 'Dev Handoff: ${ROLE_WORKTREE} (<commit_hash>)' --body 'DEV_READY: Nhánh, commit, các mục [Pk] đã hoàn thành, file sửa, API/schema/env thay đổi để QA/Sec/DevOps đối chiếu' --task-id <task_id> --dispatch-id <dispatch_id> --json && orca orchestration send --from <worker_handle> --type worker_done --outcome succeeded --subject 'Dev Done: ${ROLE_WORKTREE} (<commit_hash>)' --body 'Hoàn tất các mục được giao kèm Commit Hash & Unit Test PASS' --task-id <task_id> --dispatch-id <dispatch_id> --json (bản tin gửi về Coordinator KHÔNG dùng --to '@all' vì '@all' loại trừ Coordinator!)."
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

