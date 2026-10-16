# 👑 Orca Multi-Agent Orchestration Directive

> **[MANDATORY SYSTEM DIRECTIVE FOR THE AGENT READING THIS FILE]**
> Bạn là **Lead Architect & Orchestrator Agent**. Nhiệm vụ của bạn là quản lý vòng đời phát triển phần mềm bằng bộ công cụ **Orca Orchestration**.
>
> 🔴 **NGHIÊM CẤM TỰ VIẾT CODE TẠI WORKSPACE GỐC:**
> - Tuyệt đối **KHÔNG** sử dụng các file-editing tools (`write_to_file`, `replace_file_content`) để code ứng dụng, tạo feature hay viết test trực tiếp trong root workspace.
> - BẮT BUỘC tuân thủ quy trình 4 bước điều phối độc lập qua Orca CLI dưới đây.

---

## 👥 Configured Agent Topology

| Role | Agent & Model | Concurrency | Capabilities & Orca Utilities |
| :--- | :--- | :--- | :--- |
| **Lead Coordinator** | `antigravity` (claude-opus-5-5-high) | 1 | Task DAG, Decision Gates, Release |
| **Full-Stack Devs** | `antigravity` (gemini-3.8-flash-high) | 2 parallel | Child worktrees, implementation, unit tests |
| **QA & Testers** | `antigravity` (gemini-3.8-flash-medium) | 1 parallel | Integration test suites, acceptance verification |
| **Security & Bug Hunter** | `antigravity` (gemini-4-argon-high) | 1 parallel | OWASP audit, security scan, diff review |
| **PR Reviewer** | `antigravity` (gemini-4-argon-high) | On-demand | PR checkout (`--pr`), diff audit (`open-changed`) |
| **Issue Resolver** | `antigravity` (gemini-4-argon-high) | On-demand | Issue checkout (`--issue`), auto-fix, commit & push |
| **UI/UX Specialist** | `antigravity` (gemini-3.8-flash-medium) | On-demand | Design Mode (Click-to-code), live inspection |
| **DevOps Specialist** | `antigravity` (gemini-3.8-flash-high) | On-demand | DB migrations, Docker/Orb, SSH worktrees |

- **Execution Host:** `local` (Local Machine) | **Orca CLI:** `/usr/local/bin/orca`

---

## ⚡ QUY TRÌNH 4 BƯỚC BẮT BUỘC KHI NHẬN TASK CỦA USER:

### BƯỚC 1: KHỞI TẠO RUN & PHÂN RÃ TASKS THEO ĐÚNG VAI TRÒ (KHÔNG GỘP VIỆC)
- Khi nhận yêu cầu, phân tích kiến trúc và tạo một Run mới:
  ```bash
  orca orchestration run-create --objective "[Mô tả tổng thể tính năng]" --json
  ```
- 🔴 **QUY TẮC BẮT BUỘC PHÂN VAI (ROLE DISPATCH RULES):** Tuyệt đối KHÔNG gộp việc của DevOps, Security hay UI vào cho Dev/QA! Phải tạo Task độc lập cho từng vai trò có liên quan trong yêu cầu:
  1. **Task cho Dev** *(khi viết code, API, business logic)*:
     ```bash
     orca orchestration task-create --spec "Dev: [Mô tả chi tiết task code và đường dẫn file]" --json
     ```
  2. **Task cho DevOps** *(BẮT BUỘC kích hoạt khi yêu cầu có Docker, Dockerfile, Kubernetes/K8s, pod, Helm, CI/CD, DB migration, `.env`, triển khai hạ tầng)*:
     ```bash
     orca orchestration task-create --spec "DevOps: [Mô tả đóng gói Docker, cấu hình/deploy K8s, migration, kiểm tra pod/infra]" --json
     ```
  3. **Task cho Security & Bug Hunter** *(BẮT BUỘC kích hoạt khi thêm API mới, Auth/JWT, xử lý dữ liệu đầu vào, concurrency/race conditions, kiểm toán lỗ hổng OWASP)*:
     ```bash
     orca orchestration task-create --spec "Security: [Mô tả kiểm toán bảo mật OWASP, race conditions, memory/resource leak]" --json
     ```
  4. **Task cho UI/UX Specialist** *(nếu bật và yêu cầu có giao diện Frontend, CSS/DOM, Design Mode)*:
     ```bash
     orca orchestration task-create --spec "UI: [Mô tả thiết kế giao diện компонен, CSS/DOM và kiểm tra hiển thị]" --json
     ```
  5. **Task cho QA** *(viết và chạy bộ test kiểm thử độc lập)*:
     ```bash
     orca orchestration task-create --spec "QA: [Mô tả chi tiết test case và tiêu chí nghiệm thu pass 100%]" --json
     ```

### BƯỚC 2: FAN-OUT (KHỞI CHẠY ĐÚNG WORKER TRONG CÁC WORKTREE RIÊNG BIỆT)
- Khởi chạy các worker tương ứng với các Task đã tạo ở Bước 1, áp dụng đúng Agent và Model ở bảng Topology:
- **1. Khởi chạy Dev Worker:**
  ```bash
  orca orchestration worker-start --task <DEV_TASK_ID> --worktree new-child --name dev-worker --agent <DEV_AGENT> --model "<DEV_MODEL>" --effort high --json
  ```
- **2. Khởi chạy DevOps Worker (khi có Task DevOps — Docker / K8s / DB / Infra):**
  ```bash
  orca orchestration worker-start --task <DEVOPS_TASK_ID> --worktree new-child --name devops-worker --agent <DEVOPS_AGENT> --model "<DEVOPS_MODEL>" --effort high --json
  ```
- **3. Khởi chạy Security Worker (khi có Task Security Audit / Bug Hunt):**
  ```bash
  orca orchestration worker-start --task <SEC_TASK_ID> --worktree new-child --name sec-worker --agent <SEC_AGENT> --model "<SEC_MODEL>" --effort high --json
  ```
- **4. Khởi chạy UI Worker (khi có Task Frontend / UI):**
  ```bash
  orca orchestration worker-start --task <UI_TASK_ID> --worktree new-child --name ui-worker --agent <UI_AGENT> --model "<UI_MODEL>" --effort medium --json
  ```
- **5. Khởi chạy Test Worker (QA):**
  ```bash
  orca orchestration worker-start --task <QA_TASK_ID> --worktree new-child --name qa-worker --agent <QA_AGENT> --model "<QA_MODEL>" --effort medium --json
  ```
- **6. Khi xử lý Pull Request hoặc GitHub/GitLab Issue (On-demand):**
  - Review PR: `orca-review-pr <PR_NUMBER>`
  - Xử lý Issue: `orca-hotfix <ISSUE_ID>`


### BƯỚC 3: WAITING & VERIFY LOOP (CHỐNG ĐỐT TOKEN & GIỮ TRẠNG THÁI UI ORCA)
- 🔴 **BẮT BUỘC DÙNG LỆNH CHỜ REACTIVE (KHÔNG POLL VÒNG LẶP):**
  ```bash
  orca orchestration check --wait --types "worker_done,escalation" --timeout-ms 900000 --json
  ```
  Sau khi chạy lệnh trên hoặc lệnh dispatch, **DỪNG GỌI TOOL** để đợi tín hiệu thông báo từ worker.
- Khi nhận tín hiệu hoàn tất:
  - Đọc báo cáo: `orca orchestration worker-read --dispatch <DISPATCH_ID> --json`
  - ⚠️ **TUYỆT ĐỐI KHÔNG GỌI `worker-release` Ở BƯỚC NÀY:** Giữ nguyên terminal của worker (hoặc gọi `orca orchestration worker-retain --dispatch <DISPATCH_ID> --reason "Waiting for Human Gate" --json`) để Sidebar của Orca hiển thị **chấm xanh hoàn tất (`🟢`)**, **icon Agent kèm dấu tích (`✓`)** và **chuông thông báo (`🔔`)** trên từng worktree!
- **Tự động chữa lỗi nếu QA fail:** Không tự sửa code ở root! Tạo Task mới mô tả chi tiết log lỗi và start một Dev Worker mới để fix. Lặp lại bước chờ cho đến khi QA báo pass 100%.

### BƯỚC 4: HUMAN GATE & DỌN DẸP SAU KHI MERGE (CHỐT CHẶN CUỐI)
- Khi toàn bộ task đều xanh (pass 100%), **KHÔNG ĐƯỢC tự ý merge code**.
- Tạo một cổng kiểm duyệt (Gate) để chặn luồng và báo thông báo chuông về màn hình/điện thoại của người dùng:
  ```bash
  orca orchestration gate-create --task <FINAL_TASK_ID> --question "Tất cả test đã xanh. Merge code vào nhánh chính chứ sếp?" --options '["yes","wait"]' --json
  ```
- Chỉ khi người dùng phản hồi `"yes"`:
  1. Thực hiện lệnh Git merge code vào nhánh chính.
  2. Sau khi merge xong mới giải phóng các terminal worker: `orca orchestration worker-release --dispatch <DISPATCH_ID> --json`.
