# 🏛️ Kiến Trúc Hệ Thống Orca Multi-Agent Orchestration

Tài liệu này giải thích chi tiết cấu trúc kiến trúc, các tầng trừu tượng, và cơ chế phối hợp giữa **Antigravity / Coordinator Agent**, **Orca Runtime**, và các **Supervised Worker Agents**.

---

## 1. Sơ Đồ Kiến Trúc Tổng Thể

```
┌────────────────────────────────────────────────────────────────────────┐
│                        TẦNG ĐIỀU PHỐI (ORCHESTRATION LAYER)            │
│  Lead Coordinator (Antigravity / Claude / Codex)                      │
│  - Phân tích bài toán, lập Architecture Blueprint & Task DAG          │
│  - Giao tiếp qua Orca IPC / CLI (/usr/local/bin/orca)                  │
│  - Giám sát tiến độ qua Hộp thư phản hồi (Supervised Mailbox)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                        Lệnh CLI / RPC / JSON Events
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│                        TẦNG RUNTIME (ORCA ADE ENGINE)                  │
│  Orca Runtime Engine (PID 1272, version 1.4.223)                       │
│  ├── Run Management (Durable Namespace & Inbox)                        │
│  ├── Task & Dispatch Engine (Dependencies, States, Decision Gates)     │
│  ├── Worktree Manager (Git checkout isolation, parent/child lineage)   │
│  └── Peripheral Hub (Browser engine, SSH bridge, PTY terminals)        │
└───────────────────┬───────────────────────────────┬────────────────────┘
                    │                               │
       Tách nhánh Git Worktree           Cấp quyền Tiện ích
                    │                               │
┌───────────────────▼──────────────────┐ ┌──────────▼────────────────────┐
│      TẦNG THỰC THI (WORKER SWARM)    │ │      TẦNG TIỆN ÍCH (UTILITIES)│
│  • Full-Stack Devs (dev-worker)      │ │  • Embedded Browser           │
│  • DevOps / SRE (devops-worker)      │ │  • Design Mode (DOM/CSS Pick) │
│  • Security Auditors (sec-worker)    │ │  • SSH Worktrees (orb host)   │
│  • QA / SDET (qa-worker)             │ │  • Desktop Computer Use       │
│  • Bench & Perf (bench-worker)       │ │  • Reverse Eng (morluto/rea)  │
│  • Reverse Eng / REA (re-worker)     │ │  • Mobile Simulators (iOS/adb)│
│  • UI/UX, PR Review, Issue Hotfix    │ │  • Per-Worktree Env & Diff    │
└──────────────────────────────────────┘ └───────────────────────────────┘
```

---

## 2. Các Tầng Thành Phần

### 2.1. Tầng Điều Phối (Orchestrator Layer)
* **Thành phần chính:** Model cấp cao đảm nhiệm vai trò Lead Coordinator (ví dụ Claude Opus, Gemini Argon, hoặc OpenAI Codex).
* **Nhiệm vụ:**
  1. Phân tích yêu cầu từ người dùng hoặc issue trên GitHub.
  2. Lập bản kế hoạch kiến trúc (`plan.md`) và chia nhỏ thành các nhiệm vụ nguyên tử (atomic tasks).
  3. Sử dụng lệnh `orca orchestration run-create` và `task-create` để nạp đồ thị phụ thuộc (DAG) vào Orca.
  4. Duyệt các chốt quyết định (Decision Gates) khi worker gửi câu hỏi (`ask`).
  5. Thẩm định code diff cuối cùng và ký duyệt release/merge.

### 2.2. Tầng Orca ADE Runtime (Runtime Layer)
Orca hoạt động như một hệ điều hành thu nhỏ dành cho các AI Agent (Agent Development Environment):
* **`Run`:** Là không gian làm việc bền vững (durable namespace) lưu trữ toàn bộ lịch sử trao đổi, danh sách task và trạng thái của một phiên làm việc.
* **`Task & Dispatch`:** Mỗi task có vòng đời chặt chẽ (`pending` ➔ `ready` ➔ `dispatched` ➔ `completed` / `failed`). Mỗi lần chạy thử nghiệm một task là một `Dispatch`.
* **`Git Worktree Isolation Engine`:** Tự động tạo thư mục git checkout riêng cho từng task thông qua `git worktree add`. Đảm bảo code của các agent không bao giờ đè lên nhau, không cần stash, không lo xung đột nhánh.
* **`Lifecycle Mailbox`:** Hộp thư trao đổi typed message chuẩn hóa giữa Coordinator và Workers (`heartbeat`, `escalation`, `ask`, `worker_done`).

### 2.3. Tầng Thực Thi (Worker Swarm Layer)
Bao gồm các agent chuyên biệt được khởi chạy có giám sát:
* Chạy trong terminal riêng của Orca (`orca orchestration worker-start`).
* Được cấp các cờ tự động hóa (ví dụ `--auto` cho Kimi, `--dangerously-skip-permissions` cho Claude).
* Tuân thủ **Worker Contract**:
  - Gửi `heartbeat` định kỳ để báo còn sống.
  - Kiểm tra chỉ đạo mới từ Leader qua `orca orchestration check`.
  - Kết thúc bằng một báo cáo `worker_done` cấu trúc 3 câu ngắn gọn.

### 2.4. Tầng Tiện Ích Bản Địa (Orca Native Utilities)
* **Embedded Browser & Design Mode:** Cung cấp browser engine gắn liền với từng worktree. Công cụ Design Mode chuyển đổi giao diện UI trực tiếp thành ngữ cảnh code cho agent.
* **SSH Remote Host Bridge:** Cho phép worktree và agent chạy trên máy chủ remote (như OrbStack Linux container hay GPU server) trong khi giao diện editor vẫn mượt mà trên macOS.
* **Native GitHub / GitLab Integration:** Đồng bộ trực tiếp issue, PR và notification bell mà không cần token API phụ từ bên ngoài.
