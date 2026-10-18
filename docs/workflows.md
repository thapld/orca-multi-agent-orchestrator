# 🔄 Sơ Đồ Quy Trình Vận Hành Chi Tiết (Workflows & Diagrams)

Tài liệu này tập hợp đầy đủ tất cả các quy trình vận hành trong hệ thống Orca Multi-Agent Orchestration dưới dạng sơ đồ Mermaid trực quan.

---

## 1. Quy Trình Phát Triển Tính Năng Mới (Feature DAG Orchestration)

Quy trình chuẩn khi tiếp nhận một yêu cầu phát triển tính năng lớn, phân rã công việc cho nhiều Dev và QA chạy song song.

```mermaid
sequenceDiagram
    autonumber
    actor User as 👤 Người dùng
    participant Lead as 🏛️ Lead Coordinator
    participant Critic as 🔎 critic-worker (Quân sư System Eng)
    participant Orca as 🐋 Orca Runtime & Pinned Plan
    participant Devs as 💻 Dev Swarm (1–N)
    participant QA as 🧪 QA Swarm (Kiểm toán Plan 100%)
    participant Sec as 🔍 Security Auditor

    User->>Lead: Yêu cầu tính năng / sửa lỗi mới
    Lead->>Orca: orca orchestration run-create & task-create (critic-worker)
    Lead->>Critic: Trình Bản nháp Plan [P1..Pn] + Bằng chứng thô
    Critic->>Critic: Tra cứu System Eng KB (orca-team kb) & điều tra độc lập
    Critic<<->>Lead: Vòng lặp thảo luận 2 chiều (ask / reply) hiệu chỉnh Plan
    Critic->>Orca: send --type handoff (CRITIC_VERDICT: CONSENSUS_REACHED) & --type worker_done
    Lead-->>User: Trình Plan đã đồng thuận để Người dùng chốt (nếu cần)
    Lead->>Orca: orca-team plan pin (Ghim Pinned Master Plan [P1..Pn] công khai cho toàn team)
    Lead->>Orca: orca orchestration task-create (Tách Task riêng cho Dev, DevOps, Sec, QA)

    par Khởi chạy Đợt 1 — Workers viết code ($(orca-team worktree-arg <role>))
        Lead->>Orca: worker-start Dev $(orca-team worktree-arg dev) (dev-worker, dev-worker-2..N)
        Devs->>Orca: orca-team plan show (Đọc Pinned Master Plan [P1..Pn] & Danh bạ P2P)
    end

    Devs->>Orca: send --from <handle> --type handoff --subject "Dev Handoff" & send --type worker_done --outcome succeeded
    Lead->>Orca: check [--ack <DELIVERY_ID>] --wait & worker-retain

    Lead->>Orca: Khởi chạy Đợt 2 — worker-start QA, Sec & DevOps ($(orca-team worktree-arg qa/sec/devops))
    QA->>Orca: orca-team merge-upstream --role qa (In Pinned Master Plan [P1..Pn] + Merge nhánh Dev)
    loop Phối hợp P2P nội bộ Team (Tối đa 3 vòng nếu thiếu mục [Pk] hoặc lỗi test)
        QA->>Devs: send --to '@all' --type handoff --subject "QA_REJECT vòng n/3" (Trả bài P2P)
        Devs->>QA: Vá bổ sung + commit + nhắn P2P "DEV_READY"
    end
    QA->>Orca: send --from <handle> --type handoff --subject "QA_FINAL: PLAN 100% & TESTS PASS" & send --type worker_done --outcome succeeded
    Sec->>Orca: send --from <handle> --type worker_done --outcome succeeded (SECURITY_VERDICT: PASS)

    Lead->>Orca: Verify lại lần cuối (Đối chiếu Bảng QA_FINAL [P1..Pn] + git log/diff --stat)
    Lead->>Orca: orca orchestration gate-create (Xin phép Human User trước khi merge)
    User->>Lead: Duyệt Merge (Approved)
    Lead->>Orca: gate-resolve, task-update --status completed, orca-team merge-upstream --to-main & release-workers
    Lead-->>User: Báo cáo hoàn tất 100% Plan [P1..Pn]
```

---

## 2. Quy Trình Đa Luồng Song Song (Concurrent Dual-Track Policy)

Tình huống: Leader đang trong phiên điều phối/review Feature thì bất ngờ có một **GitHub Issue P0/Hotfix** được gán cho bạn.

```mermaid
flowchart TD
    subgraph Track1 ["LUỒNG 1: FEATURE ĐANG CHẠY (BẢO LƯU 100%)"]
        FeatureWork["Worktree: feature-checkout\nDevs đang code & Lead đang review"]
    end

    Event["🔔 Sự kiện GitHub: Issue #42 được assign\n'Lỗi crash trang thanh toán'"] --> NativeDetect["Orca Native GitHub Sync\n(Chấm đỏ Bell Inbox & Dock badge)"]
    
    NativeDetect --> Triage{"Lead Architect Triage"}

    Triage -- "P0 Hotfix / Cần sửa gấp" --> Fork["TÁCH NHÁNH SONG SONG NGAY LẬP TỨC\n(Zero Interference)"]
    
    subgraph Track2 ["LUỒNG 2: ISSUE HOTFIX (CÔ LẬP TRÊN WORKTREE MỚI)"]
        Fork --> CreateWorktree["orca worktree create --name hotfix-issue-42 --issue 42 --no-parent --agent $ISSUE_AGENT"]
        CreateWorktree --> AutoPull["Orca tự động kéo mô tả Issue từ GitHub vào làm context"]
        AutoPull --> IssueWorker["Issue Resolver Agent tái hiện lỗi & viết patch"]
        IssueWorker --> TestFix["Chạy test kiểm thử bản vá"]
    end

    Track1 --> Merge1["Feature xong trước ➔ Merge Feature trước"]
    TestFix --> Merge2["Issue xong trước ➔ Merge & Đóng Issue trước"]
    
    Merge1 --> Independent["2 luồng hoàn toàn độc lập, không đợi nhau"]
    Merge2 --> Independent
```

---

## 3. Quy Trình Đổi Người & Bàn Giao (Issue Reassignment & Handoff Policy)

Tình huống: Issue #42 đang được agent sửa dở dang thì bị Project Manager gán lại cho một lập trình viên khác.

```mermaid
flowchart TD
    ReassignEvent["Phát hiện Issue #42 được gán cho người khác (@developer_b)"] --> StopAgent["1. DỪNG AGENT NGAY LẬP TỨC\norca orchestration worker-stop --dispatch <id>\n(Tiết kiệm token & tránh conflict)"]
    
    StopAgent --> AssessState{"Tiến độ hiện tại thế nào?"}
    
    AssessState -- "Chưa viết code / Mới đọc issue" --> DirectRelease["Hủy bỏ nhẹ nhàng"]
    
    AssessState -- "Đã có code phác thảo / Tìm ra root cause" --> MakeHandoff["2. ĐÓNG GÓI TIẾN ĐỘ BÀN GIAO\n• Tạo branch: wip/issue-42-handover\n• Commit toàn bộ code dở dang & logs\n• git push origin wip/issue-42-handover"]
    
    MakeHandoff --> PostComment["Gửi comment lịch sự lên GitHub Issue:\n'Đã chuyển giao cho @developer_b. AI agent đã lưu tiến độ nháp\nvà ghi chú tái hiện lỗi tại branch wip/issue-42-handover.'"]
    
    PostComment --> DirectRelease
    
    DirectRelease --> Cleanup["3. GIẢI PHÓNG WORKTREE TRONG ORCA\n• orca orchestration worker-release --dispatch <id>\n• orca worktree rm --worktree active"]
    
    Cleanup --> ResumeMain["Leader quay lại tập trung 100% cho luồng Feature chính"]
```

---

## 4. Quy Trình Thẩm Định Pull Request (PR Reviewer)

Sử dụng agent chuyên biệt để checkout PR vào worktree riêng, chạy kiểm tra CI và soi diff từng dòng.

```mermaid
flowchart LR
    PROpen["Pull Request #123 được tạo"] --> CheckoutPR["orca worktree create --name review-pr-123 --pr 123 --no-parent --agent $PR_AGENT"]
    CheckoutPR --> RunCI["Chạy test suite nội bộ trong worktree PR"]
    RunCI --> DiffAudit["Soi diff chi tiết từng dòng qua Orca Annotate AI Diff"]
    DiffAudit --> Verdict{"Kết quả thẩm định"}
    Verdict -- "Có lỗ hổng / Thiếu test" --> CommentBack["Đăng comment yêu cầu sửa lên PR"]
    Verdict -- "Đạt chuẩn" --> ApprovePR["Ký duyệt & Gọi API: github.markPRReadyForReview"]
```

---

## 5. Quy Trình Sửa Lỗi UI Với Design Mode & Embedded Browser

Khai thác tối đa tiện ích đồ họa độc quyền của Orca để sửa lỗi visual bug chỉ bằng một cú click chuột.

```mermaid
sequenceDiagram
    autonumber
    actor Dev as 🖥️ UI/UX Specialist
    participant Browser as 🌐 Orca Embedded Browser
    participant DesignMode as 🎯 Orca Design Mode
    participant Agent as 🤖 Agent Terminal

    Dev->>Browser: orca goto --url http://localhost:3000
    Dev->>DesignMode: Bật nút Design Mode trên thanh công cụ
    Dev->>Browser: Di chuột và CLICK vào phần tử bị lỗi giao diện
    DesignMode->>DesignMode: Trích xuất HTML snippet + CSS computed + Screenshot
    DesignMode->>Agent: Tự động đính kèm toàn bộ dữ liệu vào Agent Chat
    Dev->>Agent: "Sửa màu nút này thành xanh dương và chỉnh padding 12px"
    Agent->>Agent: Đọc mã nguồn, sửa file CSS/Component
    Agent->>Browser: Hot-reload trang web
    Dev->>Browser: orca screenshot (Xác nhận giao diện đã chuẩn)
```

---

## 6. Quy Trình Ra Quyết Định Trung Tâm, Bàn Giao & Xử Lý Sự Cố (3-Tier Decision Hierarchy & Escalation)

Tình huống: Worker (Dev/DevOps/QA/Sec/Bench/RE) cần quyết định kiến trúc (`ask`), gửi kết quả bàn giao (`handoff`), hoặc thực thi Quyền Phủ quyết Chuyên môn (`escalation`) khi phát hiện lỗi Runtime Log/Security/Performance. **Lead Coordinator điều phối và ra quyết định kỹ thuật** trong các bước thực thi (Steps 1–3) nhưng chịu đối trọng của `critic-worker` khi gặp trigger Anti-Autocracy (xem mục 6.1), và **Human User** giữ quyền quyết định cuối cùng (Step 4 `gate-create`, hoặc khi Leader–Critic bế tắc).

```mermaid
sequenceDiagram
    autonumber
    actor User as 👤 Người dùng (Human Gate)
    participant Lead as 🏛️ Lead Coordinator
    participant Orca as 🐋 Orca Mailbox & CLI
    participant Worker as 🤖 Worker (Dev/DevOps/QA/Sec/Bench/RE)

    Note over Lead,Orca: Lắng nghe sự kiện: orca orchestration check [--ack <DELIVERY_ID>] --wait --types "worker_done,escalation,question,handoff"

    alt Trường hợp A: Worker hỏi quyết định kiến trúc (Blocking Question)
        Worker->>Orca: orca orchestration ask --from <handle> --question "Cần làm rõ format JWT header" --json
        Orca-->>Lead: Sự kiện 'question' (id: <MSG_ID>)
        Lead->>Orca: orca orchestration reply --id <MSG_ID> --body "Quyết định của Chief Architect: Dùng Authorization: Bearer <token>" --json
        Orca-->>Worker: Mở khóa 'ask' và trả về câu trả lời của Coordinator
    else Trường hợp B: Worker trình duyệt kết quả bàn giao (Handoff Submission)
        Worker->>Orca: orca orchestration send --from <handle> --task-id <tid> --dispatch-id <did> --type handoff --subject "Dev Handoff" --body "DEV_HANDOFF: branch=dev-worker, commit=..." --json
        Orca-->>Lead: Sự kiện 'handoff'
        Lead->>Lead: Kiểm duyệt Commit Hash / Clean-Room Spec / Runtime Logs
        Lead->>Orca: Duyệt & Điều phối cho role kế tiếp (send --to dispatch:<LIVE_ID> --type dispatch hoặc task-create + worker-start $(orca-team worktree-arg <role>))
    else Trường hợp C: Worker thực thi Quyền Phủ quyết Chuyên môn (Domain Veto / Escalation)
        Worker->>Orca: orca orchestration send --from <handle> --task-id <tid> --dispatch-id <did> --type escalation --subject "Runtime Log Error" --body "DEVOPS_ALERT / QA_ALERT / SEC_BLOCK: <error_detail>" --json
        Orca-->>Lead: Sự kiện 'escalation'
        Lead->>Lead: Phân tích nguyên nhân gốc rễ & Phân xử xung đột liên role
        alt Worker cần sửa vẫn đang chạy (live)
            Lead->>Orca: orca orchestration send --to dispatch:<LIVE_DISPATCH_ID> --type dispatch --subject "Coordinator Directive" --body "<Chỉ dẫn vá lỗi>" --json
        else Worker cần sửa đã kết thúc (settled / đã gửi worker_done)
            Lead->>Orca: orca orchestration task-create & worker-start $(orca-team worktree-arg <role>) (Tái sử dụng slot rảnh hoặc tự đẻ <role>-worker-2 nếu bận)
        end
    else Trường hợp D: Vấn đề vượt phạm vi spec cần ý kiến con người
        Lead->>Orca: orca orchestration gate-create --task <tid> --question "..." --options '["approve","revise"]' --json
        Lead-->>User: Tham vấn người dùng (Human Gate)
        User->>Lead: Phản hồi quyết định
    end
```


### 6.1. Vòng Phản Biện Leader ↔ Critic (Anti-Autocracy & Dialectical Consensus)

Kích hoạt khi gặp 1 trong 4 trigger: (a) bug đã fix ≥1 lần vẫn tái phát, (b) escalation quy lỗi sang role khác hoặc bug liên tầng App ↔ DB ↔ Infra ↔ Network, (c) thay đổi schema/API contract/auth/payment hoặc chạm ≥2 tầng, (d) các worker báo cáo mâu thuẫn. Critic dùng `ask` (lệnh **chặn** tới khi Leader `reply`) nên đây là đối thoại 2 chiều thật; tối đa 3 vòng để chặn tranh cãi vô hạn đốt token.

```mermaid
sequenceDiagram
    autonumber
    actor User as 👤 Người dùng (Human Gate)
    participant Lead as 🏛️ Lead Coordinator
    participant Orca as 🐋 Orca Mailbox
    participant Critic as 🔎 critic-worker (Read-Only)

    Lead->>Orca: task-create [ROLE: PRINCIPAL RESEARCH & ADVERSARIAL CRITIC] gồm Triệu chứng + bằng chứng thô và Giả thuyết Leader
    Orca->>Critic: worker-start $(orca-team worktree-arg critic)
    Critic->>Critic: Lập giả thuyết độc lập TRƯỚC khi đọc giả thuyết Leader, kiểm chứng file:line, git blame, DB, runtime log
    loop Tối đa 3 vòng
        Critic->>Orca: ask --question "CRITIC_CHALLENGE vòng n/3: bằng chứng + phương án" (chặn chờ)
        Orca-->>Lead: Sự kiện question (id MSG_ID)
        Lead->>Orca: reply --id MSG_ID kèm phản chứng file:line/log HOẶC chấp nhận và sửa kế hoạch
        Orca-->>Critic: Mở khóa ask, trả về phản hồi của Leader
        Critic->>Critic: Tự kiểm chứng lại phản chứng của Leader
    end
    alt Đồng thuận
        Critic->>Orca: send --type handoff --subject "Critic Consensus" (CRITIC_VERDICT: CONSENSUS_REACHED)
        Orca-->>Lead: Sự kiện handoff
        Lead->>Orca: Khóa Spec, giao dev-worker / devops-worker
    else Bế tắc sau 3 vòng
        Critic->>Orca: send --type handoff --subject "Critic Deadlock" (CRITIC_VERDICT: HYPOTHESIS_CHALLENGED)
        Orca-->>Lead: Sự kiện handoff
        Lead->>Orca: gate-create --task <tid> --options '["leader","critic","wait"]' (CẤM Leader tự phán quyết)
        Orca-->>User: Trình 2 phương án + bằng chứng mỗi bên
        User->>Lead: Chọn phương án
    end
```

> **Giới hạn:** Đây là ràng buộc ở tầng chỉ dẫn + kiểm tra Zero-Trust; Orca CLI không chặn cứng việc Leader bỏ qua Critic. Dấu vết kiểm toán nằm ở hộp thư Run (`orca orchestration check --json`): thiếu `Critic Consensus`/`Critic Deadlock` cho một task có trigger là vi phạm có thể phát hiện được.

---

## 7. Quy Trình Giám Sát Sức Khỏe & Xử Lý Agent Bị Treo / Cạn Token (Watchdog & Hung Worker Recovery Policy)

Tình huống: Dev hoặc QA đang làm việc thì đột ngột dừng lại giữa chừng do:
* **Hết token / Tràn context window:** Model bị cắt cụt câu trả lời hoặc provider báo lỗi giới hạn token.
* **Kẹt câu hỏi xác nhận:** Agent đang chờ xác nhận `[y/N]` hoặc xin quyền chạy lệnh từ người dùng.
* **Treo vô tận / Vòng lặp lỗi:** Agent bị kẹt trong vòng lặp linter/test hoặc script chạy ngầm không hồi kết.

```mermaid
sequenceDiagram
    autonumber
    participant Lead as 🏛️ Lead Coordinator
    participant Orca as 🐋 Orca Runtime & Watchdog
    participant Worker as 🤖 Worker (Dev)

    Note over Lead,Worker: Giao task có chốt chặn trên (--timeout-ms 300000 / 5 phút)
    Lead->>Orca: worker-start --task <id> --timeout-ms 300000

    Worker->>Worker: Đang code... Bất ngờ dừng (Cạn token / Kẹt prompt [y/n] / Lặp lỗi)
    
    Note over Lead,Orca: 🩺 Reactive Watchdog (Chỉ kiểm tra khi nhận sự kiện worker dừng hoặc chạy 'orca-team watchdog')
    Note over Lead,Orca: Phát hiện worker im tiếng > 1-2 phút hoặc timeout
    Lead->>Orca: orca orchestration worker-show --dispatch <id>
    
    alt Trường hợp 1: Kẹt câu hỏi xác nhận (observation.agentWait != null)
        Lead->>Orca: orca terminal send --terminal <handle> --text "y" --enter
        Worker->>Worker: Nhận input tự động và tiếp tục hoàn thiện
    else Trường hợp 2: Cạn Token / Tràn Context / Tiến trình đã thoát
        Lead->>Orca: orca orchestration worker-read --dispatch <id> (Đọc log cuối xác nhận cạn token)
        Lead->>Orca: orca orchestration worker-stop --dispatch <id> (Dừng worker cũ an toàn)
        Note over Lead,Orca: Toàn bộ code viết dở vẫn nằm nguyên vẹn trên worktree con của role
        Lead->>Orca: worker-start --task <id> --retry-of <id> --worktree path:<role_worktree_path> --agent <agent> [--model <model>]
        Note over Worker: Tab worker mới mở ngay trong worktree của role, đọc git log & viết tiếp phần dở dang đến khi xong
    else Trường hợp 3: Quá thời gian timeout 5 phút
        Orca-->>Lead: Báo lỗi timeout (outcome: timed_out)
        Lead->>Orca: orca terminal read --terminal <handle>
        Lead->>Orca: Đổi model mạnh hơn hoặc chia nhỏ task DAG để worker tiếp quản
    end
```

### 7.1. Phân Biệt Giới Hạn Token & Chiến Lược Chuyển Nhà Cung Cấp (Cross-Provider Failover)

Khi phát hiện worker bị dừng, Leader phân tích chính xác bản chất lỗi qua `worker-read`:

1. **Tràn Context Window Phiên (Session Context Overflow):**
   * *Hiện tượng:* Agent đọc quá nhiều file hoặc log test quá dài vượt quá context window (128k/200k tokens).
   * *Giải pháp:* Khởi tạo worker tab mới (kể cả cùng model) với context sạch (`fresh context`), tiếp quản ngay trong worktree định danh của role (`--retry-of <dispatch_id> --worktree path:<role_worktree_path>`).

2. **Khóa Hạn Ngạch Toàn Tài Khoản (Account Rate Limit / HTTP 429 / Insufficient Quota):**
   * *Hiện tượng:* Tài khoản nhà cung cấp bị cạn tiền hoặc dính giới hạn request/phút (429).
   * *Nguyên tắc sống còn:* **TUYỆT ĐỐI KHÔNG spawn lại cùng nhà cung cấp** (vì sẽ lập tức thất bại tiếp).
   * *Cơ chế Cầu Dao Ngắt (Circuit Breaker):* Gắn nhãn Cooldown cho provider đó (tạm dừng 15–30 phút).
   * *Chuyển Nhà Cung Cấp Tức Thì (Cross-Provider Failover):*
     * Đang dùng **Claude** (Anthropic) ➔ Chuyển sang **Kimi** (Moonshot) hoặc **Codex** (OpenAI).
     * Đang dùng **Kimi** (Moonshot) ➔ Chuyển sang **Codex** (OpenAI) hoặc **Claude** (Anthropic).
     * Đang dùng **Codex** (OpenAI) ➔ Chuyển sang **Claude** (Anthropic) hoặc **Kimi** (Moonshot).
   * *Cập nhật cấu hình:* `orca-team set dev --agent <failover_agent>` rồi cho worker mới tiếp quản worktree hiện tại.

3. **Toàn Bộ Nhà Cung Cấp Đồng Loạt Cạn Hạn Ngạch:**
   * Bảo toàn 100% mã nguồn trên Git Worktree con.
   * Dừng an toàn mọi worker: `orca orchestration worker-stop --dispatch <id>`.
   * Báo cáo Human Gate lên giao diện chat để người dùng nạp thêm quota hoặc can thiệp.



