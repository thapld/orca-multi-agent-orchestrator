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
AGENT_EXEC_NAMES = {
    "agy",
    "antigravity",
    "claude",
    "kimi",
    "codex",
    "orca",
    "electron",
    "orca-team",
    "orca-monitor",
    "orca-project-init",
    "orca-init",
}


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
            is_transient = exe in transient or exe.startswith("python")
            # Bỏ qua wrapper ngắn hạn (sh -c của hook, python harness): PID của chúng có thể bị tái sử dụng cho lệnh runaway
            if not is_transient or _looks_like_agent(args_str):
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
    """Match agent markers on the executable name (or installed agent CLI paths/wrappers for node/python),
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
    if exe in INTERPRETERS or exe.startswith("python"):
        non_flag_args = [t for t in toks[1:5] if not t.startswith("-")]
        for raw_arg in non_flag_args[:2]:
            script_arg = raw_arg.lower().replace("\\", "/")
            script_base = os.path.basename(script_arg)
            if script_base in AGENT_EXEC_NAMES:
                return True
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
    return exe in AGENT_EXEC_NAMES


_INTERP_INLINE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?P<interp>python3?(?:\.\d+)?|node|ruby|perl|bash|zsh|sh)"
    r"(?P<opts>(?:\s+--?[a-zA-Z0-9_=.-]+)*)\s+(?P<flag>-(?:[a-zA-Z0-9]*[ce]|p)|--eval)(?:\s+|=)?\""
)
_UNQUOTED_HEREDOC_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:python3?(?:\.\d+)?|node|ruby|perl|bash|zsh|sh)\b[^\n<]*<<-?\s*(?P<delim>[A-Za-z_][A-Za-z0-9_-]*)[ \t]*\n"
)
_SAFE_TEXT_PATH_RE = re.compile(
    r"(?:/|^)(?:\.[a-zA-Z0-9_.-]+|"
    r"(?:dockerfile|makefile|procfile|gemfile|rakefile|caddyfile|jenkinsfile|vagrantfile|"
    r"readme|license|version|changelog|notice|authors|contributors|codeowners|pipfile|brewfile|justfile)|"
    r"[^\s/\"']+\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|tsx|jsx|vue|svelte|json|jsonc|ya?ml|toml|ini|cfg|conf|env|"
    r"txt|md|rst|sql|html?|css|scss|less|xml|svg|csv|tsv|log|lock|prisma|graphql|gql|proto|go|rs|rb|php|java|"
    r"kt|kts|scala|c|h|cpp|hpp|cc|cs|gradle|properties|pem|crt|key|pub|cnf)(?:\.[a-zA-Z0-9_.-]+)?)\s*['\"]?\s*$",
    re.IGNORECASE,
)
_PROSE_FLAG_PREFIX_RE = re.compile(
    r"(?:--(?:spec|body|question|objective|task-title|display-name|comment|resolution|result|subject|message|notes|title)|-m)(?:\s+|=)"
)
_WHILE_TRUE_RE = re.compile(r"\bwhile\s*(?:\(\s*)?(?:True|1)(?:\s*\))?\s*:")


def _find_double_quote_end(s: str, quote_idx: int) -> int:
    """Given s[quote_idx] == '\"', find the matching closing '\"' while respecting backslash escapes
    and arbitrarily nested $(...) command substitutions and inner quotes in POSIX shells."""
    i = quote_idx + 1
    n = len(s)
    stack: list[str] = ["dq"]
    while i < n:
        ch = s[i]
        top = stack[-1]
        if ch == "\\":
            i += 2
            continue
        if top == "dq":
            if ch == '"':
                stack.pop()
                if not stack:
                    return i
            elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                stack.append("cmd_sub")
                i += 2
                continue
        else:  # top == "cmd_sub"
            if ch == "'":
                end_sq = s.find("'", i + 1)
                if end_sq == -1:
                    return -1
                i = end_sq + 1
                continue
            elif ch == '"':
                stack.append("dq")
            elif ch == "$" and i + 1 < n and s[i + 1] == "(":
                stack.append("cmd_sub")
                i += 2
                continue
            elif ch == "(":
                stack.append("cmd_sub")
            elif ch == ")":
                stack.pop()
            elif ch == "#" and (i == 0 or s[i - 1] in " \t\n;(&|"):
                nl = s.find("\n", i + 1)
                if nl == -1:
                    return -1
                i = nl + 1
                continue
        i += 1
    return -1


def _strip_quoted_heredocs(cmd: str) -> str:
    """Strip quoted heredoc bodies: << 'EOF' ... EOF, << "EOF" ... EOF, or << \\EOF ... EOF."""
    def _heredoc_repl(m: re.Match) -> str:
        return m.group(1) + "\n" + m.group(3)

    out = re.sub(
        r"(<<-?\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"][^\n]*)\n(?:.*?\n)?([ \t]*\2[ \t]*(?=\n|$))",
        _heredoc_repl,
        cmd,
        flags=re.DOTALL,
    )
    out = re.sub(
        r"(<<-?\s*\\([A-Za-z_][A-Za-z0-9_-]*)[^\n]*)\n(?:.*?\n)?([ \t]*\2[ \t]*(?=\n|$))",
        _heredoc_repl,
        out,
        flags=re.DOTALL,
    )
    return out


def _blank_single_quotes(cmd: str) -> str:
    """Blank out single-quoted literals while respecting double quotes (including $(...) nesting) and backslash escapes."""
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            out.append(cmd[i : i + 2])
            i += 2
            continue
        if ch == "'":
            out.append("'")
            i += 1
            while i < n and cmd[i] != "'":
                out.append(" ")
                i += 1
            if i < n:
                out.append("'")
                i += 1
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(cmd, i)
            if end_dq == -1:
                out.append(ch)
                i += 1
                continue
            out.append(cmd[i : end_dq + 1])
            i = end_dq + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_single_quotes_and_quoted_heredocs(cmd: str) -> str:
    """Replace single-quoted string contents ('...') and quoted heredoc bodies with spaces."""
    return _blank_single_quotes(_strip_quoted_heredocs(cmd))


def _is_safe_orca_spec_substitution(dq_body: str) -> bool:
    """Return True if dq_body has no shell expansion, or is purely a single `$(orca-team spec ...)` call
    without unescaped backticks, extra command substitutions, or chained commands."""
    # Scan dq_body using our quote/cmd_sub state machine
    if not dq_body:
        return True
    stripped = dq_body.strip()
    if "$(" not in stripped and "`" not in stripped:
        return True
    if not re.match(r"^\$\(\s*(?:\S*/)?orca-team\s+spec\b", stripped) or not stripped.endswith(")"):
        return False
    inner = stripped[2:-1]
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end_sq = inner.find("'", i + 1)
            if end_sq == -1:
                return False
            i = end_sq + 1
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(inner, i)
            if end_dq == -1:
                return False
            # Inside inner "...", neither unescaped ` nor $( is safe in a static spec string
            sub_dq = inner[i + 1 : end_dq]
            j = 0
            while j < len(sub_dq):
                c2 = sub_dq[j]
                if c2 == "\\":
                    j += 2
                    continue
                if c2 == "`" or (c2 == "$" and j + 1 < len(sub_dq) and sub_dq[j + 1] == "("):
                    return False
                j += 1
            i = end_dq + 1
            continue
        # Outside quotes inside $(orca-team spec ...): forbid backticks, nested $(), subshells, or command chaining
        if ch in "`;&|\n()" or (ch == "$" and i + 1 < n and inner[i + 1] == "("):
            return False
        i += 1
    return True


def _strip_non_exec_prose_flags(cmd: str) -> str:
    """Strip non-executable prose flag values (--spec, --body, --question, -m, etc.)
    and non-executed cat/tee quoted heredocs (`cat << 'EOF' > file`),
    only matching flags at top-level (outside single/double quotes) and properly handling
    nested quotes inside `--spec "$(orca-team spec ...)"`."""
    cleaned = re.sub(
        r"\b(?:cat|tee)\b[^\n<;|&]*<<-?\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"][^\n]*\n(?:.*?\n)?[ \t]*\1[ \t]*(?=\n|$)",
        "",
        cmd,
        flags=re.DOTALL,
    )
    cleaned = re.sub(
        r"\b(?:cat|tee)\b[^\n<;|&]*<<-?\s*\\([A-Za-z_][A-Za-z0-9_-]*)[^\n]*\n(?:.*?\n)?[ \t]*\1[ \t]*(?=\n|$)",
        "",
        cleaned,
        flags=re.DOTALL,
    )
    out: list[str] = []
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch == "\\":
            out.append(cleaned[i : i + 2])
            i += 2
            continue
        if ch == "'":
            end_sq = cleaned.find("'", i + 1)
            if end_sq == -1:
                out.append(cleaned[i:])
                break
            out.append(cleaned[i : end_sq + 1])
            i = end_sq + 1
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(cleaned, i)
            if end_dq == -1:
                out.append(ch)
                i += 1
                continue
            out.append(cleaned[i : end_dq + 1])
            i = end_dq + 1
            continue

        if i == 0 or cleaned[i - 1].isspace():
            m = _PROSE_FLAG_PREFIX_RE.match(cleaned, i)
            if m:
                val_start = m.end()
                if val_start < n and cleaned[val_start] == "'":
                    # Consume single-quoted literal (and any adjacent '\'' continuations)
                    cur = val_start
                    while cur < n and cleaned[cur] == "'":
                        end_sq = cleaned.find("'", cur + 1)
                        if end_sq == -1:
                            cur = n
                            break
                        cur = end_sq + 1
                        if cur + 2 <= n and cleaned[cur : cur + 2] == "\\'":
                            cur += 2
                    out.append(cleaned[i:val_start] + '""')
                    i = cur
                    continue
                elif val_start < n and cleaned[val_start] == '"':
                    end_dq = _find_double_quote_end(cleaned, val_start)
                    if end_dq != -1:
                        dq_body = cleaned[val_start + 1 : end_dq]
                        if _is_safe_orca_spec_substitution(dq_body):
                            out.append(cleaned[i:val_start] + '""')
                            i = end_dq + 1
                            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _double_quoted_arg_has_backtick(cmd: str, start: int) -> bool:
    """cmd[start] is the opening double quote; scan to the matching closing quote honoring backslash escapes
    and ignoring backticks inside single-quoted literals ONLY when directly inside $(...) command substitutions."""
    end_dq = _find_double_quote_end(cmd, start)
    if end_dq == -1:
        return False
    body = cmd[start + 1 : end_dq]
    i = 0
    n = len(body)
    stack: list[str] = ["dq"]
    while i < n:
        ch = body[i]
        top = stack[-1]
        if ch == "\\":
            i += 2
            continue
        if top == "dq":
            if ch == "`":
                return True
            if ch == '"':
                stack.pop()
                if not stack:
                    break
            elif ch == "$" and i + 1 < n and body[i + 1] == "(":
                stack.append("cmd_sub")
                i += 2
                continue
        else:  # top == "cmd_sub"
            if ch == "'":
                end_sq = body.find("'", i + 1)
                if end_sq == -1:
                    break
                i = end_sq + 1
                continue
            if ch == "`":
                return True
            if ch == '"':
                stack.append("dq")
            elif ch == "$" and i + 1 < n and body[i + 1] == "(":
                stack.append("cmd_sub")
                i += 2
                continue
            elif ch == "(":
                stack.append("cmd_sub")
            elif ch == ")":
                stack.pop()
            elif ch == "#" and (i == 0 or body[i - 1] in " \t\n;(&|"):
                nl = body.find("\n", i + 1)
                if nl == -1:
                    break
                i = nl + 1
                continue
        i += 1
    return False


def _inline_script_backtick_risk(cmd: str) -> bool:
    """True only when the inline-script argument itself (-c/-e/--eval "...") contains an unescaped backtick,
    or an UNQUOTED heredoc fed to an interpreter contains one (the shell substitutes both)."""
    pre_sanitized = _strip_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    # 1. Check unquoted heredocs BEFORE blanking single quotes (shells expand `...` even inside '...' in <<EOF)
    for m in _UNQUOTED_HEREDOC_RE.finditer(pre_sanitized):
        delim = m.group("delim")
        body = pre_sanitized[m.end() :]
        end = re.search(rf"^[ \t]*{re.escape(delim)}[ \t]*$", body, re.M)
        body = body[: end.start()] if end else body
        if re.search(r"(?<!\\)`", body):
            return True
    # 2. Check single-quoted shell -c wrappers (e.g. `docker exec c sh -c 'python3 -c "x = `key`"'`)
    for m in re.finditer(
        r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|zsh|sh)(?:\s+--?[a-zA-Z0-9_=.-]+)*\s+-[a-zA-Z0-9]*c(?:\s+|=)?'([^']*)'",
        pre_sanitized,
    ):
        if _inline_script_backtick_risk(m.group(1)):
            return True

    sanitized = _blank_single_quotes(pre_sanitized)
    for m in _INTERP_INLINE_RE.finditer(sanitized):
        interp, flag = m.group("interp"), m.group("flag")
        if (interp in ("bash", "zsh", "sh") or interp.startswith("python")) and not flag.endswith("c"):
            continue  # -e = errexit cho shell, không phải inline script
        if _double_quoted_arg_has_backtick(sanitized, m.end() - 1):
            return True
    return False


def _strip_non_shell_quoted_heredocs(cmd: str) -> str:
    """Strip quoted heredoc bodies when the command before `<<` is not a shell or docker (e.g. `python3 - <<'PY'`)."""
    def _repl(m: re.Match[str]) -> str:
        prefix = m.group(1)
        last_cmd = re.split(r"[;&|]+", prefix)[-1]
        if re.search(r"(?:^|[\s(])(?:\S*/)?(?:sh|bash|zsh|dash|ksh|docker)\b", last_cmd):
            return m.group(0)
        return prefix + m.group(2) + "\n" + m.group(4)

    out = re.sub(
        r"([^\n]*?)(<<-?\s*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"][^\n]*)\n(?:.*?\n)?([ \t]*\3[ \t]*(?=\n|$))",
        _repl,
        cmd,
        flags=re.DOTALL,
    )
    out = re.sub(
        r"([^\n]*?)(<<-?\s*\\([A-Za-z_][A-Za-z0-9_-]*)[^\n]*)\n(?:.*?\n)?([ \t]*\3[ \t]*(?=\n|$))",
        _repl,
        out,
        flags=re.DOTALL,
    )
    return out


def _is_raw_binary_cat_risk(cmd: str) -> bool:
    """Detect unbounded `cat` of compiled container/system binaries while allowing text/script/config files."""
    sanitized = _strip_non_exec_prose_flags(cmd)
    for m in re.finditer(
        r"(?:check_output|subprocess\.(?:run|Popen))\([^)]*['\"]cat['\"]([^)\]]*)",
        sanitized,
    ):
        for path_m in re.finditer(r"['\"](/(?:app|usr|bin|sbin|opt)/[^'\"]+)['\"]", m.group(1)):
            if not _SAFE_TEXT_PATH_RE.search(path_m.group(1)):
                return True
    shell_sanitized = _strip_non_shell_quoted_heredocs(sanitized)
    for m in re.finditer(
        r"(?:^|[;&|()\n]|\b(?:sudo|env|time|nohup|command)\s+|-[a-zA-Z]*c\s+['\"])\s*"
        r"docker\s+exec\s+[^\n|>&;]+?\bcat(?:\s+--?[a-zA-Z0-9_-]*)*((?:\s+[^\s|>&;\"')]+)+)"
        r"(?:\s+2>(?:/dev/null|&1))*\s*(?:[\"')](?!\s*[,:])|\s*(?:;|&&|\|\|)|\s*$)",
        shell_sanitized,
        re.MULTILINE,
    ):
        for raw_arg in m.group(1).split():
            arg = raw_arg.strip("'\"")
            if re.match(r"^/(?:app|bin|sbin|opt|usr/bin|usr/sbin|usr/local/bin|usr/local/sbin)/", arg):
                if not _SAFE_TEXT_PATH_RE.search(arg):
                    return True
    return False


def _is_unbounded_find_loop_risk(cmd: str) -> bool:
    """Detect unbounded `while True:` / `while 1:` + `.find(` loops in inline Python scripts."""
    sanitized = _strip_non_shell_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    if not _WHILE_TRUE_RE.search(sanitized) or not re.search(
        r"\bpython3?(?:\.\d+)?(?:\s+--?[a-zA-Z0-9_=.-]+)*\s+-[a-zA-Z0-9]*c\b",
        sanitized,
    ):
        return False
    matches = list(_WHILE_TRUE_RE.finditer(sanitized))
    for idx, m in enumerate(matches):
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(sanitized)
        raw_segment = sanitized[m.end() : next_start]
        # Bound multiline while-loop body by indentation so trailing statements after the loop cannot exempt it
        line_start = sanitized.rfind("\n", 0, m.start()) + 1
        while_prefix = sanitized[line_start : m.start()]
        base_indent = len(while_prefix) - len(while_prefix.lstrip(" \t"))
        seg_lines = raw_segment.splitlines(keepends=True)
        bounded_lines: list[str] = []
        if seg_lines:
            bounded_lines.append(seg_lines[0])
            for ln in seg_lines[1:]:
                stripped_ln = ln.strip()
                if stripped_ln and not stripped_ln.startswith("#"):
                    indent = len(ln) - len(ln.lstrip(" \t"))
                    if indent <= base_indent and not while_prefix.strip():
                        break
                bounded_lines.append(ln)
        loop_body = "".join(bounded_lines)
        # Also stop at chained shell commands outside Python (`&&`, `||`)
        loop_body = re.split(r"\s+(?:&&|\|\|)\s+", loop_body, maxsplit=1)[0]
        if ".find(" not in loop_body:
            continue
        if re.search(r"\bfor\s+[^:\n]+\s+in\s+range\s*\(", loop_body):
            continue
        # Strip EOF-only sentinel breaks (`if idx == -1: break`, `if pos < 0: break`, `if idx >= len(data): break`)
        cleaned_loop = re.sub(
            r"if\s+[^:\n]+?(?:==\s*-1|<\s*0|is\s+None|>=\s*len\s*\([^)]*\))\s*:\s*break\b",
            "",
            loop_body,
        )
        if re.search(r"\b(?:break|return|exit)\b", cleaned_loop):
            continue
        return True
    return False


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

        if tail_buffered > 0 or cutoff_triggered:
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
            if tail_data:
                stdout_fd.write(tail_data)
                if not tail_data.endswith(b"\n"):
                    stdout_fd.write(b"\n")
            stdout_fd.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return


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
        agent_pids = _load_agent_pids(tasks_dir) | {
            pid for pid, (_, _, a_str) in proc_table.items() if _looks_like_agent(a_str)
        }
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
                    d_info = proc_table.get(d_pid)
                    if d_info and _looks_like_agent(d_info[2]):
                        continue
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
                    if ch_pid in agent_pids or _looks_like_agent(ch_args):
                        continue
                    # Exclude MCP servers that share process group with language_server and don't run a shell command
                    if ch_pgid == h_pgid and not re.search(r"\b(?:zsh|bash|sh|python3?|node|docker)\b", ch_low):
                        continue
                    tree_args = " ; ".join(
                        proc_table[dp][2]
                        for dp in _collect_descendants(ch_pid, children_map)
                        if dp in proc_table
                    )
                    candidate_children.append((ch_pid, ch_pgid, tree_args or ch_args))

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
                        d_info = proc_table.get(d_pid)
                        if d_info and _looks_like_agent(d_info[2]):
                            continue
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
    marker = b"\n\n[ORCA CIRCUIT-BREAKER:"
    try:
        for log_file in tasks_dir.glob("task-*.log"):
            try:
                sz = log_file.stat().st_size
            except Exception:
                continue
            if sz <= MAX_TASK_LOG_BYTES:
                continue

            # 1. Terminate ONLY the runaway child command writing to this log file first (never the Agent CLI)
            #    so the PTY buffer drains into log_file BEFORE we sample new_tail and truncate.
            killed = _kill_runaway_process_for_log(log_file, tasks_dir)
            if killed:
                time.sleep(0.25)
                try:
                    sz = log_file.stat().st_size
                except Exception:
                    pass

            # 2. Read head + any previously saved tail (when a sparse \x00 hole occurred) + latest non-NUL tail
            head_bytes = b""
            prev_tail = b""
            new_tail = b""
            had_sparse_hole = False
            try:
                with open(log_file, "rb") as f:
                    prefix_buf = f.read(HEAD_KEEP_BYTES + TAIL_KEEP_BYTES + 8192)
                    if marker in prefix_buf:
                        raw_head, after_marker = prefix_buf.split(marker, 1)
                        head_bytes = raw_head
                        if b"]\n\n" in after_marker:
                            after_notice = after_marker.split(b"]\n\n", 1)[1]
                            if b"\x00" in after_notice:
                                had_sparse_hole = True
                            prev_tail = after_notice.split(b"\x00", 1)[0][:TAIL_KEEP_BYTES]
                    else:
                        head_bytes = _trim_utf8_tail(prefix_buf[:HEAD_KEEP_BYTES])
                    f.seek(max(HEAD_KEEP_BYTES, sz - TAIL_KEEP_BYTES))
                    raw_new_tail = f.read(TAIL_KEEP_BYTES)
                    if b"\x00" in raw_new_tail:
                        had_sparse_hole = True
                    new_tail = raw_new_tail.replace(b"\x00", b"")
            except Exception:
                pass

            combined_tail = (prev_tail + new_tail) if (prev_tail and (had_sparse_hole or not new_tail)) else new_tail
            tail_bytes = _trim_utf8_head(combined_tail[-TAIL_KEEP_BYTES:]) if len(combined_tail) > TAIL_KEEP_BYTES else _trim_utf8_head(combined_tail)

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
    my_pid_str = str(os.getpid())
    try:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(my_pid_str, encoding="utf-8")
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
                    if lock_file.read_text(encoding="utf-8").strip() != my_pid_str:
                        break
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
            if lock_file.exists() and lock_file.read_text(encoding="utf-8").strip() == my_pid_str:
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
            meaningful_lines = [
                ln
                for ln in non_comment_lines
                if not re.match(r"^(?:set\s+-[a-zA-Z]+|cd\s+\S+|export\s+\w+=.*)$", ln)
            ] or non_comment_lines
            first_line_cmd = meaningful_lines[0][:160] if meaningful_lines else ""
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

        if oversized:
            _enforce_task_log_cap(tasks_dir)

        lock_file = tasks_dir / ".orca-watcher.pid"
        if lock_file.exists():
            try:
                old_pid_str = lock_file.read_text(encoding="utf-8").strip()
                lock_age = time.time() - lock_file.stat().st_mtime
                if old_pid_str.isdigit() and lock_age < 600:
                    old_pid = int(old_pid_str)
                    os.kill(old_pid, 0)
                    ps_chk = subprocess.run(
                        ["ps", "-p", str(old_pid), "-o", "args="],
                        capture_output=True,
                        text=True,
                        timeout=1,
                    )
                    if ps_chk.returncode == 0 and "--watch-dir" in ps_chk.stdout:
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

