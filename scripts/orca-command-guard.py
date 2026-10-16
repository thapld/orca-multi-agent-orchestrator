#!/usr/bin/env python3
"""
Orca Multi-Agent Runtime Command & Stdout Flood Guard Hook (`orca-command-guard.py`).
Executes on `PreToolUse` / `PostToolUse` for Antigravity (`~/.gemini/config/hooks.json`)
and Claude Code (`~/.claude/settings.json`) with zero impact on normal workflows:
  1. PreToolUse (`run_command` / `Bash`):
     - Blocks unescaped backticks (`...`) inside double-quoted inline scripts (`python3 -c "...`..."`, `node -e "...`..."`, `sh -c "...`..."`)
       which trigger shell command substitution (`b""`) and infinite stdout loops.
     - Blocks unbounded raw binary dumps (`cat /app/<binary>`, `subprocess.check_output(['docker', 'exec', ..., 'cat', '/app/...'])`)
       into stdout or memory.
     - Spawns a lightweight background circuit-breaker monitor (`--watch-dir <tasks_dir>`) that checks running background `task-*.log` files
       for the current conversation over the next 30 seconds; if any single task log exceeds 16 MB, it terminates the runaway child command
       (keeping the Worker CLI alive) and truncates the log tail with a diagnostic notice.
  2. PostToolUse:
     - Immediately sweeps any `task-*.log` in the current conversation exceeding 16 MB.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_TASK_LOG_BYTES = 16 * 1024 * 1024  # 16 MB hard cap per single tool call log
HEAD_KEEP_BYTES = 256 * 1024           # Keep first 256 KB of output for debugging
TAIL_KEEP_BYTES = 128 * 1024           # Keep last 128 KB of output for debugging


def _has_unescaped_backtick_in_double_quotes(cmd: str) -> bool:
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "`" and in_double:
            return True
        i += 1
    return False


def _wrap_long_lines(data: bytes, max_line: int = 4096) -> bytes:
    """Normalize CR to LF and guarantee no single line exceeds max_line bytes."""
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    out_lines = []
    for raw_line in data.split(b"\n"):
        if len(raw_line) <= max_line:
            out_lines.append(raw_line)
        else:
            for i in range(0, len(raw_line), max_line):
                out_lines.append(raw_line[i : i + max_line])
    return b"\n".join(out_lines)


def _run_stream_filter() -> None:
    """
    O(1) RAM streaming pipe filter (`<cmd> | orca-command-guard --stream`):
    - Streams first HEAD_KEEP_BYTES (256 KB) immediately to stdout with 4KB line wrapping.
    - Buffers the latest TAIL_KEEP_BYTES (128 KB) in a fixed-size ring buffer in RAM.
    - If upstream exceeds MAX_TASK_LOG_BYTES (16 MB), closes stdin (sending SIGPIPE to stop the producer)
      and flushes the ring-buffer tail with a circuit-breaker notice.
    """
    from collections import deque

    stdin_fd = sys.stdin.buffer
    stdout_fd = sys.stdout.buffer
    total_read = 0
    head_remaining = HEAD_KEEP_BYTES
    tail_chunks: deque[bytes] = deque()
    tail_buffered = 0
    cutoff_triggered = False

    try:
        while True:
            chunk = stdin_fd.read(65536)
            if not chunk:
                break
            total_read += len(chunk)
            wrapped = _wrap_long_lines(chunk, 4096)

            if head_remaining > 0:
                emit = wrapped[:head_remaining]
                stdout_fd.write(emit)
                stdout_fd.flush()
                head_remaining -= len(emit)
                rest = wrapped[len(emit) :]
                if rest:
                    tail_chunks.append(rest)
                    tail_buffered += len(rest)
            else:
                tail_chunks.append(wrapped)
                tail_buffered += len(wrapped)
                while tail_buffered > TAIL_KEEP_BYTES and len(tail_chunks) > 1:
                    removed = tail_chunks.popleft()
                    tail_buffered -= len(removed)

            if total_read >= MAX_TASK_LOG_BYTES:
                cutoff_triggered = True
                try:
                    stdin_fd.close()
                except Exception:
                    pass
                break
    except (BrokenPipeError, KeyboardInterrupt):
        return

    if tail_buffered > 0:
        omitted = max(0, total_read - HEAD_KEEP_BYTES - tail_buffered)
        if omitted > 0 or cutoff_triggered:
            notice = (
                f"\n[ORCA STREAM GUARD: Stream output reached {total_read / (1024 * 1024):.2f} MB "
                f"(omitted {omitted:,} bytes in middle; ring-buffer preserved last {min(tail_buffered, TAIL_KEEP_BYTES):,} bytes)]\n"
            ).encode("utf-8")
            stdout_fd.write(notice)
        tail_data = b"".join(tail_chunks)[-TAIL_KEEP_BYTES:]
        stdout_fd.write(tail_data)
        if not tail_data.endswith(b"\n"):
            stdout_fd.write(b"\n")
        stdout_fd.flush()


def _enforce_task_log_cap(tasks_dir: Path) -> None:
    if not tasks_dir.is_dir():
        return
    try:
        for log_file in tasks_dir.glob("task-*.log"):
            try:
                sz = log_file.stat().st_size
            except Exception:
                continue
            if sz <= MAX_TASK_LOG_BYTES:
                continue

            # 1. Find and terminate ONLY the runaway child command writing to this log file (not the agent CLI itself)
            try:
                lsof_res = subprocess.run(
                    ["lsof", "-t", str(log_file)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if lsof_res.returncode == 0:
                    for pid_str in lsof_res.stdout.splitlines():
                        pid_str = pid_str.strip()
                        if not pid_str.isdigit():
                            continue
                        pid = int(pid_str)
                        if pid <= 1 or pid == os.getpid():
                            continue
                        # Check process command name to avoid killing agy / claude / node parent CLI
                        ps_res = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "comm="],
                            capture_output=True,
                            text=True,
                            timeout=1,
                        )
                        comm = (ps_res.stdout or "").strip().lower()
                        if any(agent_bin in comm for agent_bin in ("agy", "antigravity", "claude", "orca")):
                            continue
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
            except Exception:
                pass

            # 2. Truncate the bloated file while preserving head + tail for the agent to read safely
            try:
                with open(log_file, "rb") as f:
                    head_bytes = f.read(HEAD_KEEP_BYTES)
                    f.seek(max(HEAD_KEEP_BYTES, sz - TAIL_KEEP_BYTES))
                    tail_bytes = f.read(TAIL_KEEP_BYTES)
                # Ensure every line has newline breaks <= 4096 bytes so `head`/`tail` never hang
                head_bytes = _wrap_long_lines(head_bytes, 4096)
                tail_bytes = _wrap_long_lines(tail_bytes, 4096)
                notice = (
                    f"\n\n[ORCA CIRCUIT-BREAKER: Command output exceeded {MAX_TASK_LOG_BYTES // (1024 * 1024)} MB "
                    f"(actual: {sz / (1024 * 1024):.1f} MB). Runaway child process was terminated and middle output "
                    f"was truncated to protect the Worker CLI from hanging. Use bounded filters (`head`, `tail`, `LIMIT`) in your next command.]\n\n"
                ).encode("utf-8")
                with open(log_file, "wb") as f:
                    f.write(head_bytes + notice + tail_bytes)
            except Exception:
                pass
    except Exception:
        pass


def _run_background_watcher(tasks_dir_str: str) -> None:
    tasks_dir = Path(tasks_dir_str)
    # Monitor every 1.5s for up to 36s while a tool command may be running asynchronously
    for _ in range(24):
        _enforce_task_log_cap(tasks_dir)
        time.sleep(1.5)


def _spawn_watcher_for_payload(payload: dict) -> None:
    tf = str(payload.get("transcriptPath") or payload.get("transcript_path") or "")
    tasks_dir = None
    if tf:
        p = Path(tf)
        # ~/.gemini/antigravity-cli/brain/<cid>/.system_generated/logs/transcript.jsonl -> .system_generated/tasks
        if p.parent.name == "logs":
            tasks_dir = p.parent.parent / "tasks"
    if not tasks_dir:
        cid = str(payload.get("conversationId") or payload.get("session_id") or "")
        if cid:
            for root_name in ("antigravity-cli", "antigravity"):
                cand = Path.home() / ".gemini" / root_name / "brain" / cid / ".system_generated" / "tasks"
                if cand.is_dir():
                    tasks_dir = cand
                    break
    if tasks_dir:
        _enforce_task_log_cap(tasks_dir)
        try:
            subprocess.Popen(
                [sys.executable, __file__, "--watch-dir", str(tasks_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--stream":
        _run_stream_filter()
        return

    if len(sys.argv) >= 3 and sys.argv[1] == "--watch-dir":
        _run_background_watcher(sys.argv[2])
        return

    event = os.environ.get("ORCA_GUARD_EVENT", "PreToolUse")
    if len(sys.argv) >= 3 and sys.argv[1] == "--event":
        event = sys.argv[2]
    raw_in = ""
    try:
        raw_in = sys.stdin.read()
    except Exception:
        pass

    payload = {}
    if raw_in.strip():
        try:
            payload = json.loads(raw_in)
        except Exception:
            payload = {}

    _spawn_watcher_for_payload(payload)

    if event != "PreToolUse":
        sys.stdout.write("{}\n")
        return

    allow_out = '{"decision":"ask"}\n' if os.environ.get("ORCA_GUARD_EVENT") == "PreToolUse" else "{}\n"

    tc = payload.get("toolCall") or payload.get("tool_call") or {}
    args = tc.get("args") or payload.get("tool_input") or {}
    cmd = str(args.get("CommandLine") or args.get("command") or "")
    if not cmd:
        sys.stdout.write(allow_out)
        return

    # Guard 1: Unescaped backticks inside double-quoted inline scripts (`python3 -c "...`key`..."`)
    if _has_unescaped_backtick_in_double_quotes(cmd) and re.search(r"\b(?:python3?|node|ruby|perl|bash|zsh|sh)\s+-[ce]\b", cmd):
        reason = (
            "[ORCA COMMAND GUARD] Blocked unescaped backtick (`...`) inside double-quoted inline script (\"...\"). "
            "In zsh/bash, unescaped backticks inside double quotes are executed as shell command substitution BEFORE Python/Node runs "
            "(expanding `key` to an empty string b\"\" and causing infinite find() loops / 90MB stdout floods). "
            "Fix: wrap the inline script in single quotes ('...'), escape the backticks (\\`), or write a script file to /tmp."
        )
        sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
        return

    # Guard 2: Unbounded raw binary dump into stdout or subprocess.check_output
    if (
        re.search(r"check_output\([^)]*['\"]cat['\"]\s*,\s*['\"]/(?:app|usr|bin|sbin|opt)/", cmd)
        or re.search(r"docker\s+exec\s+[^\n|>;]+?\bcat\s+/(?:app|usr|bin|sbin)/[^\s|>;]+\s*$", cmd)
    ):
        reason = (
            "[ORCA COMMAND GUARD] Blocked unbounded raw binary dump (`cat` on compiled container binary into stdout/memory). "
            "Dumping multi-megabyte compiled binaries into stdout or check_output() hangs the Worker CLI. "
            "Fix: use bounded queries (`strings /app/binary | grep -i ... | head -n 40`) or escalate to Coordinator to dispatch `re-worker`."
        )
        sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
        return

    sys.stdout.write(allow_out)


if __name__ == "__main__":
    main()
