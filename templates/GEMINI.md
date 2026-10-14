# Orca Full-Spectrum Multi-Agent Orchestration Directive

This project operates under the Orca Agent Development Environment (ADE) with a full-spectrum team topology.
Follow this operational runbook for all feature development, bug fixing, and code review in this workspace.

---

## 👥 Configured Agent Team Topology

| Role | Agent | Concurrency | Capabilities & Orca Utilities |
| :--- | :--- | :--- | :--- |
| **Lead Coordinator** | `{{COORD_AGENT}}` | 1 | Task DAG, Run Mailbox, Decision Gates |
| **Full-Stack Devs** | `{{DEV_AGENT}}` | {{DEV_COUNT}} parallel | Isolated child worktrees, code implementation, unit tests |
| **QA & Testers** | `{{QA_AGENT}}` | {{QA_COUNT}} parallel | Integration test suites, browser verification, acceptance tests |
| **Security & Bug Hunter** | `{{SEC_AGENT}}` | {{SEC_COUNT}} parallel | OWASP audit, concurrency checks, memory leak prevention |
| **PR Reviewer** | `{{PR_AGENT}}` | On-demand | PR checkout (`--pr`), line-by-line diff review, CI verification |
| **Issue Resolver** | `{{ISSUE_AGENT}}` | On-demand | Issue checkout (`--issue`), auto-reproduce, commit & push |
| **UI/UX Specialist** | `{{UI_AGENT}}` | On-demand | Design Mode (Click-to-code), DOM & CSS live inspection |
| **DevOps Specialist** | `{{DEVOPS_AGENT}}` | On-demand | DB migrations, Docker/Orb, SSH worktrees (`--host {{EXEC_HOST}}`) |

- **Default Execution Host:** `{{EXEC_HOST}}`
- **Orca CLI Path:** `/usr/local/bin/orca`

---

## 🚀 Playbooks for Core Workflows

### 1. Feature Development (DAG Orchestration)
```bash
# 1. Initialize Run
RUN_ID=$(/usr/local/bin/orca orchestration run-create --objective "<Feature Objective>" --json)

# 2. Create atomic tasks
DEV_TASK=$(/usr/local/bin/orca orchestration task-create --task-title "Dev: Implement Feature" --spec "<Spec>" --json)
QA_TASK=$(/usr/local/bin/orca orchestration task-create --task-title "QA: Verification" --spec "<Criteria>" --depends-on <devTaskId> --json)

# 3. Dispatch workers (parallel child worktrees)
/usr/local/bin/orca orchestration worker-start \
  --task <devTaskId> \
  --worktree new-child \
  --name "feature-dev" \
  --agent {{DEV_AGENT}} \
  --setup run \
  --json
```

### 2. Fixing Assigned Git Issues (Issue Auto-Resolver)
Whenever an issue is assigned:
```bash
/usr/local/bin/orca worktree create \
  --name "issue-<id>-fix" \
  --issue <id> \
  --agent {{ISSUE_AGENT}} \
  --setup run \
  --json

/usr/local/bin/orca worktree set --worktree active --comment "Reproduced bug; implementing fix" --json
```

### 3. Reviewing Pull Requests (PR Reviewer)
Whenever a PR is opened or requested for review:
```bash
/usr/local/bin/orca worktree create \
  --name "review-pr-<id>" \
  --pr <id> \
  --agent {{PR_AGENT}} \
  --setup run \
  --json

/usr/local/bin/orca file open-changed --mode diff --worktree active --json
```

### 4. UI Bug Fix with Design Mode & Browser
```bash
/usr/local/bin/orca goto --url "http://localhost:3000" --worktree active --json
/usr/local/bin/orca snapshot --worktree active --json
/usr/local/bin/orca screenshot --worktree active --json
```

---

## 🔀 Concurrent Dual-Track Policy (Chính sách Đa luồng Song song)

Cơ chế phát hiện Issue chính thức: **Orca Native GitHub Integration (Settings → Integrations → GitHub) & Bell Inbox**.
Khi tài khoản GitHub được kết nối trong Orca, app tự động đồng bộ Issues được gán vào Notification Bell và Activity Feed của workspace.

Nếu trong lúc Leader đang điều phối / review Feature mà có **GitHub Issue mới được gán**:

1. **Nguyên tắc Không Can thiệp (Zero Interference):**
   - TUYỆT ĐỐI KHÔNG dừng luồng Feature đang chạy, không chuyển branch, không stash code.
2. **Kích hoạt nhánh song song tức thì (Parallel Fork qua Native Issue Linking):**
   - Leader khởi chạy ngay `{{ISSUE_AGENT}}` bằng cờ `--issue <id>` và `--no-parent` để Orca tự động tạo worktree độc lập với context từ GitHub:
     ```bash
     /usr/local/bin/orca worktree create \
       --name "hotfix-issue-<id>" \
       --issue <id> \
       --no-parent \
       --agent {{ISSUE_AGENT}} \
       --setup run \
       --json
     ```
3. **Theo dõi đa luồng độc lập (Dual Monitoring):**
   - Leader tiếp tục vòng review Feature hiện tại.
   - Nhánh Issue tự động nhân bản, reproduce bug, sửa code và test riêng.
   - Trạng thái tiến độ tự động gắn vào Issue qua `orca worktree set --comment "..."`.
   - Thông báo ngắn gọn cho User: *"Đã nhận diện Issue #<id> từ Orca GitHub Inbox, khởi tạo nhánh song song hotfix-issue-<id>. Luồng review Feature vẫn tiếp diễn bình thường."*
4. **Merge độc lập:**
   - Luồng nào hoàn thành và pass test trước thì Leader review và tạo PR merge trước, không đợi nhau.

---

## 🛑 Issue Reassignment & Handoff Policy (Chính sách Đổi người & Bàn giao)

Nếu một Issue đang được `{{ISSUE_AGENT}}` sửa mà bị **gỡ assign hoặc gán cho người khác** (human dev hoặc team khác):

1. **Dừng worker an toàn (Stop to Save Compute):**
   - Dừng ngay tiến trình agent để tiết kiệm token và tránh xung đột code:
     ```bash
     /usr/local/bin/orca orchestration worker-stop --dispatch <dispatch_id> --json
     ```
2. **Commit bàn giao (Handoff Branch):**
   - Nếu đã có code phác thảo hoặc tài liệu tái hiện lỗi (reproduction steps):
     - Tạo branch bàn giao: `git checkout -b wip/issue-<id>-handover`
     - Commit toàn bộ thay đổi dở dang và push lên remote:
       ```bash
       git add . && git commit -m "wip: draft fix and reproduction notes before reassignment"
       git push origin wip/issue-<id>-handover
       ```
     - Gửi comment tóm tắt lên GitHub Issue:
       *"Issue đã được chuyển giao cho assignee mới. AI agent đã lưu tiến độ dở dang và ghi chú tái hiện lỗi tại branch `wip/issue-<id>-handover`."*
3. **Giải phóng tài nguyên Worktree:**
   - Giải phóng terminal: `/usr/local/bin/orca orchestration worker-release --dispatch <dispatch_id> --json`
   - Xóa hoặc archive worktree: `/usr/local/bin/orca worktree rm --worktree active --json`
   - Leader quay trở về tiếp tục 100% tài nguyên cho luồng Feature chính.

---

## 🚨 Worker Escalation & Blocker Handling Policy (Chính sách Xử lý Bế tắc & Sự cố Worker)

Khi một worker (Dev hoặc QA) gặp vấn đề không tự giải quyết được:
1. **Worker gửi câu hỏi/escalation:**
   - Worker gửi typed message: `orca orchestration send --type ask --text "<Nội dung vướng mắc>"`
2. **Leader kiểm tra định kỳ (Check Mailbox):**
   - Leader chạy `orca orchestration check --json` để đọc tin nhắn từ workers.
3. **Leader phân loại và xử lý:**
   - **Tự giải quyết được:** Leader gửi chỉ đạo trực tiếp: `orca orchestration send --to <worker> --type steer --text "<Hướng giải quyết>"`
   - **Cần ý kiến người dùng:** Leader tạm dừng và hỏi người dùng (Human Gate), sau đó chuyển tiếp kết luận cho worker.
   - **Worker bị crash hoặc lặp vô tận:** Leader đọc log bằng `orca terminal read --terminal <handle>`, sau đó dừng bằng `orca orchestration worker-stop --dispatch <id>` và điều phối lại.

---

## 🩺 Worker Health Watchdog & Hung Agent Recovery Policy (Chính sách Giám Sát & Phục Hồi Agent Bị Treo)

Khi khởi chạy worker (Dev, QA, Security), Leader phải chủ động kiểm soát nguy cơ agent bị dừng giữa chừng (hết token, kẹt câu hỏi [y/n], crash hoặc lặp lỗi):

1. **Luôn đặt Timeout chặn trên khi khởi chạy:**
   Mọi lệnh `worker-start` phải kèm cờ `--timeout-ms 900000` (15 phút) để tránh terminal chạy ngầm vĩnh viễn:
   ```bash
   /usr/local/bin/orca orchestration worker-start \
     --task <taskId> \
     --worktree new-child \
     --name "dev-worker" \
     --agent {{DEV_AGENT}} \
     --setup run \
     --timeout-ms 900000 \
     --json
   ```

2. **Quy tắc Giám sát Nhịp tim (Watchdog Heartbeat & Mailbox):**
   - Định kỳ kiểm tra: `/usr/local/bin/orca orchestration check --json`
   - Nếu quá 3 phút không nhận được tin nhắn heartbeat hoặc `worker_done`, Leader lập tức thanh tra:
     ```bash
     /usr/local/bin/orca orchestration worker-show --dispatch <dispatch_id> --json
     ```

3. **Chẩn đoán & Xử lý 3 kịch bản dừng:**
   - **Kịch bản A: Agent bị kẹt câu hỏi xác nhận (`observation.agentWait != null`):**
     Agent đang đợi trả lời `[y/N]` hoặc xin cấp quyền trong terminal. Leader gửi phím xác nhận tự động:
     ```bash
     /usr/local/bin/orca terminal send --terminal <handle> --text "y" --enter
     ```
   - **Kịch bản B: Agent cạn Token / Tràn Context Window / Bị ngắt câu trả lời:**
     Leader đọc 50 dòng log cuối bằng:
     ```bash
     /usr/local/bin/orca orchestration worker-read --dispatch <dispatch_id> --source auto --limit 50 --json
     ```
     Nếu thấy dấu hiệu token limit:
     a. Dừng worker cũ an toàn: `/usr/local/bin/orca orchestration worker-stop --dispatch <dispatch_id> --json`
     b. Bảo toàn worktree hiện tại (mã nguồn dở dang đã lưu trên worktree).
     c. Khởi chạy worker mới tiếp quản ngay trên worktree đó bằng cờ `--retry-of` và `--worktree current`:
     ```bash
     /usr/local/bin/orca orchestration worker-start \
       --task <taskId> \
       --retry-of <dispatch_id> \
       --worktree current \
       --agent {{DEV_AGENT}} \
       --setup run \
       --comment "Resume implementation after previous worker hit token limit" \
       --timeout-ms 900000 \
       --json
     ```
     Worker mới sẽ tự động đọc `git status`/`git log` trong worktree, kế thừa phần việc dở dang và tiếp tục code đến khi hoàn thành.
   - **Kịch bản C: Vòng lặp lỗi không dứt (Deadlock / Linter loop > 5 lần):**
     Leader dừng worker bằng `worker-stop`, thông báo tóm tắt lỗi cho Người dùng (Human Gate), đề xuất giải pháp (như đổi thư viện, nới lỏng linter) trước khi tái kích hoạt.

---

### 5. Quality Gate & Release
- All workers must send: `orca orchestration send --type worker_done --outcome succeeded`.
- Security audit (`{{SEC_AGENT}}`) must report 0 Critical/High issues.
- Clean up settled workers: `orca orchestration worker-release --dispatch <dispatch_id> --json`.
