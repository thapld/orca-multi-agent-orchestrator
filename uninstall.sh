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
  "orca-command-guard"
)

echo "🐋 [Orca Orchestrator Uninstaller] Đang gỡ bỏ các CLI khỏi $INSTALL_DIR..."

removed_count=0
for bin in "${BINARIES[@]}"; do
  target="$INSTALL_DIR/$bin"
  # Chỉ xóa symlink trỏ vào bộ công cụ này, hoặc file copy có chứa dấu hiệu Orca (không xóa file trùng tên của người dùng)
  if [[ -L "$target" && "$(readlink "$target")" == *orca-multi-agent-orchestrator* ]] || { [[ -f "$target" && ! -L "$target" ]] && grep -qi "orca" "$target" 2>/dev/null; }; then
    rm -f "$target"
    echo "  🗑️  Đã xóa: $target"
    removed_count=$((removed_count + 1))
  fi
done

# Kiểm tra thêm trong /usr/local/bin nếu từng cài bằng quyền root
for bin in "${BINARIES[@]}"; do
  target="/usr/local/bin/$bin"
  if [[ -L "$target" && "$(readlink "$target")" == *orca-multi-agent-orchestrator* ]]; then
    if rm -f "$target" 2>/dev/null; then
      echo "  🗑️  Đã xóa: $target"
      removed_count=$((removed_count + 1))
    fi
  fi
done

# Gỡ bỏ orca-command-guard khỏi ~/.gemini/config/hooks.json
python3 - << 'PY' >/dev/null 2>&1 || true
import json
from pathlib import Path
hooks_path = Path.home() / ".gemini" / "config" / "hooks.json"
if hooks_path.exists():
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        if "orca-command-guard" in data:
            del data["orca-command-guard"]
            hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
PY

echo ""
if [[ $removed_count -gt 0 ]]; then
  echo "🎉 Đã gỡ bỏ thành công $removed_count công cụ CLI khỏi hệ thống."
else
  echo "ℹ️ Không tìm thấy công cụ CLI nào trong $INSTALL_DIR để gỡ bỏ."
fi
echo ""
