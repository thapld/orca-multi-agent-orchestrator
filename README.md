# 🐋 Orca Multi-Agent Orchestration Framework

Hệ thống điều phối kỹ thuật phần mềm đa agent (Multi-Agent Engineering Orchestrator) toàn diện xây dựng trên nền tảng **Orca Agent Development Environment (ADE)**.

Framework này cho phép biến bất kỳ kho mã nguồn (repository) nào thành một trung tâm kỹ thuật tự trị, nơi các agent AI (`claude`, `codex`, `kimi`, `cursor`, `antigravity`, `opencode`...) cùng làm việc nhịp nhàng theo các vai trò chuyên biệt, cô lập bằng **Git Worktrees**, kiểm định chất lượng tự động, và xử lý đa luồng song song.

---

## 🌟 Tính Năng Nổi Bật

1. **Linh Hoạt Đa Agent (Agnostic Multi-Agent Support — 10 Vai Trò Cấp Principal):**
   * Tự do lựa chọn bất kỳ model/agent nào Orca hỗ trợ cho từng vị trí: **Lead Coordinator**, **Full-Stack Devs**, **QA & SDET**, **Security & Bug Hunter**, **PR Reviewer**, **Git Issue Resolver**, **UI/UX Specialist**, **DevOps & SRE**, **Bench & Perf Specialist** và **Reverse Eng Specialist (REA)**.
2. **Cấu Hình Concurrency (Song Song Hóa Tác Vụ):**
   * Cho phép chỉ định số lượng agent song song (1–5 workers) cho nhóm Developer, QA hoặc Security.
3. **Khai Thác 100% Tiện Ích Độc Quyền Của Orca & REA:**
   * **Git Worktrees:** Mỗi task/agent sở hữu một worktree độc lập (`--worktree new-child`), loại bỏ hoàn toàn xung đột git.
   * **Embedded Browser & Design Mode:** Click trực tiếp vào element trên màn hình để trích xuất HTML/CSS và mã nguồn ném thẳng vào terminal của agent.
   * **Computer Use:** Cho phép agent tương tác GUI với các phần mềm desktop ngoài trình duyệt qua Accessibility Tree.
   * **SSH Remote Worktrees:** Chạy các tác vụ nặng trên máy chủ từ xa (như host `orb` hay GPU box) trong khi giao diện editor vẫn nằm trên macOS.
   * **Orca Skills Registry & Reverse Engineering (`morluto/rea`):** Nạp các bộ công cụ chuẩn hóa (`orchestration`, `orca-cli`, `orca-linear`, `orca-emulator`...) và bộ giải phẫu binary/package/protocol hộp đen `reverse-engineer-anything`.
4. **Git Lifecycle Chuyên Biệt:**
   * **PR Reviewer:** Checkout PR độc lập (`--pr <id>`), soi diff từng dòng (`Annotate AI Diff`) và chạy kiểm tra CI.
   * **Git Issue Auto-Resolver:** Tự động kết nối với GitHub qua **Orca Native GitHub Integration & Bell Inbox**, checkout issue (`--issue <id>`), tự tái hiện bug và tạo PR.
5. **Chính Sách Đa Luồng Song Song (Concurrent Dual-Track Policy):**
   * Khi Leader đang điều phối hoặc review tính năng mà có Issue P0/Hotfix gán đột xuất: tự động fork ra worktree riêng (`--no-parent`) để fix ngầm song song mà không làm gián đoạn luồng chính.
6. **Chính Sách Bàn Giao Khi Đổi Người (Reassignment & Handoff Policy):**
   * Khi issue đang fix bị chuyển sang người khác: tự động dừng worker an toàn (`worker-stop`), đóng gói tiến độ nháp thành branch bàn giao `wip/issue-<id>-handover`, để lại comment trên GitHub và thu dọn worktree.
7. **Phân Cấp Thẩm Quyền Quyết Định 3 Tầng & Giao Tiếp Đa Chiều (3-Tier Decision Hierarchy & Handoff Protocol):**
   * **Worker** giữ Quyền Phủ quyết Chuyên môn (`escalation`), trình duyệt kết quả bàn giao (`handoff`) và hỏi quyết định kiến trúc (`ask`). **Lead Coordinator** là Người Ra Quyết Định Kỹ Thuật & Điều Phối Duy Nhất trong các bước thực thi (`check --ack`, `reply`, `send --to dispatch:<id> --type dispatch` hoặc tạo Task vá lỗi). **Human User** giữ quyền quyết định cuối cùng trước khi merge (`gate-create`).
8. **Universal Auto-Discovery (Khởi chạy tự động không cần dán prompt):**
   * Tự động tạo symlink `CLAUDE.md` và `AGENTS.md` trỏ về `.agents/GEMINI.md`. Dù bạn mở Antigravity, Claude Code (`claude`), hay Codex CLI làm Leader, agent đều tự động đọc toàn bộ directive và quy trình điều phối ngay khi khởi động mà không cần dán prompt thủ công.
9. **Điều Chỉnh Model & Agent Năng Động Ngay Khi Ở Leader (`orca-team` CLI):**
   * Cung cấp công cụ dòng lệnh `orca-team` để xem và thay đổi agent, model provider, reasoning effort hoặc số lượng concurrency của bất kỳ vai trò nào ngay trong lúc làm việc mà không cần sửa file thủ công.

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
│   ├── orca-team                  # CLI quản lý, chuyển đổi model/agent cho từng role
│   ├── orca-project-init          # Wizard tương tác cài đặt framework vào bất kỳ project nào
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
2. Chọn agent, model, số lượng song song & gói tiện ích cho **Developers**.
3. Chọn agent, model, số lượng song song & gói tiện ích cho **QA Testers**.
4. Chọn agent, model & gói tiện ích cho **Bug Hunter & Security**.
5. Chọn agent & model cho **PR Reviewer** và **Git Issue Resolver**.
6. Bật/tắt các vai trò mở rộng: **UI/UX Designer** (Design Mode), **DevOps Specialist** (Docker/K8s/DB/SSH), **Performance & Benchmark Specialist** (Đo tải RPS/EPS, p95/p99 latency, profiling) và **Reverse Engineering Specialist (REA)** (Giải phẫu Binary/ASAR/APK/Firmware/Protocol với skill [`morluto/rea`](https://github.com/morluto/rea.git)).
7. Chọn môi trường thực thi: **Local** hoặc **SSH (`orb`)**.

### Vận hành tự động sau khi khởi tạo:
* Tự động tạo `.agents/GEMINI.md` (tích hợp đầy đủ **Hiến pháp 10 Vai trò cấp Principal**, tối ưu token) cùng các symlink `GEMINI.md`, `AGENTS.md` / `CLAUDE.md` ở thư mục gốc.
* Tự động thêm toàn bộ file cấu hình & thư mục runtime của Orchestrator vào `.gitignore` để giữ sạch Git status của dự án.
* Tự động kích hoạt **Orca Agent Status Hooks** để hiển thị vòng xoay loading (`⟳`), chấm xanh (`🟢`), icon hoàn thành (`✓`) và chuông thông báo (`🔔`) trên thanh bên của Orca ADE.
* Mở dự án trong Antigravity, Claude Code (`claude`), Codex hoặc Orca ADE: mọi yêu cầu của bạn sẽ tự động vận hành theo đúng **Quy trình 4 Bước** (Tách Task theo vai trò ➔ Fan-out sang Child Worktrees ➔ Reactive Wait `check --wait` ➔ Human Gate xin phép trước khi merge).

---

## 🎛️ Thay Đổi Agent & Model Khi Ở Leader (`orca-team` CLI)

Trong quá trình làm việc, nếu bạn muốn đổi sang model mạnh hơn cho Developer, đổi agent cho QA hay tăng/giảm số worker song song:

```bash
# 1. Xem đội hình và model hiện tại:
orca-team status

# 2. Đổi agent & model cho Developer (ví dụ sang Claude 3.5 Sonnet với reasoning high):
orca-team set dev --agent claude --model claude-3-5-sonnet --effort high --concurrency 3

# 3. Đổi agent cho QA & Tester:
orca-team set qa --agent antigravity

# 4. Đổi agent cho Security Auditor:
orca-team set sec --agent codex --model gpt-4o

# 5. Bật/tắt các vai trò mở rộng chuyên biệt (UI, DevOps, Bench, Reverse Eng):
orca-team set ui --enable --agent claude
orca-team set devops --enable --agent antigravity
orca-team set bench --enable --agent antigravity --model gemini-3.8-flash-high
orca-team set re --enable --agent antigravity --model gemini-4-argon-high

# 6. Chế độ tương tác từng bước (điều hướng bằng phím mũi tên):
orca-team set dev
orca-team set re

# 7. Quản lý Discovery Links cho nhiều AI CLI (Claude / Antigravity / Codex):
orca-team link all      # Kích hoạt CLAUDE.md và AGENTS.md để mở bất kỳ AI CLI nào làm Leader
orca-team link clean    # Dọn dẹp symlink thừa nếu chỉ dùng Antigravity
orca-team link status   # Xem trạng thái liên kết

# 8. Watchdog kiểm tra & tự động giải cứu worker bị kẹt:
orca-team watchdog           # Quét trạng thái, phát hiện worker cạn token hoặc kẹt prompt [y/n]
orca-team watchdog --unblock # Tự động gửi 'y' mở khóa toàn bộ worker đang chờ câu hỏi

# 9. Giám sát & Bóc tách nguyên nhân tiêu tốn Token (Token Monitor & Forensic Debugger):
orca-monitor                 # hoặc: orca-team monitor
orca-team monitor --verbose  # Xem chi tiết từng thông điệp và buffer màn hình
orca-team monitor --fix      # Tự động sửa các bẫy token (gitignore, symlink, status hooks)
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

## 📖 Đọc Thêm

* [Kiến Trúc Hệ Thống (Architecture)](./docs/architecture.md)
* [Sơ Đồ Quy Trình Vận Hành (Workflows & Diagrams)](./docs/workflows.md)
* [Chi Tiết Từng Vai Trò (Roles & Responsibilities)](./docs/roles.md)
* [Cẩm Nang Tiện Ích Orca (Orca Utilities Guide)](./docs/orca-utilities.md)
