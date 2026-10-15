# 🐋 Orca Multi-Agent Orchestration Framework

Hệ thống điều phối kỹ thuật phần mềm đa agent (Multi-Agent Engineering Orchestrator) toàn diện xây dựng trên nền tảng **Orca Agent Development Environment (ADE)**.

Framework này cho phép biến bất kỳ kho mã nguồn (repository) nào thành một trung tâm kỹ thuật tự trị, nơi các agent AI (`claude`, `codex`, `kimi`, `cursor`, `antigravity`, `opencode`...) cùng làm việc nhịp nhàng theo các vai trò chuyên biệt, cô lập bằng **Git Worktrees**, kiểm định chất lượng tự động, và xử lý đa luồng song song.

---

## 🌟 Tính Năng Nổi Bật

1. **Linh Hoạt Đa Agent (Agnostic Multi-Agent Support):**
   * Tự do lựa chọn bất kỳ model/agent nào Orca hỗ trợ cho từng vị trí (Coordinator, Developer, QA, Security, PR Reviewer, Issue Resolver, UI/UX, DevOps).
2. **Cấu Hình Concurrency (Song Song Hóa Tác Vụ):**
   * Cho phép chỉ định số lượng agent song song (1–5 workers) cho nhóm Developer, QA hoặc Security.
3. **Khai Thác 100% Tiện Ích Độc Quyền Của Orca:**
   * **Git Worktrees:** Mỗi task/agent sở hữu một worktree độc lập (`--worktree new-child`), loại bỏ hoàn toàn xung đột git.
   * **Embedded Browser & Design Mode:** Click trực tiếp vào element trên màn hình để trích xuất HTML/CSS và mã nguồn ném thẳng vào terminal của agent.
   * **Computer Use:** Cho phép agent tương tác GUI với các phần mềm desktop ngoài trình duyệt qua Accessibility Tree.
   * **SSH Remote Worktrees:** Chạy các tác vụ nặng trên máy chủ từ xa (như host `orb` hay GPU box) trong khi giao diện editor vẫn nằm trên macOS.
   * **Orca Skills Registry:** Nạp các bộ công cụ chuẩn hóa (`orchestration`, `orca-cli`, `orca-linear`, `orca-emulator`...).
4. **Git Lifecycle Chuyên Biệt:**
   * **PR Reviewer:** Checkout PR độc lập (`--pr <id>`), soi diff từng dòng (`Annotate AI Diff`) và chạy kiểm tra CI.
   * **Git Issue Auto-Resolver:** Tự động kết nối với GitHub qua **Orca Native GitHub Integration & Bell Inbox**, checkout issue (`--issue <id>`), tự tái hiện bug và tạo PR.
5. **Chính Sách Đa Luồng Song Song (Concurrent Dual-Track Policy):**
   * Khi Leader đang điều phối hoặc review tính năng mà có Issue P0/Hotfix gán đột xuất: tự động fork ra worktree riêng (`--no-parent`) để fix ngầm song song mà không làm gián đoạn luồng chính.
6. **Chính Sách Bàn Giao Khi Đổi Người (Reassignment & Handoff Policy):**
   * Khi issue đang fix bị chuyển sang người khác: tự động dừng worker an toàn (`worker-stop`), đóng gói tiến độ nháp thành branch bàn giao `wip/issue-<id>-handover`, để lại comment trên GitHub và thu dọn worktree.
7. **Xử Lý Sự Cố & Chỉ Đạo Khi Bế Tắc (Worker Escalation & Blocker Handling):**
   * Khi worker gặp blocker, tự động gửi `ask`/`escalation` lên Coordinator qua Orca Mailbox. Leader kiểm tra qua `orca orchestration check`, đưa ra chỉ đạo (`steer`), hoặc tham vấn Human Gate nếu cần quyết định kiến trúc lớn.
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
npx github:thapld/orca-multi-agent-orchestrator /Users/macbook/work/gem76
```

---

### Cách 2: Chạy trực tiếp qua `curl` (Không cần Node.js)
```bash
curl -fsSL https://raw.githubusercontent.com/thapld/orca-multi-agent-orchestrator/main/scripts/orca-project-init | zsh -s -- /Users/macbook/work/gem76
```

---

### Cách 3: Sử dụng script cục bộ đã cài đặt
Script đã được cài sẵn vào PATH tại `/Users/macbook/.local/bin/orca-project-init`:
```bash
orca-project-init /Users/macbook/work/gem76
```

---

### Các bước wizard sẽ hướng dẫn:
1. Chọn agent làm **Lead Coordinator** (ví dụ: `claude`).
2. Chọn agent & số lượng song song cho **Developers** (ví dụ: 2 agents `kimi`).
3. Chọn agent & số lượng song song cho **QA Testers** (ví dụ: 1 agent `kimi`).
4. Chọn agent cho **Bug Hunter & Security** (ví dụ: `codex`).
5. Chọn agent cho **PR Reviewer** và **Git Issue Resolver**.
6. Bật/tắt các vai trò mở rộng: **UI/UX Designer** (Design Mode) và **DevOps** (SSH Worktrees).
7. Chọn môi trường thực thi: **Local** hoặc **SSH (`orb`)**.

### Vận hành:
Sau khi khởi tạo, bạn mở dự án trong Antigravity, Claude Code (`claude`), hoặc Orca. Bất kỳ yêu cầu nào bạn đưa ra đều được tự động vận hành theo đúng quy trình 6 bước và chính sách đa luồng đã thiết lập mà không cần dán prompt.

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

# 5. Bật/tắt vai trò mở rộng:
orca-team set ui --enable --agent claude
orca-team set devops --disable

# 6. Chế độ tương tác từng bước:
orca-team set dev

# 7. Quản lý Discovery Links cho nhiều AI CLI (Claude / Antigravity / Codex):
orca-team link all      # Kích hoạt CLAUDE.md và AGENTS.md để mở bất kỳ AI CLI nào làm Leader
orca-team link clean    # Dọn dẹp symlink thừa nếu chỉ dùng Antigravity
orca-team link status   # Xem trạng thái liên kết

# 8. Watchdog kiểm tra & tự động giải cứu worker bị kẹt:
orca-team watchdog           # Quét trạng thái, phát hiện worker cạn token hoặc kẹt prompt [y/n]
orca-team watchdog --unblock # Tự động gửi 'y' mở khóa toàn bộ worker đang chờ câu hỏi
```

> **Ghi chú:** Khi chạy `orca-team set`, công cụ sẽ **tự động cập nhật `orca-team.json` đồng thời đồng bộ lại bảng vai trò trong `.agents/GEMINI.md`**, giúp Leader agent lập tức nhận biết cấu hình mới mà không cần khởi động lại.

---

## ⚡ Các Lệnh Dispatch Tác Vụ Nhanh (Anti-Token-Waste)

Các script tiện ích đã được cấu hình sẵn timeout 5 phút (`--timeout-ms 300000`) và tự động tra cứu đúng Agent từ `orca-team.json`:

```bash
# Giao việc cho Developer (tự động dùng agent đã config cho dev):
orca-dispatch "Tạo module Auth" "Viết JWT login/refresh token có kiểm thử đầy đủ"

# Hoặc chỉ định rõ agent khác nếu muốn:
orca-dispatch "Tạo module Auth" claude "Viết JWT login..."

# Mở worktree fix Issue GitHub song song (không ảnh hưởng nhánh chính):
orca-hotfix 142

# Mở worktree review line-by-line diff PR GitHub độc lập:
orca-review 89
```

---

## 📖 Đọc Thêm

* [Kiến Trúc Hệ Thống (Architecture)](./docs/architecture.md)
* [Sơ Đồ Quy Trình Vận Hành (Workflows & Diagrams)](./docs/workflows.md)
* [Chi Tiết Từng Vai Trò (Roles & Responsibilities)](./docs/roles.md)
* [Cẩm Nang Tiện Ích Orca (Orca Utilities Guide)](./docs/orca-utilities.md)
