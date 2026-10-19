#!/usr/bin/env bash
# install.sh: Cài đặt bộ công cụ Orca Multi-Agent Orchestrator vào ~/.local/bin
set -e

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--uninstall" || "${1:-}" == "uninstall" || "${1:-}" == "-u" ]]; then
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
link_or_copy "$SCRIPT_DIR/scripts/orca-team" "$INSTALL_DIR/orca-monitor"
link_or_copy "$SCRIPT_DIR/scripts/orca-dispatch-task.sh" "$INSTALL_DIR/orca-dispatch"
link_or_copy "$SCRIPT_DIR/scripts/orca-issue-hotfix.sh" "$INSTALL_DIR/orca-hotfix"
link_or_copy "$SCRIPT_DIR/scripts/orca-pr-review.sh" "$INSTALL_DIR/orca-review"
link_or_copy "$SCRIPT_DIR/scripts/orca-pr-review.sh" "$INSTALL_DIR/orca-review-pr"
link_or_copy "$SCRIPT_DIR/scripts/orca-command-guard.py" "$INSTALL_DIR/orca-command-guard"

if [[ -d "$SCRIPT_DIR/knowledge/system-engineer" ]]; then
  mkdir -p "${HOME}/.orca/knowledge"
  rm -rf "${HOME}/.orca/knowledge/system-engineer"
  ln -sf "$SCRIPT_DIR/knowledge/system-engineer" "${HOME}/.orca/knowledge/system-engineer" 2>/dev/null || cp -R "$SCRIPT_DIR/knowledge/system-engineer" "${HOME}/.orca/knowledge/system-engineer"
  echo "  🧠 Đã cài đặt: System Engineering Knowledge Base (~/.orca/knowledge/system-engineer)"
fi

# Kích hoạt Orca Agent Status Hooks (vòng xoay vàng, chấm xanh & chuông thông báo cho Antigravity/Claude/Kimi/Gemini)
if [[ -x "/Applications/Orca.app/Contents/MacOS/Orca" && -f "/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/agent-hooks/managed-agent-hook-controls.js" ]]; then
  ELECTRON_RUN_AS_NODE=1 /Applications/Orca.app/Contents/MacOS/Orca -e '
    const m = require("/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/agent-hooks/managed-agent-hook-controls.js");
    m.installManagedAgentHooks(null, {shouldHydrateShellPath: true}).then(() => process.exit(0)).catch(() => process.exit(1));
  ' >/dev/null 2>&1 || true
  echo "  🔔 Đã kích hoạt Orca Agent Status Hooks (hiển thị vòng xoay loading, chấm xanh & chuông thông báo)."
elif command -v orca >/dev/null 2>&1; then
  orca agent hooks on --json >/dev/null 2>&1 || true
fi

# Đăng ký Orca Command & Stdout Flood Guard Hook trên toàn bộ Agent CLI hiện có (Antigravity, Claude Code, Gemini CLI, Kimi, OpenClaude, Qwen)
if hook_res=$(python3 "$INSTALL_DIR/orca-command-guard" --install-hooks "$INSTALL_DIR/orca-command-guard" 2>/dev/null); then
  hook_targets=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(", ".join(d.get("configured") or ["antigravity"]))' "$hook_res" 2>/dev/null || echo "antigravity, claude, gemini, kimi")
  echo "  🛡️  Đã kích hoạt Orca Command & Token-Leak Guard Hook (PreToolUse/BeforeTool + 16MB Log Circuit-Breaker) cho: $hook_targets."
else
  echo "  ⚠️  Chưa đăng ký đầy đủ Orca Command Guard cho một số Agent CLI. Chạy 'orca-monitor --fix' để kiểm tra chi tiết."
fi

# Đảm bảo ~/.zshenv có ~/.local/bin trong PATH cho cả non-interactive zsh -c (Claude Code / Codex / Antigravity)
if [[ -f "$HOME/.zshenv" ]]; then
  if ! grep -Eq '(^|[/:])\.local/bin([/:"'\''[:space:]]|$)' "$HOME/.zshenv" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.zshenv"
  fi
else
  printf 'export PATH="$HOME/.local/bin:$PATH"\n' > "$HOME/.zshenv"
fi

echo ""
echo "🎉 Cài đặt hoàn tất! Đã đảm bảo '$INSTALL_DIR' có trong PATH (~/.zshenv):"
echo '   export PATH="$HOME/.local/bin:$PATH"'
echo ""
echo "========================================================================"
echo "👉 HƯỚNG DẪN KÍCH HOẠT MULTI-AGENT ORCHESTRATION CHO DỰ ÁN CỦA BẠN:"
echo "========================================================================"
echo "1. Di chuyển tới thư mục dự án cần lập trình:"
echo "   cd /path/to/your/project"
echo ""
echo "2. Khởi tạo cấu hình đội ngũ (chọn agent & model bằng con trỏ; worktree tự động co giãn khi bận):"
echo "   orca-project-init"
echo ""
echo "3. Mở Antigravity, Claude Code, hoặc Orca tại thư mục dự án đó:"
echo "   - Antigravity: agy"
echo "   - Claude Code: claude"
echo "   - Orca ADE:    orca"
echo ""
echo "4. Khi bạn prompt yêu cầu làm tính năng, Lead Coordinator sẽ tự động tuân thủ"
echo "   quy trình 4 bước (run-create/task-create ➔ worker-start cho Dev/DevOps/Sec/QA/Bench/RE/Critic"
echo "   ➔ check --wait ➔ gate-create chờ duyệt trước khi merge)!"
echo "========================================================================"
echo ""

