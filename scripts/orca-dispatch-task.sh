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

# Tự động lấy agent của dev/qa/sec/devops/bench/re từ orca-team nếu truyền role name
ORCA_TEAM_BIN="$(command -v orca-team 2>/dev/null || echo "${0:A:h}/orca-team")"
ROLE_QUERY="${REQ_AGENT:-dev}"
EFF_ROLE_QUERY="$ROLE_QUERY"
CONFIG_AGENT=""
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  CONFIG_AGENT=$("$ORCA_TEAM_BIN" get "$ROLE_QUERY" 2>/dev/null || true)
  if [[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]]; then
    EFF_ROLE_QUERY="coord"
    CONFIG_AGENT=$("$ORCA_TEAM_BIN" get coord 2>/dev/null || true)
  fi
fi
[[ "$CONFIG_AGENT" == "none" || "$CONFIG_AGENT" == "null" ]] && CONFIG_AGENT=""

if [[ "$REQ_AGENT" =~ ^(kimi|claude|codex|cursor|antigravity|opencode|aider|goose|copilot|gemini|muse)$ ]]; then
  AGENT="$REQ_AGENT"
else
  AGENT="${CONFIG_AGENT:-antigravity}"
fi

ORCA_BIN="${ORCA_BIN:-$(command -v orca 2>/dev/null || echo "/usr/local/bin/orca")}"
TIMEOUT_MS="${TIMEOUT_MS:-300000}"

# Lấy model và reasoning effort nếu đã được cấu hình trong orca-team
MODEL_ARGS=()
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  DEV_MODEL=$("$ORCA_TEAM_BIN" get "$EFF_ROLE_QUERY" --field model 2>/dev/null || true)
  DEV_EFFORT=$("$ORCA_TEAM_BIN" get "$EFF_ROLE_QUERY" --field effort 2>/dev/null || true)
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
      SPEC="[ROLE: PRINCIPAL DEVOPS & SRE] Nhiệm vụ: ${SPEC}. Chuẩn mực: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO: Chỉ sửa tầng Infra/Docker/K8s/DB/CI; CẤM tự ý sửa business logic của Dev. RÀ SOÁT RUNTIME LOGS & BÁO CÁO LÊN COORDINATOR (BẮT BUỘC trước khi gửi worker_done): (1) Chạy orca orchestration check --peek --json nhận chỉ đạo nhánh/commit đã được Coordinator duyệt và git merge --no-edit <nhánh-dev> trước khi build, (2) CẤM chỉ nhìn Pod Running (1/1 Ready) hay /health 200 lúc mới bật rồi báo xong! Bắt buộc chờ qua chu kỳ chạy thật của background worker/@Scheduled/consumer và quét TOÀN BỘ Runtime Logs từ lúc container start (kubectl logs/docker logs KHÔNG dùng --since ngắn cắt lỗi) trên TẤT CẢ các pod/container (App + DB + Kafka/Broker + Sidecar) cùng trạng thái vận hành trong DB, (3) Nếu phát hiện BẤT KỲ dòng ERROR, FATAL, PANIC, Exception, Stacktrace, lỗi SQL, SSLHandshakeException, Auth 401/403 hay health=ERROR nào lúc runtime ➔ BẮT BUỘC trình báo ngay lên Coordinator để Coordinator ra quyết định xử lý: orca orchestration send --type escalation --subject 'Runtime Log Error' --body '<Chi tiết pod & log lỗi trình Coordinator phán quyết>' --json (KHÔNG dùng --to '@all' đơn lẻ vì sẽ bỏ sót Coordinator!) rồi báo worker_done --outcome failed; khi sạch lỗi 100%, gửi orca orchestration send --type handoff --subject 'Infra Ready' --body '<Pod/Port/Log sạch 100%>' --json về Coordinator."
      ;;
    sec|security|bug|auditor)
      SPEC="[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: ${SPEC}. Chuẩn mực: Kiểm toán độc lập (Red-Team) từng dòng git diff theo OWASP Top 10 (SQLi, Injection, Broken Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự ý sửa code tính năng của Dev. GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: Diff đúng commit mới của Dev; nếu phát hiện lỗ hổng phải trình ngay lên Coordinator bằng orca orchestration send --type escalation --subject 'Security Vulnerability Found' --body '<file:line & PoC trình Coordinator ra quyết định>' --json; liệt kê rõ file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets] và chốt rõ SECURITY_VERDICT: PASS hoặc BLOCK."
      ;;
    qa|test|tester|sdet)
      SPEC="[ROLE: PRINCIPAL QA & SDET] Nhiệm vụ: ${SPEC}. Chuẩn mực: Tư duy bẻ gãy hệ thống (Adversarial Testing) & Kiểm toán Runtime Logs — phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB) và 100% Acceptance Criteria. CẤM VƯỢT RÀO & CẤM CHE GIẤU LỖI: Chỉ viết/chạy test và nghiệm thu; CẤM TUYỆT ĐỐI tự sửa code business logic của Dev; CẤM xóa/nới lỏng assert (vd: chỉ check HTTP 200 hay attempts>=1 mà lờ đi status=FAILED/health=ERROR/last_error/SSLHandshakeException); CẤM dùng --since ngắn để cắt bỏ log lỗi cũ hoặc bỏ sót không quét pod DB/Kafka! GIAO TIẾP & BÁO CÁO LÊN COORDINATOR (BẮT BUỘC trước khi gửi worker_done): (1) Kiểm tra hộp thư (orca orchestration check --peek --json) nhận thông tin nhánh/môi trường từ Coordinator và git merge --no-edit <nhánh-dev> trước khi test, (2) Chạy lệnh test thật VÀ kiểm toán trực tiếp TOÀN BỘ Runtime Logs (kubectl logs/docker logs từ lúc start trên MỌI container App + DB + Broker) trong & sau khi chạy E2E/scheduled jobs, (3) Nếu vướng mắc môi trường phải hỏi Coordinator qua orca orchestration ask --question '...' --json; nếu phát hiện BẤT KỲ lỗi ERROR/Exception/FATAL/SQL/SSL/health=ERROR nào trong Runtime Logs hay DB ➔ BẮT BUỘC trình ngay lên Coordinator bằng orca orchestration send --type escalation --subject 'QA Runtime/Test Failure' --body '<Trích xuất log lỗi trình Coordinator phán quyết>' --json và chốt QA_VERDICT: FAIL!"
      ;;
    ui|ux|frontend|design)
      SPEC="[ROLE: PRINCIPAL UI/UX ARCHITECT] Nhiệm vụ: ${SPEC}. Chuẩn mực: Tinh chỉnh chuẩn xác CSS/DOM qua Orca Design Mode, đảm bảo Responsive (Mobile/Tablet/Desktop), Accessibility (a11y), Loading/Empty/Error states. CẤM VƯỢT RÀO: Chỉ sửa tầng UI/Frontend; CẤM sửa backend core/DB schema. BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: Build/typecheck EXIT_CODE=0, không lỗi Console, kiểm tra snapshot/screenshot thực tế, gửi orca orchestration send --type handoff --subject 'UI Ready' --body '<Branch/Commit/Routes>' --json về Coordinator và git commit kèm Commit Hash."
      ;;
    bench|benchmark|perf|performance)
      SPEC="[ROLE: PRINCIPAL BENCHMARK & PERF] Nhiệm vụ: ${SPEC}. Chuẩn mực: Có bước warm-up, dùng công cụ chuẩn (k6/wrk/JMH/pytest-benchmark/go test -bench/EXPLAIN ANALYZE), đo Before vs After. CẤM VƯỢT RÀO: Chỉ viết/chạy benchmark & phân tích bottleneck; CẤM tự sửa business logic hoặc drop dữ liệu/bỏ validate để ăn gian điểm số! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: (1) Nhận chỉ đạo nhánh/môi trường từ Coordinator và git merge --no-edit <nhánh-dev> để đo trên code mới nhất, (2) Xuất bảng số liệu thật: Concurrency, Throughput (req/s hoặc EPS), Latency (p50/p95/p99), Error Rate (0%), CPU/RAM; nếu phát hiện nghẽn cổ chai, gửi ngay orca orchestration send --type escalation --subject 'Perf Bottleneck Found' --body '<file:line/query gây nghẽn trình Coordinator phán quyết>' --json và chốt PERF_VERDICT: PASS hoặc BOTTLENECK_FOUND."
      ;;
    re|reverse|rea|respecialist)
      SPEC="[ROLE: PRINCIPAL REVERSE ENGINEER (REA)] Nhiệm vụ: ${SPEC}. Chuẩn mực: Bắt buộc kích hoạt skill reverse-engineer-anything (morluto/rea) & công cụ rea/rea-agents (open_binary, inspect_artifact, analyze_javascript_application, inspect_web_network_capture...). CẤM VƯỢT RÀO: (1) Read-Only đối với source code dự án (chỉ xuất Đặc tả Kỹ thuật Clean-Room, CẤM copy code decompile rối rắm vào repo), (2) CẤM dùng REA cho source code repo thông thường, (3) CẤM đoán mò hành vi hộp đen khi không có Evidence từ REA! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC trước khi gửi worker_done: Xuất Hồ sơ Evidence thực tế (Target SHA-256/Path, REA tool đã chạy, Symbol/Offset/Decompiled Function/Struct/Protocol contract, Unknowns/Limitations), gửi bản tin orca orchestration send --type handoff --subject 'REA Clean-Room Spec' --body '<Tóm tắt Spec trình Coordinator duyệt và giao Dev>' --json và chốt RE_VERDICT: EVIDENCE_COMPLETE hoặc BLOCKED_BY_UNKNOWN."
      ;;
    *)
      SPEC="[ROLE: PRINCIPAL FULL-STACK DEV] Nhiệm vụ: ${SPEC}. Chuẩn mực: Clean Architecture, type-safe 100%, xử lý triệt để edge cases (null, timeout, thread-safety/concurrency, backward-compat), không để TODO/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code ứng dụng & unit test của mình; CẤM sửa K8s/Docker/CI của DevOps; CẤM xóa/skip/comment-out (@Ignore, .skip) test cũ đang fail để lách lỗi. GIAO TIẾP & THẨM QUYỀN QUYẾT ĐỊNH (BẮT BUỘC trước khi gửi worker_done): (1) Kiểm tra hộp thư (orca orchestration check --peek --json) để nhận chỉ đạo/spec từ Coordinator; nếu cần quyết định kiến trúc/cấu hình, BẮT BUỘC hỏi Coordinator qua orca orchestration ask --question '...' --json (worker KHÔNG tự ý quyết định thay đổi thiết kế liên role), (2) Chạy compile/build & unit test thực tế đạt EXIT_CODE=0 và git commit sạch sẽ vào nhánh worktree, (3) Trước khi gửi worker_done, BẮT BUỘC gửi bản tin bàn giao về cho Coordinator duyệt và điều phối: orca orchestration send --type handoff --subject 'Dev Handoff: <branch> (<commit_hash>)' --body 'Tóm tắt file sửa, API/schema/env thay đổi để Coordinator điều phối cho DevOps/QA/Sec/Bench' --json."
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
  WORKTREE_NAME="current"
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
