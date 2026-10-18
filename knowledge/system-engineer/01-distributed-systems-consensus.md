# 01. Hệ Phân Tán, Đồng Thuận & Chịu Lỗi (Distributed Systems, Consensus & Fault Tolerance)

## 1. Tính Lũy Đẳng (Idempotency) — Chống Xử Lý Trùng Lặp
- **Triệu chứng & Điểm mù thường gặp trong Plan của Leader:**
  - Thiết kế API `POST /orders`, `POST /payments`, webhook handler hoặc Kafka/RabbitMQ consumer theo giả định *"mỗi message chỉ đến đúng 1 lần (Exactly-Once)"*.
  - Trong thực tế mạng phân tán, cơ chế truyền tin chuẩn là **At-Least-Once**: khi client timeout hoặc consumer rebalance/crash trước khi commit offset, cùng một sự kiện sẽ được gửi lại.
- **Giải pháp chuẩn System Engineering cần yêu cầu trong Plan:**
  1. Bắt buộc có `idempotency_key` / `event_id` duy nhất kèm ràng buộc **`UNIQUE INDEX`** ở tầng Database (không chỉ kiểm tra bằng `SELECT` trong bộ nhớ hoặc code ứng dụng vì sẽ dính Race Condition kiểu Check-Then-Act!).
  2. Sử dụng `INSERT ... ON CONFLICT DO NOTHING / DO UPDATE` (PostgreSQL) hoặc `INSERT IGNORE` / `UPSERT` nguyên tử (atomic).

## 2. Bài Toán Dual-Write & Transactional Outbox Pattern
- **Điểm mù kiến trúc:**
  - Plan của Leader viết: *(1) Lưu bản ghi vào Database ➔ (2) Bắn message lên Kafka/RabbitMQ hoặc gọi HTTP sang service khác*.
  - **Rủi ro:** Nếu bước (1) commit thành công nhưng bước (2) lỗi mạng/crash pod ➔ Dữ liệu giữa DB và Message Broker lệch nhau vĩnh viễn (Silent Data Loss / Inconsistency). Ngược lại, nếu bắn Kafka trước rồi mới commit DB nhưng DB rollback ➔ Consumer nhận phải dữ liệu ma (Phantom Event).
- **Giải pháp chuẩn:**
  - Dùng **Transactional Outbox Pattern**: Ghi cả bản ghi nghiệp vụ và bản ghi `outbox_events` trong **cùng một DB Transaction**, sau đó dùng tiến trình relay/poller có khóa (`FOR UPDATE SKIP LOCKED`) hoặc CDC đẩy lên Broker và đánh dấu `published_at`.
  - Hoặc nếu bắt buộc gọi đồng bộ trong các tác vụ đơn giản, phải có trạng thái trung gian (`status = PENDING_DISPATCH` ➔ `DISPATCHED` / `FAILED` kèm `last_error`) và cơ chế đối soát/bù (reconciliation job).

## 3. Điều Phối Giao Dịch Liên Dịch Vụ (Saga Pattern & Compensation)
- Không bao giờ giữ một Database Transaction mở trong lúc chờ gọi HTTP/gRPC sang dịch vụ bên ngoài (gây cạn kiệt Connection Pool và khóa bảng).
- Tách thành các bước cục bộ: mỗi bước có trạng thái rõ ràng và hành động bù trừ (**Compensating Action**) nếu bước sau thất bại.

## 4. Khóa Phân Tán & Tác Vụ Định Kỳ (`@Scheduled` / Cron / Worker Pool)
- **Điểm mù kiến trúc:**
  - Khi chạy `>= 2` replicas/pods trên K8s, một hàm `@Scheduled` hoặc poller quét bảng `jobs WHERE status = 'PENDING'` sẽ chạy đồng thời trên mọi pod, bốc trùng cùng một job.
- **Giải pháp chuẩn:**
  - Với hàng đợi trong PostgreSQL: Bắt buộc dùng **`SELECT ... FOR UPDATE SKIP LOCKED LIMIT <batch_size>`** trong transaction để cập nhật nguyên tử sang `status = 'PROCESSING'`, kèm cột `locked_until` / `heartbeat_at` để thu hồi job bị treo (stale lock recovery) khi worker crash.
  - Với tác vụ toàn cục đơn lẻ: Dùng Distributed Lock (ShedLock, Redis Redlock kèm fencing token, hoặc PostgreSQL Advisory Lock `pg_try_advisory_xact_lock`).

## 5. Backpressure, Retry, Exponential Backoff & Dead-Letter Queue (DLQ)
- **Cấm Retry vô hạn hoặc Retry tức thì trong vòng lặp chặt (`while True`):** Khi downstream đang quá tải, retry tức thì sẽ tạo ra **Retry Storm** đánh sập toàn hệ thống.
- **Chuẩn thiết kế:**
  - Giới hạn `max_attempts`, áp dụng **Exponential Backoff + Full Jitter**.
  - Phân biệt rõ **Transient Errors** (timeout mạng, `HTTP 502/503/504`, deadlock DB ➔ cho phép retry) và **Permanent/Poison Errors** (`HTTP 400/401/403/422`, sai định dạng JSON/schema ➔ **CẤM retry**, chuyển thẳng vào **DLQ** hoặc cập nhật `status = FAILED` kèm `last_error` đầy đủ để quan sát).
