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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
removed_count=0
for bin in "${BINARIES[@]}"; do
  target="$INSTALL_DIR/$bin"
  link_tgt="$(readlink "$target" 2>/dev/null || true)"
  # Chỉ xóa symlink trỏ vào bộ công cụ này, hoặc regular file có chứa dấu hiệu Orca Orchestrator (không xóa symlink/file riêng của người dùng)
  if [[ -L "$target" ]]; then
    if [[ "$link_tgt" == *orca-multi-agent-orchestrator* || "$link_tgt" == "$SCRIPT_DIR/"* ]]; then
      rm -f "$target"
      echo "  🗑️  Đã xóa: $target"
      removed_count=$((removed_count + 1))
    fi
  elif [[ -f "$target" ]] && grep -Eq "Orca (Multi-Agent|Orchestrator|Dispatch|Hotfix|PR Review)|orca-team|orca-command-guard" "$target" 2>/dev/null; then
    rm -f "$target"
    echo "  🗑️  Đã xóa: $target"
    removed_count=$((removed_count + 1))
  fi
done

# Kiểm tra thêm trong /usr/local/bin nếu từng cài bằng quyền root
for bin in "${BINARIES[@]}"; do
  target="/usr/local/bin/$bin"
  link_tgt="$(readlink "$target" 2>/dev/null || true)"
  if [[ -L "$target" && ( "$link_tgt" == *orca-multi-agent-orchestrator* || "$link_tgt" == "$SCRIPT_DIR/"* ) ]]; then
    if rm -f "$target" 2>/dev/null; then
      echo "  🗑️  Đã xóa: $target"
      removed_count=$((removed_count + 1))
    fi
  fi
done

# Gỡ bỏ orca-command-guard khỏi ~/.gemini/config/hooks.json
python3 - << 'PY' >/dev/null 2>&1 || true
import json, os
from pathlib import Path
hooks_path = Path.home() / ".gemini" / "config" / "hooks.json"
if hooks_path.exists():
    try:
        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "orca-command-guard" in data:
            del data["orca-command-guard"]
            tmp_path = hooks_path.with_name(f"{hooks_path.name}.{os.getpid()}.orca-tmp")
            tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp_path, hooks_path)
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
