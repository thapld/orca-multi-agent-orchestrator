# 06. Frontend Design-System Parity, Figma Fidelity & Realtime Multi-Provider Isolation

> **Mục tiêu:** Chặn 5 căn bệnh kinh niên khi phát triển UI/Full-Stack & Realtime: (1) Lệch thiết kế Figma/App gốc, (2) Lệch chuẩn so với các trang anh em (Sibling Pages) trong cùng dự án (vỡ Dark/Light mode, mất khoảng cách lề, tự chế component), (3) Sót chuỗi ngoại ngữ (i18n) hoặc đè khung hình (Overlay Occlusion), (4) Lẫn dữ liệu chéo giữa các Provider/Channel/Table/Tenant, và (5) Bỏ sót edge-case khi port từ repo/module mẫu.

---

## 1. Đối Chiếu Tam Giác Giao Diện (Triangular UI Parity)
Mọi màn hình mới hoặc chỉnh sửa UI bắt buộc đồng nhất trên cả **3 đỉnh tam giác**:
1. **Đỉnh 1 — Thiết kế gốc (Figma / App mẫu):**
   - Khớp 1:1 về **Layout Topology**: `Overlay Drawer` trượt trên bảng hay `Split-Pane` đẩy co bảng; số hàng/cột thẻ KPI; vị trí thanh lọc/tìm kiếm; hành động bấm nút mở `Modal` tại chỗ hay chuyển trang (`Page Route`).
   - **Cấm bê nguyên chuỗi mẫu (Mock Literal Copy-Paste)** từ Figma vào code (tên giả, số ngày giả, mã đơn vị giả). Trường chưa có dữ liệu API phải hiển thị trạng thái rỗng (`—`, `N/A`, hoặc `EmptyState` chuẩn).
2. **Đỉnh 2 — Các trang anh em cùng phân hệ (Sibling Pages):**
   - Trước khi viết code UI, bắt buộc đọc **tối thiểu 2 trang hiện có trong cùng phân hệ** để kế thừa chính xác:
     - **Page Shell / Layout Wrapper** (`edge padding`, `section/card gap`, cấu trúc Header/Breadcrumb/Tab).
     - **Shared Components** (`SearchableSelect`, `DataTable`, `Pagination/Pager`, `Drawer`, `Modal`, `Badge`, `Tabs`, `Skeleton`) — **cấm** tự viết thẻ `<select>`, `<table>` hay phân trang HTML thô khi dự án đã có component chung.
3. **Đỉnh 3 — Hệ thống Theme Tokens (Dark & Light Mode Parity):**
   - Figma thường chỉ vẽ ở 1 chế độ (VD Light Mode nền `#ffffff`, chữ `#1e293b`). Nếu copy-paste mã màu hex cứng vào ứng dụng chạy **Dark Mode**, trang mới sẽ biến thành "đảo trắng" hoặc chữ tối trên nền tối.
   - **Bắt buộc:** Chuyển hóa 100% màu nền, chữ, viền và bóng đổ sang **CSS Variables / Design Tokens của dự án** để tự động thích ứng trên cả **Dark Mode & Light Mode**.
   - **Cấm gian lận Linter:** Cấm chạy `--update` / `--update-baseline` trên UI Linter để hợp thức hóa lỗi vi phạm token mới.

---

## 2. Tinh Khiết Đa Ngôn Ngữ (i18n Purity) & Không Gian Hiển Thị (Viewport Non-Occlusion)
1. **Kiểm toán chuỗi ngôn ngữ (i18n Purity):** Quét 100% `label`, `placeholder`, `tooltip`, `toast`, `status` và thông báo lỗi phải đi qua từ điển `i18n` của dự án; cấm để sót chuỗi ngoại ngữ gốc (tiếng Trung, tiếng Anh cứng).
2. **Chống che khuất (Overlay Non-Occlusion) & Chống tràn chữ:**
   - `Floating Widget`, `Overlay Panel` không bao giờ được đè lên vùng tương tác chính hoặc khung `Video Player`. Gỡ bỏ hoàn toàn các nút `Demo/Debug Controls` trên giao diện thật.
   - Chuỗi định danh động (Session ID, Hash, UUID, tên dài) trong thẻ/bảng phải có chống tràn (`truncate`, `min-w-0`, `overflow-hidden`, `text-ellipsis`).

---

## 3. Cô Lập Khóa Phức Hợp Đa Nguồn (Multi-Provider / Multi-Table Composite-Key Isolation)
1. **Cấm dùng khóa đơn (Single-Key Collision):** Hai provider khác nhau có thể trùng `tableId = "1"` hoặc `channel = "baccarat"`. Mọi khóa trong DB, Cache, WebSocket Room và Frontend Store phải dùng **Khóa Phức Hợp Đầy Đủ**: `(provider, channel, tableId, sessionId)` hoặc `(tenantId, provider, resourceId)`.
2. **Tính đơn điệu của phiên Realtime (Monotonic Session/Version Guard):** Khi sang phiên mới (`session n`), gói tin trễ của `session n-1` hoặc hai luồng sự kiện song song (`generic_event` + `provider_specific_event`) không được ghi đè ngược gây **nháy kết quả (UI Flickering)**. Bắt buộc kiểm tra `event.sessionId == currentSessionId` và `event.sequence >= lastSequence`.
3. **Đồng nhất bộ lọc Số Đếm Tổng (Outer Badge) & Danh Sách Chi Tiết (Inner List):** Thẻ đếm bên ngoài và danh sách bên trong bắt buộc dùng chung một hàm lọc (`filter predicate`).

---

## 4. Bảo Toàn Hành Vi Khi Port Từ Mã Nguồn Mẫu & Dịch Ngược 1:1
1. **Khi nâng cấp, tách dịch vụ hoặc port từ repo/module mẫu:** Khảo sát mã nguồn gốc lập **Parity Matrix** trước khi code: (a) Cơ chế fallback khi mất tín hiệu (WebSocket im lặng quá `N` giây tự bật HTTP polling), (b) Chuyển đổi trạng thái ngược hợp lệ (`settled -> cancelled/recalculated`), (c) Bảo toàn 100% các cột dữ liệu DB/API cho hệ thống downstream.
2. **Khi dịch ngược và tái tạo ứng dụng (1:1 Reconstruction):** Cấm thay ứng dụng gốc bằng web giả lập sơ sài hoặc nhồi toàn bộ code vào 1 file (`MainActivity`). Phải trích xuất nguyên vẹn kiến trúc phân tầng, tài nguyên đồ họa gốc sạch (không cắt ảnh chụp màn hình dính chữ làm nền) và luồng điều hướng đúng 1:1.
