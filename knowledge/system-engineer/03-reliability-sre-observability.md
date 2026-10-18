# 03. Độ Tin Cậy, SRE & Quan Sát Hệ Thống (Reliability, SRE & Observability)

## 1. Mọi Lệnh Gọi I/O Bắt Buộc Phải Có Timeout Tường Minh
- **Điểm mù nguy hiểm nhất:**
  - Sử dụng `HttpClient`, `fetch`, `axios`, `requests`, `RestTemplate`/`WebClient`, `gRPC`, hoặc kết nối Database/Kafka/SMTP/SSH mà để mặc định (nhiều thư viện mặc định `timeout = vô cực`!).
  - Chỉ cần 1 dịch vụ đích bị treo khớp nối mạng (blackhole), toàn bộ worker threads / connection pool của hệ thống sẽ bị kẹt cứng vĩnh viễn (**Cascading Hang**).
- **Giải pháp bắt buộc trong Plan:**
  - Thiết lập rõ: `connectTimeout`, `readTimeout` / `responseTimeout`, `statement_timeout` / `lock_timeout` (DB), và `idleTimeout`.

## 2. Cấm Che Giấu Hoặc Nuốt Lỗi (Anti-Swallowing & Anti-Masking Law)
- **Điểm mù thường gặp khi fix bug:**
  - Dev bọc `try { ... } catch (Exception e) { log.warn(...); }` rồi vẫn cập nhật trạng thái bản ghi là `COMPLETED` / `SUCCESS` hoặc trả về `HTTP 200 OK` với dữ liệu rỗng!
  - Hoặc hệ thống có lỗi kết nối hạ tầng (`SSLHandshakeException`, `Connection refused`, `401 Unauthorized`, `BadSqlGrammarException`) nhưng health-check vẫn báo `UP` / `OK`.
- **Chuẩn System Engineering:**
  - **Fail-Fast & Honest State Reporting:** Khi thao tác thất bại, phải cập nhật trạng thái thực chất (`status = FAILED` / `ERROR`), lưu nguyên nhân vào `last_error` / `error_detail`, xuất log cấp `ERROR` kèm ngữ cảnh (`correlation_id`, `entity_id`, `endpoint`), và trả mã lỗi chuẩn.
  - Tuyệt đối không biến lỗi hệ thống (5xx / lỗi DB / lỗi SSL) thành kết quả thành công giả tạo.

## 3. Quản Lý Vòng Đời Tài Nguyên & Chống Rò Rỉ (Resource Lifecycle & Leak Prevention)
- Mọi kết nối DB, file descriptor, HTTP response body stream, SSH session, Kafka producer/consumer, `setInterval`/timer hoặc goroutine/thread background phải được đóng trong khối `finally` / `try-with-resources` / `contextlib` / `defer`.
- Trong các bài test (đặc biệt là Jest / Node.js / Python asyncio), phải đóng toàn bộ pool/server/timer khi teardown (và dùng `--forceExit` / kiểm tra open handles).

## 4. Khởi Động, Sẵn Sàng & Tắt Hệ Thống An Toàn (Startup, Probes & Graceful Shutdown)
- **Liveness vs. Readiness Probe:**
  - `Liveness`: Chỉ kiểm tra tiến trình có bị deadlock/treo cứng không (không phụ thuộc vào dịch vụ ngoài tạm thời chập chờn để tránh K8s restart toàn bộ cluster cùng lúc).
  - `Readiness`: Kiểm tra kết nối DB/Broker/Migration đã sẵn sàng nhận tải chưa.
- **Graceful Shutdown (`SIGTERM`):**
  - Khi dừng container, ngừng nhận request mới, chờ các request/job đang chạy hoàn tất trong khoảng `terminationGracePeriodSeconds`, đóng kết nối DB/Kafka sạch sẽ rồi mới thoát.

## 5. Tiêu Chuẩn Kiểm Toán Runtime Logs Toàn Trình
- Một tính năng chỉ được coi là hoạt động đúng khi:
  1. Đã chạy qua ít nhất **1 chu kỳ thực thi thật** của toàn bộ luồng (HTTP API + Background Worker/`@Scheduled` + Message Consumer).
  2. Quét **TOÀN BỘ Runtime Logs từ lúc container khởi động** (không dùng `--since` ngắn để lờ đi lỗi lúc startup/migration) trên **TẤT CẢ các container liên quan** (App + DB + Kafka/Broker + Sidecar).
  3. Không xuất hiện bất kỳ dòng `ERROR`, `FATAL`, `PANIC`, `UnhandledPromiseRejection`, `Exception`, lỗi SQL hay `SSLHandshakeException` nào.
