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
* **Thành phần chính:** Model cấp cao đảm nhiệm vai trò **Lead Coordinator (`Principal Chief Architect`)** (ví dụ Claude Opus, Gemini Argon, hoặc OpenAI Codex).
* **Nhiệm vụ & Thẩm quyền Quyết định Trung tâm (Sole Technical & Orchestration Decision-Maker):**
  1. Phân tích yêu cầu từ người dùng hoặc issue trên GitHub, lập bản kế hoạch kiến trúc (`plan.md`) và chia nhỏ thành các nhiệm vụ nguyên tử (atomic tasks).
  2. Sử dụng lệnh `orca orchestration run-create` và `task-create` để nạp đồ thị phụ thuộc (DAG) vào Orca.
  3. **Là Người Ra Quyết Định Kỹ Thuật & Điều Phối Duy Nhất trong các bước thực thi (Steps 1–3):**
     - Giải đáp câu hỏi kiến trúc (`question` từ `orca orchestration ask`) qua `orca orchestration reply --id <MSG_ID>`.
     - Kiểm duyệt kết quả bàn giao (`handoff`) trước khi truyền cho role kế tiếp.
     - Phân xử các cảnh báo phủ quyết chuyên môn (`escalation` từ DevOps/QA/Sec/Bench/RE) và quyết định điều phối bản vá (`send --to dispatch:<LIVE_ID> --type dispatch` cho worker đang chạy hoặc `task-create` + `worker-start` cho worker đã kết thúc).
  4. Thẩm định code diff cuối cùng (`orca file open-changed --mode diff`) và trình **Human Gate** (`orca orchestration gate-create`) để người dùng (Human User) ra quyết định release/merge ở Bước 4.

### 2.2. Tầng Orca ADE Runtime (Runtime Layer)
Orca hoạt động như một hệ điều hành thu nhỏ dành cho các AI Agent (Agent Development Environment):
* **`Run`:** Là không gian làm việc bền vững (durable namespace) lưu trữ toàn bộ lịch sử trao đổi, danh sách task và trạng thái của một phiên làm việc.
* **`Task & Dispatch`:** Mỗi task có vòng đời chặt chẽ (`pending` ➔ `ready` ➔ `dispatched` ➔ `completed` / `failed`). Mỗi lần chạy thử nghiệm một task là một `Dispatch`.
* **`Git Worktree Isolation Engine`:** Tự động tạo thư mục git checkout riêng cho từng task thông qua `git worktree add`. Đảm bảo code của các agent không bao giờ đè lên nhau, không cần stash, không lo xung đột nhánh.
* **`Lifecycle Mailbox`:** Hộp thư trao đổi typed message chuẩn hóa theo hàng đợi FIFO giữa Coordinator và Workers (`heartbeat`, `question` / `ask` / `reply`, `handoff`, `escalation`, `dispatch`, `worker_done`) với cơ chế xác nhận `--ack <delivery_id>`.

### 2.3. Tầng Thực Thi (Worker Swarm Layer)
Bao gồm các agent chuyên biệt được khởi chạy có giám sát:
* Chạy trong terminal riêng của Orca (`orca orchestration worker-start`).
* Được cấp các cờ tự động hóa (ví dụ `--auto` cho Kimi, `--dangerously-skip-permissions` cho Claude).
* Tuân thủ **Orca Worker Contract**:
  - Giữ nguyên bộ cờ định danh từ Orca Preamble (`--from <worker_handle> --task-id <task_id> --dispatch-id <dispatch_id>` cho `send`/`ask` và `--terminal <worker_handle>` cho `check`).
  - Gửi `heartbeat` định kỳ để báo còn sống và kiểm tra chỉ đạo mới từ Leader qua `orca orchestration check --terminal <worker_handle> --peek --json`.
  - Gửi `handoff` (kèm Commit Hash / Clean-Room Spec / Runtime Log Audit) hoặc thực thi Quyền Phủ quyết Chuyên môn qua `escalation` về hộp thư Run của Lead Coordinator.
  - Kết thúc bằng một báo cáo `worker_done` cấu trúc 3 câu ngắn gọn và dừng kiểm tra hộp thư sau khi đã gửi `worker_done`.

### 2.4. Tầng Tiện Ích Bản Địa (Orca Native Utilities)
* **Embedded Browser & Design Mode:** Cung cấp browser engine gắn liền với từng worktree. Công cụ Design Mode chuyển đổi giao diện UI trực tiếp thành ngữ cảnh code cho agent.
* **SSH Remote Host Bridge:** Cho phép worktree và agent chạy trên máy chủ remote (như OrbStack Linux container hay GPU server) trong khi giao diện editor vẫn mượt mà trên macOS.
* **Native GitHub / GitLab Integration:** Đồng bộ trực tiếp issue, PR và notification bell mà không cần token API phụ từ bên ngoài.
