# 🛠️ Cẩm Nang Tiện Ích Độc Quyền Của Orca (Orca Utilities Runbook)

Orca không chỉ là một trình chạy CLI mà là một **Agent Development Environment (ADE)** với hệ sinh thái tiện ích bản địa cực kỳ phong phú. Tài liệu này cung cấp hướng dẫn sử dụng chi tiết cho từng tiện ích.

---

## 1. Git Worktree Isolation (`orca worktree`)

Mỗi agent hoặc task trong Orca được cấp một Git Worktree riêng biệt, giải quyết triệt để vấn đề xung đột mã nguồn.

### Các lệnh phổ biến:
```bash
# Xem danh sách các worktree đang hoạt động
orca worktree ps --json

# Tạo một worktree con mới từ nhánh hiện tại
orca worktree create --repo id:<repoId> --name "feature-auth" --parent-worktree active --json

# Tạo worktree gắn liền với GitHub Issue (độc lập, không kế thừa context dở dang)
orca worktree create --repo id:<repoId> --name "fix-login" --issue 42 --no-parent --agent kimi --setup run --json

# Tạo worktree để review GitHub PR (độc lập)
orca worktree create --repo id:<repoId> --name "review-pr-123" --pr 123 --no-parent --agent claude --json

# Mở toàn bộ diff thay đổi của worktree để review
orca file open-changed --mode diff --worktree active --json

# Cập nhật ghi chú tiến độ vào worktree
orca worktree set --worktree active --comment "Đã tái hiện lỗi; đang viết patch" --json

# Xóa worktree sau khi hoàn thành
orca worktree rm --worktree id:<worktreeId> --force --json
```

---

## 2. Embedded Browser & Design Mode (`orca browser` / `orca goto`)

Mỗi worktree trong Orca sở hữu một browser tab riêng biệt. 

### Vòng lặp tự động hóa (Snapshot - Interact Loop):
```bash
# 1. Điều hướng đến URL
orca goto --url http://localhost:3000 --worktree active --json

# 2. Chụp cấu trúc accessibility tree để lấy ref các phần tử (@e1, @e2...)
orca snapshot --worktree active --json

# 3. Thao tác click, điền text
orca fill --element @e1 --value "admin@example.com" --worktree active --json
orca click --element @e2 --worktree active --json

# 4. Chụp ảnh màn hình kiểm chứng
orca screenshot --worktree active --json
```

### Tiện ích Design Mode (Click-to-Code):
1. Bật nút **Design Mode** trên thanh công cụ của Orca Browser.
2. Di chuột đến bất kỳ phần tử UI nào bị lỗi và click vào nó.
3. Orca sẽ tự động trích xuất:
   * Đoạn mã HTML outer của phần tử đó.
   * Toàn bộ CSS computed (màu sắc, padding, font).
   * Ảnh chụp màn hình crop riêng phần tử.
   * File nguồn và số dòng tương ứng (`source mapping`).
4. Toàn bộ dữ liệu này tự động rơi thẳng vào chat terminal của agent đang mở.

---

## 3. SSH Worktrees & Remote Execution (`orca host`)

Cho phép agent chạy trên máy chủ từ xa (như máy chủ GPU, Linux VM hay OrbStack container) trong khi giao diện editor vẫn hiển thị tại chỗ trên macOS.

### Các lệnh kiểm tra và sử dụng:
```bash
# Xem danh sách các host đã đăng ký trong Orca
orca host list --json
# Kết quả hiển thị: local machine và các target SSH (như ssh:orb)

# Tạo worktree chạy trên remote host qua SSH
orca worktree create \
  --repo id:<repoId> \
  --name "remote-build" \
  --host "ssh:orb" \
  --agent codex \
  --json
```

---

## 4. Desktop Computer Use (`orca computer`)

Dành cho các tác vụ cần tương tác trực tiếp với phần mềm desktop ngoài trình duyệt thông qua hệ thống Accessibility Tree:
```bash
# Kiểm tra quyền hạn computer use
orca computer permissions --json

# Liệt kê các ứng dụng đang mở trên máy
orca computer list-apps --json

# Lấy trạng thái của một ứng dụng cụ thể (ví dụ Safari hoặc Finder)
orca computer get-app-state --app com.apple.Safari --json

# Click vào phần tử theo index
orca computer click --app com.apple.Safari --element-index 12 --json
```

---

## 5. Orca Skills Registry (`orca skills`)

Orca cung cấp các bộ kỹ năng chuẩn hóa cho các agent:
* `orchestration`: Quản lý Run, Task DAG, worker_done, heartbeat, decision gates.
* `orca-cli`: Toàn bộ lệnh điều khiển worktree, terminal, browser từ shell.
* `orca-linear`: Đọc và cập nhật ticket Linear trực tiếp.
* `orca-emulator`: Điều khiển iOS Simulator.
* `orca-emulator-android`: Điều khiển Android Emulator qua adb.

### Lệnh cài đặt và đọc tài liệu:
```bash
# Xem danh sách kỹ năng đã cài đặt
orca skills list --json

# Đọc tài liệu chi tiết của một kỹ năng
orca skills get orchestration --full
orca skills get orca-cli --full
```
