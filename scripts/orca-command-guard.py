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


AGENT_PIDS_FILE = ".orca-agent-pids.json"
INTERPRETERS = {"node", "python", "python3", "bun", "deno", "ruby", "perl"}


def _ancestor_pids() -> list[int]:
    """PID chain of this hook process up to init (agent CLI / language_server is one of them)."""
    pids: list[int] = []
    try:
        table = _get_process_table()
        cur = os.getppid()
        seen = set()
        transient = {"sh", "bash", "zsh", "dash", "env", "python", "python3", "timeout"}
        while cur > 1 and cur not in seen and len(seen) < 16:
            seen.add(cur)
            ppid, _, args_str = table.get(cur, (0, 0, ""))
            toks = args_str.split()
            exe = os.path.basename(toks[0]).lower() if toks else ""
            # Bỏ qua wrapper ngắn hạn (sh -c của hook, python harness): PID của chúng có thể bị tái sử dụng cho lệnh runaway
            if exe not in transient or _looks_like_agent(args_str):
                pids.append(cur)
            cur = ppid
    except Exception:
        pass
    return pids


def _record_agent_pids(tasks_dir: Path) -> None:
    try:
        f = tasks_dir / AGENT_PIDS_FILE
        old: list[int] = []
        if f.exists():
            try:
                old = [int(x) for x in json.loads(f.read_text(encoding="utf-8")) if str(x).isdigit()]
            except Exception:
                old = []
        merged = [p for p in dict.fromkeys(_ancestor_pids() + old) if _pid_alive(p)][:24]
        f.write_text(json.dumps(merged), encoding="utf-8")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _load_agent_pids(tasks_dir: Path) -> set[int]:
    try:
        raw = json.loads((tasks_dir / AGENT_PIDS_FILE).read_text(encoding="utf-8"))
        return {int(x) for x in raw if str(x).isdigit() and _pid_alive(int(x))}
    except Exception:
        return set()


def _looks_like_agent(args_str: str) -> bool:
    """Match agent markers on the executable name (or installed agent CLI paths for node/python),
    never on arbitrary user script basenames like `python3 orca_test.py` or `node claude_test.js`."""
    toks = args_str.split()
    if not toks:
        return False
    exe = os.path.basename(toks[0]).lower()
    low_all = args_str.lower()
    if ".app/contents/macos/" in low_all:
        app_name = low_all.split(".app/contents/macos/", 1)[0].rsplit("/", 1)[-1]
        if any(m in app_name for m in ("orca", "antigravity", "electron", "claude", "cursor")):
            return True
    if exe in INTERPRETERS:
        if len(toks) > 1:
            script_arg = toks[1].lower().replace("\\", "/")
            if any(
                pkg in script_arg
                for pkg in (
                    "@anthropic-ai/claude-code",
                    "/claude-code/",
                    "/antigravity/",
                    "/antigravity-cli/",
                    "/kimi-cli/",
                    "@openai/codex",
                    "/orca.app/",
                )
            ):
                return True
        return False
    if exe.startswith("language_server"):
        return True
    return exe in {"agy", "antigravity", "claude", "kimi", "codex", "orca", "electron"}


_INTERP_INLINE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?P<interp>python3?|node|ruby|perl|bash|zsh|sh)"
    r"(?P<opts>(?:\s+-[a-zA-Z0-9]+)*)\s+(?P<flag>-c|-e|--eval|-p)\s+\""
)
_UNQUOTED_HEREDOC_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:python3?|node|ruby|perl|bash|zsh|sh)\b[^\n<]*<<-?\s*(?P<delim>[A-Za-z_][A-Za-z0-9_]*)\s*\n"
)
_SAFE_TEXT_PATH_RE = re.compile(
    r"(?:/|^)(?:\.[a-zA-Z0-9_.-]+|"
    r"(?:dockerfile|makefile|procfile|gemfile|rakefile|caddyfile|jenkinsfile|vagrantfile)|"
    r"[^\s/\"']+\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|tsx|jsx|vue|svelte|json|jsonc|ya?ml|toml|ini|cfg|conf|env|"
    r"txt|md|rst|sql|html?|css|scss|less|xml|svg|csv|tsv|log|lock|prisma|graphql|gql|proto|go|rs|rb|php|java|"
    r"kt|kts|scala|c|h|cpp|hpp|cc|cs|gradle|properties|pem|crt|key|pub|cnf)(?:\.[a-zA-Z0-9_.-]+)?)\s*['\"]?\s*$",
    re.IGNORECASE,
)
_PROSE_FLAG_RE = re.compile(
    r"(?:^|\s)(?:--(?:spec|body|question|objective|task-title|display-name|comment|resolution|result|subject|message|notes|title)|-m)"
    r"(?:\s+|=)(?:\"([^\"\\]*(?:\\.[^\"\\]*)*)\"|'([^']*)')"
)


def _strip_single_quotes_and_quoted_heredocs(cmd: str) -> str:
    """Replace single-quoted string contents ('...') and quoted heredoc bodies with spaces
    because POSIX shells never expand backticks or execute inline commands inside single quotes."""
    # 1. Strip quoted heredoc bodies: << 'EOF' ... EOF or << "EOF" ... EOF
    def _heredoc_repl(m: re.Match) -> str:
        return m.group(1) + "\n" + m.group(2)

    cleaned = re.sub(
        r"(<<-?\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"][^\n]*)\n.*?\n(\s*\2\b)",
        _heredoc_repl,
        cmd,
        flags=re.DOTALL,
    )
    # 2. Blank out single-quoted literals while respecting double quotes and backslash escapes
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch == "\\" and not in_single:
            if i + 1 < n:
                out.append(cleaned[i : i + 2])
            else:
                out.append(ch)
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            out.append("'")
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            out.append('"')
            i += 1
            continue
        out.append(" " if in_single else ch)
        i += 1
    return "".join(out)


def _strip_non_exec_prose_flags(cmd: str) -> str:
    """Strip non-executable prose flag values (--spec, --body, --question, -m, etc.)
    and non-executed cat/tee quoted heredocs (`cat << 'EOF' > file`),
    unless the double-quoted value contains an active command substitution $(...) other than orca-team spec."""
    cleaned = re.sub(
        r"\b(?:cat|tee)\b[^\n<]*<<-?\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"][^\n]*\n.*?\n\s*\1\b",
        "",
        cmd,
        flags=re.DOTALL,
    )

    def _repl(m: re.Match) -> str:
        dq_body = m.group(1)
        if dq_body is not None and "$(" in dq_body:
            # Allow $(orca-team spec ...) prose templates to be stripped, but keep active $(python3 -c ...)
            if not re.match(r"^\s*\$\(\s*(?:\S*/)?orca-team\s+spec\b", dq_body) or dq_body.count("$(") > 1:
                return m.group(0)
        prefix = m.group(0).split('"', 1)[0] if dq_body is not None else m.group(0).split("'", 1)[0]
        return prefix + '""'

    return _PROSE_FLAG_RE.sub(_repl, cleaned)


def _double_quoted_arg_has_backtick(cmd: str, start: int) -> bool:
    """cmd[start] is the opening double quote; scan to the matching closing quote honoring backslash escapes."""
    i = start + 1
    n = len(cmd)
    saw_backtick = False
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return saw_backtick
        if ch == "`":
            saw_backtick = True
        i += 1
    return False


def _inline_script_backtick_risk(cmd: str) -> bool:
    """True only when the inline-script argument itself (-c/-e/--eval "...") contains an unescaped backtick,
    or an UNQUOTED heredoc fed to an interpreter contains one (the shell substitutes both)."""
    sanitized = _strip_single_quotes_and_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    for m in _INTERP_INLINE_RE.finditer(sanitized):
        interp, flag = m.group("interp"), m.group("flag")
        if interp in ("bash", "zsh", "sh") and flag != "-c":
            continue  # -e = errexit cho shell, không phải inline script
        if interp in ("python", "python3") and flag != "-c":
            continue
        if _double_quoted_arg_has_backtick(sanitized, m.end() - 1):
            return True
    for m in _UNQUOTED_HEREDOC_RE.finditer(sanitized):
        delim = m.group("delim")
        body = sanitized[m.end() :]
        end = re.search(rf"^\s*{re.escape(delim)}\s*$", body, re.M)
        body = body[: end.start()] if end else body
        if re.search(r"(?<!\\)`", body):
            return True
    return False


def _is_raw_binary_cat_risk(cmd: str) -> bool:
    """Detect unbounded `cat` of compiled container/system binaries while allowing text/script/config files."""
    sanitized = _strip_non_exec_prose_flags(cmd)
    for m in re.finditer(
        r"check_output\([^)]*['\"]cat['\"]\s*,\s*['\"](/(?:app|usr|bin|sbin|opt)/[^'\"]+)['\"]",
        sanitized,
    ):
        target_path = m.group(1)
        if not _SAFE_TEXT_PATH_RE.search(target_path):
            return True
    for m in re.finditer(
        r"docker\s+exec\s+[^\n|>;]+?\bcat\s+(/(?:app|bin|sbin|usr/bin|usr/sbin|usr/local/bin)/[^\s|>;\"']+)\s*[\"']?\s*$",
        sanitized,
        re.MULTILINE,
    ):
        target_path = m.group(1)
        if not _SAFE_TEXT_PATH_RE.search(target_path):
            return True
    return False


def _is_unbounded_find_loop_risk(cmd: str) -> bool:
    """Detect unbounded `while True:` + `.find(` loops in inline Python scripts."""
    sanitized = _strip_non_exec_prose_flags(cmd)
    if (
        "while True:" not in sanitized
        or not re.search(r"\bpython3?(?:\s+-[a-zA-Z0-9]+)*\s+-c\b", sanitized)
        or "range(" in sanitized
    ):
        return False
    after_while = sanitized.split("while True:", 1)[1]
    if ".find(" not in after_while or "len(" in after_while:
        return False
    # Strip EOF-only sentinel breaks (`if idx == -1: break` or `if pos < 0: break`) which still flood millions of lines
    after_cleaned = re.sub(
        r"if\s+[^:\n]+?(?:==\s*-1|<\s*0|is\s+None)\s*:\s*break\b",
        "",
        after_while,
    )
    if re.search(r"\bbreak\b", after_cleaned):
        return False
    return True


def _align_utf8_split(data: bytes, pos: int) -> int:
    """Back up pos (up to 3 bytes) so we never split inside a multi-byte UTF-8 sequence."""
    if pos <= 0 or pos >= len(data):
        return max(0, min(pos, len(data)))
    start = max(0, pos - 3)
    while pos > start and (data[pos] & 0xC0) == 0x80:
        pos -= 1
    return pos


def _trim_utf8_head(data: bytes) -> bytes:
    """Skip leading orphaned UTF-8 continuation bytes at the start of a tail slice."""
    i = 0
    lim = min(len(data), 3)
    while i < lim and (data[i] & 0xC0) == 0x80:
        i += 1
    return data[i:]


def _trim_utf8_tail(data: bytes) -> bytes:
    """Strip trailing incomplete multi-byte UTF-8 starter sequence at the end of a head slice."""
    if not data:
        return data
    for back in range(1, min(len(data), 3) + 1):
        b = data[-back]
        if (b & 0x80) == 0x00:
            return data
        if (b & 0xC0) == 0xC0:
            expected_len = 2 if (b & 0xE0) == 0xC0 else (3 if (b & 0xF0) == 0xE0 else 4)
            return data[:-back] if back < expected_len else data
    return data


def _wrap_long_lines(data: bytes, max_line: int = 4096) -> bytes:
    """Strip NUL sparse-hole bytes, normalize CR to LF, and guarantee no single line exceeds max_line bytes
    without splitting multi-byte UTF-8 sequences."""
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    out_lines: list[bytes] = []
    for raw_line in data.split(b"\n"):
        if len(raw_line) <= max_line:
            out_lines.append(raw_line)
        else:
            pos = 0
            n_line = len(raw_line)
            while pos < n_line:
                if n_line - pos <= max_line:
                    out_lines.append(raw_line[pos:])
                    break
                end_pos = _align_utf8_split(raw_line, pos + max_line)
                if end_pos <= pos:
                    end_pos = pos + max_line
                out_lines.append(raw_line[pos:end_pos])
                pos = end_pos
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
    total_wrapped = 0
    head_emitted = 0
    head_remaining = HEAD_KEEP_BYTES
    tail_chunks: deque[bytes] = deque()
    tail_buffered = 0
    cutoff_triggered = False
    utf8_carry = b""

    try:
        while True:
            chunk = stdin_fd.read(65536)
            if not chunk:
                if utf8_carry:
                    wrapped_tail = _wrap_long_lines(utf8_carry, 4096)
                    utf8_carry = b""
                    total_wrapped += len(wrapped_tail)
                    if head_remaining > 0:
                        emit = wrapped_tail[:head_remaining]
                        stdout_fd.write(emit)
                        stdout_fd.flush()
                        head_emitted += len(emit)
                        head_remaining -= len(emit)
                        rest = wrapped_tail[len(emit) :]
                        if rest:
                            tail_chunks.append(rest)
                            tail_buffered += len(rest)
                    else:
                        tail_chunks.append(wrapped_tail)
                        tail_buffered += len(wrapped_tail)
                break
            total_read += len(chunk)
            combined = utf8_carry + chunk
            safe_trimmed = _trim_utf8_tail(combined)
            utf8_carry = combined[len(safe_trimmed) :]
            wrapped = _wrap_long_lines(safe_trimmed, 4096)
            total_wrapped += len(wrapped)

            if head_remaining > 0:
                if len(wrapped) <= head_remaining:
                    emit = wrapped
                    rest = b""
                    head_remaining -= len(emit)
                else:
                    split_at = _align_utf8_split(wrapped, head_remaining)
                    if split_at <= 0:
                        split_at = head_remaining
                    emit = wrapped[:split_at]
                    rest = wrapped[split_at:]
                    head_remaining = 0
                stdout_fd.write(emit)
                stdout_fd.flush()
                head_emitted += len(emit)
                if rest:
                    tail_chunks.append(rest)
                    tail_buffered += len(rest)
            else:
                tail_chunks.append(wrapped)
                tail_buffered += len(wrapped)

            while len(tail_chunks) > 1 and (tail_buffered - len(tail_chunks[0])) >= TAIL_KEEP_BYTES:
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
        raw_tail = b"".join(tail_chunks)
        if len(raw_tail) > TAIL_KEEP_BYTES:
            tail_data = _trim_utf8_head(raw_tail[-TAIL_KEEP_BYTES:])
        else:
            tail_data = raw_tail
        tail_data = _wrap_long_lines(tail_data, 4096)
        omitted = max(0, total_wrapped - head_emitted - len(tail_data))
        if omitted > 0 or cutoff_triggered:
            notice = (
                f"\n[ORCA STREAM GUARD: Stream output reached {total_read / (1024 * 1024):.2f} MB "
                f"(omitted {omitted:,} bytes in middle; ring-buffer preserved last {len(tail_data):,} bytes)]\n"
            ).encode("utf-8")
            stdout_fd.write(notice)
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
    - If held directly by a non-agent child process, kill that process and its process group.
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
        agent_pids = _load_agent_pids(tasks_dir)
        agent_pgids = {proc_table[p][1] for p in agent_pids if p in proc_table}

        # Check if we recorded command snippets for this task log during PreToolUse
        cmd_hints: list[str] = []
        cmd_map_file = tasks_dir / ".orca-task-cmds.json"
        if cmd_map_file.exists():
            try:
                cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                raw_entry = cmap.get(log_file.name)
                if isinstance(raw_entry, list):
                    cmd_hints = [str(x)[:160].strip() for x in raw_entry if str(x).strip()]
                elif raw_entry:
                    cmd_hints = [str(raw_entry)[:160].strip()]
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
            is_protected_agent = (h_pid in agent_pids) or _looks_like_agent(h_args)

            if not is_protected_agent:
                # Direct child process holding the log FD
                if any(ex in h_args_low for ex in EXCLUDED_CHILD_MARKERS):
                    continue
                for d_pid in _collect_descendants(h_pid, children_map):
                    if d_pid > 1 and d_pid != my_pid and d_pid not in agent_pids:
                        try:
                            os.kill(d_pid, signal.SIGKILL)
                            killed_any = True
                        except Exception:
                            pass
                if h_pgid > 1 and h_pgid != my_pgid and h_pgid not in agent_pgids:
                    try:
                        os.killpg(h_pgid, signal.SIGKILL)
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
                    if ch_pid in agent_pids or (not agent_pids and _looks_like_agent(ch_args)):
                        continue
                    # Exclude MCP servers that share process group with language_server and don't run a shell command
                    if ch_pgid == h_pgid and not re.search(r"\b(?:zsh|bash|sh|python3?|node|docker)\b", ch_low):
                        continue
                    candidate_children.append((ch_pid, ch_pgid, ch_args))

                # Chỉ kill khi xác định ĐƠN NHẤT được tiến trình chạy lệnh của log này.
                # Mơ hồ (0 hoặc >1 ứng viên) ➔ KHÔNG kill gì (chỉ cắt log) để không giết nhầm dev server / MCP server / lệnh song song.
                if cmd_hints:
                    matched: list[tuple[int, int, str]] = []
                    for hint in reversed(cmd_hints):
                        exact = [c for c in candidate_children if hint in c[2]]
                        if len(exact) == 1:
                            matched = exact
                            break
                        hint_tokens = [t for t in re.split(r"\s+", hint) if len(t) > 2][:8]
                        tok_match = [
                            c for c in candidate_children
                            if hint in c[2] or (hint_tokens and all(t in c[2] for t in hint_tokens))
                        ]
                        if len(tok_match) == 1:
                            matched = tok_match
                            break
                    candidate_children = matched
                elif len(candidate_children) != 1:
                    candidate_children = []

                for ch_pid, ch_pgid, _ in candidate_children:
                    # Kill all descendants bottom-up first
                    for d_pid in _collect_descendants(ch_pid, children_map):
                        if d_pid > 1 and d_pid != my_pid and d_pid != h_pid and d_pid not in agent_pids:
                            try:
                                os.kill(d_pid, signal.SIGKILL)
                                killed_any = True
                            except Exception:
                                pass
                    # If the child is a PTY session/process-group leader (ch_pgid != h_pgid), kill its process group
                    if ch_pgid > 1 and ch_pgid != h_pgid and ch_pgid != my_pgid and ch_pgid not in agent_pgids:
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
                    head_bytes = _trim_utf8_tail(f.read(HEAD_KEEP_BYTES))
                    f.seek(max(HEAD_KEEP_BYTES, sz - TAIL_KEEP_BYTES))
                    tail_bytes = _trim_utf8_head(f.read(TAIL_KEEP_BYTES))
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
                marker = b"\n\n[ORCA CIRCUIT-BREAKER:"
                if marker in head_bytes:
                    head_bytes = head_bytes.split(marker, 1)[0]
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
    # Monitor every 1.5s while any command is active (extending window whenever lock_file is touched)
    try:
        while True:
            _enforce_task_log_cap(tasks_dir)
            time.sleep(1.5)
            now = time.time()
            lock_mtime = start_ts
            try:
                if lock_file.exists():
                    lock_mtime = lock_file.stat().st_mtime
            except Exception:
                pass
            if now - max(start_ts, lock_mtime) >= 600:
                break
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


def _has_oversized_log(tasks_dir: Path) -> bool:
    try:
        return any(lf.stat().st_size > MAX_TASK_LOG_BYTES for lf in tasks_dir.glob("task-*.log"))
    except Exception:
        return False


def _ensure_watcher_for_payload(payload: dict, event: str, cmd: str) -> None:
    tasks_dir = _resolve_tasks_dir(payload)
    if not tasks_dir:
        return
    # Hook path chỉ stat() — lsof/ps/kill chạy trong watcher tách rời để không vượt timeout 5s của hook
    oversized = tasks_dir.is_dir() and _has_oversized_log(tasks_dir)
    if event != "PreToolUse" and not oversized:
        return

    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        _record_agent_pids(tasks_dir)
        step_idx = payload.get("stepIdx")
        if step_idx is not None and cmd:
            cmd_map_file = tasks_dir / ".orca-task-cmds.json"
            cmap: dict = {}
            if cmd_map_file.exists():
                try:
                    cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                except Exception:
                    cmap = {}
            if len(cmap) > 40:
                cmap = dict(list(cmap.items())[-20:])
            non_comment_lines = [
                ln.strip()
                for ln in cmd.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            first_line_cmd = non_comment_lines[0][:160] if non_comment_lines else ""
            if str(step_idx).isdigit() and first_line_cmd:
                for key in (f"task-{step_idx}.log", f"task-{int(step_idx) + 1}.log"):
                    prev = cmap.get(key)
                    if isinstance(prev, list):
                        if first_line_cmd not in prev:
                            cmap[key] = (prev + [first_line_cmd])[-4:]
                    elif isinstance(prev, str) and prev and prev != first_line_cmd:
                        cmap[key] = [prev, first_line_cmd]
                    else:
                        cmap[key] = [first_line_cmd]
            cmd_map_file.write_text(json.dumps(cmap), encoding="utf-8")

        lock_file = tasks_dir / ".orca-watcher.pid"
        if lock_file.exists():
            try:
                old_pid_str = lock_file.read_text(encoding="utf-8").strip()
                lock_age = time.time() - lock_file.stat().st_mtime
                if old_pid_str.isdigit() and lock_age < 600:
                    old_pid = int(old_pid_str)
                    os.kill(old_pid, 0)
                    # Existing watcher is alive — touch lockfile to extend its window and return
                    os.utime(lock_file, None)
                    return
            except Exception:
                pass

        proc = subprocess.Popen(
            [sys.executable, __file__, "--watch-dir", str(tasks_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            lock_file.write_text(str(proc.pid), encoding="utf-8")
        except Exception:
            pass
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
            if len(text) > 8 * 1024 * 1024:
                break  # payload bất thường (>8MB) — fail-open
            val = text.lstrip()
            if not val or not val.rstrip().endswith("}"):
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
        if _inline_script_backtick_risk(cmd[:200000]):
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
        if _is_raw_binary_cat_risk(cmd[:200000]):
            reason = (
                "[ORCA COMMAND GUARD] Blocked unbounded raw binary dump (`cat` on compiled container binary into stdout/memory). "
                "Dumping multi-megabyte compiled binaries into stdout or check_output() hangs the Worker CLI. "
                "Fix: use bounded queries (`strings /app/binary | grep -i ... | head -n 40`) or escalate to Coordinator to dispatch `re-worker`."
            )
            sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
            sys.stdout.flush()
            return

        # Guard 3: Unbounded `while True:` + `.find(` binary search loops in inline Python scripts
        if _is_unbounded_find_loop_risk(cmd[:200000]):
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

