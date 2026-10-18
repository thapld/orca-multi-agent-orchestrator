# 04. Kiến Trúc Bảo Mật & Zero-Trust (Security & Zero-Trust Architecture)

## 1. Xác Thực & Phân Quyền Theo Ngữ Cảnh (AuthN, AuthZ & Multi-Tenancy)
- **Điểm mù kiến trúc phổ biến nhất (IDOR / BOLA):**
  - Endpoint kiểm tra JWT hợp lệ (`@Authenticated`), nhưng khi truy vấn hoặc cập nhật dữ liệu chỉ dùng `WHERE id = :id` mà quên ràng buộc `AND tenant_id = :currentTenantId` (hoặc `AND owner_id = :currentUserId`).
  - Kết quả: User của Tenant A chỉ cần đoán/thay đổi `id` là đọc, sửa hoặc xóa được dữ liệu của Tenant B.
- **Chuẩn thiết kế:**
  - Mọi truy vấn đọc/ghi/xóa tài nguyên phải gắn chặt với danh tính và phạm vi quyền hạn lấy từ Security Context đã xác thực (không bao giờ tin `user_id`/`tenant_id` gửi từ body/query của client nếu chưa đối chiếu với token).

## 2. Phòng Chống Injection & Thực Thi Mã Từ Xa (Injection Prevention)
- **SQL / NoSQL Injection:**
  - Bắt buộc dùng Parameterized Queries / Prepared Statements 100%.
  - Với các tham số động không thể bind trực tiếp (như tên cột `ORDER BY` hoặc hướng sắp xếp `ASC/DESC`), bắt buộc kiểm tra qua **Allowlist (Whitelist) cứng**, tuyệt đối cấm nối chuỗi trực tiếp từ request vào câu SQL.
- **Command Injection:**
  - Cấm ghép chuỗi input của người dùng vào `sh -c`, `os.system`, `subprocess(..., shell=True)`, `child_process.exec`. Bắt buộc truyền mảng đối số (`execFile` / `subprocess.run([bin, arg1, arg2], shell=False)`) và kiểm tra định dạng chặt chẽ.

## 3. Phòng Chống SSRF (Server-Side Request Forgery)
- Khi hệ thống có tính năng kết nối tới URL/IP do người dùng cấu hình (Webhook, Connector, SIEM Collector, Health Check URL, Image Fetcher):
  - Phải kiểm tra scheme chỉ cho phép `http`/`https` (chặn `file://`, `gopher://`, `dict://`).
  - Phân giải DNS và chặn kết nối tới dải IP nội bộ/loopback/link-local (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.169.254` metadata cloud, `::1`) trừ khi được cấu hình allowlist tường minh cho môi trường on-prem.

## 4. Quản Lý Bí Mật, Mã Hóa & TLS (Secrets, Crypto & TLS)
- **Cấm Hardcode Secrets:** Không bao giờ nhúng API key, JWT secret, mật khẩu DB, private key vào source code, Dockerfile, hoặc file cấu hình commit lên Git.
- **Cấm Ghi Lộ Thông Tin Nhạy Cảm Ra Log:** Không log raw password, `Authorization` header, JWT token, secret key, hoặc PII vào stdout/log file.
- **TLS / SSL:** Khi gặp lỗi `SSLHandshakeException` hoặc `PKIX path building failed`, ưu tiên cấu hình đúng TrustStore / CA bundle hoặc cờ cấu hình `verify_tls` có kiểm soát thay vì tắt xác thực chứng chỉ toàn cục một cách mù quáng trên mọi kết nối.

## 5. Xác Thực Đầu Vào & Cô Lập Bán Kính Ảnh Hưởng (Input Validation & Blast-Radius Isolation)
- Xác thực chặt chẽ kiểu dữ liệu, độ dài chuỗi, giới hạn kích thước mảng/file upload (`max_payload_size`, `max_batch_size`) ngay tại biên API (API Boundary / DTO Validation) trước khi đi vào tầng nghiệp vụ.
- Thiết kế Rate Limiting và phân tách quyền DB tối thiểu (Least Privilege).
