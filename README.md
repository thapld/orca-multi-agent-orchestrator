# 🐋 Orca Multi-Agent Orchestration Framework

Hệ thống điều phối kỹ thuật phần mềm đa agent (Multi-Agent Engineering Orchestrator) toàn diện xây dựng trên nền tảng **Orca Agent Development Environment (ADE)**.

Framework này cho phép biến bất kỳ kho mã nguồn (repository) nào thành một trung tâm kỹ thuật tự trị, nơi các agent AI (`claude`, `codex`, `kimi`, `cursor`, `antigravity`, `opencode`...) cùng làm việc nhịp nhàng theo các vai trò chuyên biệt, cô lập bằng **Git Worktrees**, kiểm định chất lượng tự động, và xử lý đa luồng song song.

---

## 🌟 Tính Năng Nổi Bật

1. **Linh Hoạt Đa Agent (Agnostic Multi-Agent Support — 11 Vai Trò Cấp Principal):**
   * Tự do lựa chọn bất kỳ model/agent nào Orca hỗ trợ cho từng vị trí: **Lead Coordinator**, **Research & Adversarial Critic (`critic-worker`)**, **Full-Stack Devs**, **QA & SDET**, **Security & Bug Hunter**, **PR Reviewer**, **Git Issue Resolver**, **UI/UX Specialist**, **DevOps & SRE**, **Bench & Perf Specialist** và **Reverse Eng Specialist (REA)**.
2. **Cơ Chế Phản Biện & Đồng Thuận Biện Chứng Chống Leader Độc Đoán (Anti-Autocracy & Dialectical Consensus):**
   * Vai trò **Principal Research & Adversarial Critic (`critic-worker`)** điều tra độc lập (`file:line` + git blame + runtime log, lập giả thuyết riêng `hypothesis.json` trước khi đọc giả thuyết Leader) và tranh biện 2 chiều với Leader qua `orca orchestration ask`/`reply` (tối đa 3 vòng). Bắt buộc khi gặp 1 trong 4 trigger: bug tái phát sau fix, escalation quy lỗi liên role/liên tầng, thay đổi schema/API/auth/payment hoặc ≥2 tầng, worker báo cáo mâu thuẫn. Bế tắc (`DEADLOCK`) ➔ `orca-team gate create --from-consensus <run>` mở Human Gate trình cả 2 phương án, Leader không được tự phán quyết.
   * **Cổng Critic Consensus Gate (`orca_critic_gate.py` + `orca-command-guard.py`):** Ràng buộc mật mã `planHash` (`SHA-256` của bản chuẩn hoá `plan-draft.md`), kiểm tra 100% coverage `[P1..Pn]` kèm `evidence` (`path:line`), kiểm tra 5 trục phản biện (`architecture`, `contracts`, `failure_modes`, `security`, `testability`), bắt buộc quota `objections` theo `riskLevel` (`low`/`medium`/`high`), đối chiếu `resolutions[].leaderReplyId` trực tiếp với bảng `messages` & `question_threads` trong SQLite DB khi chiếu `orca-team plan consensus` và khi chạy `orca-team plan pin` / `worker-start`, đồng thời khóa Read-Only với `critic-worker`.
   * *Lưu ý trung thực về giới hạn (Honest Limitations):*
     1. **Không phát hiện thông đồng ngữ nghĩa (Semantic Collusion):** Nếu Critic bịa ra một `objection` nghe có vẻ hợp lý nhưng vô thưởng vô phạt, kèm `path:line` có thật, và Leader trả lời dài `≥ 80` ký tự, cổng máy không thể phân biệt với một phản biện sâu thật sự. Cổng chỉ nâng chi phí gian lận từ *"1 dòng chữ `CONSENSUS_REACHED`"* lên *"phải đọc file thật, trích dòng thật, dựng chuỗi `ask`/`reply` thật trong DB, và giữ nguyên plan sau khi ký"*.
     2. **Cùng OS user (`same-UID`):** Vì các tiến trình chạy cùng UID, một agent cố tình sửa trực tiếp SQLite DB hoặc đặt biến môi trường `ORCA_CRITIC_GATE_MODE=off` trong shell con về mặt lý thuyết có thể vượt qua nếu không có kiểm soát lệnh shell. Guard hiện chặn việc truyền `ORCA_CRITIC_GATE_MODE=off` / `ORCA_SKIP_CRITIC=1` từ bash tool call của agent và đối chiếu `consensus.json` với `messages` trong DB.
     3. **Tự chấm điểm `axes[].score`:** Điểm số 1–5 do chính Critic tự điền; máy chỉ ép phải có ghi chú (`notes ≥ 20` ký tự) và không có điểm `< 3` khi đóng đồng thuận.
3. **Tự Động Co Giãn Worktree & Agent Song Song (Dynamic Elastic Worktree Scaling):**
   * Bỏ hoàn toàn việc chọn cứng số lượng agent khi cài đặt. Khi có task mới cho bất kỳ vai trò nào (`dev`, `qa`, `sec`, `devops`, `bench`, `re`, `ui`, `critic`), lệnh `orca-team worktree-arg <role>` tự động kiểm tra trạng thái toàn cục (`orca worktree ps` + `worker-list` + khóa đặt chỗ atomic):
   * **Khi rảnh (`done` / `settled` / `released`):** Tái sử dụng ngay worktree hiện có (`--worktree path:<abs_path>`) và gom nhóm Terminal Tab mới bên trong đó.
   * **Khi bận (`working` / `live` ở bất kỳ Leader/Run nào):** Tự động đẻ thêm hoặc chọn worktree slot kế tiếp (`<role>-worker-2`, `<role>-worker-3`...) hoàn toàn cô lập, ngăn chặn 100% rủi ro 2 agent/2 Leader giẫm chân lên cùng một worktree hoặc đụng `git index.lock`.
4. **Khai Thác 100% Tiện Ích Độc Quyền Của Orca & REA:**
   * **Elastic Git Worktrees & Terminal Tab Grouping:** Tái sử dụng worktree định danh khi rảnh để gom nhóm Terminal Tabs gọn gàng (`--worktree path:<abs_path>`), đồng thời tự động mở rộng sang `<role>-worker-2`, `<role>-worker-3`... khi chạy đa luồng hoặc đa Leader.
   * **Embedded Browser & Design Mode:** Click trực tiếp vào element trên màn hình để trích xuất HTML/CSS và mã nguồn ném thẳng vào terminal của agent.
   * **Computer Use:** Cho phép agent tương tác GUI với các phần mềm desktop ngoài trình duyệt qua Accessibility Tree.
   * **SSH Remote Worktrees:** Chạy các tác vụ nặng trên máy chủ từ xa (như host `orb` hay GPU box) trong khi giao diện editor vẫn nằm trên macOS.
   * **Orca Skills Registry & Reverse Engineering (`morluto/rea`):** Nạp các bộ công cụ chuẩn hóa (`orchestration`, `orca-cli`, `orca-linear`, `orca-emulator`...) và bộ giải phẫu binary/package/protocol hộp đen `reverse-engineer-anything`.
5. **Git Lifecycle Chuyên Biệt:**
   * **PR Reviewer:** Checkout PR độc lập (`--pr <id>`), soi diff từng dòng (`Annotate AI Diff`) và chạy kiểm tra CI.
   * **Git Issue Auto-Resolver:** Tự động kết nối với GitHub qua **Orca Native GitHub Integration & Bell Inbox**, checkout issue (`--issue <id>`), tự tái hiện bug và tạo PR.
6. **Chính Sách Đa Luồng Song Song (Concurrent Dual-Track Policy):**
   * Khi Leader đang điều phối hoặc review tính năng mà có Issue P0/Hotfix gán đột xuất: tự động fork ra worktree riêng (`--no-parent`) để fix ngầm song song mà không làm gián đoạn luồng chính.
7. **Chính Sách Bàn Giao Khi Đổi Người (Reassignment & Handoff Policy):**
   * Khi issue đang fix bị chuyển sang người khác: tự động dừng worker an toàn (`worker-stop`), đóng gói tiến độ nháp thành branch bàn giao `wip/issue-<id>-handover`, để lại comment trên GitHub và thu dọn worktree.
8. **Phân Cấp Thẩm Quyền Quyết Định 3 Tầng & Giao Tiếp Đa Chiều (3-Tier Decision Hierarchy & Handoff Protocol):**
   * **Worker** giữ Quyền Phủ quyết Chuyên môn (`escalation`), Phản biện Giả thuyết (`ask` `CRITIC_CHALLENGE`, tối đa 3 vòng), trình duyệt kết quả bàn giao (`handoff`) và hỏi quyết định kiến trúc (`ask`). **Lead Coordinator** phối hợp phản biện với `critic-worker` để đạt đồng thuận (`CONSENSUS_REACHED`) và điều phối các bước thực thi (`check --ack`, `reply`, `send --to dispatch:<id> --type dispatch` hoặc tạo Task vá lỗi). **Human User** giữ quyền quyết định cuối cùng trước khi merge (`gate-create`).
9. **Universal Auto-Discovery (Khởi chạy tự động không cần dán prompt):**
   * Tự động tạo symlink `CLAUDE.md` và `AGENTS.md` trỏ về `.agents/GEMINI.md`. Dù bạn mở Antigravity, Claude Code (`claude`), hay Codex CLI làm Leader, agent đều tự động đọc toàn bộ directive và quy trình điều phối ngay khi khởi động mà không cần dán prompt thủ công.
10. **Điều Chỉnh Model & Agent Năng Động Ngay Khi Ở Leader (`orca-team` CLI):**
   * Cung cấp công cụ dòng lệnh `orca-team` để xem và thay đổi agent, model provider, reasoning effort hoặc chế độ `/goal` của bất kỳ vai trò nào ngay trong lúc làm việc mà không cần sửa file thủ công.

---

## 📁 Cấu Trúc Dự Án

```
orca-multi-agent-orchestrator/
├── README.md                      # Tài liệu tổng quan & hướng dẫn sử dụng
├── docs/
│   ├── architecture.md            # Kiến trúc hệ thống & nguyên lý tích hợp Orca
│   ├── workflows.md               # Sơ đồ quy trình chi tiết (Mermaid Diagrams)
│   ├── roles.md                   # Ma trận vai trò, nhiệm vụ & prompt templates
│   └── orca-utilities.md          # Hướng dẫn chi tiết từng tiện ích độc quyền của Orca
├── scripts/
│   ├── orca-team                  # CLI quản lý, chuyển đổi model/agent & forensic monitor
│   ├── orca-project-init          # Wizard tương tác cài đặt framework vào bất kỳ project nào
│   ├── orca-command-guard.py      # PreToolUse/PostToolUse guard & 16MB log circuit-breaker
│   ├── orca-dispatch-task.sh      # Script tiện ích giao nhanh task vào Orca DAG
│   ├── orca-issue-hotfix.sh       # Script tiện ích fork nhánh fix issue song song
│   └── orca-pr-review.sh          # Script tiện ích mở worktree review PR
├── templates/
│   ├── GEMINI.md                  # Template chỉ dẫn toàn diện nạp vào .agents/ của project
│   ├── orca-team.json             # Schema cấu hình đội ngũ dạng máy đọc (JSON)
│   └── plan-template.md           # Mẫu bản kế hoạch kiến trúc & chia task (Phase 0)
└── .agents/
    └── GEMINI.md                  # Chỉ dẫn vận hành Antigravity khi mở project này
```

---

## 🚀 Khởi Đầu Nhanh (Quick Start)

### Cách 1: Chạy trực tiếp qua `npx` (Khuyên dùng — Không cần clone hay tải về)
Bạn có thể khởi tạo orchestrator cho bất kỳ dự án nào chỉ bằng 1 dòng lệnh `npx`:

```bash
# Khởi tạo cho dự án hiện tại:
npx github:thapld/orca-multi-agent-orchestrator

# Hoặc truyền trực tiếp đường dẫn dự án mục tiêu:
npx github:thapld/orca-multi-agent-orchestrator /path/to/project
```

---

### Cách 2: Chạy trực tiếp qua `curl` (Không cần Node.js)
```bash
zsh <(curl -fsSL https://raw.githubusercontent.com/thapld/orca-multi-agent-orchestrator/main/scripts/orca-project-init) /path/to/project
```

---

### Cách 3: Sử dụng script cục bộ đã cài đặt (sau khi chạy `./install.sh`)
```bash
orca-project-init /path/to/project
```

---

### Các bước wizard (điều hướng 100% bằng phím mũi tên `↑`/`↓` & `Enter`):
1. Chọn agent & model cho **Lead Coordinator** (ví dụ: `antigravity` / `claude`).
2. Chọn agent, model & gói tiện ích cho **Developers** (tự động co giãn worktree `dev-worker`, `dev-worker-2`... khi bận).
3. Chọn agent, model & gói tiện ích cho **QA Testers** (tự động co giãn worktree `qa-worker`, `qa-worker-2`... khi bận).
4. Chọn agent, model & gói tiện ích cho **Bug Hunter & Security** (tự động co giãn worktree `sec-worker`, `sec-worker-2`... khi bận).
5. Chọn agent & model cho **PR Reviewer** và **Git Issue Resolver**.
6. Bật/tắt các vai trò mở rộng: **UI/UX Designer** (Design Mode), **DevOps Specialist** (Docker/K8s/DB/SSH), **Performance & Benchmark Specialist** (Đo tải RPS/EPS, p95/p99 latency, profiling), **Reverse Engineering Specialist (REA)** (Giải phẫu Binary/ASAR/APK/Firmware/Protocol với skill [`morluto/rea`](https://github.com/morluto/rea.git)) và **Research & Adversarial Critic (`critic-worker`)** (Điều tra nguyên nhân gốc & phản biện thiết kế với Leader).
7. Chọn môi trường thực thi: **Local** hoặc **SSH (`orb`)**.

### Vận hành tự động sau khi khởi tạo:
* Tự động tạo `.agents/GEMINI.md` (tích hợp **Luật Điều phối & Chống Độc Đoán cho Leader**, đồng thời offload toàn bộ **Hiến pháp 10 Vai trò cấp Principal của Worker** qua `orca-team spec <role> "<nhiệm vụ>"` giúp tiết kiệm ~2.500 tokens/turn cho Leader) cùng các symlink `GEMINI.md`, `AGENTS.md` / `CLAUDE.md` ở thư mục gốc.
* Tự động thêm toàn bộ file cấu hình & thư mục runtime của Orchestrator vào `.gitignore` để giữ sạch Git status của dự án.
* Tự động kích hoạt **Orca Agent Status Hooks** để hiển thị vòng xoay loading (`⟳`), chấm xanh (`🟢`), icon hoàn thành (`✓`) và chuông thông báo (`🔔`) trên thanh bên của Orca ADE.
* Mở dự án trong Antigravity, Claude Code (`claude`), Codex hoặc Orca ADE: mọi yêu cầu của bạn sẽ tự động vận hành theo đúng **Quy trình 4 Bước** (Tách Task theo vai trò ➔ Fan-out sang Child Worktrees ➔ Reactive Wait `check --wait` & Phản biện Đồng thuận ➔ Human Gate xin phép trước khi merge).

---

## 🎛️ Thay Đổi Agent & Model Khi Ở Leader (`orca-team` CLI)

Trong quá trình làm việc, nếu bạn muốn đổi sang model mạnh hơn cho Developer hay đổi agent cho QA/Security:

```bash
# 1. Xem đội hình và model hiện tại:
orca-team status

# 2. Đổi agent & model cho Developer (ví dụ sang Claude Sonnet 4.6 với reasoning high):
orca-team set dev --agent claude --model claude-sonnet-4-6 --effort high

# 3. Đổi agent cho QA & Tester:
orca-team set qa --agent antigravity

# 4. Đổi agent cho Security Auditor:
orca-team set sec --agent codex --model gpt-5.3-codex --effort high

# 5. Bật/tắt các vai trò mở rộng chuyên biệt (UI, DevOps, Bench, Reverse Eng, Critic):
orca-team set ui --enable --agent claude
orca-team set devops --enable --agent antigravity
orca-team set bench --enable --agent antigravity --model gemini-3.8-flash-high
orca-team set re --enable --agent antigravity --model gemini-4-argon-high
orca-team set critic --enable --agent antigravity --model gemini-4-argon-high

# 6. Chế độ tương tác từng bước (điều hướng bằng phím mũi tên):
orca-team set dev
orca-team set critic

# 7. Sinh bản Hiến pháp Vai trò cấp Principal, Cấp phát Worktree Slot tự động & Dọn tab Worker:
orca-team spec critic "Kiểm chứng chéo giả thuyết lỗi và phản biện thiết kế của Leader"
orca-team spec dev "Triển khai API..."
orca-team worktree-arg dev       # Trả về --worktree path:... (và tự đóng tab cũ) nếu rảnh, hoặc --worktree new-child --name dev-worker-2 nếu bận
orca-team release-workers        # Lưu trữ archive & đóng triệt để toàn bộ tab terminal cũ (done/retained/user_takeover) của các worker đã xong việc

# 8. Quản lý Discovery Links cho nhiều AI CLI (Claude / Antigravity / Codex):
orca-team link all      # Kích hoạt CLAUDE.md và AGENTS.md để mở bất kỳ AI CLI nào làm Leader
orca-team link clean    # Dọn dẹp symlink thừa nếu chỉ dùng Antigravity
orca-team link status   # Xem trạng thái liên kết

# 9. Watchdog kiểm tra & tự động giải cứu worker bị kẹt:
orca-team watchdog           # Quét trạng thái, phát hiện worker cạn token hoặc kẹt prompt [y/n]
orca-team watchdog --unblock # Tự động gửi 'y' mở khóa toàn bộ worker đang chờ câu hỏi

# 10. Giám sát & Bóc tách nguyên nhân tiêu tốn Token (Token Monitor & Forensic Debugger):
orca-monitor                 # hoặc: orca-team monitor
orca-team monitor --verbose  # Xem chi tiết từng thông điệp và buffer màn hình
orca-team monitor --fix      # Tự động sửa các bẫy token & đóng các tab terminal worker cũ còn sót lại
```

> **Ghi chú:** Khi chạy `orca-team set`, công cụ sẽ **tự động cập nhật `orca-team.json` đồng thời đồng bộ lại bảng Topology, danh sách phân vai và lệnh khởi tạo ở Bước 1 & Bước 2 trong `.agents/GEMINI.md`**, giúp Leader agent lập tức nhận biết cấu hình mới mà không cần khởi động lại.

---

## 🔬 Chế Độ Giám Sát Token & Khắc Phục Rò Rỉ (Token Monitor)

Nếu bạn hoặc AI Leader thấy **tiêu tốn quá nhiều token** mà không rõ nguyên nhân, công cụ `orca-monitor` (hoặc `orca-team monitor`) sẽ tiến hành rà soát pháp y 5 tầng:

1. **Chỉ dẫn hệ thống nạp mỗi turn (Per-Turn System Prompt):** Đo lường chính xác dung lượng byte và ước tính token của `.agents/GEMINI.md`, `CLAUDE.md`, `AGENTS.md`. Phát hiện trường hợp file bị copy độc lập thay vì symlink (gây x2 token mỗi turn).
2. **Bẫy đường dẫn chết (Dead Reference Traps):** Tự động phát hiện các file template/doc được nhắc đến trong chỉ dẫn nhưng không tồn tại trong project — nguyên nhân khiến AI Agent rơi vào vòng lặp tìm file bất tận (`view_file` / `glob`) đốt 1.500 - 3.000 tokens ngay khi mở màn.
3. **Vệ sinh Workspace & Git Status:** Kiểm tra `.gitignore` xem đã chặn log rác và receipt của Orca (`.orca/`, `.orca-cache/`, `*.orca.log`) chưa. Đếm số lượng file untracked làm phình output của `git status`.
4. **Orca Runtime Telemetry:** Kiểm tra trạng thái thực thi của các worker trong Run hiện tại, phân tích hộp thư `Mailbox Event-Driven` và đối chiếu định lượng số lượng token đã cô lập sang child worktrees.
5. **Auto-Fix Thông Minh (`--fix`):** Tự động thêm luật vào `.gitignore`, tự động chuyển file trùng lặp sang symlink và tự động kích hoạt Orca Agent Status Hooks (loading spinner & chuông thông báo).

---

## ⚡ Các Lệnh Dispatch Tác Vụ Nhanh (Anti-Token-Waste)

Các script tiện ích đã được cấu hình sẵn timeout 5 phút (`--timeout-ms 300000`), tự động tiêm Hiến pháp Vai trò cấp Principal (`[ROLE: PRINCIPAL ...]`) và tra cứu đúng Agent + Model từ `orca-team.json`:

```bash
# Giao việc điều tra nguyên nhân gốc & phản biện thiết kế (Research & Adversarial Critic):
orca-dispatch "Critique Payment Plan" critic "Kiểm chứng chéo giả thuyết lỗi callback EPUSDT và phản biện phương án thiết kế của Leader"

# Giao việc cho Developer (tự động dùng agent/model đã config cho dev):
orca-dispatch "Tạo module Auth" dev "Viết JWT login/refresh token có kiểm thử đầy đủ"

# Giao việc cho DevOps / SRE:
orca-dispatch "Rollout K8s" devops "Cập nhật Dockerfile multi-stage và rollout pod mới"

# Giao việc đo kiểm hiệu năng (Benchmark & Perf):
orca-dispatch "Stress Test Ingest" bench "Đo tải 10.000 EPS trên endpoint /api/v1/ingest và xuất p95/p99"

# Giao việc dịch ngược Binary / Package / Protocol (REA):
orca-dispatch "Unpack Plugin" re "Giải phẫu binary/JAR plugin bằng morluto/rea và xuất Clean-Room Spec"

# Mở worktree fix Issue GitHub độc lập song song (--no-parent):
orca-hotfix 142

# Mở worktree review line-by-line diff PR GitHub độc lập (--no-parent):
orca-review 89
```

---

## 🗑️ Hướng Dẫn Gỡ Bỏ (Uninstall)

### 1. Gỡ bỏ cấu hình Orchestrator khỏi một project (Khôi phục dự án về ban đầu)
Nếu bạn không còn muốn dùng multi-agent trong một repo cụ thể:
```bash
# Cách 1: Đứng tại project và chạy lệnh orca-team:
orca-team uninstall

# Cách 2: Chạy trực tiếp qua npx hoặc script init (không cần cài đặt):
npx github:thapld/orca-multi-agent-orchestrator --uninstall /path/to/project
# hoặc:
orca-project-init --uninstall /path/to/project
```
> Lệnh này sẽ tự động xóa sạch `orca-team.json`, `.agents/GEMINI.md`, các symlink `CLAUDE.md`, `AGENTS.md` và khôi phục project về nguyên trạng sạch sẽ ban đầu.

### 2. Gỡ cài đặt toàn bộ công cụ CLI khỏi máy (System Uninstall)
Nếu bạn muốn xóa sạch toàn bộ các lệnh CLI (`orca-team`, `orca-project-init`, `orca-dispatch`, `orca-hotfix`, `orca-review`) khỏi máy tính (`~/.local/bin`):
```bash
# Chạy script gỡ bỏ trong repo:
./uninstall.sh

# Hoặc qua install.sh:
./install.sh --uninstall
```

---

## 🛡️ Zero-Trust QA & Critic Gate (`C-FIX-01..C-FIX-38`)

* **Canonical Test Execution, Root-Hijack Guard & Policy Anchor (`C-FIX-13`, `C-FIX-17`, `C-FIX-19`, `C-FIX-27..C-FIX-38`):** Guard (`orca-command-guard.py`) **không bao giờ** lấy lệnh `cmd` từ `evidence.jsonl` để thực thi (ngăn chặn Confused Deputy / RCE) và **không bao giờ** tin cờ `cd`/`--project` trong chuỗi lệnh để đổi thư mục gốc dự án (`[ORCA ROOT-HIJACK GUARD]`, chỉ lấy root từ `real_cwd` + `git rev-parse --show-toplevel`). Lệnh kiểm thử chuẩn (`acceptanceCommands`), danh sách file/hàm kiểm thử gốc (`baselineTestFiles`, `baselineTestIds`), và `testAllowlist` được ghim vào `.policy_anchor.json` + seal + bảng `orca_policy_anchors` trong SQLite DB + `.orca/acceptance-manifest.json`. Mọi đường dẫn cờ argv trong trình chạy test bắt buộc `realpath` nằm trong worktree và thuộc cây thư mục test chuẩn (`tests/`, `test/`, `spec/`, `e2e/`, `__tests__/`).
* **Test-Tree Integrity (`C-FIX-14`), TDD AST/Source-Diff & Hermetic RED & GREEN Re-Verification (`C-FIX-15`, `C-FIX-30`, `C-FIX-33..C-FIX-38`):** `orca-team verify` ghi nhận `test_tree_hash` (bao gồm cả manifest trình chạy `package.json`, `Makefile`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `conftest.py`). Guard bắt buộc chuỗi TDD RED→GREEN phải có thay đổi **mã nguồn thực chất** (đối chiếu AST với Python và lọc bỏ comment/whitespace/file tài liệu) **và** thực hiện **Hermetic RED & GREEN Re-Verification** (`C-FIX-38`): trích xuất cây git tại cả hai commit pha RED (`git archive <red_sha>`) và pha GREEN (`git archive <green_sha>`) ra hai thư mục cô lập độc lập để chạy lại lệnh kiểm thử chuẩn — bắt buộc commit RED phải FAIL (`exit_code != 0`), commit GREEN phải PASS (`exit_code = 0`), và thực thi fail-closed nếu xảy ra lỗi giải nén/timeout (`[ORCA TDD-PROGRESSION GUARD]`). Nhờ đó, bất kỳ bài test nào dựa vào file/trạng thái ngoại lai ngoài git (dù ở pha RED như `F12` hay ở pha GREEN như `F13`) đều bị phát hiện và chặn đứng.
* **Critic Risk Binding, Provenance, Hypothesis Content & Best-Effort DB-Integrity Guard (`C-FIX-20..C-FIX-25`, `C-FIX-31`, `C-FIX-35..C-FIX-38`):**
  * **Risk Binding (`C-FIX-20`):** Tại mọi điểm (`plan consensus`, `plan pin`, `worker-start`), hệ thống luôn tính lại `compute_risk(extract_items(plan), contract_lines(plan))` trực tiếp từ nội dung plan và chặn ngay (`[ORCA RISK BINDING GUARD]`) nếu `riskLevel` trong `contracts.json` hoặc `consensus.json` bị sửa lệch.
  * **Strict Provenance, Reply Binding & Best-Effort DB-Integrity Guard (`C-FIX-21`, `C-FIX-22`, `C-FIX-24`, `C-FIX-35..C-FIX-38`):** Bắt buộc có `plan-draft.contracts.json`, `plan-draft.md`, và bản ghi lịch sử `plan-history/<hash>.md` do `orca-team plan draft` khởi tạo; danh tính critic xác định từ role của task trong DB (`tasks.spec` / `task_title`). Mỗi challenge phải có `to_handle` không thuộc critic, có bản ghi `question_threads` (`status='answered'`, `answered_at > created_at`) gắn đúng `dispatch_id` của critic, và mỗi reply phải có `from_handle` thuộc `("leader", "coordinator", "orca", f"run:{run_id}")`, `to_handle=<critic>`, `thread_id=challenge.id`, `id=question_threads.answer_message_id` (`>= 60` ký tự). `[ORCA DB-INTEGRITY GUARD]` chặn lệnh `sqlite3` CLI (kể cả `.read`), giải mã base64 chứa chuỗi DB/sqlite3, lệnh copy `*.db`, truyền trực tiếp `orchestration.db`/`ORCA_ORCHESTRATION_DB`, `exec(open(...))`, quét nội dung các file tham số (`<= 16 MB`, mọi phần mở rộng) có tham chiếu `sqlite3`, DML, hoặc bảng điều phối, và không bao giờ miễn trừ đường dẫn dưới `/tmp` hay `/var` (**lưu ý: mọi bộ lọc chuỗi/file ở tầng hook Bash trong cùng một UID chỉ là lớp bảo vệ best-effort và không thể thay thế daemon tách biệt UID**).
  * **Hypothesis Content & Read-Only Allowlist (`C-FIX-23`, `C-FIX-25`, `C-FIX-35`):** Băm lại `sha256(hypothesis.md)` đối chiếu với `hypothesis.json.hash` và `review.hypothesisHash`, kiểm tra nội dung `hypothesis.md` thực sự chứa mọi `path:line` trong `hypothesis.json.refs`, kiểm tra độ sâu `objections[].claim` (`>= 8` ký tự), và `createdAt <= min(challenge.created_at)`. Slot `critic-worker` áp dụng allowlist lệnh chỉ-đọc và lệnh ghi duy nhất `orca-team critic write --run <RUN> --file <name>`.
* **Giới hạn Mô hình Đe dọa Cùng-UID (`C-FIX-18`, `C-FIX-26`, `C-FIX-34..C-FIX-38`):** Khi tiến trình Agent chạy dưới cùng một UID hệ điều hành với các file trên đĩa (`.policy_anchor.json`, seal, `orchestration.db`, `.hmac_secret`), cơ chế neo, kiểm tra provenance, `[ORCA DB-INTEGRITY GUARD]` và chữ ký HMAC trên đĩa chỉ đóng vai trò **cổng best-effort chống agent lười/ẩu và chống các dạng giả mạo phổ biến qua CLI/Bash**, **không phải ranh giới bảo mật same-UID** (một tác nhân cùng-UID có quyền thực thi mã tùy ý luôn có thể tìm cách ghi DB/file ngoài bộ lọc chuỗi hoặc đọc khóa). Để đạt ranh giới bảo mật chống same-UID thực sự, cơ sở dữ liệu điều phối (`orchestration.db`) phải do daemon tách biệt UID sở hữu (chỉ nhận ghi qua IPC/socket có kiểm soát) và danh tính `critic-worker` phải được chứng thực bằng khóa phiên mật mã (session key/token) chỉ cấp riêng cho tiến trình critic.

---

## 📖 Đọc Thêm

* [Kiến Trúc Hệ Thống (Architecture)](./docs/architecture.md)
* [Sơ Đồ Quy Trình Vận Hành (Workflows & Diagrams)](./docs/workflows.md)
* [Chi Tiết Từng Vai Trò (Roles & Responsibilities)](./docs/roles.md)
* [Cẩm Nang Tiện Ích Orca (Orca Utilities Guide)](./docs/orca-utilities.md)
