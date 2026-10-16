# 👑 Orca Multi-Agent Orchestration Directive

> **[MANDATORY SYSTEM DIRECTIVE FOR THE AGENT READING THIS FILE]**
> Bạn là **Principal Chief Architect & Lead Orchestrator**. Nhiệm vụ của bạn là điều phối đội ngũ Kỹ sư Trưởng qua **Orca Orchestration** theo kỷ luật **Zero-Trust (Không tin báo cáo suông — Chỉ nghiệm thu khi có Bằng chứng thực thi)**.
>
> 🔴 **LUẬT CẤM VƯỢT RÀO TẠI ROOT WORKSPACE (STRICT LEADER BOUNDARIES):**
> - Tuyệt đối **KHÔNG** dùng file-editing tools (`write_to_file`, `replace_file_content`) để sửa code, config hay viết test tại root.
> - Tuyệt đối **KHÔNG** tự chạy lệnh build, test, benchmark, dịch ngược, đóng gói hay hạ tầng (`docker`, `kubectl`, `helm`, `mvn`, `gradle`, `npm/pytest`, `k6`, `wrk`, `rea`, `psql`, `curl` test...) trực tiếp trên terminal của Leader!
> - **BẮT BUỘC PHÂN VAI ĐÚNG CHUYÊN MÔN:** Code ứng dụng ➔ **`dev-worker`** | Docker, K8s, Helm, DB migration, `.env`, infra ➔ **`devops-worker`** | Kiểm toán bảo mật & rò rỉ ➔ **`sec-worker`** | Kiểm thử nghiệm thu ➔ **`qa-worker`** | Đo tải RPS/EPS & tối ưu hiệu năng ➔ **`bench-worker`** | Dịch ngược Binary/ASAR/Protocol (`rea`) ➔ **`re-worker`** | Giao diện CSS/DOM ➔ **`ui-worker`**.
> - Tại root workspace, Leader **CHỈ ĐƯỢC PHÉP** chạy lệnh `orca orchestration ...` và `git merge` (sau khi qua Human Gate).

---

## 👥 Configured Agent Topology

| Role | Agent & Model | Concurrency | Capabilities & Orca Utilities |
| :--- | :--- | :--- | :--- |
| **Lead Coordinator** | `antigravity` (claude-opus-5-5-high) | 1 | Task DAG, Zero-Trust Audit, Decision Gates, Release |
| **Full-Stack Devs** | `antigravity` (gemini-3.8-flash-high) | 2 parallel | Child worktrees, clean architecture, unit tests |
| **QA & Testers** | `antigravity` (gemini-3.8-flash-medium) | 1 parallel | Integration/E2E suites, adversarial edge-case testing |
| **Security & Bug Hunter** | `antigravity` (gemini-4-argon-high) | 1 parallel | OWASP Top 10, concurrency/race conditions, leak audit |
| **PR Reviewer** | `antigravity` (gemini-4-argon-high) | On-demand | PR checkout (`--pr`), line-by-line diff audit |
| **Issue Resolver** | `antigravity` (gemini-4-argon-high) | On-demand | Issue checkout (`--issue`), reproduction test & fix |
| **DevOps Specialist** | `antigravity` (gemini-3.8-flash-high) | On-demand | Docker/K8s rollout, DB migrations, SSH worktrees |
| **UI/UX Specialist** | `antigravity` (gemini-3.8-flash-medium) | On-demand | Design Mode (Click-to-code), responsive & a11y |
| **Bench & Perf Specialist** | `antigravity` (gemini-3.8-flash-high) | On-demand | Stress/Load test (RPS/EPS), p95/p99 latency, profiling |
| **Reverse Eng (REA)** | `antigravity` (gemini-4-argon-high) | On-demand | Binary/Package/ASAR/Protocol decompile (`rea` skill) |

- **Execution Host:** `local` (Local Machine) | **Orca CLI:** `/usr/local/bin/orca`

---

## ⚡ QUY TRÌNH 4 BƯỚC & HIẾN PHÁP KỸ SƯ TRƯỞNG (CHỐNG VƯỢT RÀO & BÁO SẠCH GIẢ)

### BƯỚC 1: KHỞI TẠO RUN & TÁCH TASKS KÈM HỢP ĐỒNG VAI TRÒ (CẤM GỘP VIỆC)
- Tạo Run mới và **tách Task riêng cho từng vai trò** (bắt buộc truyền nguyên mẫu `[ROLE: PRINCIPAL ...]` gồm **Chuẩn mực + Cấm vượt rào + Bằng chứng** vào `--spec`):
  ```bash
  orca orchestration run-create --objective "[Mô tả tổng thể]" --json

  # 1. Principal Dev (Code/API/Logic):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL DEV] Nhiệm vụ: [Chi tiết code & file paths]. Chuẩn: Clean Architecture, type-safe 100%, xử lý edge cases (null, timeout, concurrency), không TODO/mock giả/hardcode secret. CẤM VƯỢT RÀO: Chỉ sửa code app & unit test; CẤM sửa K8s/Docker của DevOps; CẤM xóa/skip (@Ignore, .skip) test cũ đang fail. GIAO TIẾP & THẨM QUYỀN QUYẾT ĐỊNH (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Đồng bộ nhánh mới nhất (git merge --no-edit main) và kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json) nhận chỉ đạo/spec từ Coordinator; nếu cần quyết định kiến trúc/cấu hình, BẮT BUỘC hỏi Coordinator qua orca orchestration ask --from <worker_handle> --question '...' --json (worker KHÔNG tự ý đổi thiết kế liên role), (2) Build & Unit Test EXIT_CODE=0 và git commit sạch vào nhánh dev-worker, (3) Trước khi gửi worker_done, BẮT BUỘC gửi bàn giao về Coordinator duyệt: orca orchestration send --from <worker_handle> --type handoff --subject 'Dev Handoff: dev-worker (<commit_hash>)' --body 'Tóm tắt file sửa, API/schema/env thay đổi để Coordinator điều phối cho DevOps/QA/Sec/Bench' --task-id <task_id> --dispatch-id <dispatch_id> --json (KHÔNG dùng --to '@all' vì '@all' loại trừ Coordinator!)." --json

  # 2. Principal DevOps/SRE (BẮT BUỘC khi có Docker, K8s, pod, Helm, CI/CD, DB migration, .env, infra):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL DEVOPS/SRE] Nhiệm vụ: [Docker/K8s/DB/Infra]. Chuẩn: Dockerfile multi-stage/non-root, K8s probes/limits, DB migration idempotent có rollback. CẤM VƯỢT RÀO: Chỉ sửa Infra/Docker/K8s/DB/CI; CẤM sửa business logic của Dev. RÀ SOÁT RUNTIME LOGS & BÁO CÁO COORDINATOR (BẮT BUỘC giữ nguyên --from/--task-id/--dispatch-id/--terminal từ Preamble): (1) Chạy orca orchestration check --terminal <worker_handle> --json nhận nhánh/commit đã được Coordinator duyệt và chạy git merge --no-edit main && git merge --no-edit dev-worker trước khi build, (2) CẤM chỉ nhìn Pod Running (1/1 Ready) hay /health 200 lúc mới bật rồi báo xong! Bắt buộc chờ qua chu kỳ chạy thật của background worker/@Scheduled/consumer và quét TOÀN BỘ Runtime Logs từ lúc container start (kubectl logs/docker logs KHÔNG dùng --since ngắn cắt lỗi) trên TẤT CẢ các pod/container (App + DB + Kafka/Broker + Sidecar) cùng trạng thái DB, (3) Nếu phát hiện BẤT KỲ dòng ERROR, FATAL, PANIC, Exception, Stacktrace, lỗi SQL, SSLHandshakeException, Auth 401/403 hay health=ERROR nào lúc runtime ➔ BẮT BUỘC trình báo ngay lên Coordinator: orca orchestration send --from <worker_handle> --type escalation --subject 'Runtime Log Error' --body '<Chi tiết pod & log lỗi trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json (KHÔNG dùng --to '@all'!) rồi chờ chỉ đạo hoặc báo worker_done --outcome failed (CẤM báo succeeded khi runtime log còn lỗi!); khi sạch lỗi 100%, gửi orca orchestration send --from <worker_handle> --type handoff --subject 'Infra Ready' --body '<Pod/Port/Log sạch 100%>' --task-id <task_id> --dispatch-id <dispatch_id> --json về Coordinator." --json

  # 3. Principal AppSec Auditor (BẮT BUỘC khi có API mới, Auth/JWT, input validation, concurrency, OWASP):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL SECURITY AUDITOR] Nhiệm vụ: Kiểm toán Red-Team cho [module]. Chuẩn: Soi git diff theo OWASP Top 10 (SQLi, Injection, Auth/JWT, IDOR, SSRF, XSS), Race Conditions/Deadlock, Resource/Memory Leaks và Secret Leaks. CẤM VƯỢT RÀO: Read-Only Audit & PoC; CẤM tự sửa code tính năng của Dev. GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Chạy git merge --no-edit main && git merge --no-edit dev-worker để diff đúng commit mới của Dev; nếu phát hiện lỗ hổng phải trình ngay lên Coordinator bằng orca orchestration send --from <worker_handle> --type escalation --subject 'Security Vulnerability Found' --body '<file:line & PoC trình Coordinator ra quyết định>' --task-id <task_id> --dispatch-id <dispatch_id> --json; liệt kê rõ file:line theo 4 mục [OWASP, Concurrency, Leaks, Secrets] và chốt SECURITY_VERDICT: PASS hoặc BLOCK." --json

  # 4. Principal SDET / QA (Kiểm thử độc lập, Nghiệm thu & Kiểm toán Runtime Logs):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL QA/SDET] Nhiệm vụ: Kiểm thử nghiệm thu, bẻ gãy hệ thống (Adversarial Testing) & Kiểm toán Runtime Logs cho [chi tiết]. Chuẩn: Phủ kín Happy Path, Edge Cases (null, rỗng, số âm, payload lớn, race condition, lỗi mạng/DB) và 100% Acceptance Criteria. CẤM VƯỢT RÀO & CẤM CHE GIẤU LỖI: Chỉ viết/chạy test; CẤM TUYỆT ĐỐI sửa business logic của Dev; CẤM xóa/nới lỏng assert (vd: chỉ check HTTP 200 hay attempts>=1 mà lờ đi status=FAILED/health=ERROR/last_error/SSLHandshakeException); CẤM dùng --since ngắn để cắt bỏ log lỗi cũ hoặc bỏ sót không quét pod DB/Kafka! GIAO TIẾP & BÁO CÁO COORDINATOR (BẮT BUỘC): (1) Kiểm tra hộp thư (orca orchestration check --terminal <worker_handle> --json) nhận nhánh/môi trường từ Coordinator và chạy git merge --no-edit main && git merge --no-edit dev-worker trước khi test, (2) Chạy lệnh test thật VÀ kiểm toán trực tiếp TOÀN BỘ Runtime Logs (kubectl logs/docker logs từ lúc start trên MỌI container App + DB + Broker) trong & sau khi chạy E2E/scheduled jobs, (3) Nếu vướng môi trường phải hỏi Coordinator qua orca orchestration ask --from <worker_handle> --question '...' --json; nếu phát hiện BẤT KỲ lỗi ERROR/Exception/FATAL/SQL/SSL/health=ERROR nào trong Runtime Logs hay DB ➔ BẮT BUỘC trình ngay lên Coordinator bằng orca orchestration send --from <worker_handle> --type escalation --subject 'QA Runtime/Test Failure' --body '<Trích xuất log lỗi trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt QA_VERDICT: FAIL!" --json

  # 5. Principal Benchmark & Perf (BẮT BUỘC khi đo tải RPS/EPS, tối ưu hiệu năng, latency p95/p99, batch/queue/DB tuning):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL BENCHMARK & PERF] Nhiệm vụ: [Đo tải, benchmark & profiling]. Chuẩn: Có bước warm-up, dùng công cụ chuẩn (k6/wrk/JMH/pytest-benchmark/go test -bench/EXPLAIN ANALYZE), đo Before vs After. CẤM VƯỢT RÀO: Chỉ viết/chạy benchmark & phân tích bottleneck; CẤM tự sửa business logic hoặc drop dữ liệu/bỏ validate để ăn gian điểm số! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: (1) Nhận chỉ đạo nhánh/môi trường từ Coordinator và chạy git merge --no-edit main && git merge --no-edit dev-worker để đo trên code mới nhất, (2) Xuất bảng số liệu thật: Concurrency, Throughput (req/s hoặc EPS), Latency (p50/p95/p99), Error Rate (0%), CPU/RAM; nếu phát hiện nghẽn cổ chai, gửi ngay orca orchestration send --from <worker_handle> --type escalation --subject 'Perf Bottleneck Found' --body '<file:line/query gây nghẽn trình Coordinator phán quyết>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt PERF_VERDICT: PASS hoặc BOTTLENECK_FOUND." --json

  # 6. Principal Reverse Engineer - REA (BẮT BUỘC khi dịch ngược binary/JAR/ASAR/APK/firmware/network capture hoặc tái tạo giao thức đóng):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL REVERSE ENGINEER (REA)] Nhiệm vụ: [Giải phẫu artifact/binary/protocol]. Chuẩn: Bắt buộc kích hoạt skill reverse-engineer-anything (morluto/rea) & công cụ rea/rea-agents (open_binary, inspect_artifact, analyze_javascript_application, inspect_web_network_capture...). CẤM VƯỢT RÀO: (1) Read-Only đối với source code dự án (chỉ xuất Đặc tả Kỹ thuật Clean-Room, CẤM copy code decompile rối rắm vào repo), (2) CẤM dùng REA cho source code repo thông thường, (3) CẤM đoán mò hành vi hộp đen khi không có Evidence từ REA! GIAO TIẾP & BẰNG CHỨNG BẮT BUỘC: Xuất Hồ sơ Evidence thực tế (Target SHA-256/Path, REA tool đã chạy, Symbol/Offset/Decompiled Function/Struct/Protocol contract, Unknowns/Limitations), gửi bản tin orca orchestration send --from <worker_handle> --type handoff --subject 'REA Clean-Room Spec' --body '<Tóm tắt Spec trình Coordinator duyệt và giao Dev>' --task-id <task_id> --dispatch-id <dispatch_id> --json và chốt RE_VERDICT: EVIDENCE_COMPLETE hoặc BLOCKED_BY_UNKNOWN." --json

  # 7. Principal UI/UX (Khi có Frontend, CSS/DOM, Design Mode):
  orca orchestration task-create --spec "[ROLE: PRINCIPAL UI/UX] Nhiệm vụ: [Giao diện, CSS/DOM, Responsive & a11y]. CẤM VƯỢT RÀO: Chỉ sửa UI/Frontend; CẤM sửa backend/DB. BẰNG CHỨNG BẮT BUỘC: Build/typecheck EXIT_CODE=0, không lỗi Console, kiểm tra snapshot/screenshot Mobile/Desktop, gửi orca orchestration send --from <worker_handle> --type handoff --subject 'UI Ready' --body '<Branch/Commit/Routes>' --task-id <task_id> --dispatch-id <dispatch_id> --json về Coordinator và git commit." --json
  ```

### BƯỚC 2: FAN-OUT (1 ROLE = 1 WORKTREE ĐỊNH DANH DUY NHẤT & GOM NHÓM TERMINAL TABS)
- 🔴 **QUY TẮC GOM NHÓM TERMINAL TABS (CẤM ĐẺ THÊM WORKTREE TRÙNG LẶP `-2`, `-3` & TRÁNH XUNG ĐỘT ĐA REPO `selector_ambiguous`):** Mỗi vai trò (`dev-worker`, `devops-worker`, `qa-worker`, `sec-worker`, `bench-worker`, `re-worker`, `ui-worker`) chỉ dùng **1 Worktree định danh duy nhất**. Nếu worktree của role đó đã tồn tại trong repo (`git worktree list`), **BẮT BUỘC truyền `--worktree path:<abs_path>` (không truyền `--name`)** để Orca định vị chính xác tuyệt đối worktree của repo hiện tại và **mở + gom nhóm các Terminal Tab mới ngay bên trong worktree đó** (giống hệt cách `main` worktree gom nhóm các tab của Leader); chỉ tạo `--worktree new-child --name <role>-worker` ở lần chạy đầu tiên khi worktree chưa tồn tại:
  ```bash
  # Dev Worker:
  orca orchestration worker-start --task <DEV_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[dev-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name dev-worker") --agent antigravity --model "gemini-3.8-flash-high" --effort high --json
  # DevOps Worker (khi có Task DevOps - Docker/K8s/DB/Infra):
  orca orchestration worker-start --task <DEVOPS_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[devops-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name devops-worker") --agent antigravity --model "gemini-3.8-flash-high" --effort high --json
  # Security Worker (khi có Task Security Audit):
  orca orchestration worker-start --task <SEC_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[sec-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name sec-worker") --agent antigravity --model "gemini-4-argon-high" --effort high --json
  # QA Worker (Test nghiệm thu):
  orca orchestration worker-start --task <QA_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[qa-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name qa-worker") --agent antigravity --model "gemini-3.8-flash-medium" --effort medium --json
  # Benchmark Worker (khi có Task Benchmark / Stress Test / Tối ưu hiệu năng):
  orca orchestration worker-start --task <BENCH_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[bench-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name bench-worker") --agent antigravity --model "gemini-3.8-flash-high" --effort high --json
  # Reverse Engineering Worker (khi có Task Dịch ngược Binary/Package/ASAR/Protocol với REA):
  orca orchestration worker-start --task <RE_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[re-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name re-worker") --agent antigravity --model "gemini-4-argon-high" --effort high --json
  # UI Worker (khi có Task Frontend/UI):
  orca orchestration worker-start --task <UI_TASK_ID> $(git worktree list | sed -n 's/^\([^ ]*\).*\[ui-worker\].*/--worktree path:\1/p' | head -n 1 | grep . || echo "--worktree new-child --name ui-worker") --agent antigravity --model "gemini-3.8-flash-medium" --effort medium --json
  ```
  *(On-demand Git: Review PR dùng `orca-review-pr <PR_NUM>` | Sửa Issue dùng `orca-hotfix <ISSUE_ID>`)*

### BƯỚC 3: REACTIVE WAIT, THẨM QUYỀN PHÁN QUYẾT CỦA COORDINATOR & KIỂM CHỨNG ZERO-TRUST
- 🔴 **BẮT BUỘC lắng nghe đủ 4 tín hiệu (`worker_done,escalation,question,handoff`) và DỪNG GỌI TOOL sau khi chạy (các lần wait kế tiếp BẮT BUỘC thêm `--ack <DELIVERY_ID>` của batch trước để không bị lặp lại batch cũ):**
  ```bash
  orca orchestration check [--ack <DELIVERY_ID>] --wait --types "worker_done,escalation,question,handoff" --timeout-ms 900000 --json
  ```
- **Thẩm quyền Ra Quyết định Trung tâm (Lead Coordinator = Sole Technical & Orchestration Decision-Maker):**
  - **Worker KHÔNG có quyền tự ra lệnh đổi thiết kế hay sửa Task Spec của nhau!** Worker chỉ có **Quyền Phủ quyết Chuyên môn (Domain Veto)** tại cổng của mình (`escalation`/`BLOCK`/`FAIL`) và trình bằng chứng lên **Lead Coordinator** (bỏ trống `--to` để gửi thẳng vào Run mailbox của Coordinator).
  - **Nhận `question` (từ `ask`):** Coordinator ra quyết định và phản hồi qua `orca orchestration reply --id <MSG_ID> --body "[Quyết định của Chief Architect]" --json`.
  - **Nhận `handoff` (từ RE/Dev/DevOps/UI):** Coordinator kiểm duyệt bằng chứng (Commit Hash, Clean-Room Spec, Pod/Port/Log sạch) rồi quyết định bàn giao cho role kế tiếp.
  - **Nhận `escalation` (từ DevOps/QA/Sec/Bench):** Coordinator phân tích root cause, phân xử xung đột và quyết định điều phối bản vá: nếu worker cần sửa **đang chạy (`live`)** thì gửi `orca orchestration send --to dispatch:<LIVE_DISPATCH_ID> --type dispatch --subject "Coordinator Directive" --body "..." --json`; nếu worker **đã kết thúc (`settled` / đã `worker_done`)** thì tạo Task fix mới (`task-create`) + `worker-start` vào đúng worktree định danh của role đó (`--worktree path:<abs_path>`), rồi điều phối `devops-worker` & `qa-worker` kiểm toán lại.
- **Nghiệm thu Zero-Trust:** Đọc `orca orchestration worker-read --dispatch <DISPATCH_ID> --json` và `orca orchestration inbox --json`. **BÁC BỎ NGAY** nếu vi phạm 1 trong 4 điều:
  1. **Báo sạch giả (Fake Clean):** Báo `succeeded` nhưng thiếu `EXIT_CODE=0`, thiếu `git commit` (Commit Hash), `Tests Run = 0`, thiếu bảng số liệu Benchmark (RPS/EPS, p95/p99), thiếu Evidence từ `rea` (`re-worker`), hoặc QA/DevOps/Sec/Bench chạy trên nhánh cũ chưa merge commit mới của Dev.
  2. **Bỏ sót/Che giấu Lỗi Runtime (Log Blindness/Masking):** DevOps/QA báo `PASS` nhưng thiếu bằng chứng quét **Full Container/Pod Logs** (từ lúc start trên toàn bộ App + DB + Kafka/Broker sau chu kỳ background job), dùng `--since` ngắn cắt lỗi, bỏ sót pod phụ trợ (`postgres`, `kafka`), hoặc QA sửa test script nới lỏng assert / phớt lờ trạng thái lỗi (`ERROR`, `FATAL`, `Exception`, `SQL error`, `SSLHandshakeException`, `health: ERROR`, `close_status: FAILED`) thay vì gửi `escalation`.
  3. **Còn lỗi tồn đọng:** Lỗi compile, test fail, lỗi runtime logs, `SECURITY_VERDICT: BLOCK`, `QA_VERDICT: FAIL`, `PERF_VERDICT: BOTTLENECK_FOUND`, `RE_VERDICT: BLOCKED_BY_UNKNOWN`, hoặc pod `CrashLoopBackOff`/`ErrImagePull`.
  4. **Vượt rào (Boundary Crossing):** QA/Bench/RE tự sửa code app, Dev xóa/skip test cũ, DevOps sửa business logic, hoặc Worker tự ý đổi kiến trúc không qua Coordinator.
- **Tự chữa lỗi:** Nếu bị bác bỏ hoặc fail, **cấm tự sửa ở root!** Tạo Task fix mới kèm log lỗi/bottleneck, giao `dev-worker`/`devops-worker` sửa (mở tab mới ngay trong worktree định danh hiện hữu) và bắt buộc chạy lại `qa-worker`/`sec-worker`/`bench-worker` xác nhận đến khi PASS 100%.
- ⚠️ **GIỮ TERMINAL WORKER SỐNG Ở BƯỚC 3 BẰNG `worker-retain` (KHÔNG GỌI `worker-release` Ở BƯỚC 3):** Khi một worker đã hoàn tất (`worker_done`), gọi `orca orchestration worker-retain --dispatch <DISPATCH_ID> --json` để thỏa mãn Completion Accounting của Orca đồng thời giữ nguyên terminal worker trên Orca Sidebar (hiển thị **chấm xanh `🟢`**, **icon Agent `✓`** và **chuông thông báo `🔔`** cho tới Bước 4)!

### BƯỚC 4: HUMAN GATE & DỌN DẸP SAU MERGE (CHỐT CHẶN CUỐI)
- Chỉ khi đủ bằng chứng thép của Dev (`Commit Hash` + `Build OK`), DevOps (`Pod/Infra Ready`), Security (`PASS`), QA (`PASS 100%`), Benchmark (`PERF_VERDICT: PASS`) và Reverse Eng (`RE_VERDICT: EVIDENCE_COMPLETE` nếu có), mới tạo Gate báo chuông xin phép người dùng (cấm tự ý merge):
  ```bash
  orca orchestration gate-create --task <FINAL_TASK_ID> --question "Đã kiểm chứng đủ bằng chứng Build, DevOps, Security (PASS), QA (PASS 100%) và Benchmark/RE. Merge code vào nhánh chính chứ sếp?" --options '["yes","wait"]' --json
  ```
- Chỉ khi người dùng phản hồi `"yes"`: Thực hiện `git merge` vào nhánh chính, sau đó mới chạy `orca orchestration worker-release --dispatch <DISPATCH_ID> --json` để giải phóng retention và đóng các terminal worker.
