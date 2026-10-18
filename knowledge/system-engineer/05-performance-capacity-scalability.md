# 05. Hiệu Năng, Dung Lượng & Khả Năng Mở Rộng (Performance, Capacity & Scalability)

## 1. Quản Lý Bộ Nhớ Có Giới Hạn (Bounded Memory & Streaming)
- **Điểm mù kiến trúc:**
  - Gọi `findAll()` không phân trang hoặc đọc toàn bộ file log/bảng dữ liệu lớn vào một danh sách trong RAM trước khi lọc/xử lý.
  - Dùng hàng đợi nội bộ trong bộ nhớ (`Queue` / `Channel` / `Array`) không giới hạn kích thước (`unbounded queue`) khi tốc độ sinh dữ liệu (Producer) nhanh hơn tốc độ tiêu thụ (Consumer) ➔ chắc chắn dẫn tới `OutOfMemoryError` (OOMKilled).
- **Giải pháp chuẩn System Engineering:**
  - Mọi API danh sách bắt buộc có **Pagination** (`limit` có trần tối đa, ưu tiên **Keyset/Cursor Pagination** `WHERE id > :after_id LIMIT :limit` thay vì `OFFSET` lớn).
  - Xử lý dữ liệu lớn theo **Stream / Cursor / Chunked Batch** với kích thước cố định (`batch_size = 500..2000`).
  - Mọi hàng đợi nội bộ phải là **Bounded Queue** kết hợp cơ chế Backpressure rõ ràng.

## 2. Xử Lý Theo Lô Thay Vì Từng Bản Ghi (Batching vs. Row-by-Row I/O)
- **Điểm mù:**
  - Trong luồng thu thập sự kiện (SIEM Collector, Log Ingestor, Sync Job), thực hiện `INSERT` hoặc gọi HTTP API từng bản ghi một trong vòng lặp (`1,000 events = 1,000 network round-trips`).
- **Giải pháp chuẩn:**
  - Gom lô theo **Kích thước hoặc Thời gian chờ (`Batch Size + Flush Interval`, ví dụ: đủ `500` bản ghi HOẶC sau mỗi `100ms`)** và dùng `Bulk Insert` / `COPY` / `UNNEST` / `saveAll` với `reWriteBatchedInserts=true`.

## 3. Chống Nghẽn Luồng Thực Thi (Thread-Pool & Event-Loop Non-Blocking)
- **Trên Node.js / Python Asyncio / Reactive (WebFlux, Netty, Go):**
  - Tuyệt đối không chạy tác vụ CPU nặng (regex phức tạp có nguy cơ ReDoS, parse/serialize JSON hàng chục MB, nén/mã hóa đồng bộ) hoặc gọi I/O đồng bộ (`fs.readFileSync`, `execSync`, `time.sleep`, blocking JDBC) trực tiếp trên Event Loop / I/O thread.
- **Cấu hình Connection Pool (HikariCP / pg-pool / Prisma):**
  - Kích thước DB Connection Pool không nên đặt quá lớn gây tranh chấp context-switch ở DB; công thức tham chiếu chuẩn: `connections ≈ (core_count * 2) + effective_spindle_count` (thường `10–30` kết nối mỗi instance, kết hợp PgBouncer khi có nhiều pod).

## 4. Giảm Tranh Chấp Khóa (Lock Contention & Hot-Spot Mitigation)
- Tránh cập nhật liên tục cùng một dòng "tổng" (hot row, ví dụ: cập nhật bộ đếm `total_events` của tenant trên mỗi event đơn lẻ). Hãy gom bộ đếm trong bộ nhớ/Redis và flush định kỳ theo lô, hoặc tách bảng thống kê theo phân đoạn thời gian (time-bucketed counters).
- Giữ phạm vi Transaction ngắn nhất có thể: thực hiện toàn bộ tính toán, parse dữ liệu và gọi API ngoài **trước hoặc sau** khối `@Transactional`.
