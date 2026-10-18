# 🧠 System Engineering Knowledge Base — Cẩm Nang Quân Sư Kiến Trúc (`critic-worker`)

> **Hướng dẫn tra cứu nhanh cho `critic-worker` (Token-Efficient Lookup):**
> 1. Đọc file `INDEX.md` này trước để xác định bài toán chạm tới trục kỹ thuật nào trong **5 Trục System Engineering**.
> 2. **Chỉ mở 1–2 file chuyên đề có liên quan trực tiếp** (cấm đọc tràn lan cả 5 file nếu bài toán không chạm tới).
> 3. Mọi nhận định phản biện với **Lead Coordinator** bắt buộc phải gắn kết giữa **Nguyên lý System Engineering** + **Bằng chứng thực tế trong repo (`file:line`, `git diff/blame`, DB schema, Runtime Logs)**.

---

## 🗺️ Bản Đồ 5 Chuyên Đề System Engineering

| Mã | File Chuyên Đề | Khi nào `critic-worker` BẮT BUỘC mở đọc? | Các Điểm Mù Kiến Trúc Cốt Lõi (Key Blind Spots) |
| :--- | :--- | :--- | :--- |
| **01** | [`01-distributed-systems-consensus.md`](./01-distributed-systems-consensus.md) | Khi có API gọi liên service, Message Queue (Kafka/RabbitMQ/Redis), Webhook, Cron/`@Scheduled` chạy đa pod, hoặc xử lý thanh toán/đơn hàng. | Thiếu **Idempotency Key**, lỗi **Dual-Write** (cập nhật DB + bắn Kafka không qua Transactional Outbox), thiếu **Distributed Lock / Fencing Token**, retry bão tố (Retry Storm không có Exponential Backoff + Jitter), thiếu **DLQ**, hoặc Race Condition giữa các worker. |
| **02** | [`02-data-storage-indexing-consistency.md`](./02-data-storage-indexing-consistency.md) | Khi thêm/sửa bảng DB, viết migration, sửa câu query phức tạp, dùng ORM (JPA/Hibernate/Prisma/TypeORM), hoặc dùng Redis Cache. | Truy vấn **N+1**, thiếu **Composite/Partial Index** đúng thứ tự cột (`WHERE` + `ORDER BY`), **Lost Update** do thiếu `SELECT ... FOR UPDATE` / Optimistic Locking (`version`), **Breaking Migration** gây downtime (rename/drop cột trực tiếp không qua Expand-Contract), Cache Stampede / Stale Cache. |
| **03** | [`03-reliability-sre-observability.md`](./03-reliability-sre-observability.md) | Khi triển khai Docker/K8s, cấu hình HTTP/DB/Kafka client, xử lý background job, hoặc điều tra lỗi treo/crash lúc runtime. | Gọi I/O ngoài **không có Timeout** (`connect`/`read`/`query` timeout), nuốt lỗi (`catch` rỗng hoặc trả `HTTP 200` khi bên trong `FAILED`), thiếu **Graceful Shutdown** (đứt kết nối đang xử lý khi rollout pod), K8s Readiness/Liveness probe giả tạo, rò rỉ tài nguyên (DB connection/stream/goroutine/thread). |
| **04** | [`04-security-zero-trust-architecture.md`](./04-security-zero-trust-architecture.md) | Khi thiết kế hoặc sửa đổi AuthN/AuthZ (JWT/OAuth/RBAC/ABAC), API public/internal, upload/parse file, webhook, TLS/mTLS, hoặc quản lý Secret/`.env`. | **IDOR / Broken Object Level Authorization** (chỉ check đăng nhập mà không check `tenant_id`/`owner_id`), **SSRF** qua URL/webhook do user truyền, **SQL/Command/Template Injection**, JWT không kiểm tra `alg`/signature/expiry, lộ Secret trong code/log, hoặc vượt rào quyền hạn giữa các tầng. |
| **05** | [`05-performance-capacity-scalability.md`](./05-performance-capacity-scalability.md) | Khi xử lý luồng dữ liệu lớn (SIEM logs, batch import/export, streaming), tối ưu latency p95/p99, hoặc tăng throughput (RPS/EPS). | Nạp toàn bộ tập dữ liệu lớn vào RAM (**Unbounded Memory / OOM**) thay vì Cursor/Pagination/Streaming, ** Event-Loop / Thread-Pool Starvation** (chạy tác vụ CPU-bound hoặc I/O đồng bộ chặn luồng chính), ghi DB từng bản ghi trong vòng lặp thay vì **Batch Insert / Bulk Upsert**, Lock Contention trên hot row. |

---

## 🛡️ Checklist 5 Câu Hỏi Bắt Buộc Khi Duyệt Plan Của Leader

Trước khi chốt `CRITIC_VERDICT: CONSENSUS_REACHED`, `critic-worker` phải kiểm tra Plan của Leader qua 5 câu hỏi sau:
1. **[Tính Đầy Đủ & Ranh Giới File (`[P1..Pn]`)]**: Plan của Leader đã liệt kê rõ từng hạng mục `[P1..Pn]` kèm file/module cụ thể và **Disjoint File Ownership** giữa các `dev-worker*` chưa? Có hạng mục nào của người dùng bị Leader bỏ sót hoặc gộp chung chung không?
2. **[Đúng Tầng & Đúng Vai Trò (Attribution & Boundaries)]**: Nguyên nhân lỗi hoặc hạng mục thay đổi nằm ở tầng App (`dev-worker`), UI (`ui-worker`), hay Infra/DB/Container (`devops-worker`)? Leader có quy kết nhầm role không?
3. **[Chịu Lỗi & Đồng Thời (Resilience & Concurrency)]**: Điều gì xảy ra nếu request/event bị gửi trùng 2 lần, mạng/DB chậm hoặc đứt kết nối giữa chừng, hoặc 2 luồng cập nhật cùng 1 bản ghi cùng lúc?
4. **[Tương Thích Ngược & Dữ Liệu (Backward Compatibility & Data Safety)]**: Thay đổi DB schema, API contract, hoặc message payload có làm vỡ dữ liệu cũ, consumer cũ, hoặc frontend hiện tại không?
5. **[Khả Kiểm Chứng cho QA (Verifiability for `qa-worker`)]**: Từng mục `[P1..Pn]` trong Plan đã có tiêu chí nghiệm thu định lượng (Unit/Integration/E2E test + trạng thái DB + Runtime Logs sạch lỗi) để `qa-worker` đối chiếu 1-1 ở Wave 2 chưa?
