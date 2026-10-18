# 🧠 System Engineering Knowledge Base — Cẩm Nang Quân Sư (`critic-worker`)

> **Tra cứu tiết kiệm Token:** Đọc `INDEX.md` này trước ➔ chỉ chạy `orca-team kb <01..05>` cho **1–2 chuyên đề liên quan trực tiếp**. Mọi phản biện với **Lead Coordinator** phải gắn **Nguyên lý System Engineering** + **Bằng chứng repo (`file:line`, `git diff/blame`, DB schema, Logs)**.

| Mã | File Chuyên Đề (`orca-team kb <mã>`) | Khi nào BẮT BUỘC mở đọc? | Điểm Mù Kiến Trúc Cốt Lõi (Key Blind Spots) |
| :--- | :--- | :--- | :--- |
| **01** | `01-distributed-systems-consensus.md` | API liên service, Kafka/RabbitMQ/Redis, Webhook, Cron đa pod, thanh toán/đơn hàng. | Thiếu `idempotency_key` (`UNIQUE INDEX`), lỗi **Dual-Write** (DB + Broker không qua Transactional Outbox), thiếu `FOR UPDATE SKIP LOCKED` / Distributed Lock, Retry Storm (thiếu Backoff + Jitter), thiếu **DLQ**, Race Condition. |
| **02** | `02-data-storage-indexing-consistency.md` | Thêm/sửa bảng DB, migration, query phức tạp, ORM, Redis Cache. | Query **N+1**, thiếu **Composite/Partial Index** (`WHERE` + `ORDER BY`), **Lost Update** (thiếu `FOR UPDATE` / Optimistic Lock `version`), **Breaking Migration** gây downtime (không qua Expand-Contract), Cache Stampede / Stale Cache. |
| **03** | `03-reliability-sre-observability.md` | Docker/K8s, HTTP/DB/Kafka client, background job, lỗi treo/crash runtime. | Gọi I/O ngoài **thiếu Timeout**, nuốt lỗi (`catch` rỗng / trả `HTTP 200` khi `FAILED`), thiếu **Graceful Shutdown**, K8s probe giả tạo, rò rỉ tài nguyên (connection/stream/goroutine/thread). |
| **04** | `04-security-zero-trust-architecture.md` | AuthN/AuthZ (JWT/OAuth/RBAC/ABAC), API, upload/parse file, webhook, TLS/mTLS, Secret/`.env`. | **IDOR / BOLA** (thiếu check `tenant_id`/`owner_id`), **SSRF**, **SQL/Command/Template Injection**, JWT không check `alg`/signature/exp, lộ Secret trong code/log, vượt rào quyền hạn. |
| **05** | `05-performance-capacity-scalability.md` | Dữ liệu lớn (SIEM logs, batch import/export, streaming), latency p95/p99, RPS/EPS. | **Unbounded Memory / OOM** (nạp toàn bộ vào RAM thay vì Cursor/Streaming), **Event-Loop / Thread-Pool Starvation**, ghi DB lẻ tẻ trong vòng lặp thay vì **Batch Insert / Bulk Upsert**, Lock Contention trên hot row. |

## 🛡️ Checklist 5 Câu Hỏi Bắt Buộc Trước Khi Chốt `CRITIC_VERDICT: CONSENSUS_REACHED`
1. **[Đầy Đủ & Ranh Giới File (`[P1..Pn]`)]**: Đã liệt kê đủ `[P1..Pn]` kèm file/module và **Disjoint File Ownership** giữa các `dev-worker*` chưa? Có sót yêu cầu nào của user không?
2. **[Đúng Tầng & Vai Trò]**: Thay đổi thuộc App (`dev-worker`), UI (`ui-worker`), hay Infra/DB/Container (`devops-worker`)? Có quy kết nhầm role không?
3. **[Chịu Lỗi & Đồng Thời]**: Nếu request/event gửi trùng 2 lần, mạng/DB timeout, hoặc 2 luồng sửa cùng 1 bản ghi cùng lúc thì sao?
4. **[Tương Thích Ngược & An Toàn Dữ Liệu]**: Sửa DB schema, API contract, hay message payload có làm vỡ dữ liệu cũ, consumer cũ, hoặc frontend không?
5. **[Khả Kiểm Chứng Cho QA (`qa-worker`)]**: Từng mục `[P1..Pn]` đã có tiêu chí nghiệm thu định lượng (Unit/Integration/E2E test + DB state + Logs sạch lỗi) cho Wave 2 chưa?
