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

### 1. Cài đặt script điều phối toàn cục
Script khởi tạo đã được đặt tại `/Users/macbook/.local/bin/orca-project-init` (đã nằm trong `PATH`).

### 2. Gắn Orchestrator vào một dự án
Mở terminal và trỏ đến dự án mục tiêu của bạn (ví dụ repo `gem76`):

```bash
orca-project-init /Users/macbook/work/gem76
```

Script sẽ mở menu tương tác từng bước:
1. Chọn agent làm **Lead Coordinator** (ví dụ: `claude`).
2. Chọn agent & số lượng song song cho **Developers** (ví dụ: 2 agents `kimi`).
3. Chọn agent & số lượng song song cho **QA Testers** (ví dụ: 1 agent `kimi`).
4. Chọn agent cho **Bug Hunter & Security** (ví dụ: `codex`).
5. Chọn agent cho **PR Reviewer** và **Git Issue Resolver**.
6. Bật/tắt các vai trò mở rộng: **UI/UX Designer** (Design Mode) và **DevOps** (SSH Worktrees).
7. Chọn môi trường thực thi: **Local** hoặc **SSH (`orb`)**.

### 3. Vận hành
Sau khi cài đặt, bạn mở dự án trong Antigravity hoặc Orca. Bất kỳ yêu cầu nào bạn đưa ra đều được tự động vận hành theo đúng quy trình 5 pha và chính sách đa luồng đã thiết lập.

---

## 📖 Đọc Thêm

* [Kiến Trúc Hệ Thống (Architecture)](./docs/architecture.md)
* [Sơ Đồ Quy Trình Vận Hành (Workflows & Diagrams)](./docs/workflows.md)
* [Chi Tiết Từng Vai Trò (Roles & Responsibilities)](./docs/roles.md)
* [Cẩm Nang Tiện Ích Orca (Orca Utilities Guide)](./docs/orca-utilities.md)
