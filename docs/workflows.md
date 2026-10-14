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
    participant Orca as 🐋 Orca Runtime
    participant Devs as 💻 Dev Swarm (1–N)
    participant QA as 🧪 QA Swarm
    participant Sec as 🔍 Security Auditor

    User->>Lead: Yêu cầu tính năng mới
    Lead->>Lead: Phân tích & Viết bản Kế hoạch (plan.md)
    Lead-->>User: Trình Plan để Người dùng duyệt (Decision Gate)
    User->>Lead: Duyệt kế hoạch (Approved)

    Lead->>Orca: orca orchestration run-create
    Lead->>Orca: orca orchestration task-create (Tạo Task DAG có dependency)

    par Khởi chạy Devs song song
        Lead->>Orca: worker-start Dev 1 (worktree new-child: dev-backend)
        Lead->>Orca: worker-start Dev 2 (worktree new-child: dev-frontend)
    end

    loop Trong khi code
        Devs->>Orca: send --type heartbeat
        Devs->>Devs: Viết code & Unit test
    end

    Devs->>Orca: send --type worker_done --outcome succeeded
    
    Lead->>Orca: worker-start QA (Chạy Integration test)
    QA->>Orca: send --type worker_done --outcome succeeded

    Lead->>Orca: worker-start Security Auditor
    Sec->>Orca: send --type worker_done (0 Critical)

    Lead->>Orca: orca file diff (Soi diff tổng thể)
    Lead->>Orca: orca review commit-push (Tạo PR / Merge vào main)
    Lead->>Orca: orca orchestration worker-release (Dọn dẹp terminal)
    Lead-->>User: Báo cáo hoàn tất
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
        Fork --> CreateWorktree["orca worktree create --name hotfix-issue-42 --issue 42 --agent $ISSUE_AGENT"]
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
    ReassignEvent["Phát hiện Issue #42 được gán cho người khác (@developer_b)"] --> StopAgent["1. DỪNG AGENT NGAY LẬP TỨC\norca orchestration worker-stop\n(Tiết kiệm token & tránh conflict)"]
    
    StopAgent --> AssessState{"Tiến độ hiện tại thế nào?"}
    
    AssessState -- "Chưa viết code / Mới đọc issue" --> DirectRelease["Hủy bỏ nhẹ nhàng"]
    
    AssessState -- "Đã có code phác thảo / Tìm ra root cause" --> MakeHandoff["2. ĐÓNG GÓI TIẾN ĐỘ BÀN GIAO\n• Tạo branch: wip/issue-42-handover\n• Commit toàn bộ code dở dang & logs\n• git push origin wip/issue-42-handover"]
    
    MakeHandoff --> PostComment["Gửi comment lịch sự lên GitHub Issue:\n'Đã chuyển giao cho @developer_b. AI agent đã lưu tiến độ nháp\nvà ghi chú tái hiện lỗi tại branch wip/issue-42-handover.'"]
    
    PostComment --> DirectRelease
    
    DirectRelease --> Cleanup["3. GIẢI PHÓNG WORKTREE TRONG ORCA\n• orca orchestration worker-release\n• orca worktree rm --worktree active"]
    
    Cleanup --> ResumeMain["Leader quay lại tập trung 100% cho luồng Feature chính"]
```

---

## 4. Quy Trình Thẩm Định Pull Request (PR Reviewer)

Sử dụng agent chuyên biệt để checkout PR vào worktree riêng, chạy kiểm tra CI và soi diff từng dòng.

```mermaid
flowchart LR
    PROpen["Pull Request #123 được tạo"] --> CheckoutPR["orca worktree create --name review-pr-123 --pr 123 --agent $PR_AGENT"]
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
