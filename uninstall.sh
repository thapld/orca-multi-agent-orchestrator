#!/usr/bin/env bash
# uninstall.sh: Gỡ cài đặt bộ công cụ Orca Multi-Agent Orchestrator khỏi máy
set -e

INSTALL_DIR="${HOME}/.local/bin"
BINARIES=(
  "orca-project-init"
  "orca-init"
  "orca-team"
  "orca-monitor"
  "orca-dispatch"
  "orca-hotfix"
  "orca-review"
  "orca-review-pr"
)

echo "🐋 [Orca Orchestrator Uninstaller] Đang gỡ bỏ các CLI khỏi $INSTALL_DIR..."

removed_count=0
for bin in "${BINARIES[@]}"; do
  target="$INSTALL_DIR/$bin"
  if [[ -f "$target" || -L "$target" ]]; then
    rm -f "$target"
    echo "  🗑️  Đã xóa: $target"
    removed_count=$((removed_count + 1))
  fi
done

# Kiểm tra thêm trong /usr/local/bin nếu từng cài bằng quyền root
for bin in "${BINARIES[@]}"; do
  target="/usr/local/bin/$bin"
  if [[ -L "$target" ]]; then
    if rm -f "$target" 2>/dev/null; then
      echo "  🗑️  Đã xóa: $target"
      removed_count=$((removed_count + 1))
    fi
  fi
done

echo ""
if [[ $removed_count -gt 0 ]]; then
  echo "🎉 Đã gỡ bỏ thành công $removed_count công cụ CLI khỏi hệ thống."
else
  echo "ℹ️ Không tìm thấy công cụ CLI nào trong $INSTALL_DIR để gỡ bỏ."
fi
echo ""
