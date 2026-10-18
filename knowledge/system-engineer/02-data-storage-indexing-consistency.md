# 02. Lưu Trữ Dữ Liệu, Đánh Chỉ Mục & Tính Nhất Quán (Data Storage, Indexing & Consistency)

## 1. Xung Đột Đồng Thời & Mất Dữ Liệu Ghi Đè (Concurrency Anomalies & Lost Updates)
- **Điểm mù thường gặp trong Plan:**
  - Luồng xử lý kiểu **Read-Modify-Write**: Đọc bản ghi lên bộ nhớ (`entity = repo.findById(id)`), cộng/trừ số dư hoặc đổi trạng thái trong code, rồi gọi `repo.save(entity)`.
  - Dưới mức cô lập mặc định `READ COMMITTED` của PostgreSQL/MySQL, hai request chạy song song sẽ đọc cùng giá trị cũ và ghi đè lên nhau (**Lost Update**).
- **Giải pháp chuẩn System Engineering:**
  1. **Atomic SQL Update:** `UPDATE accounts SET balance = balance - :amt WHERE id = :id AND balance >= :amt` (kiểm tra số dòng affected `== 1`).
  2. **Optimistic Locking:** Thêm cột `@Version` / `version INT` (`UPDATE ... SET version = version + 1 WHERE id = :id AND version = :old_version`).
  3. **Pessimistic Locking:** `SELECT ... FOR UPDATE` (luôn khóa theo thứ tự ID tăng dần cố định để tránh **Deadlock** khi khóa nhiều dòng!).

## 2. Thiết Kế Chỉ Mục (Indexing Strategy) & Chống Quét Toàn Bảng (Seq Scan)
- **Điểm mù kiến trúc:**
  - Thêm bộ lọc truy vấn (`WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT 50`) nhưng không thêm **Composite Index** tương ứng, khiến DB phải `Seq Scan` + `Sort` trên hàng triệu dòng.
- **Quy tắc ESR (Equality ➔ Sort ➔ Range) khi thiết kế Composite Index:**
  1. Các cột so sánh bằng (`=`: ví dụ `tenant_id`, `status`) đứng đầu.
  2. Cột sắp xếp (`ORDER BY created_at DESC`) đứng tiếp theo (để tránh `filesort` / `top-N heapsort`).
  3. Các cột điều kiện khoảng (`>`, `<`, `BETWEEN`) đứng cuối.
- **Partial Index (PostgreSQL):** Nếu chỉ thường xuyên quét các bản ghi đang chờ xử lý (`WHERE status IN ('PENDING', 'RETRY')`), hãy dùng Partial Index `CREATE INDEX ... ON jobs (next_run_at) WHERE status IN ('PENDING', 'RETRY')` để giữ kích thước index cực nhỏ và tốc độ cực nhanh.

## 3. Bệnh Kinh Niên N+1 Queries trong ORM
- **Điểm mù:** Lặp qua danh sách `N` phần tử ở tầng code/serializer và truy cập thuộc tính quan hệ (`lazy loading`) hoặc gọi query con bên trong vòng lặp `for`.
- **Giải pháp bắt buộc trong Plan:**
  - Sử dụng `JOIN FETCH`, `EntityGraph`, `WHERE id IN (...)` gom theo batch, hoặc Projection DTO lấy đúng các cột cần thiết trong 1–2 câu truy vấn cố định.

## 4. Zero-Downtime Database Migration (Expand-Contract Pattern)
- **Điểm mù:**
  - Đổi tên cột (`RENAME COLUMN`), xóa cột (`DROP COLUMN`), hoặc thêm cột `NOT NULL` không có `DEFAULT` trực tiếp trên bảng lớn đang chạy production, hoặc chạy `CREATE INDEX` thường (gây khóa ghi toàn bảng `ACCESS EXCLUSIVE` / `SHARE` lock).
- **Giải pháp chuẩn:**
  1. Mọi migration phải **Idempotent** (`IF NOT EXISTS` / `IF EXISTS`) và tương thích ngược với code phiên bản hiện tại trong lúc rolling update.
  2. Thêm cột mới phải cho phép `NULL` hoặc có `DEFAULT` hằng số (PostgreSQL 11+ thêm cột có default hằng số là tức thì không rewrite bảng).
  3. Kiểm tra kỹ kiểu dữ liệu khớp 100% giữa DB Migration (Flyway/Liquibase/Alembic/Prisma) và Entity trong code (tránh lỗi `BadSqlGrammarException`, `operator does not exist: character varying = uuid`, `jsonb` vs `varchar`).

## 5. Cache Consistency (Redis / In-Memory Cache)
- Luôn định nghĩa rõ **TTL**, chiến lược **Cache Invalidation** khi dữ liệu gốc thay đổi, và chống **Cache Stampede** (nhiều luồng cùng miss cache một lúc và ồ ạt đập vào DB).
- Không dùng Cache làm nguồn dữ liệu duy nhất (Single Source of Truth) cho dữ liệu trạng thái quan trọng nếu không có cơ chế khôi phục từ DB.
