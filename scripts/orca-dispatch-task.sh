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
KNOWN_ROLES_RE='^(coord|coordinator|leader|lead|architect|orchestrator|dev|developer|developers|coder|fullstack|backend|doc|docs|fix|devops|devopsspecialist|infra|sre|ops|sec|security|bughunter|bug|auditor|qa|test|tester|sdet|ui|ux|frontend|uispecialist|design|bench|benchmark|perf|performance|benchspecialist|re|reverse|rea|respecialist|critic|research|researcher|challenger|advocate|criticspecialist|pr|prreviewer|reviewer|issue|issueresolver|hotfix)$'

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
    REQ_AGENT="${match[1]}"
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
  if [[ "${TASK_TITLE:l}" =~ ^review-pr-[0-9]+$ ]]; then
    ROLE_QUERY="pr"
  elif [[ "${TASK_TITLE:l}" =~ ^hotfix-issue-[0-9]+$ ]]; then
    ROLE_QUERY="issue"
  elif [[ "${TASK_TITLE:l}" =~ ^(dev|devops|qa|sec|bench|re|ui|critic|pr|issue)-worker(-[0-9]+)?$ ]]; then
    ROLE_QUERY="${match[1]}"
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
  if [[ "$CONFIG_AGENT" == "none" ]] && ! [[ "$REQ_AGENT" =~ $KNOWN_AGENTS_RE ]] && ! [[ "$ROLE_QUERY" =~ ^(pr|prreviewer|reviewer|issue|issueresolver|hotfix|fix)$ ]]; then
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
if [[ "$CHECK_RUN" == *"No Run is bound"* ]]; then
  echo "ℹ️  Chưa có Run orchestration active. Đang tự động tạo Run..."
  if ! "$ORCA_BIN" orchestration run-create --objective "Orchestration Run: $TASK_TITLE" >/dev/null 2>&1; then
    echo "❌ Không thể tạo Run trong Orca." >&2
    exit 1
  fi
fi

# Xác định định danh worktree cố định theo từng vai trò & số lượng slot song song (Concurrency)
ROLE_WORKTREE="dev-worker"
EXPLICIT_SLOT=false
if [[ "${TASK_TITLE:l}" =~ ^(review-pr-[0-9]+|hotfix-issue-[0-9]+|(dev|devops|qa|sec|bench|re|ui|critic|pr|issue)-worker(-[0-9]+)?)$ ]]; then
  ROLE_WORKTREE="${TASK_TITLE:l}"
  EXPLICIT_SLOT=true
else
  case "${ROLE_QUERY:l}" in
    devops|devopsspecialist|infra|sre|ops) ROLE_WORKTREE="devops-worker" ;;
    sec|security|bughunter|bug|auditor) ROLE_WORKTREE="sec-worker" ;;
    qa|test|tester|sdet) ROLE_WORKTREE="qa-worker" ;;
    ui|ux|frontend|uispecialist|design) ROLE_WORKTREE="ui-worker" ;;
    bench|benchmark|perf|performance|benchspecialist) ROLE_WORKTREE="bench-worker" ;;
    re|reverse|rea|respecialist) ROLE_WORKTREE="re-worker" ;;
    critic|research|researcher|challenger|advocate|criticspecialist) ROLE_WORKTREE="critic-worker" ;;
    pr|prreviewer|reviewer) ROLE_WORKTREE="pr-worker" ;;
    issue|issueresolver|hotfix|fix) ROLE_WORKTREE="issue-worker" ;;
    *) ROLE_WORKTREE="dev-worker" ;;
  esac
fi

# Tự động chọn Worktree Slot đang rảnh (<role>-worker .. <role>-worker-N) khi role có Concurrency >= 2
ROLE_CONCURRENCY=1
DEV_CONCURRENCY=1
ROLE_GOAL_MODE="true"
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  RAW_CONC=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$ROLE_QUERY" --field concurrency 2>/dev/null || true)
  [[ "$RAW_CONC" =~ ^[0-9]+$ ]] && ROLE_CONCURRENCY="$RAW_CONC"
  RAW_DEV_CONC=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get dev --field concurrency 2>/dev/null || true)
  [[ "$RAW_DEV_CONC" =~ ^[0-9]+$ ]] && DEV_CONCURRENCY="$RAW_DEV_CONC"
  RAW_GOAL=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" get "$ROLE_QUERY" --field goal 2>/dev/null || true)
  [[ "$RAW_GOAL" == "false" ]] && ROLE_GOAL_MODE="false"
fi

if [[ "$EXPLICIT_SLOT" == false && "$ROLE_CONCURRENCY" -ge 2 ]]; then
  ACTIVE_WORKERS_JSON=$("$ORCA_BIN" orchestration worker-list --json 2>/dev/null || true)
  git worktree prune >/dev/null 2>&1 || true
  REPO_WT_PATHS=$(git worktree list --porcelain 2>/dev/null | awk '/^worktree / { print substr($0, 10) }' || true)
  CHOSEN_SLOT=$(python3 -c '
import sys, json, os

base_wt = sys.argv[1]
try:
    conc = max(1, min(10, int(sys.argv[2])))
except Exception:
    conc = 1
repo_root = (sys.argv[3] if len(sys.argv) > 3 else "").rstrip("/")
repo_slug = os.path.basename(repo_root).lower() if repo_root else ""
repo_wts = set(ln.strip() for ln in (sys.argv[4] if len(sys.argv) > 4 else "").splitlines() if ln.strip())

slots = [base_wt if i == 1 else f"{base_wt}-{i}" for i in range(1, conc + 1)]
live_slots = set()
counts = {s: 0 for s in slots}
try:
    d = json.loads(sys.stdin.read())
    res = d.get("result") if isinstance(d, dict) and isinstance(d.get("result"), dict) else (d if isinstance(d, dict) else {})
    scope = str(res.get("scope") or "")
    for w in (res.get("workers") or []):
        res_obj = w.get("resource") if isinstance(w.get("resource"), dict) else {}
        ws_id = str(res_obj.get("worktreeId") or w.get("worktreeId") or "")
        wt_path_part = ws_id.split("::", 1)[1] if "::" in ws_id else ws_id
        if scope == "all" and repo_root:
            ws_low = ws_id.lower()
            if (
                wt_path_part not in repo_wts
                and repo_root.lower() not in ws_low
                and (not repo_slug or (f"/{repo_slug}/" not in ws_low and not ws_low.endswith(f"/{repo_slug}")))
            ):
                continue
        wt_n = ws_id.rsplit("/", 1)[-1] if "/" in ws_id else (ws_id.split("::", 1)[-1] if "::" in ws_id else ws_id)
        st = str(w.get("workerState") or w.get("state") or "").lower()
        d_st = str(w.get("dispatchStatus") or "").lower()
        t_st = str(w.get("terminalState") or res_obj.get("terminalState") or "").lower()
        lv = str((w.get("liveness") or {}).get("verdict") or "").lower()
        if wt_n in counts:
            counts[wt_n] += 1
            is_dead = (
                st in ("succeeded", "failed", "stopped", "timed_out", "settled", "completed", "cancelled", "released")
                or d_st in ("succeeded", "failed", "stopped", "timed_out", "completed", "cancelled")
                or t_st in ("released", "closed", "exited")
                or lv in ("exited", "dead")
            )
            if not is_dead:
                live_slots.add(wt_n)
except Exception:
    pass
free_slots = [s for s in slots if s not in live_slots]
if free_slots:
    print(free_slots[0])
else:
    print(min(slots, key=lambda s: (counts.get(s, 0), slots.index(s))))
' "$ROLE_WORKTREE" "$ROLE_CONCURRENCY" "$REPO_ROOT" "$REPO_WT_PATHS" <<< "$ACTIVE_WORKERS_JSON" 2>/dev/null || true)
  [[ -n "$CHOSEN_SLOT" ]] && ROLE_WORKTREE="$CHOSEN_SLOT"
fi

GOAL_MODE_CLAUSE=" CHẾ ĐỘ /goal (LÀM TRIỆT ĐỂ 100% DELIVERABLES): Tự lập checklist đối chiếu từng yêu cầu với bằng chứng thực tế (file diff, build/test EXIT_CODE=0, runtime log); khi chạy Jest bắt buộc kèm --forceExit chống treo open handles; khi lệnh chạy ngầm (task-XX) CẤM gọi manage_task(status) lặp lại mỗi vài giây gây phình turns (hãy chờ thông báo hoàn tất hoặc đợi tối thiểu 30-60s); CẤM dừng nửa chừng hoặc bỏ sót bất kỳ hạng mục nào; chỉ sau khi đã hoàn tất 100% nhiệm vụ + gửi bản tin báo cáo + gửi worker_done về Coordinator mới được xuất thẻ <!-- GOAL_COMPLETE --> ở câu trả lời cuối cùng để đóng Goal."

# Tự động tiêm Hiến pháp Vai trò Cấp Principal (Chống vượt rào, Chống báo sạch giả & Chế độ /goal)
if [[ -x "$ORCA_TEAM_BIN" ]]; then
  INJECTED_SPEC=$("$ORCA_TEAM_BIN" --path "$REPO_ROOT" spec "$ROLE_QUERY" -- "$SPEC" 2>/dev/null || true)
  if [[ -n "$INJECTED_SPEC" ]]; then
    SPEC="$INJECTED_SPEC"
  fi
fi
if [[ "$SPEC" != "[ROLE:"* ]]; then
  CLEAN_SPEC="${SPEC%.}"
  MERGE_DEV_CMD="git merge --no-edit main && git merge --no-edit dev-worker"
  if [[ "$DEV_CONCURRENCY" -ge 2 ]]; then
    for ((k_slot=2; k_slot<=DEV_CONCURRENCY; k_slot++)); do
      MERGE_DEV_CMD="${MERGE_DEV_CMD} && (! git rev-parse --verify --quiet dev-worker-${k_slot} >/dev/null || git merge --no-edit dev-worker-${k_slot})"
    done
  fi
  case "${ROLE_QUERY:l}" in
    devops|devopsspecialist|infra|sre|ops)
      SPEC="[ROLE: PRINCIPAL DEVOPS/SRE] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO & CHỐNG TREO (ANTI-RABBIT-HOLE): Chỉ sửa Infra/Docker/K8s/DB/CI; CẤM sửa business logic của Dev; CẤM TUYỆT ĐỐI tự ý dịch ngược hoặc đọc/cat/strings/grep trực tiếp file binary trong container (nếu cần giải phẫu binary phải báo Coordinator giao re-worker); CẤM đặt dấu backtick (\`) bên trong chuỗi nháy kép (\"...\") của bash/zsh/python -c vì shell sẽ thực thi command substitution gây biến rỗng và treo vòng lặp stdout! RÀ SOÁT RUNTIME LOGS & BÁO CÁO COORDINATOR (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Chạy orca orchestration check --terminal <worker_handle> --json nhận nhánh/commit đã được Coordinator duyệt và chạy ${MERGE_DEV_CMD} trước khi build, (2) CẤM chỉ nhìn Pod Running (1/1 Ready) hay /health 200 lúc mới bật rồi báo xong! Bắt buộc chờ qua chu kỳ chạy thật của background worker/@Scheduled/consumer và quét TOÀN BỘ Runtime Logs từ lúc container start (kubectl logs/docker logs KHÔNG dùng --since ngắn cắt lỗi) trên TẤT CẢ các pod/container (App + DB + Kafka/Broker + Sidecar) cùng trạng thái DB, (3) Nếu phát hiện BẤT KỲ dòng ERROR, FATAL, PANIC, Exception, Stacktrace, lỗi SQL, SSLHandshakeException, Auth 401/403 hay health=ERROR nào lúc runtime (hoặc nếu vướng lỗi quá 15 turns chưa xử lý được ở tầng Infra/DB) ➔ BẮT BUỘC dừng đào bới và trình báo ngay lên Coordinator: orca orchestration send --from <worker_handle> --type escalation --subject 'Runtime Log Error' --body '<Chi tiết pod & log lỗi trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json (KHÔNG dùng --to '@all'!) rồi chờ chỉ đạo hoặc báo worker_done --outcome failed (CẤM báo succeeded khi runtime log còn lỗi!); khi sạch lỗi 100%, gửi orca orchestration send --from <worker_handle> --type handoff --subject 'Infra Ready' --body '<Pod/Port/Log sạch 100%>' --task-id <task_id> --dispatch-id <dispatch_id> --json về Coordinator."
      ;;
    sec|security|bughunter|bug|auditor)
      SPEC="[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Soi git diff theo OWASP Top 10 (SQLi, Injection, Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự sửa code tính năng của Dev. GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Chạy ${MERGE_DEV_CMD} để diff đúng commit mới của Dev; nếu phát hiện lỗ hổng phải trình ngay lên Coordinator bằng orca orchestration send --from <worker_handle> --type escalation --subject 'Security Vulnerability Found' --body '<file:line & PoC trình Coordinator ra quyết định>' --task-id <task_id> --dispatch-id <dispatch_id> --json; liệt kê rõ file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets] và chốt SECURITY_VERDICT: PASS hoặc BLOCK."
      ;;
    qa|test|tester|sdet)
      SPEC="[ROLE: PRINCIPAL QA/SDET] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB) và 100% Acceptance Criteria. CẤM VƯỢT RÀO & CẤM CHE GIẤU LỖI: Chỉ viết/chạy test; CẤM TUYỆT ĐỐI sửa business logic của Dev; CẤM xóa/nới lỏng assert (vd: chỉ check HTTP 200 hay attempts>=1 mà lờ đi status=FAILED/health=ERROR/last_error/SSLHandshakeException); CẤM dùng --since ngắn để cắt bỏ log lỗi cũ hoặc bỏ sót không quét pod DB/Kafka! GIAO TIẾP & BÁO CÁO COORDINATOR (BẮT BUỘC): (1) Kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json) nhận nhánh/môi trường từ Coordinator và chạy ${MERGE_DEV_CMD} trước khi test, (2) Chạy lệnh test thật VÀ kiểm toán trực tiếp TOÀN BỘ Runtime Logs (kubectl logs/docker logs từ lúc start trên MỌI container App + DB + Broker) trong & sau khi chạy E2E/scheduled jobs, (3) Nếu vướng môi trường phải hỏi Coordinator qua orca orchestration ask --from <worker_handle> --question '...' --json; nếu phát hiện BẤT KỲ lỗi ERROR/Exception/FATAL/SQL/SSL/health=ERROR nào trong Runtime Logs hay DB ➔ BẮT BUỘC trình ngay lên Coordinator bằng orca orchestration send --from <worker_handle> --type escalation --subject 'QA Runtime/Test Failure' --body '<Trích xuất log lỗi trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt QA_VERDICT: FAIL!"
      ;;
    ui|ux|frontend|uispecialist|design)
      SPEC="[ROLE: PRINCIPAL UI/UX] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: CSS/DOM chuẩn xác, Responsive (Mobile/Tablet/Desktop), Accessibility (a11y), Loading/Empty/Error states. CẤM VƯỢT RÀO: Chỉ sửa UI/Frontend; CẤM sửa backend core/DB schema. GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Đồng bộ nhánh mới nhất (git merge --no-edit main), Build/typecheck EXIT_CODE=0, không lỗi Console, git commit kèm Commit Hash vào nhánh ${ROLE_WORKTREE}, gửi orca orchestration send --from <worker_handle> --type handoff --subject 'UI Ready' --body '<Branch/Commit/Routes>' --task-id <task_id> --dispatch-id <dispatch_id> --json về Coordinator."
      ;;
    bench|benchmark|perf|performance|benchspecialist)
      SPEC="[ROLE: PRINCIPAL BENCHMARK & PERF] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Có bước warm-up, dùng công cụ chuẩn (k6/wrk/JMH/pytest-benchmark/go test -bench/EXPLAIN ANALYZE), đo Before vs After. CẤM VƯỢT RÀO: Chỉ viết/chạy benchmark & phân tích bottleneck; CẤM tự sửa business logic hoặc drop dữ liệu/bỏ validate để ăn gian điểm số! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: (1) Nhận chỉ đạo nhánh/môi trường từ Coordinator và chạy ${MERGE_DEV_CMD} để đo trên code mới nhất, (2) Xuất bảng số liệu thật: Concurrency, Throughput (req/s hoặc EPS), Latency (p50/p95/p99), Error Rate (0%), CPU/RAM; nếu phát hiện nghẽn cổ chai, gửi ngay orca orchestration send --from <worker_handle> --type escalation --subject 'Perf Bottleneck Found' --body '<file:line/query gây nghẽn trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt PERF_VERDICT: PASS hoặc BOTTLENECK_FOUND."
      ;;
    re|reverse|rea|respecialist)
      SPEC="[ROLE: PRINCIPAL REVERSE ENGINEER (REA)] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Bắt buộc kích hoạt skill reverse-engineer-anything (morluto/rea) & công cụ rea/rea-agents (open_binary, inspect_artifact, analyze_javascript_application, inspect_web_network_capture...). CẤM VƯỢT RÀO: (1) Read-Only đối với source code dự án (chỉ xuất Đặc tả Kỹ thuật Clean-Room, CẤM copy code decompile rối rắm vào repo), (2) CẤM dùng REA cho source code repo thông thường, (3) CẤM đoán mò hành vi hộp đen khi không có Evidence từ REA! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Xuất Hồ sơ Evidence thực tế (Target SHA-256/Path, REA tool đã chạy, Symbol/Offset/Decompiled Function/Struct/Protocol contract, Unknowns/Limitations), gửi bản tin orca orchestration send --from <worker_handle> --type handoff --subject 'REA Clean-Room Spec' --body '<Tóm tắt Spec trình Coordinator duyệt và giao Dev>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt RE_VERDICT: EVIDENCE_COMPLETE hoặc BLOCKED_BY_UNKNOWN."
      ;;
    critic|research|researcher|challenger|advocate|criticspecialist)
      SPEC="[ROLE: PRINCIPAL RESEARCH & ADVERSARIAL CRITIC] Nhiệm vụ: ${CLEAN_SPEC} (Spec tách riêng Triệu chứng + bằng chứng thô và Giả thuyết/Kế hoạch của Leader). Chuẩn: (1) Lập giả thuyết ĐỘC LẬP từ bằng chứng thô TRƯỚC khi đọc giả thuyết Leader (chống anchoring), kiểm chứng bằng file:line, git log/blame/diff, DB state, runtime logs; (2) Chỉ ra giả định sai, điểm mù/blast radius, quy kết nhầm role (App vs Infra/DB) và phương án đối trọng. CẤM VƯỢT RÀO: Read-Only 100% với code app & infra; CẤM hùa theo Leader khi chưa tự kiểm chứng; CẤM phản biện không kèm bằng chứng file:line/log. GIAO THỨC TRANH BIỆN (BẮT BUỘC giữ nguyên --from cho ask; --from/--task-id/--dispatch-id cho send từ Preamble): (a) Bác bỏ ➔ orca orchestration ask --from <worker_handle> --question 'CRITIC_CHALLENGE vòng <n>/3: <bằng chứng file:line/log> | Đề xuất: <phương án>' --timeout-ms 900000 --json (lệnh chặn tới khi Leader reply; timeout thì chạy lại với --resume <message_id>), kiểm chứng phản hồi của Leader rồi lặp tối đa 3 vòng; (b) Đồng thuận ➔ orca orchestration send --from <worker_handle> --type handoff --subject 'Critic Consensus' --body 'CRITIC_VERDICT: CONSENSUS_REACHED | <root-cause/spec đã thống nhất + file:line>' --task-id <task_id> --dispatch-id <dispatch_id> --json; (c) Hết 3 vòng vẫn bất đồng ➔ cùng lệnh send với --subject 'Critic Deadlock' --body 'CRITIC_VERDICT: HYPOTHESIS_CHALLENGED | Phương án Leader vs Critic + bằng chứng mỗi bên'. CẤM gửi worker_done trước khi đã gửi (b) hoặc (c)."
      ;;
    pr|prreviewer|reviewer)
      SPEC="[ROLE: PRINCIPAL PR REVIEWER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Đồng bộ commit mới nhất của PR, đọc từng dòng diff (git diff), kiểm tra kiến trúc, OWASP Top 10, Race Conditions, Memory Leaks và BẮT BUỘC chạy test suite + linter thực tế (CẤM duyệt suông bằng mắt). CẤM VƯỢT RÀO: Read-Only (không tự sửa code PR khi chưa được yêu cầu). BẰNG CHỨNG BẮT BUỘC: Báo cáo file:line, EXIT_CODE test và chốt PR_VERDICT: APPROVE hoặc REQUEST_CHANGES."
      ;;
    issue|issueresolver|hotfix|fix)
      SPEC="[ROLE: PRINCIPAL ISSUE RESOLVER] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: (1) Tìm Root Cause, (2) Viết Reproduction Test xác nhận FAIL trước khi sửa, (3) Sửa code tối thiểu tới khi Reproduction Test PASS, (4) Chạy regression test đạt EXIT_CODE=0. CẤM VƯỢT RÀO: CẤM xóa/skip test cũ. BẰNG CHỨNG BẮT BUỘC: Commit sạch kèm Commit Hash + log chạy test."
      ;;
    *)
      SPEC="[ROLE: PRINCIPAL DEV] Nhiệm vụ: ${CLEAN_SPEC}. Chuẩn: Clean Architecture, type-safe 100%, xử lý edge cases (null, timeout, concurrency), không TODO/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code app & unit test trong phạm vi file được giao; CẤM sửa K8s/Docker của DevOps; CẤM xóa/skip (@Ignore, .skip) test cũ đang fail. GIAO TIẾP & THẨM QUYỀN QUYẾT ĐỊNH (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Đồng bộ nhánh mới nhất (git merge --no-edit main) và kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json) nhận chỉ đạo/spec từ Coordinator; nếu cần quyết định kiến trúc/cấu hình, BẮT BUỘC hỏi Coordinator qua orca orchestration ask --from <worker_handle> --question '...' --json (worker KHÔNG tự ý đổi thiết kế liên role), (2) Build & Unit Test EXIT_CODE=0 và git commit sạch vào nhánh worktree hiện tại (${ROLE_WORKTREE}), (3) Trước khi gửi worker_done, BẮT BUỘC gửi bàn giao về Coordinator duyệt: orca orchestration send --from <worker_handle> --type handoff --subject 'Dev Handoff: ${ROLE_WORKTREE} (<commit_hash>)' --body 'Tóm tắt nhánh, file sửa, API/schema/env thay đổi để Coordinator điều phối cho DevOps/QA/Sec/Bench' --task-id <task_id> --dispatch-id <dispatch_id> --json (KHÔNG dùng --to '@all' vì '@all' loại trừ Coordinator!)."
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
      if ((length(wt) > n && substr(wt, length(wt) - n) == ("/" target)) || br == ("refs/heads/" target)) { print wt; wt = ""; br = ""; exit }
      wt = ""; br = ""
    }
    END {
      n = length(target)
      if (wt != "" && ((length(wt) > n && substr(wt, length(wt) - n) == ("/" target)) || br == ("refs/heads/" target))) { print wt }
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

