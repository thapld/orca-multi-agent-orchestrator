#!/usr/bin/env python3
"""
Orca Multi-Agent Runtime Command & Stdout Flood Guard Hook (`orca-command-guard.py`).
Executes on `PreToolUse` / `PostToolUse` for Antigravity (`~/.gemini/config/hooks.json`)
with zero impact on normal workflows:
  1. PreToolUse (`run_command` / `Bash`):
     - Blocks unescaped backticks (`...`) inside double-quoted inline scripts (`python3 -c "...`..."`, `node -e "...`..."`, `sh -c "...`..."`)
       which trigger shell command substitution (`b""`) and infinite stdout loops.
     - Blocks unbounded `while True:` + `.find(` binary search loops in inline Python scripts without iteration caps (`for _ in range(50):`).
     - Blocks unbounded raw binary dumps (`cat /app/<binary>`, `subprocess.check_output(['docker', 'exec', ..., 'cat', '/app/...'])`)
       into stdout or memory.
     - Ensures a single-instance background circuit-breaker watcher (`--watch-dir <tasks_dir>`) monitors running background `task-*.log`
       files for the conversation; if any single task log exceeds 16 MB, it locates and terminates the runaway child command process group
       (walking `ppid == agent_pid` so the Worker CLI itself is never harmed), waits briefly for PTY drain, strips POSIX sparse `\x00` holes,
       and truncates the log to 256 KB head + notice + 128 KB tail with 4 KB line wrapping.
  2. PostToolUse:
     - Immediately sweeps any `task-*.log` in the current conversation exceeding 16 MB.
  3. Stream Filter Mode (`<cmd> | orca-command-guard --stream`):
     - O(1) RAM streaming filter (256 KB head passthrough + 128 KB ring-buffer tail + 16 MB SIGPIPE upstream cutoff).
"""
from __future__ import annotations

import codecs
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

MAX_TASK_LOG_BYTES = 16 * 1024 * 1024  # 16 MB hard cap per single tool call log
HEAD_KEEP_BYTES = 256 * 1024           # Keep first 256 KB of output for debugging
TAIL_KEEP_BYTES = 128 * 1024           # Keep last 128 KB of output for debugging

PROTECTED_AGENT_MARKERS = (
    "language_server",
    "antigravity",
    "/agy",
    "claude",
    "kimi",
    "orca",
    "electron",
)
EXCLUDED_CHILD_MARKERS = (
    "orca-command-guard",
    "antigravity-hook",
    "language_server",
    " mcp",
    "/mcp",
)


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
    """Strip NUL sparse-hole bytes, normalize CR to LF, and guarantee no single line exceeds max_line bytes."""
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
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


def _get_process_table() -> dict[int, tuple[int, int, str]]:
    """Return mapping of pid -> (ppid, pgid, args_str)."""
    table: dict[int, tuple[int, int, str]] = {}
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid=,args="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split(None, 3)
                if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
                    pid = int(parts[0])
                    ppid = int(parts[1])
                    pgid = int(parts[2])
                    args_str = parts[3] if len(parts) > 3 else ""
                    table[pid] = (ppid, pgid, args_str)
    except Exception:
        pass
    return table


def _collect_descendants(root_pid: int, children_map: dict[int, list[int]]) -> list[int]:
    """Return all descendant PIDs of root_pid in bottom-up order (grandchildren first, root_pid last)."""
    result: list[int] = []
    visited = set()

    def _dfs(curr: int) -> None:
        if curr in visited:
            return
        visited.add(curr)
        for ch in children_map.get(curr, []):
            _dfs(ch)
        result.append(curr)

    _dfs(root_pid)
    return result


def _kill_runaway_process_for_log(log_file: Path, tasks_dir: Path) -> bool:
    """
    Find the process holding `log_file` open:
    - If held directly by a non-agent child process, kill that process.
    - If held by the Agent parent process (`agy` / `language_server`), locate the active tool-execution
      child shell (`ppid == agent_pid`) and terminate its process group and descendants without touching the Agent CLI.
    """
    killed_any = False
    try:
        lsof_res = subprocess.run(
            ["lsof", "-t", str(log_file)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if lsof_res.returncode != 0 or not lsof_res.stdout.strip():
            return False

        holder_pids = [int(x.strip()) for x in lsof_res.stdout.splitlines() if x.strip().isdigit()]
        if not holder_pids:
            return False

        proc_table = _get_process_table()
        children_map: dict[int, list[int]] = {}
        for pid, (ppid, _, _) in proc_table.items():
            children_map.setdefault(ppid, []).append(pid)

        my_pid = os.getpid()
        my_pgid = os.getpgrp()

        # Check if we recorded a command snippet for this task log during PreToolUse
        cmd_hint = ""
        cmd_map_file = tasks_dir / ".orca-task-cmds.json"
        if cmd_map_file.exists():
            try:
                cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                cmd_hint = str(cmap.get(log_file.name) or "")[:48].strip()
            except Exception:
                pass

        for h_pid in holder_pids:
            if h_pid <= 1 or h_pid == my_pid:
                continue
            info = proc_table.get(h_pid)
            if not info:
                continue
            h_ppid, h_pgid, h_args = info
            h_args_low = h_args.lower()
            is_protected_agent = any(m in h_args_low for m in PROTECTED_AGENT_MARKERS)

            if not is_protected_agent:
                # Direct child process holding the log FD
                if any(ex in h_args_low for ex in EXCLUDED_CHILD_MARKERS):
                    continue
                for d_pid in _collect_descendants(h_pid, children_map):
                    if d_pid > 1 and d_pid != my_pid:
                        try:
                            os.kill(d_pid, signal.SIGKILL)
                            killed_any = True
                        except Exception:
                            pass
            else:
                # `h_pid` is the Agent parent (`agy` / `language_server`) writing PTY master output to `task-*.log`.
                # Find its active tool-execution child process (`ppid == h_pid`).
                candidate_children: list[tuple[int, int, str]] = []
                for ch_pid in children_map.get(h_pid, []):
                    if ch_pid <= 1 or ch_pid == my_pid:
                        continue
                    ch_info = proc_table.get(ch_pid)
                    if not ch_info:
                        continue
                    _, ch_pgid, ch_args = ch_info
                    if ch_pgid == my_pgid:
                        continue
                    ch_low = ch_args.lower()
                    if any(ex in ch_low for ex in EXCLUDED_CHILD_MARKERS):
                        continue
                    if any(ag in ch_low for ag in PROTECTED_AGENT_MARKERS):
                        continue
                    # Exclude MCP servers that share process group with language_server and don't run a shell command
                    if ch_pgid == h_pgid and not re.search(r"\b(?:zsh|bash|sh|python3?|node|docker)\b", ch_low):
                        continue
                    candidate_children.append((ch_pid, ch_pgid, ch_args))

                if cmd_hint and len(candidate_children) > 1:
                    matched = [c for c in candidate_children if cmd_hint in c[2]]
                    if matched:
                        candidate_children = matched

                for ch_pid, ch_pgid, _ in candidate_children:
                    # Kill all descendants bottom-up first
                    for d_pid in _collect_descendants(ch_pid, children_map):
                        if d_pid > 1 and d_pid != my_pid and d_pid != h_pid:
                            try:
                                os.kill(d_pid, signal.SIGKILL)
                                killed_any = True
                            except Exception:
                                pass
                    # If the child is a PTY session/process-group leader (ch_pgid != h_pgid), kill its process group
                    if ch_pgid > 1 and ch_pgid != h_pgid and ch_pgid != my_pgid:
                        try:
                            os.killpg(ch_pgid, signal.SIGKILL)
                            killed_any = True
                        except Exception:
                            pass
    except Exception:
        pass
    return killed_any


def _enforce_task_log_cap(tasks_dir: Path) -> int:
    if not tasks_dir.is_dir():
        return 0
    truncated_count = 0
    try:
        for log_file in tasks_dir.glob("task-*.log"):
            try:
                sz = log_file.stat().st_size
            except Exception:
                continue
            if sz <= MAX_TASK_LOG_BYTES:
                continue

            # 1. Read head + non-NUL tail BEFORE killing/truncating
            head_bytes = b""
            tail_bytes = b""
            try:
                with open(log_file, "rb") as f:
                    head_bytes = f.read(HEAD_KEEP_BYTES)
                    f.seek(max(HEAD_KEEP_BYTES, sz - TAIL_KEEP_BYTES))
                    tail_bytes = f.read(TAIL_KEEP_BYTES)
            except Exception:
                pass

            # 2. Terminate ONLY the runaway child command writing to this log file (never the Agent CLI)
            killed = _kill_runaway_process_for_log(log_file, tasks_dir)
            if killed:
                # Wait briefly for PTY buffer to drain and parent Agent CLI to close its write FD
                # so POSIX non-O_APPEND offset doesn't create a sparse \x00 hole after truncation.
                time.sleep(0.25)

            # 3. Ensure every line has newline breaks <= 4096 bytes and zero \x00 sparse holes
            try:
                head_bytes = _wrap_long_lines(head_bytes, 4096)
                tail_bytes = _wrap_long_lines(tail_bytes, 4096)
                notice = (
                    f"\n\n[ORCA CIRCUIT-BREAKER: Command output exceeded {MAX_TASK_LOG_BYTES // (1024 * 1024)} MB "
                    f"(actual: {sz / (1024 * 1024):.1f} MB). Runaway child process was terminated and middle output "
                    f"was truncated to protect the Worker CLI from hanging. Use bounded filters (`head`, `tail`, `for _ in range(50)`) in your next command.]\n\n"
                ).encode("utf-8")
                with open(log_file, "wb") as f:
                    f.write(head_bytes + notice + tail_bytes)
                truncated_count += 1
            except Exception:
                pass
    except Exception:
        pass
    return truncated_count


def _run_background_watcher(tasks_dir_str: str) -> None:
    tasks_dir = Path(tasks_dir_str)
    lock_file = tasks_dir / ".orca-watcher.pid"
    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    start_ts = time.time()
    # Monitor every 1.5s while any command is active (up to 10 minutes max for long builds/tasks)
    try:
        while time.time() - start_ts < 600:
            _enforce_task_log_cap(tasks_dir)
            time.sleep(1.5)
            now = time.time()
            lock_mtime = 0.0
            try:
                if lock_file.exists():
                    lock_mtime = lock_file.stat().st_mtime
            except Exception:
                pass
            recent_log_activity = False
            try:
                for lf in tasks_dir.glob("task-*.log"):
                    if now - lf.stat().st_mtime < 25.0:
                        recent_log_activity = True
                        break
            except Exception:
                pass
            if (now - lock_mtime > 20.0) and not recent_log_activity:
                break
    finally:
        try:
            if lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock_file.unlink(missing_ok=True)
        except Exception:
            pass


def _resolve_tasks_dir(payload: dict) -> Path | None:
    tf = str(payload.get("transcriptPath") or payload.get("transcript_path") or "")
    if tf:
        p = Path(tf)
        if p.parent.name == "logs":
            return p.parent.parent / "tasks"
    cid = str(payload.get("conversationId") or payload.get("session_id") or "")
    if cid:
        for root_name in ("antigravity-cli", "antigravity"):
            cand = Path.home() / ".gemini" / root_name / "brain" / cid / ".system_generated" / "tasks"
            if cand.parent.is_dir():
                return cand
    return None


def _ensure_watcher_for_payload(payload: dict, event: str, cmd: str) -> None:
    tasks_dir = _resolve_tasks_dir(payload)
    if not tasks_dir:
        return
    _enforce_task_log_cap(tasks_dir)
    if event != "PreToolUse":
        return

    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        step_idx = payload.get("stepIdx")
        if step_idx is not None and cmd:
            cmd_map_file = tasks_dir / ".orca-task-cmds.json"
            cmap = {}
            if cmd_map_file.exists():
                try:
                    cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                except Exception:
                    cmap = {}
            if len(cmap) > 40:
                cmap = dict(list(cmap.items())[-20:])
            first_line_cmd = cmd.strip().splitlines()[0][:80] if cmd.strip() else ""
            cmap[f"task-{step_idx}.log"] = first_line_cmd
            cmap[f"task-{int(step_idx) + 1}.log"] = first_line_cmd
            cmd_map_file.write_text(json.dumps(cmap), encoding="utf-8")

        lock_file = tasks_dir / ".orca-watcher.pid"
        if lock_file.exists():
            try:
                old_pid_str = lock_file.read_text(encoding="utf-8").strip()
                if old_pid_str.isdigit():
                    old_pid = int(old_pid_str)
                    os.kill(old_pid, 0)
                    # Existing watcher is alive — touch lockfile to extend its window and return
                    os.utime(lock_file, None)
                    return
            except Exception:
                pass

        subprocess.Popen(
            [sys.executable, __file__, "--watch-dir", str(tasks_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        pass


def _read_json_stdin_nonblocking() -> dict:
    """Read a single JSON object from stdin using select + raw_decode so it never hangs waiting for EOF."""
    text = ""
    try:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        timeout = 1.5
        while True:
            rlist, _, _ = select.select([0], [], [], timeout)
            if not rlist:
                text += decoder.decode(b"", True)
                break
            chunk = os.read(0, 65536)
            if not chunk:
                text += decoder.decode(b"", True)
                break
            timeout = 0.2
            text += decoder.decode(chunk)
            val = text.lstrip()
            if not val:
                continue
            try:
                obj, _ = json.JSONDecoder().raw_decode(val)
                return obj if isinstance(obj, dict) else {}
            except ValueError:
                continue
    except Exception:
        pass
    if text.strip():
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
    return {}


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

    allow_out = '{"decision":"ask"}\n' if event == "PreToolUse" else "{}\n"

    try:
        payload = _read_json_stdin_nonblocking()
        tc = payload.get("toolCall") or payload.get("tool_call") or {}
        args = tc.get("args") or payload.get("tool_input") or {}
        cmd = str(args.get("CommandLine") or args.get("command") or "")

        _ensure_watcher_for_payload(payload, event, cmd)

        if event != "PreToolUse" or not cmd:
            sys.stdout.write(allow_out)
            sys.stdout.flush()
            return

        # Guard 1: Unescaped backticks inside double-quoted inline scripts (`python3 -c "...`key`..."`)
        if _has_unescaped_backtick_in_double_quotes(cmd) and re.search(
            r"\b(?:python3?|node|ruby|perl|bash|zsh|sh)(?:\s+-[a-zA-Z0-9]+)*\s+-[ce]\b", cmd
        ):
            reason = (
                "[ORCA COMMAND GUARD] Blocked unescaped backtick (`...`) inside double-quoted inline script (\"...\"). "
                "In zsh/bash, unescaped backticks inside double quotes are executed as shell command substitution BEFORE Python/Node runs "
                "(expanding `key` to an empty string b\"\" and causing infinite find() loops / 90MB stdout floods). "
                "Fix: wrap the inline script in single quotes ('...'), escape the backticks (\\`), or write a script file to /tmp."
            )
            sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
            sys.stdout.flush()
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
            sys.stdout.flush()
            return

        # Guard 3: Unbounded `while True:` + `.find(` binary search loops in inline Python scripts
        if (
            "while True:" in cmd
            and ".find(" in cmd
            and re.search(r"\bpython3?(?:\s+-[a-zA-Z0-9]+)*\s+-c\b", cmd)
            and "range(" not in cmd
            and "len(" not in cmd.split("while True:", 1)[1]
        ):
            reason = (
                "[ORCA COMMAND GUARD] Blocked unbounded 'while True:' loop with '.find()' in inline Python script. "
                "If the target pattern is short or empty, 'while True: ... data.find()' loops millions of times and floods stdout. "
                "Fix: replace 'while True:' with a bounded loop 'for _ in range(50):' so matches are safely capped at 50."
            )
            sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
            sys.stdout.flush()
            return

        sys.stdout.write(allow_out)
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(allow_out)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
