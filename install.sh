#!/usr/bin/env bash
# install.sh: Cài đặt bộ công cụ Orca Multi-Agent Orchestrator vào ~/.local/bin
set -e

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$1" == "--uninstall" || "$1" == "uninstall" || "$1" == "-u" ]]; then
  exec "$SCRIPT_DIR/uninstall.sh"
fi

echo "🐋 [Orca Orchestrator Installer] Đang cài đặt vào $INSTALL_DIR..."

link_or_copy() {
  local src="$1"
  local dst="$2"
  chmod +x "$src"
  ln -sf "$src" "$dst" 2>/dev/null || cp -f "$src" "$dst"
  echo "  ✅ Đã cài đặt: $(basename "$dst")"
}

link_or_copy "$SCRIPT_DIR/scripts/orca-project-init" "$INSTALL_DIR/orca-project-init"
link_or_copy "$SCRIPT_DIR/scripts/orca-project-init" "$INSTALL_DIR/orca-init"
link_or_copy "$SCRIPT_DIR/scripts/orca-team" "$INSTALL_DIR/orca-team"
link_or_copy "$SCRIPT_DIR/scripts/orca-dispatch-task.sh" "$INSTALL_DIR/orca-dispatch"
link_or_copy "$SCRIPT_DIR/scripts/orca-issue-hotfix.sh" "$INSTALL_DIR/orca-hotfix"
link_or_copy "$SCRIPT_DIR/scripts/orca-pr-review.sh" "$INSTALL_DIR/orca-review"

echo ""
echo "🎉 Cài đặt hoàn tất! Hãy đảm bảo '$INSTALL_DIR' có trong PATH của bạn:"
echo '   export PATH="$HOME/.local/bin:$PATH"'
echo ""
