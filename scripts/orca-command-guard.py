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
import stat
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
    "openclaude",
    "kimi",
    "gemini",
    "codex",
    "cursor",
    "opencode",
    "qwen",
    "codebuddy",
    "copilot",
    "droid",
    "hermes",
    "aider",
    "goose",
    "orca",
    "electron",
)
EXCLUDED_CHILD_MARKERS = (
    "orca-command-guard",
    "antigravity-hook",
    "claude-hook",
    "gemini-hook",
    "kimi-hook",
    "codex-hook",
    "cursor-hook",
    "agent-hooks/",
    "-hook.sh",
    "language_server",
    " mcp",
    "/mcp",
    "-mcp",
    "mcp-",
    "mcp_",
    "modelcontextprotocol",
)


AGENT_PIDS_FILE = ".orca-agent-pids.json"
INTERPRETERS = {"node", "python", "python3", "bun", "deno", "ruby", "perl"}
LEAF_AGENT_EXEC_NAMES = {
    "agy",
    "antigravity",
    "claude",
    "kimi",
    "kimi-code",
    "codex",
    "gemini",
    "opencode",
    "opencode2",
    "cursor-agent",
    "aider",
    "goose",
    "copilot",
    "codebuddy",
    "cbc",
    "muse",
    "omp",
}
AGENT_EXEC_NAMES = LEAF_AGENT_EXEC_NAMES | {
    "orca",
    "electron",
    "orca-team",
    "orca-monitor",
    "orca-project-init",
    "orca-init",
}


def _ancestor_pids() -> list[int]:
    """PID chain of this hook process up to init (innermost agent CLI / language_server first)."""
    pids: list[int] = []
    try:
        table = _get_process_table()
        cur = os.getppid()
        seen = set()
        transient = {"sh", "bash", "zsh", "dash", "env", "python", "python3", "timeout"}
        found_agent = False
        while cur > 1 and cur not in seen and len(seen) < 16:
            seen.add(cur)
            ppid, _, args_str = table.get(cur, (0, 0, ""))
            toks = args_str.split()
            exe = os.path.basename(toks[0]).lower().lstrip("-") if toks else ""
            is_agent = _looks_like_agent(args_str)
            if is_agent:
                found_agent = True
            is_transient = exe in transient or exe.startswith("python")
            # Chỉ bỏ qua wrapper ngắn hạn (sh -c của hook) nằm BÊN DƯỚI agent CLI;
            # các shell/tiến trình phía trên agent (như -zsh của Orca pane) là tổ tiên cố định cần bảo vệ.
            if found_agent or not is_transient or is_agent:
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
                loaded = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    old = [int(x) for x in loaded if str(x).isdigit()]
            except Exception:
                old = []
        merged = [p for p in dict.fromkeys(_ancestor_pids() + old) if _pid_alive(p)][:24]
        tmp_f = f.with_name(f"{f.name}.{os.getpid()}.tmp")
        tmp_f.write_text(json.dumps(merged), encoding="utf-8")
        os.replace(tmp_f, f)
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


def _load_agent_pids_ordered(tasks_dir: Path) -> list[int]:
    try:
        raw = json.loads((tasks_dir / AGENT_PIDS_FILE).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [int(x) for x in raw if str(x).isdigit() and _pid_alive(int(x))]
    except Exception:
        return []


def _load_agent_pids(tasks_dir: Path) -> set[int]:
    return set(_load_agent_pids_ordered(tasks_dir))


def _looks_like_leaf_agent(args_str: str) -> bool:
    """Return True ONLY for leaf worker CLI / language_server processes that directly spawn tool commands.
    Excludes GUI/PTY host daemons like `Orca Helper.app`, `Orca.app`, `Antigravity.app` (Electron), `orca-team`, etc."""
    toks = args_str.split()
    if not toks:
        return False
    exe = os.path.basename(toks[0]).lower()
    if exe.startswith("language_server"):
        return True
    low_all = args_str.lower()
    if ".app/contents/macos/" in low_all:
        return False
    if exe in INTERPRETERS or exe.startswith("python"):
        non_flag_args = [t for t in toks[1:5] if not t.startswith("-")]
        for raw_arg in non_flag_args[:2]:
            script_arg = raw_arg.lower().replace("\\", "/")
            script_base = os.path.basename(script_arg)
            script_stem = os.path.splitext(script_base)[0]
            if script_base in LEAF_AGENT_EXEC_NAMES or script_stem in LEAF_AGENT_EXEC_NAMES:
                return True
            if any(
                pkg in script_arg
                for pkg in (
                    "@anthropic-ai/claude-code",
                    "/claude-code/",
                    "/antigravity-cli/",
                    "/kimi-cli/",
                    "@openai/codex",
                    "@google/gemini-cli",
                    "/gemini-cli/",
                    "@github/copilot",
                    "/opencode",
                    "/cursor-agent",
                    "/codebuddy",
                    "/aider",
                )
            ):
                return True
        return False
    exe_stem = os.path.splitext(exe)[0]
    return exe in LEAF_AGENT_EXEC_NAMES or exe_stem in LEAF_AGENT_EXEC_NAMES


def _load_leaf_agent_pid(tasks_dir: Path, proc_table: dict[int, tuple[int, int, str]]) -> int | None:
    """Return the innermost alive leaf agent PID recorded for this conversation.
    Never matches parent shells (`-zsh`, `login`) or global GUI/PTY daemons (`Orca Helper.app`, `Orca.app`)."""
    try:
        raw = json.loads((tasks_dir / AGENT_PIDS_FILE).read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            return None
        for item in raw:
            if str(item).isdigit():
                pid = int(item)
                if pid in proc_table and _looks_like_leaf_agent(proc_table[pid][2]):
                    return pid
    except Exception:
        pass
    return None


def _looks_like_agent(args_str: str) -> bool:
    """Match agent markers on the executable name (or installed agent CLI paths/wrappers for node/python),
    never on arbitrary user script basenames like `python3 orca_test.py` or `node claude_test.js`."""
    toks = args_str.split()
    if not toks:
        return False
    exe = os.path.basename(toks[0]).lower()
    if exe in INTERPRETERS or exe.startswith("python"):
        non_flag_args = [t for t in toks[1:5] if not t.startswith("-")]
        for raw_arg in non_flag_args[:2]:
            script_arg = raw_arg.lower().replace("\\", "/")
            script_base = os.path.basename(script_arg)
            script_stem = os.path.splitext(script_base)[0]
            if script_base in AGENT_EXEC_NAMES or script_stem in AGENT_EXEC_NAMES:
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
                    "@google/gemini-cli",
                    "/gemini-cli/",
                    "@github/copilot",
                    "/opencode",
                    "/cursor-agent",
                    "/codebuddy",
                    "/aider",
                    "/orca.app/",
                )
            ):
                return True
        return False
    low_all = args_str.lower()
    if ".app/contents/macos/" in low_all:
        app_name = low_all.split(".app/contents/macos/", 1)[0].rsplit("/", 1)[-1]
        if any(m in app_name for m in ("orca", "antigravity", "electron", "claude", "cursor")):
            return True
    if exe.startswith("language_server"):
        return True
    return exe in AGENT_EXEC_NAMES


_INTERP_INLINE_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?P<interp>python3?(?:\.\d+)?|node|bun|deno|ruby|perl|bash|zsh|sh|dash|ksh)"
    r"(?P<opts>(?:\s+(?:(?:-(?:W|X|r|C|I)|--(?:input-type|require|import|loader|experimental-loader|conditions))\s+\S+|--?[a-zA-Z0-9_=./-]+))*)"
    r"\s+(?P<flag>-(?:[a-zA-Z0-9]*[ce]|p)|--(?:eval|print))(?:\s+|=)?\""
)
_UNQUOTED_HEREDOC_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"(?:\S*/)?(?:python3?(?:\.\d+)?|node|bun|deno|ruby|perl|bash|zsh|sh|dash|ksh)\b[^\n<]*<<-?\s*(?P<delim>[A-Za-z_][A-Za-z0-9_-]*)[^\n]*\n|"
    r"(?:\S*/)?(?:cat|tee)\b[^\n<|;&]*<<-?\s*(?P<delim2>[A-Za-z_][A-Za-z0-9_-]*)[^\n]*\|\s*(?:\S*/)?(?:python3?(?:\.\d+)?|node|bun|deno|ruby|perl|bash|zsh|sh|dash|ksh)\b[^\n]*\n"
    r")"
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
    r"(?:--(?:spec|body|content|question|objective|task-title|display-name|comment|resolution|result|subject|message|notes|title|prompt|summary|description|reason)|-[a-zA-Z]*m)(?:\s+|=)"
)
_WHILE_TRUE_RE = re.compile(r"\bwhile\s*(?:\(\s*)?(?:True|1)(?:\s*\))?\s*:")


def _find_single_quote_end(s: str, quote_idx: int) -> int:
    """Find the closing "'" for s[quote_idx] == "'".
    Standard single quotes ('...') have no backslash escapes; ANSI-C $'...' strings support \\ and \\'."""
    if quote_idx > 0 and s[quote_idx - 1] == "$":
        i = quote_idx + 1
        n = len(s)
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == "'":
                return i
            i += 1
        return -1
    return s.find("'", quote_idx + 1)


def _match_shell_case_open(s: str, i: int) -> int:
    """Return length of `case <word> in` if s[i:] opens a genuine shell `case` statement at command position
    with a matching `esac` later in `s`, else 0."""
    if i > 0 and (s[i - 1].isalnum() or s[i - 1] == "_"):
        return 0
    p = i - 1
    while p >= 0 and s[p] in " \t":
        p -= 1
    if p >= 0 and s[p] not in "\n;(&|{":
        if not re.search(r"(?:^|[\s;(&|{])(?:then|do|else|elif|if|while|until)$", s[: p + 1]):
            return 0
    m_case = re.match(r"case\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)\s+in(?=$|[\s;)&|])", s[i:])
    if not m_case:
        return 0
    if not re.search(r"(?:^|[\s;(&|{])esac(?=$|[\s;)&|\"'])", s[i + m_case.end() :]):
        return 0
    return m_case.end()


def _match_shell_case_close(s: str, i: int) -> bool:
    """Return True if s[i:] is a genuine shell `esac` token at command position."""
    if i > 0 and (s[i - 1].isalnum() or s[i - 1] == "_"):
        return False
    if not re.match(r"esac(?=$|[\s;)&|\"'])", s[i:]):
        return False
    p = i - 1
    while p >= 0 and s[p] in " \t":
        p -= 1
    if p >= 0 and s[p] not in "\n;(&|{":
        if not re.search(r"(?:^|[\s;(&|{])in$", s[: p + 1]):
            return False
    return True


def _find_double_quote_end(s: str, quote_idx: int) -> int:
    """Given s[quote_idx] == '\"', find the matching closing '\"' while respecting backslash escapes
    and arbitrarily nested $(...) command substitutions, case...esac blocks, and inner quotes in POSIX shells."""
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
        else:  # top in ("cmd_sub", "case")
            if ch == "'":
                end_sq = _find_single_quote_end(s, i)
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
                if top == "cmd_sub":
                    stack.pop()
            elif ch == "c":
                case_len = _match_shell_case_open(s, i)
                if case_len > 0:
                    stack.append("case")
                    i += case_len
                    continue
            elif top == "case" and ch == "e":
                if _match_shell_case_close(s, i):
                    stack.pop()
                    i += 4
                    continue
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
    """Blank out single-quoted literals (including $'...' ANSI-C strings) and top-level comments while respecting double quotes."""
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            out.append(cmd[i : i + 2])
            i += 2
            continue
        if ch == "#" and (i == 0 or cmd[i - 1] in " \t\n;(&|"):
            nl = cmd.find("\n", i + 1)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "'":
            end_sq = _find_single_quote_end(cmd, i)
            if end_sq == -1:
                out.append("'")
                out.append(" " * (n - i - 1))
                break
            out.append("'")
            out.append(" " * (end_sq - i - 1))
            out.append("'")
            i = end_sq + 1
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


def _is_safe_orca_spec_inner(inner: str) -> bool:
    """Verify the interior of $(orca-team spec|worktree-arg|merge-upstream|plan|kb|verify|gate|state|card|brief ...) has no unescaped backticks, nested $(), or command chaining."""
    if not re.match(
        r"^\s*(?:\S*/)?orca-team(?:\s+(?:--path|-C)\s+\S+|\s+--path=\S+)*\s+(?:spec|role-spec|prompt|worktree-arg|wt-arg|slot-arg|merge-upstream|merge-run|run-branches|release-workers|plan|pin-plan|master-plan|kb|knowledge|system-engineer|verify|gate|state|card|brief)\b",
        inner,
    ):
        return False
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end_sq = _find_single_quote_end(inner, i)
            if end_sq == -1:
                return False
            i = end_sq + 1
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(inner, i)
            if end_dq == -1:
                return False
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
        if ch in "`;&|\n()" or (ch == "$" and i + 1 < n and inner[i + 1] == "("):
            return False
        i += 1
    return True


def _is_safe_orca_spec_substitution(dq_body: str) -> bool:
    """Return True if dq_body has no unescaped shell expansion, or only contains safe `$(orca-team spec ...)`
    substitutions (optionally surrounded by static prefix/suffix text) without unescaped backticks."""
    if not dq_body:
        return True
    stripped = dq_body.strip()
    if not re.search(r"(?<!\\)(?:\\\\)*(?:`|\$\()", stripped):
        return True
    i = 0
    n = len(stripped)
    while i < n:
        ch = stripped[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return False
        if ch == "$" and i + 1 < n and stripped[i + 1] == "(":
            # Find matching ')' for this $(...) using _find_double_quote_end on a synthetic wrapper
            synth = '"' + stripped[i:] + '"'
            end_synth = _find_double_quote_end(synth, 0)
            if end_synth == -1:
                return False
            # Scan inside stripped[i+2:] to find where this specific $(...) closes
            k = i + 2
            depth = 1
            while k < n and depth > 0:
                ck = stripped[k]
                if ck == "\\":
                    k += 2
                    continue
                if ck == "'":
                    end_sq = _find_single_quote_end(stripped, k)
                    if end_sq == -1:
                        return False
                    k = end_sq + 1
                    continue
                if ck == '"':
                    end_dq = _find_double_quote_end(stripped, k)
                    if end_dq == -1:
                        return False
                    k = end_dq + 1
                    continue
                if ck == "(":
                    depth += 1
                elif ck == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if depth != 0:
                return False
            inner = stripped[i + 2 : k]
            if not _is_safe_orca_spec_inner(inner):
                return False
            i = k + 1
            continue
        i += 1
    return True


def _has_unquoted_comment(s: str) -> bool:
    """Return True if `s` contains an unquoted shell `#` comment token (preceded by start-of-line or shell delimiter)."""
    in_sq = False
    in_dq = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_sq:
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if in_dq:
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch == "#" and (i == 0 or s[i - 1] in " \t\n;(&|"):
            return True
        i += 1
    return False


def _strip_non_piped_cat_tee_heredocs(cmd: str) -> str:
    """Scan top-level shell lines in order:
    - Any heredoc (`python3 - << 'EOF'`, `cat << 'EOF'`, etc.) advances `i` past its closing delimiter line
      so interior lines of `python3 - << 'EOF'` are never mistaken for top-level `cat << 'EOF'` commands.
    - If a top-level command opening a QUOTED heredoc is `cat` or `tee` and is NOT piped (`|`) into another
      command, strip the heredoc body while preserving any chained command (`;`, `&&`, `||`) on the header line."""
    lines = cmd.splitlines(keepends=True)
    out_lines: list[str] = []
    i = 0
    n = len(lines)
    heredoc_open_re = re.compile(
        r"<<-?\s*(?:(['\"])([A-Za-z_][A-Za-z0-9_-]*)\1|\\([A-Za-z_][A-Za-z0-9_-]*)|([A-Za-z_][A-Za-z0-9_-]*))"
    )
    while i < n:
        line = lines[i]
        if line.lstrip().startswith("#"):
            out_lines.append(line)
            i += 1
            continue
        # Strip any unquoted # comment from line before checking for <<
        code_part = line
        m_hd = heredoc_open_re.search(code_part)
        if not m_hd:
            out_lines.append(line)
            i += 1
            continue
        # Ensure << is not inside a top-level # comment on the same line
        pre_hd = code_part[: m_hd.start()]
        if _has_unquoted_comment(pre_hd):
            out_lines.append(line)
            i += 1
            continue
        delim = m_hd.group(2) or m_hd.group(3) or m_hd.group(4)
        is_quoted_delim = bool(m_hd.group(2) or m_hd.group(3))
        end_idx = -1
        for j in range(i + 1, n):
            if lines[j].strip() == delim:
                end_idx = j
                break
        if end_idx == -1:
            out_lines.append(line)
            i += 1
            continue

        # Check if the command opening this quoted heredoc is cat or tee
        last_cmd_match = re.search(
            r"(?:^|[;&|()])\s*(?:(?:sudo|env|command)\s+)*(?:(?:/usr)?/bin/)?(?:cat|tee)\b[^;&|()]*$",
            pre_hd,
        )
        if is_quoted_delim and last_cmd_match:
            after_hd = code_part[m_hd.end() :]
            # Scan after_hd outside quotes to check for single pipe '|' vs chained ';', '&&', '||'
            k = 0
            na = len(after_hd)
            is_piped = False
            chain_pos = -1
            while k < na:
                ca = after_hd[k]
                if ca == "\\":
                    k += 2
                    continue
                if ca == "'":
                    esq = _find_single_quote_end(after_hd, k)
                    k = (esq + 1) if esq != -1 else na
                    continue
                if ca == '"':
                    edq = _find_double_quote_end(after_hd, k)
                    k = (edq + 1) if edq != -1 else na
                    continue
                if ca == "#" and (k == 0 or after_hd[k - 1] in " \t;(&|"):
                    break
                if ca == ";":
                    chain_pos = k
                    break
                if ca == "&" and k + 1 < na and after_hd[k + 1] == "&":
                    chain_pos = k
                    break
                if ca == "|":
                    if k + 1 < na and after_hd[k + 1] == "|":
                        chain_pos = k
                        break
                    is_piped = True
                    break
                k += 1

            if not is_piped:
                prefix_before_cat = pre_hd[: last_cmd_match.start()]
                sep_char = last_cmd_match.group(0)[0] if last_cmd_match.group(0) and last_cmd_match.group(0)[0] in ";&|(" else ""
                chained_suffix = after_hd[chain_pos:].rstrip("\r\n") if chain_pos != -1 else ""
                replacement_line = (prefix_before_cat + sep_char + " " + chained_suffix).strip()
                if replacement_line:
                    out_lines.append(replacement_line + "\n")
                i = end_idx + 1
                continue

        # Non-cat/tee heredoc (or piped cat/tee heredoc): keep entire heredoc block and advance past closing delim
        out_lines.extend(lines[i : end_idx + 1])
        i = end_idx + 1
    return "".join(out_lines)


def _strip_non_exec_prose_flags(cmd: str) -> str:
    """Strip non-executable prose flag values (--spec, --body, --content, --question, -m, etc.),
    direct `orca-team spec|plan ...` task literals, top-level shell comments, and non-piped cat/tee quoted heredocs."""
    cleaned = _strip_non_piped_cat_tee_heredocs(cmd)
    out: list[str] = []
    i = 0
    n = len(cleaned)
    orca_spec_direct_re = re.compile(
        r"(?:\S*/)?orca-team(?:\s+(?:--path|-C)\s+\S+|\s+--path=\S+)*\s+(?:spec|role-spec|prompt|plan|pin-plan|master-plan)"
        r"(?:\s+(?:--(?:goal|no-goal|json)|(?:--role|-r|--run|--run-id|--title|-t)(?:\s+\S+|=\S+)|--|[a-zA-Z0-9_-]+))*(?:\s+(?:--task|-t|--content|-c|--body))?\s+"
    )
    while i < n:
        ch = cleaned[i]
        if ch == "\\":
            out.append(cleaned[i : i + 2])
            i += 2
            continue
        if ch == "#" and (i == 0 or cleaned[i - 1] in " \t\n;(&|"):
            nl = cleaned.find("\n", i + 1)
            if nl == -1:
                break
            i = nl
            continue
        if ch == "'":
            end_sq = _find_single_quote_end(cleaned, i)
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

        if i == 0 or cleaned[i - 1].isspace() or cleaned[i - 1] in ";(&|":
            m = _PROSE_FLAG_PREFIX_RE.match(cleaned, i) or orca_spec_direct_re.match(cleaned, i)
            if m:
                val_start = m.end()
                if val_start < n and (
                    cleaned[val_start] == "'"
                    or (cleaned[val_start] == "$" and val_start + 1 < n and cleaned[val_start + 1] == "'")
                ):
                    # Consume single-quoted or $'...' literal (and any adjacent '\'' continuations)
                    cur = val_start
                    while cur < n:
                        if cleaned[cur] == "$" and cur + 1 < n and cleaned[cur + 1] == "'":
                            end_sq = _find_single_quote_end(cleaned, cur + 1)
                        elif cleaned[cur] == "'":
                            end_sq = _find_single_quote_end(cleaned, cur)
                        else:
                            break
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
        else:  # top in ("cmd_sub", "case")
            if ch == "'":
                end_sq = _find_single_quote_end(body, i)
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
                if top == "cmd_sub":
                    stack.pop()
            elif ch == "c":
                case_len = _match_shell_case_open(body, i)
                if case_len > 0:
                    stack.append("case")
                    i += case_len
                    continue
            elif top == "case" and ch == "e":
                if _match_shell_case_close(body, i):
                    stack.pop()
                    i += 4
                    continue
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
        delim = m.group("delim") or m.group("delim2")
        body = pre_sanitized[m.end() :]
        end = re.search(rf"^[ \t]*{re.escape(delim)}[ \t]*$", body, re.M)
        body = body[: end.start()] if end else body
        if re.search(r"(?<!\\)(?:\\\\)*`", body):
            return True
    # 2. Check single-quoted shell -c wrappers (including concatenated '\'' or '"'"' segments)
    for m in re.finditer(
        r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|zsh|sh|dash|ksh)(?:\s+--?[a-zA-Z0-9_=.-]+)*\s+-[a-zA-Z0-9]*c(?:\s+|=)?((?:'[^']*'(?:\\'|\"'\"')?)+)",
        pre_sanitized,
    ):
        raw_sq = m.group(1)
        unquoted_sq = re.sub(r"'\\''|'\"'\"'", "'", raw_sq)
        if len(unquoted_sq) >= 2 and unquoted_sq[0] == "'" and unquoted_sq[-1] == "'":
            unquoted_sq = unquoted_sq[1:-1]
        if _inline_script_backtick_risk(unquoted_sq):
            return True

    sanitized = _blank_single_quotes(pre_sanitized)
    for m in _INTERP_INLINE_RE.finditer(sanitized):
        interp, flag = m.group("interp"), m.group("flag")
        if (interp in ("bash", "zsh", "sh", "dash", "ksh") or interp.startswith("python")) and not flag.endswith("c"):
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
    # 1a. List-form Python subprocess calls (`check_output(['docker', 'exec', 'c', 'cat', '/app/bin'])`)
    for m in re.finditer(
        r"(?:check_output|subprocess\.(?:run|Popen))\([^)]*['\"](?:(?:/usr)?/bin/)?cat['\"]([^)\]]*)",
        sanitized,
    ):
        for path_m in re.finditer(r"['\"](/(?:app|usr|bin|sbin|opt)/[^'\"]+)['\"]", m.group(1)):
            if not _SAFE_TEXT_PATH_RE.search(path_m.group(1)):
                return True
    # 1b. String-form Python subprocess/popen calls (`check_output("docker exec c cat /app/bin", shell=True)`)
    for m in re.finditer(
        r"(?:check_output|getoutput|getstatusoutput|os\.popen|subprocess\.(?:run|Popen))\(\s*(['\"])(.*?)\1",
        sanitized,
        re.DOTALL,
    ):
        inner_cmd = m.group(2)
        for cat_m in re.finditer(
            r"\b(?:(?:/usr)?/bin/)?cat(?:\s+--?[a-zA-Z0-9_-]*)*\s+([^\n|>&;]+)",
            inner_cmd,
        ):
            for raw_arg in cat_m.group(1).split():
                arg = raw_arg.strip("'\"")
                if re.match(r"^/(?:app|bin|sbin|opt|usr/bin|usr/sbin|usr/local/bin|usr/local/sbin)/", arg):
                    if not _SAFE_TEXT_PATH_RE.search(arg):
                        return True
    # 2. Direct shell `docker exec ... cat /app/binary` (including `sh -c "cd /app; cat /app/binary"`)
    shell_sanitized = _strip_non_shell_quoted_heredocs(sanitized)
    for m in re.finditer(
        r"(?:^|[;&|()\n]|\b(?:sudo|env|time|nohup|command)\s+|-[a-zA-Z]*c\s+['\"])\s*"
        r"docker\s+exec\s+(?:[^\n|>&;]+?|(?:[^\n|>&;]+?\b(?:sh|bash|zsh|dash)\s+-[a-zA-Z]*c\s+['\"][^\n\"']*?[;&]\s*))(?<!-)\b['\"]?(?:(?:/usr)?/bin/)?cat['\"]?(?:\s+--?[a-zA-Z0-9_-]*)*((?:\s+(?:['\"][^'\n\"|>&;]+['\"]|[^\s|>&;\"')]+))+)"
        r"(?:\s+2>(?:/dev/null|&1))*\s*(?:[\"')](?!\s*[,:])|\s*(?:;|&&|\|\|)|\s*$)",
        shell_sanitized,
        re.MULTILINE,
    ):
        for raw_arg in re.findall(r"['\"][^'\n\"|>&;]+['\"]|[^\s|>&;\"')]+", m.group(1)):
            arg = raw_arg.strip("'\"")
            if re.match(r"^/(?:app|bin|sbin|opt|usr/bin|usr/sbin|usr/local/bin|usr/local/sbin)/", arg):
                if not _SAFE_TEXT_PATH_RE.search(arg):
                    return True
    return False


_PY_CMD_EXEC_RE = re.compile(
    r"(?:(?:^|[\s;&|()]|-[a-zA-Z0-9]*c\s+['\"])\s*(?:\S*/)?python3?(?:\.\d+)?(?:\s+(?:-(?:W|X)\s+\S+|--?[a-zA-Z0-9_=.-]+))*(?:\s+-[a-zA-Z0-9]*c\b|[^\n|;&]*<<)|\|\s*(?:\S*/)?python3?(?:\.\d+)?\b)"
)


def _is_unbounded_find_loop_risk(cmd: str) -> bool:
    """Detect unbounded `while True:` / `while 1:` + `.find(` loops in inline/heredoc/piped Python scripts."""
    sanitized = _strip_non_exec_prose_flags(cmd)
    if not _WHILE_TRUE_RE.search(sanitized) or not _PY_CMD_EXEC_RE.search(sanitized):
        return False
    matches = list(_WHILE_TRUE_RE.finditer(sanitized))
    for idx, m in enumerate(matches):
        # Verify this `while True:` belongs to a python inline/heredoc/piped invocation (not a grep/rg search pattern)
        prefix_to_m = sanitized[: m.start()]
        last_cmd_sep = max(
            prefix_to_m.rfind("&&"),
            prefix_to_m.rfind("||"),
            prefix_to_m.rfind(";"),
        )
        # If there is no python invocation in the current/enclosing command segment before `m`,
        # nor a `| python3` after `m` before the next `;`/`&&`/`||`, skip (e.g. `grep "while True:"`)
        after_to_sep = re.split(r"(?:\s+(?:&&|\|\|)\s+|['\"]\s*(?:;|&&|\|\|))", sanitized[m.start() :], maxsplit=1)[0]
        in_py_before = bool(_PY_CMD_EXEC_RE.search(prefix_to_m[max(0, last_cmd_sep) :])) or (
            "\n" in prefix_to_m and bool(re.search(r"(?:\bpython3?(?:\.\d+)?\b[^\n|;&]*<<|\b(?:cat|tee)\b[^\n|;&]*<<[^\n]*\|\s*(?:\S*/)?python3?(?:\.\d+)?\b)", prefix_to_m))
        )
        in_py_pipe_after = bool(re.search(r"\|\s*(?:\S*/)?python3?(?:\.\d+)?\b", after_to_sep))
        if not in_py_before and not in_py_pipe_after:
            continue
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
            first_line_code = re.sub(r"#.*", "", seg_lines[0]).strip()
            if not first_line_code:
                body_indent: int | None = None
                for ln in seg_lines[1:]:
                    stripped_ln = ln.strip()
                    if stripped_ln and not stripped_ln.startswith("#"):
                        indent = len(ln) - len(ln.lstrip(" \t"))
                        if body_indent is None:
                            if not while_prefix.strip() and indent <= base_indent:
                                break
                            body_indent = indent
                        elif indent < body_indent or (not while_prefix.strip() and indent <= base_indent):
                            break
                    bounded_lines.append(ln)
        loop_body = "".join(bounded_lines)
        # Also stop at chained shell commands outside Python (`&&`, `||`, or `"` / `'` followed by `;`, `&&`, `||`)
        loop_body = re.split(r"(?:\s+(?:&&|\|\|)\s+|['\"]\s*(?:;|&&|\|\|))", loop_body, maxsplit=1)[0]
        if ".find(" not in loop_body:
            continue
        if re.search(r"\bfor\s+[^:\n]+\s+in\s+range\s*\(", loop_body):
            continue
        # Strip EOF-only sentinel breaks (`if idx == -1: break`, `if pos < 0: break`, `if idx >= len(data): break`),
        # without stripping compound bounded breaks like `if count >= 50 or idx == -1: break`
        cleaned_loop = re.sub(
            r"if\s+(?:(?!(?:\bor\b|\band\b))[^:\n])+?(?:==\s*-1|<=\s*-1|<\s*0|-1\s*==|is\s+None|>=\s*len\s*\([^)]*\))\s*:(?:[ \t]*#[^\n]*)?\s*break\b",
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
                        split_at = _align_utf8_split(wrapped_tail, head_remaining)
                        if split_at <= 0:
                            split_at = head_remaining
                        emit = wrapped_tail[:split_at]
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


def _parse_ps_etime(etime_str: str) -> int:
    s = etime_str.strip()
    if not s:
        return -1
    days = 0
    if "-" in s:
        d_part, s = s.split("-", 1)
        if d_part.isdigit():
            days = int(d_part)
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    if len(parts) == 3:
        return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return days * 86400 + parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return days * 86400 + parts[0]
    return -1


def _get_process_table_with_etime() -> tuple[dict[int, tuple[int, int, str]], dict[int, int]]:
    """Return (mapping of pid -> (ppid, pgid, args_str), mapping of pid -> elapsed_seconds)."""
    table: dict[int, tuple[int, int, str]] = {}
    etimes: dict[int, int] = {}
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid=,etime=,args="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split(None, 4)
                if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
                    pid = int(parts[0])
                    ppid = int(parts[1])
                    pgid = int(parts[2])
                    etime_s = _parse_ps_etime(parts[3])
                    args_str = parts[4] if len(parts) > 4 else ""
                    table[pid] = (ppid, pgid, args_str)
                    etimes[pid] = etime_s
    except Exception:
        pass
    return table, etimes


def _get_process_table() -> dict[int, tuple[int, int, str]]:
    """Return mapping of pid -> (ppid, pgid, args_str)."""
    table, _ = _get_process_table_with_etime()
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


def _normalize_cmd_hint(raw_cmd: str) -> str:
    s = re.sub(r"\\\r?\n\s*", " ", raw_cmd).strip()
    prev = None
    while s and s != prev:
        prev = s
        s = re.sub(r"^[\s({]+", "", s)
        s = re.sub(
            r"^(?:(?:sudo|env|command|nohup|nice)\s+)*(?:\S*/)?(?:sh|bash|zsh|dash|ksh)\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*c\s+['\"]?",
            "",
            s,
        ).strip()
        s = re.sub(
            r"^(?:\s*(?:set\s+[+-][a-zA-Z0-9\s+-]+|(?:cd|pushd|popd)\s+[^;&|\n]+|export\s+[^;&|\n]+|unset\s+[^;&|\n]+|ulimit\s+[^;&|\n]+|source\s+[^;&|\n]+|\.\s+[^;&|\n]+)\s*(?:&&|;|$)\s*)+",
            "",
            s,
        ).strip()
        s = re.sub(
            r"^(?:\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\$\([^)]*\)|\S*)(?:\s+|(?:\s*;\s*)+|$))+",
            "",
            s,
        ).strip()
    s = re.sub(r"<<-?\s*['\"\\]?[A-Za-z0-9_.-]+['\"]?", "", s)
    s = re.sub(r"(?:^|(?<=[\s;&|()]))(?:\d*>&\d+|\d*>>?\s*\S+|&>>?\s*\S+)", "", s)
    s = re.sub(r"(?:^|(?<=[\s;&|()]))(?:\d*>>?|\d*<|&>>?)\s*$", "", s)
    s = re.sub(r"[\s)};]+$", "", s)
    return s.strip()


def _extract_hint_tokens(raw_hint: str) -> list[str]:
    norm = _normalize_cmd_hint(raw_hint)
    tokens: list[str] = []
    for raw_tok in re.split(r"\s+", norm):
        t = raw_tok.strip("'\"`;|&()")
        if not t or len(t) <= 2:
            continue
        if t in ("&&", "||", "|", ";", "-c", "-e", "--"):
            continue
        if "=" in t and not t.startswith("-"):
            continue
        if "$" in t or t.startswith("~") or "\\" in t:
            for sub in re.split(r"(?:^~|\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z0-9_@#?*!-]+|\\[\"'$`\\])", t):
                sub_clean = sub.strip("'\"`;|&()")
                if len(sub_clean) > 2 and sub_clean not in tokens:
                    tokens.append(sub_clean)
                    if len(tokens) >= 6:
                        break
        else:
            tokens.append(t)
        if len(tokens) >= 6:
            break
    return tokens


def _kill_runaway_process_for_log(
    log_file: Path,
    tasks_dir: Path,
    require_jest: bool = False,
) -> bool:
    """
    Find the process responsible for `log_file`:
    - If held directly by a non-agent child process, kill that process and its process group.
    - If held by or running under the Agent parent process (`agy` / `language_server`), locate the active tool-execution
      child shell (`ppid == agent_pid`) and terminate its process group and descendants without touching the Agent CLI.
    """
    killed_any = False
    try:
        holder_pids: list[int] = []
        try:
            lsof_res = subprocess.run(
                ["lsof", "-t", str(log_file)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if lsof_res.returncode == 0 and lsof_res.stdout.strip():
                holder_pids = [int(x.strip()) for x in lsof_res.stdout.splitlines() if x.strip().isdigit()]
        except Exception:
            pass

        proc_table, etime_map = _get_process_table_with_etime()
        if not proc_table:
            return False

        # Fallback khi `agy`/`language_server` không giữ mở FD của `task-XX.log` liên tục (chỉ open->write->close khi có output):
        # chỉ lấy PID của LEAF agent (`raw[0]`) thuộc conversation này từ `.orca-agent-pids.json` hoặc `lsof` trên `.system_generated`.
        if not holder_pids:
            leaf_pid = _load_leaf_agent_pid(tasks_dir, proc_table)
            if leaf_pid:
                holder_pids.append(leaf_pid)
            elif tasks_dir.parent.name == ".system_generated":
                try:
                    sg_res = subprocess.run(
                        ["lsof", "-t", str(tasks_dir.parent)],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if sg_res.returncode == 0 and sg_res.stdout.strip():
                        for x in sg_res.stdout.splitlines():
                            if x.strip().isdigit():
                                p_cand = int(x.strip())
                                if p_cand in proc_table and _looks_like_leaf_agent(proc_table[p_cand][2]):
                                    holder_pids.append(p_cand)
                except Exception:
                    pass

        if not holder_pids:
            return False

        children_map: dict[int, list[int]] = {}
        for pid, (ppid, _, _) in proc_table.items():
            children_map.setdefault(ppid, []).append(pid)

        my_pid = os.getpid()
        my_pgid = os.getpgrp()
        agent_pids = _load_agent_pids(tasks_dir) | {
            pid for pid, (_, _, a_str) in proc_table.items() if _looks_like_agent(a_str)
        }
        agent_pgids = {proc_table[p][1] for p in agent_pids if p in proc_table}

        log_idle_s = 0.0
        try:
            log_idle_s = max(0.0, time.time() - log_file.stat().st_mtime)
        except Exception:
            pass

        # Check if we recorded command snippets for this task log during PreToolUse
        cmd_hints: list[str] = []
        cmd_map_file = tasks_dir / ".orca-task-cmds.json"
        if cmd_map_file.exists():
            try:
                cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                raw_entry = cmap.get(log_file.name) if isinstance(cmap, dict) else None
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
                if require_jest and not re.search(r"\b(?:jest|vitest)\b", h_args_low):
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
                    desc_pids = _collect_descendants(ch_pid, children_map)
                    tree_args = " ; ".join(
                        proc_table[dp][2]
                        for dp in desc_pids
                        if dp in proc_table
                    )
                    full_args = tree_args or ch_args
                    full_low = full_args.lower()
                    if any(ex in full_low for ex in EXCLUDED_CHILD_MARKERS):
                        continue
                    # Chống giết nhầm tiến trình mới vừa khởi chạy cho một file log cũ đã ngừng ghi từ trước:
                    max_etime = max([etime_map.get(dp, -1) for dp in desc_pids] + [etime_map.get(ch_pid, -1)])
                    if log_idle_s >= 2.0 and max_etime >= 0 and (max_etime + 1.5) < log_idle_s:
                        continue
                    if require_jest and not re.search(r"\b(?:jest|vitest)\b", full_low):
                        continue
                    candidate_children.append((ch_pid, ch_pgid, full_args))

                # Chỉ kill khi xác định ĐƠN NHẤT được tiến trình chạy lệnh của log này.
                # Mơ hồ (0 hoặc >1 ứng viên) ➔ KHÔNG kill gì (chỉ cắt log) để không giết nhầm dev server / MCP server / lệnh song song.
                if cmd_hints:
                    matched: list[tuple[int, int, str]] = []
                    has_any_tokens = False
                    for hint in reversed(cmd_hints):
                        sub_hints = [hint] + [
                            seg.strip()
                            for seg in reversed(re.split(r"(?:\s*&&\s*|\s*\|\|\s*|\s*;\s*)", hint))
                            if seg.strip() and seg.strip() != hint
                        ]
                        for shint in sub_hints:
                            norm_hint = _normalize_cmd_hint(shint)
                            exact = [
                                c
                                for c in candidate_children
                                if (shint and shint in c[2]) or (norm_hint and norm_hint in c[2])
                            ]
                            if len(exact) == 1:
                                matched = exact
                                break
                            hint_tokens = _extract_hint_tokens(shint)
                            if hint_tokens:
                                has_any_tokens = True
                            tok_match = [
                                c
                                for c in candidate_children
                                if (shint and shint in c[2])
                                or (norm_hint and norm_hint in c[2])
                                or (hint_tokens and all(t in c[2] for t in hint_tokens))
                            ]
                            if len(tok_match) == 1:
                                matched = tok_match
                                break
                        if matched:
                            break
                    if not matched and len(candidate_children) == 1:
                        # Chỉ fallback khi require_jest=True (đã xác nhận trình chạy là jest/vitest và etime khớp log_idle_s)
                        # hoặc khi cmd_hints không trích xuất được token nào; nếu cmd_hints có token mà không khớp bất kỳ
                        # tiến trình con nào thì tiến trình gốc đã thoát và ứng viên hiện tại thuộc lệnh mới khác.
                        if require_jest or (not has_any_tokens and candidate_children[0][1] != h_pgid):
                            matched = candidate_children
                    candidate_children = matched
                elif len(candidate_children) != 1:
                    candidate_children = []

                for ch_pid, ch_pgid, ch_full_args in candidate_children:
                    # Protect legitimate Docker BuildKit / package-manager builds from being killed if their PTY progress redraws exceed 16 MB
                    if not require_jest and re.search(
                        r"\b(?:docker\s+compose\b[^\n;|&]*\b(?:build|up|pull)|docker-compose\b[^\n;|&]*\b(?:build|up|pull)|docker\s+(?:buildx\s+)?build\b|buildkit|composer\s+install|npm\s+(?:ci|install)|pnpm\s+install|cargo\s+build)\b",
                        ch_full_args.lower(),
                    ):
                        continue
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


def _compact_buildkit_redraw_log_if_needed(log_file: Path, sz: int) -> bool:
    """
    When Docker BuildKit or `docker compose up` runs inside a PTY without plain progress, it redraws its TUI
    10 times per second, inflating `task-XX.log` to 1 MB–15 MB of duplicate `[+] Building` / `[+] Running` frames.
    Once the command is idle (`>= 3.0s`), compact duplicate step/container timer lines so `task-XX.log`
    drops from ~8 MB to ~40 KB, preventing `<SYSTEM_MESSAGE>` truncation and `bloated_logs` false alarms.
    """
    if sz < 256 * 1024:
        return False
    compact_marker = b"[ORCA BUILDKIT-COMPACTED:"
    try:
        with open(log_file, "rb") as bf:
            head_sample = bf.read(16384)
            if compact_marker in head_sample or b"[ORCA CIRCUIT-BREAKER:" in head_sample:
                return False
            if (
                b"[+] Building " not in head_sample
                and b"[+] build " not in head_sample
                and b"[+] Running " not in head_sample
                and b"=> [" not in head_sample
                and b"Container " not in head_sample
            ):
                return False
            bf.seek(0)
            raw_data = bf.read()
        lines = raw_data.splitlines(keepends=True)
        if len(lines) < 400:
            return False
        dedup: list[bytes] = []
        seen_keys: dict[bytes, int] = {}
        for ln in lines:
            clean = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", ln).strip()
            if not clean:
                continue
            if re.match(rb"^\[\+\]\s+(?:Building|build|Running)\s+\d", clean):
                continue
            if b"Image " in clean and b"Building" in clean and b"Built" not in clean:
                continue
            if b"Container " in clean and any(st in clean for st in (b"Waiting", b"Starting", b"Creating", b"Recreating")):
                continue
            m = re.match(rb"^(=>\s+(?:=>\s+)?\[[^\]]+\]\s+.+?)\s+\d+\.\d+s$", clean)
            if not m:
                m = re.match(rb"^(.+?\bContainer\s+\S+\s+\S+)\s+\d+\.\d+s$", clean)
            if m:
                key = m.group(1)
                if key in seen_keys:
                    dedup[seen_keys[key]] = ln
                    continue
                seen_keys[key] = len(dedup)
            dedup.append(ln)
        kept = dedup[-500:] if len(dedup) > 500 else dedup
        header = (
            f"[ORCA BUILDKIT-COMPACTED: Deduped {len(lines)} PTY Docker redraw lines ({sz / (1024 * 1024):.1f} MB) "
            f"down to {len(kept)} unique step lines]\n"
        ).encode("utf-8")
        compacted_bytes = header + b"".join(kept)
        if len(compacted_bytes) < sz * 0.6:
            with open(log_file, "wb") as wf:
                wf.write(compacted_bytes)
            return True
    except Exception:
        pass
    return False


def _enforce_task_log_cap(tasks_dir: Path) -> int:
    if not tasks_dir.is_dir():
        return 0
    truncated_count = 0
    marker = b"\n\n[ORCA CIRCUIT-BREAKER:"
    jest_hang_marker = b"Jest did not exit one second after the test run has completed"
    jest_exit_notice = b"[ORCA AUTO-EXIT:"
    now_ts = time.time()
    try:
        for log_file in tasks_dir.glob("task-*.log"):
            try:
                st = log_file.stat()
                sz = st.st_size
                mtime = st.st_mtime
            except Exception:
                continue
            if sz <= MAX_TASK_LOG_BYTES:
                idle_age = now_ts - mtime
                if sz >= 256 * 1024 and idle_age >= 3.0:
                    if _compact_buildkit_redraw_log_if_needed(log_file, sz):
                        truncated_count += 1
                        continue
                if sz >= 120 and 3.0 <= idle_age <= 1800.0:
                    try:
                        with open(log_file, "rb") as jf:
                            if sz > 8192:
                                jf.seek(sz - 8192)
                            tail_j = jf.read(8192)
                        if jest_exit_notice not in tail_j:
                            tail_clean = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", tail_j)
                            if jest_hang_marker in tail_clean:
                                if _kill_runaway_process_for_log(log_file, tasks_dir, require_jest=True):
                                    with open(log_file, "ab") as jf_out:
                                        jf_out.write(
                                            b"\n[ORCA AUTO-EXIT: Terminated hung Jest process after all tests completed (open handles). Tip: pass --forceExit to jest.]\n"
                                        )
                                    truncated_count += 1
                                else:
                                    # Đánh dấu ngay sau lần kiểm tra ở >=3.0s để không tạo cửa sổ mù 3.0s..5.0s giết nhầm lần chạy Jest kế tiếp
                                    with open(log_file, "ab") as jf_out:
                                        jf_out.write(
                                            b"\n[ORCA AUTO-EXIT: Jest open-handle warning noted (process already exited). Tip: pass --forceExit to jest.]\n"
                                        )
                    except Exception:
                        pass
                continue

            # 1. Terminate ONLY the runaway child command writing to this log file first (never the Agent CLI)
            #    so the PTY buffer drains into log_file BEFORE we sample new_tail and truncate.
            already_truncated_idle = False
            if now_ts - mtime >= 2.0:
                try:
                    with open(log_file, "rb") as chk_f:
                        if marker in chk_f.read(HEAD_KEEP_BYTES + 1024):
                            already_truncated_idle = True
                except Exception:
                    pass
            killed = False if already_truncated_idle else _kill_runaway_process_for_log(log_file, tasks_dir)
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

            combined_tail = (prev_tail + new_tail) if (prev_tail and (had_sparse_hole or len(new_tail) < TAIL_KEEP_BYTES)) else new_tail
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


def _has_active_tool_children(tasks_dir: Path) -> bool:
    """Return True if the conversation's leaf agent process still has an active non-MCP tool child running."""
    try:
        proc_table = _get_process_table()
        if not proc_table:
            return False
        leaf_agent = _load_leaf_agent_pid(tasks_dir, proc_table)
        if not leaf_agent:
            return False
        _, h_pgid, _ = proc_table[leaf_agent]
        my_pid = os.getpid()
        for pid, (ppid, pgid, args_str) in proc_table.items():
            if ppid == leaf_agent and pid != my_pid:
                low = args_str.lower()
                if any(ex in low for ex in EXCLUDED_CHILD_MARKERS):
                    continue
                if _looks_like_agent(args_str):
                    continue
                if pgid != h_pgid or re.search(r"\b(?:zsh|bash|sh|python3?|node|docker|mvn|gradle|cargo|go|jest|vitest|pytest)\b", low):
                    return True
    except Exception:
        pass
    return False


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
                    if now - lf.stat().st_mtime < 120.0:
                        recent_log_activity = True
                        break
            except Exception:
                pass
            if (now - lock_mtime > 45.0) and not recent_log_activity and not _has_active_tool_children(tasks_dir):
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
                    loaded_cmap = json.loads(cmd_map_file.read_text(encoding="utf-8"))
                    if isinstance(loaded_cmap, dict):
                        cmap = loaded_cmap
                except Exception:
                    cmap = {}
            if len(cmap) > 200:
                cmap = dict(list(cmap.items())[-120:])
            stripped_cmd = _strip_quoted_heredocs(_strip_non_piped_cat_tee_heredocs(cmd))
            joined_cmd = re.sub(r"\\\r?\n\s*", " ", stripped_cmd)
            joined_cmd = re.sub(r"(-[a-zA-Z]*c\s+['\"])\s*\r?\n\s*", r"\1", joined_cmd)
            non_comment_lines: list[str] = []
            for raw_ln in joined_cmd.splitlines():
                s_ln = raw_ln.strip()
                if not s_ln or s_ln.startswith("#"):
                    continue
                norm_ln = _normalize_cmd_hint(s_ln)
                if norm_ln and not norm_ln.startswith("#"):
                    non_comment_lines.append(norm_ln)
            hint_candidates: list[str] = []
            if non_comment_lines:
                hint_candidates.append(non_comment_lines[0][:160])
                if len(non_comment_lines) > 1 and non_comment_lines[-1][:160] not in hint_candidates:
                    hint_candidates.append(non_comment_lines[-1][:160])
            if str(step_idx).isdigit() and hint_candidates:
                base_idx = int(step_idx)
                for key in (f"task-{base_idx}.log", f"task-{base_idx + 1}.log", f"task-{base_idx + 2}.log"):
                    log_p = tasks_dir / key
                    try:
                        if event == "PreToolUse" and log_p.exists() and log_p.stat().st_size > 0:
                            continue
                    except Exception:
                        pass
                    prev = cmap.get(key)
                    cur_list = list(prev) if isinstance(prev, list) else ([prev] if isinstance(prev, str) and prev else [])
                    for hc in hint_candidates:
                        if hc and hc not in cur_list:
                            cur_list.append(hc)
                    cmap[key] = cur_list[-4:]
            tmp_cmap = cmd_map_file.with_name(f"{cmd_map_file.name}.{os.getpid()}.tmp")
            tmp_cmap.write_text(json.dumps(cmap), encoding="utf-8")
            os.replace(tmp_cmap, cmd_map_file)

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


def _resolve_orca_team_bin_and_cwd(payload: dict, args: dict) -> tuple[str, str]:
    cwd = str(
        args.get("Cwd")
        or args.get("cwd")
        or (payload.get("cwd") if isinstance(payload, dict) else "")
        or os.getcwd()
    ).strip()
    if not cwd or not os.path.isdir(cwd):
        cwd = os.getcwd()
    candidates = [
        os.path.expanduser("~/.local/bin/orca-team"),
        str(Path(__file__).resolve().parent / "orca-team"),
        "/usr/local/bin/orca-team",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c, cwd
    return "", cwd


_WORKER_SLOT_SUFFIX_RE = re.compile(
    r"(?:^|[.])((?:dev|devops|qa|sec|ui|bench|re|critic|doc|fix|pr|issue)-worker(?:-\d+)?|hotfix-issue-\d+(?:-\d+)?|review-pr-\d+(?:-\d+)?)$",
    re.IGNORECASE,
)


def _extract_canonical_worker_slot(raw_str: str) -> str:
    if not raw_str:
        return ""
    s = raw_str.strip().strip("\"'")
    if s.startswith("path:"):
        s = s[5:].strip()
    # Strip any trailing $(...) or `...` shell expansion attached to --name dev-worker$([ ... ])
    s = re.sub(r"(?:\$\(|`).*$", "", s).strip()
    if not s or "$" in s:
        return ""
    base = os.path.basename(s.rstrip("/"))
    m = _WORKER_SLOT_SUFFIX_RE.search(base)
    return m.group(1).lower() if m else ""


def _role_prefix_from_slot(slot_name: str) -> str:
    if slot_name.startswith("hotfix-issue-"):
        return re.sub(r"-(\d+)-\d+$", r"-\1", slot_name)
    if slot_name.startswith("review-pr-"):
        return re.sub(r"-(\d+)-\d+$", r"-\1", slot_name)
    if "-worker" in slot_name:
        return slot_name.split("-worker", 1)[0]
    return "dev"


def _split_top_level_shell_segments(s: str) -> list[str]:
    """Split shell string on top-level unquoted ';', '&', '|', or newline while keeping $(...) and quotes intact."""
    segs: list[str] = []
    start = 0
    i = 0
    n = len(s)
    paren_depth = 0
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end_sq = _find_single_quote_end(s, i)
            i = (end_sq + 1) if end_sq != -1 else n
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(s, i)
            i = (end_dq + 1) if end_dq != -1 else n
            continue
        if ch == "#" and (i == 0 or s[i - 1] in " \t\n;(&|"):
            nl = s.find("\n", i + 1)
            i = nl if nl != -1 else n
            continue
        if ch == "(":
            paren_depth += 1
            i += 1
            continue
        if ch == ")":
            if paren_depth > 0:
                paren_depth -= 1
            i += 1
            continue
        if paren_depth == 0 and ch in ";|&\n":
            seg = s[start:i].strip()
            if seg:
                segs.append(seg)
            while i < n and s[i] in ";|&\n":
                i += 1
            start = i
            continue
        i += 1
    tail = s[start:].strip()
    if tail:
        segs.append(tail)
    return segs


_ROLE_TOKEN_MAP = {
    "dev": "dev",
    "developer": "dev",
    "fullstack": "dev",
    "backend": "dev",
    "devops": "devops",
    "devopsspecialist": "devops",
    "sre": "devops",
    "infra": "devops",
    "sec": "sec",
    "security": "sec",
    "appsec": "sec",
    "secops": "sec",
    "qa": "qa",
    "tester": "qa",
    "test": "qa",
    "bench": "bench",
    "benchspecialist": "bench",
    "benchmark": "bench",
    "perf": "bench",
    "ui": "ui",
    "uispecialist": "ui",
    "frontend": "ui",
    "ux": "ui",
    "re": "re",
    "respecialist": "re",
    "reverse": "re",
    "critic": "critic",
    "criticspecialist": "critic",
    "architect": "critic",
    "doc": "doc",
    "docs": "doc",
    "fix": "fix",
    "hotfix": "fix",
    "issue": "fix",
    "pr": "pr",
}


def _normalize_role_token(raw_role: str) -> str:
    s = (raw_role or "").strip().strip("\"'").lower()
    if not s:
        return ""
    s = re.sub(r"-worker(?:-\d+)?$", "", s)
    return _ROLE_TOKEN_MAP.get(s, s)


def _extract_role_from_worktree_arg_subcmd(arg_tail: str) -> str:
    tokens = re.findall(r"(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)", arg_tail or "")
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx].strip("\"'")
        if tok in ("--path", "-C", "--check-slot", "--run", "--run-id", "--task", "--task-id", "-t"):
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return _normalize_role_token(tok)
    return ""


def _is_wildcard_dev_branch_merge_risk(cmd: str) -> bool:
    """
    Block legacy wildcard `for _br in $(git for-each-ref ... 'refs/heads/dev-worker*')` loops
    coupled with `git merge` so concurrent Leaders/Workers never merge branches from other Runs.
    """
    if "for-each-ref" not in cmd or "merge" not in cmd:
        return False
    sanitized = _strip_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    if "for-each-ref" not in sanitized or "merge" not in sanitized:
        return False
    return bool(
        re.search(r"\bgit\s+for-each-ref\b[^\n]*refs/heads/(?:dev|ui|devops|fix)-worker\*", sanitized)
        and re.search(r"\bgit\s+merge\b", sanitized)
    )


def _check_hardcoded_worker_start_collision(cmd: str, payload: dict, args: dict) -> str:
    """
    When a Leader runs `orca orchestration worker-start`:
    1. Enforce Wave-Order Gate:
       - Block simultaneous Wave 1 (`dev`/`ui`/`fix`) + Wave 2 (`devops`/`sec`/`qa`/`bench`) in the same command.
       - Block simultaneous `devops` + `qa`/`bench` in the same command.
       - When starting `devops`/`sec`/`qa`/`bench` with `--task <TASK_ID>` (or `--run <RUN_ID>`), verify via
         `orca-team merge-upstream --check-only` that no upstream worker in the same Run is still working,
         unstarted (`ready`), or failed.
    2. Enforce Run-Scoped Elastic Worktree Guard:
       - Immediately block deprecated `$(git worktree list | sed ...)` one-liners.
       - Block duplicate hardcoded slots in the same compound command.
       - Block `--worktree new-child --name <slot>` if `<slot>` already exists on disk.
       - Block if a hardcoded target worker slot is BUSY (`working`/`live`, reserved, or `RETAINED` by another Run).
    """
    if "worker-start" not in cmd:
        return ""
    sanitized = _strip_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    if "worker-start" not in sanitized or not re.search(r"\borca\s+orchestration\s+worker-start\b", sanitized):
        return ""

    orca_team_bin, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
    segments = _split_top_level_shell_segments(sanitized)

    var_task_map: dict[str, str] = {}
    for m_var in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)=['\"]?(task_[A-Za-z0-9_-]{6,64})['\"]?", sanitized):
        var_task_map[m_var.group(1)] = m_var.group(2)

    m_global_run = re.search(r"\b(run_[A-Za-z0-9_-]{6,64})\b", sanitized)
    global_run_id = m_global_run.group(1) if m_global_run else ""

    candidates: list[tuple[str, bool, str, str]] = []
    started_roles_in_cmd: list[tuple[str, str, str]] = []
    seen_slots_in_cmd: set[str] = set()

    for seg in segments:
        if not re.search(
            r"(?:^|\b(?:env|command|sudo|nohup|do|then|else|elif)\s+|-[a-zA-Z0-9]*c\s+['\"])\s*(?:\S*/)?orca\s+orchestration\s+worker-start\b",
            seg,
        ):
            continue

        # Extract --task <TASK_ID> and --run <RUN_ID> if present in segment
        seg_task_id = ""
        m_task = re.search(r"--task(?:\s+|=)(?:\"([^\"]+)\"|'([^']+)'|([^\s;|&\"']+))", seg)
        if m_task:
            raw_tid = (m_task.group(1) or m_task.group(2) or m_task.group(3) or "").strip()
            if raw_tid.startswith("$"):
                v_name = raw_tid.lstrip("$").strip("{}")
                raw_tid = var_task_map.get(v_name, "")
            if re.match(r"^task_[A-Za-z0-9_-]{6,64}$", raw_tid):
                seg_task_id = raw_tid

        m_seg_run = re.search(r"\b(run_[A-Za-z0-9_-]{6,64})\b", seg)
        seg_run_id = m_seg_run.group(1) if m_seg_run else global_run_id

        # 1. If this segment uses `orca-team worktree-arg`, record its role for Wave-Order checking, then skip slot collision check
        m_wt_sub = re.search(r"\borca-team\b[^\n;|&]*\b(?:worktree-arg|wt-arg|slot-arg)\b([^\n;|&)]*)", seg)
        if m_wt_sub:
            sub_role = _extract_role_from_worktree_arg_subcmd(m_wt_sub.group(1))
            if sub_role:
                started_roles_in_cmd.append((sub_role, seg_task_id, seg_run_id))
            continue

        # 2. Block deprecated `$(git worktree list | sed ...)` pattern immediately
        if re.search(r"\bgit\s+worktree\s+list\b", seg):
            m_legacy = re.search(
                r"((?:dev|devops|qa|sec|ui|bench|re|critic|doc|fix|pr|issue)-worker(?:-\d+)?|hotfix-issue-\d+(?:-\d+)?|review-pr-\d+(?:-\d+)?)",
                seg,
                re.IGNORECASE,
            )
            legacy_slot = m_legacy.group(1).lower() if m_legacy else "dev-worker"
            role_prefix = _role_prefix_from_slot(legacy_slot)
            next_slot = f"{role_prefix}-worker-2" if "-worker" in legacy_slot else f"{legacy_slot}-2"
            task_flag_hint = f" --task {seg_task_id}" if seg_task_id else " --task <TASK_ID>"
            next_args = f"$(orca-team worktree-arg {role_prefix}{task_flag_hint})"
            if orca_team_bin:
                try:
                    cmd_peek = [sys.executable, orca_team_bin, "--path", cwd, "worktree-arg", role_prefix, "--no-reserve", "--json"]
                    if seg_task_id:
                        cmd_peek.extend(["--task", seg_task_id])
                    if seg_run_id:
                        cmd_peek.extend(["--run", seg_run_id])
                    proc = subprocess.run(
                        cmd_peek,
                        capture_output=True,
                        text=True,
                        timeout=2.5,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        info = json.loads(proc.stdout)
                        if isinstance(info, dict):
                            next_slot = info.get("slot") or next_slot
                            next_args = info.get("argString") or next_args
                except Exception:
                    pass
            return (
                f"[ORCA ELASTIC WORKTREE GUARD] Cú pháp cũ '$(git worktree list | sed ...)' cho '{legacy_slot}' đã bị CẤM "
                f"vì không kiểm tra được worktree đang bận hoặc đang giữ bởi Run khác (gây ghi đè worktree)! "
                f"Cách sửa bắt buộc: Thay toàn bộ cụm '$(git worktree list ...)' bằng $(orca-team worktree-arg {role_prefix}{task_flag_hint}) "
                f"(slot rảnh hiện tại: '{next_slot}' -> {next_args})."
            )

        # 3. Extract hardcoded --worktree and --name in this worker-start segment
        wt_tokens: list[str] = []
        for m_wt in re.finditer(
            r"--worktree(?:\s+|=)(?:\"([^\"]+)\"|'([^']+)'|([^\s;|&\"']+))",
            seg,
        ):
            val = (m_wt.group(1) or m_wt.group(2) or m_wt.group(3) or "").strip()
            if val:
                wt_tokens.append(val)

        nm_tokens: list[str] = []
        for m_nm in re.finditer(
            r"--name(?:\s+|=)(?:\"([^\"]+)\"|'([^']+)'|([^\s;|&\"']+))",
            seg,
        ):
            val = (m_nm.group(1) or m_nm.group(2) or m_nm.group(3) or "").strip()
            if val:
                nm_tokens.append(val)

        is_new_child = any(w in ("new-child", "new", "new-top-level") for w in wt_tokens)
        seg_slot = ""
        for w in wt_tokens:
            if w in ("current", "parent"):
                task_flag_hint = f" --task {seg_task_id}" if seg_task_id else " --task <TASK_ID>"
                return (
                    f"[ORCA ELASTIC WORKTREE GUARD] CẤM dùng '--worktree {w}' khi gọi 'orca orchestration worker-start' "
                    f"(sẽ chạy Worker trực tiếp trên nhánh gốc main của Leader, gây bẩn git status và xung đột nhánh)! "
                    f"Cách sửa bắt buộc: Thay '--worktree {w}' bằng $(orca-team worktree-arg <role>{task_flag_hint})."
                )
            if w in ("new-child", "new", "new-top-level"):
                continue
            cand = _extract_canonical_worker_slot(w)
            if cand:
                seg_slot = cand
                break
        if not seg_slot:
            for n_val in nm_tokens:
                cand = _extract_canonical_worker_slot(n_val)
                if cand:
                    seg_slot = cand
                    break

        if not seg_slot:
            continue

        seg_role_prefix = _role_prefix_from_slot(seg_slot)
        started_roles_in_cmd.append((seg_role_prefix, seg_task_id, seg_run_id))

        if seg_slot in seen_slots_in_cmd:
            return (
                f"[ORCA ELASTIC WORKTREE GUARD] Phát hiện 2 lệnh worker-start trong cùng một cụm lệnh cùng trỏ vào worktree '{seg_slot}'! "
                f"CẤM chạy 2 worker song song trên cùng 1 worktree. "
                f"Cách sửa: Dùng $(orca-team worktree-arg {seg_role_prefix} --task <TASK_ID>) cho mỗi lệnh worker-start để tự động tách sang '{seg_role_prefix}-worker-2'."
            )
        seen_slots_in_cmd.add(seg_slot)
        candidates.append((seg_slot, is_new_child, seg_task_id, seg_run_id))

    # Wave-Order Check 1: Prevent starting Wave 0 (critic), Wave 1 (dev/ui/fix), and Wave 2 (devops/sec/qa/bench) simultaneously in one command
    if len(started_roles_in_cmd) > 1:
        w0_roles = sorted({r for r, _, _ in started_roles_in_cmd if r == "critic"})
        w1_roles = sorted({r for r, _, _ in started_roles_in_cmd if r in ("dev", "ui", "fix")})
        w2_roles = sorted({r for r, _, _ in started_roles_in_cmd if r in ("devops", "sec", "qa", "bench")})
        if w0_roles and (w1_roles or w2_roles):
            other_roles = sorted(set(w1_roles + w2_roles))
            return (
                f"[ORCA WAVE-ORDER GUARD] Phát hiện lệnh khởi chạy đồng thời Quân sư 'critic-worker' (Wave 0) "
                f"và Worker thi công/kiểm thử ({', '.join(other_roles)}) trong cùng một đợt! "
                f"CẤM chạy song song critic-worker với các Worker khác vì critic-worker BẮT BUỘC phải tranh biện 2 chiều (ask <-> reply) "
                f"và chốt CRITIC_VERDICT: CONSENSUS_REACHED + ghim 'orca-team plan pin' TRƯỚC khi giao việc cho {', '.join(other_roles)}! "
                f"Cách sửa: Chỉ khởi chạy 'critic-worker' trước, gọi 'orca-team wait-events' để nhận câu hỏi phản biện 'CRITIC_CHALLENGE', "
                f"trả lời qua 'orca orchestration reply', chờ 'CONSENSUS_REACHED' và ghim 'orca-team plan pin' rồi mới khởi chạy {', '.join(other_roles)}."
            )
        if w1_roles and w2_roles:
            return (
                f"[ORCA WAVE-ORDER GUARD] Phát hiện lệnh khởi chạy đồng thời Worker viết code ({', '.join(w1_roles)}) "
                f"và Worker kiểm thử/triển khai ({', '.join(w2_roles)}) trong cùng một đợt! "
                f"CẤM chạy song song Wave 1 và Wave 2 vì khi {w1_roles[0]}-worker chưa làm xong, {w2_roles[0]}-worker sẽ merge thiếu code! "
                f"Cách sửa: Chỉ khởi chạy các Worker Wave 1 ({', '.join(w1_roles)}) trước, chờ 'orca orchestration check --wait' báo hoàn tất, "
                f"sau đó mới khởi chạy Wave 2 ({', '.join(w2_roles)})."
            )
        if "devops" in w2_roles and ("qa" in w2_roles or "bench" in w2_roles):
            verifiers = [r for r in w2_roles if r in ("qa", "bench")]
            return (
                f"[ORCA WAVE-ORDER GUARD] Phát hiện lệnh khởi chạy đồng thời 'devops' và '{'/'.join(verifiers)}' trong cùng một đợt! "
                f"Khi Run có devops-worker, bạn phải chờ devops-worker dựng xong hạ tầng/container và commit xong rồi mới khởi chạy {'/'.join(verifiers)}-worker."
            )

    # Wave-Order Check 2: Verify in orchestration.db that no upstream worker (including Wave 0 critic) in the same Run is still working/unstarted/failed
    if orca_team_bin:
        for seg_role, seg_tid, seg_rid in started_roles_in_cmd:
            if seg_role not in ("dev", "ui", "fix", "devops", "sec", "qa", "bench"):
                continue
            if not seg_tid and not seg_rid:
                continue
            try:
                cmd_wave = [
                    sys.executable,
                    orca_team_bin,
                    "--path",
                    cwd,
                    "merge-upstream",
                    "--check-only",
                    "--role",
                    seg_role,
                    "--json",
                ]
                if seg_tid:
                    cmd_wave.extend(["--task", seg_tid])
                if seg_rid:
                    cmd_wave.extend(["--run", seg_rid])
                proc_w = subprocess.run(
                    cmd_wave,
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
                if proc_w.returncode == 0 and proc_w.stdout.strip():
                    w_info = json.loads(proc_w.stdout)
                    if isinstance(w_info, dict) and w_info.get("runId") and w_info.get("readyToMerge") is False:
                        r_id_str = w_info.get("runId")
                        act_list = w_info.get("blockingActiveUpstream") or w_info.get("activeUpstreamWorkers") or []
                        unst_list = w_info.get("blockingUnstartedUpstream") or w_info.get("unstartedUpstreamTasks") or []
                        fail_list = w_info.get("blockingFailedUpstream") or w_info.get("failedUpstreamDispatches") or []
                        reasons: list[str] = []
                        if act_list:
                            act_desc = ", ".join(
                                f"{a.get('slot') or a.get('role') or a.get('rolePrefix')} ({a.get('taskId')}: {a.get('state')})"
                                for a in act_list
                            )
                            reasons.append(f"Worker phía trước đang chạy dở: [{act_desc}]")
                        if unst_list:
                            unst_desc = ", ".join(
                                f"{u.get('role') or u.get('rolePrefix')}-worker ({u.get('taskId')}: chưa worker-start)"
                                for u in unst_list
                            )
                            reasons.append(f"Task phía trước đã tạo nhưng chưa khởi chạy: [{unst_desc}]")
                        if fail_list:
                            fail_desc = ", ".join(
                                f"{f.get('slot') or f.get('role') or f.get('rolePrefix')} ({f.get('taskId')}: {f.get('state') or f.get('status')})"
                                for f in fail_list
                            )
                            reasons.append(f"Worker phía trước bị lỗi chưa xử lý: [{fail_desc}]")
                        reason_joined = "; ".join(reasons) or "upstream chưa hoàn tất"
                        return (
                            f"[ORCA WAVE-ORDER GUARD] CẤM khởi chạy '{seg_role}-worker' trong Run '{r_id_str}' khi các Worker viết code/hạ tầng phía trước chưa hoàn tất! "
                            f"Chi tiết: {reason_joined}. "
                            f"Nếu khởi chạy '{seg_role}-worker' lúc này, lệnh merge-upstream sẽ bị thiếu code hoặc bị chặn. "
                            f"Cách sửa: Chờ các Worker phía trước hoàn tất bằng 'orca orchestration check --wait' (hoặc khởi chạy/xử lý xong task phía trước) rồi mới gọi worker-start cho '{seg_role}-worker'."
                        )
            except Exception:
                pass

    if not candidates or not orca_team_bin:
        return ""

    need_two_phase = len(candidates) > 1 or any(is_nc for _, is_nc, _, _ in candidates)

    # Phase 1: Validate all candidate slots (with --no-reserve if compound or new-child to prevent reservation poisoning)
    for slot_name, is_new_child, seg_tid, seg_rid in candidates:
        try:
            cmd_check = [
                sys.executable,
                orca_team_bin,
                "--path",
                cwd,
                "worktree-arg",
                slot_name,
                "--check-slot",
                slot_name,
            ]
            if seg_tid:
                cmd_check.extend(["--task", seg_tid])
            if seg_rid:
                cmd_check.extend(["--run", seg_rid])
            if need_two_phase:
                cmd_check.append("--no-reserve")
            cmd_check.append("--json")
            proc = subprocess.run(
                cmd_check,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                info = json.loads(proc.stdout)
                if isinstance(info, dict):
                    role_prefix = _role_prefix_from_slot(slot_name)
                    next_slot = info.get("slot") or f"{role_prefix}-worker-2"
                    task_flag_hint = f" --task {seg_tid}" if seg_tid else " --task <TASK_ID>"
                    next_args = info.get("argString") or f"$(orca-team worktree-arg {role_prefix}{task_flag_hint})"
                    locked_by = str(info.get("lockedByRun") or "").strip()
                    if info.get("slotBusy"):
                        if locked_by:
                            return (
                                f"[ORCA ELASTIC WORKTREE GUARD] Worktree '{slot_name}' đang được GIỮ (RETAINED - chưa merge vào main) "
                                f"bởi Run '{locked_by}' của một Leader khác! "
                                f"CẤM ghi đè worktree của Run chưa hoàn tất Bước 4 (sẽ làm mất nhánh chưa merge của Run '{locked_by}'). "
                                f"Cách sửa: Thay tham số '--worktree ...' bằng $(orca-team worktree-arg {role_prefix}{task_flag_hint}) "
                                f"(hoặc dùng: {next_args}) để tự động điều phối sang '{next_slot}'."
                            )
                        return (
                            f"[ORCA ELASTIC WORKTREE GUARD] Worktree '{slot_name}' đang BẬN chạy một tác vụ khác! "
                            f"CẤM truyền cứng '--worktree' khi gọi worker-start vì sẽ khiến 2 agent ghi đè worktree của nhau. "
                            f"Cách sửa: Thay tham số '--worktree ...' bằng $(orca-team worktree-arg {role_prefix}{task_flag_hint}) "
                            f"(hoặc dùng: {next_args}) để tự động điều phối sang '{next_slot}'."
                        )
                    if is_new_child and info.get("exists"):
                        existing_p = info.get("path") or slot_name
                        return (
                            f"[ORCA ELASTIC WORKTREE GUARD] Worktree '{slot_name}' đã tồn tại sẵn tại '{existing_p}'! "
                            f"CẤM dùng '--worktree new-child --name {slot_name}' trên worktree đã tồn tại (sẽ gây lỗi trùng git branch). "
                            f"Cách sửa: Thay bằng $(orca-team worktree-arg {role_prefix}{task_flag_hint}) (hoặc dùng: --worktree path:{existing_p})."
                        )
        except Exception:
            pass

    # Phase 2: Only needed when need_two_phase is True -> reserve each slot and clean any lingering `done` tabs
    if need_two_phase:
        for slot_name, _, seg_tid, seg_rid in candidates:
            try:
                cmd_res = [
                    sys.executable,
                    orca_team_bin,
                    "--path",
                    cwd,
                    "worktree-arg",
                    slot_name,
                    "--check-slot",
                    slot_name,
                ]
                if seg_tid:
                    cmd_res.extend(["--task", seg_tid])
                if seg_rid:
                    cmd_res.extend(["--run", seg_rid])
                cmd_res.append("--json")
                subprocess.run(
                    cmd_res,
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
            except Exception:
                pass

    return ""


def _trigger_post_worker_release_sweep(cmd: str, payload: dict, args: dict) -> None:
    """
    When a Leader runs `orca orchestration worker-release --dispatch <id>`, spawn a lightweight
    detached background sweep after 3 seconds so that:
    1) Any `user_takeover` or `stopped` worker tab that `worker-release` left open is closed via `orca terminal close`.
    2) If the Run has finished all workers (Step 4 teardown), any forgotten earlier-wave `retained` tabs
       in that Run and idle worker worktrees are automatically archived and closed.
    """
    if "worker-release" not in cmd:
        return
    sanitized = _strip_quoted_heredocs(_strip_non_exec_prose_flags(cmd))
    if "worker-release" not in sanitized or not re.search(
        r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo|nohup)\s+)\s*(?:\S*/)?orca\s+orchestration\s+worker-release\b",
        sanitized,
    ):
        return
    dids = sorted(set(re.findall(r"\b((?:ctx|disp)_[A-Za-z0-9_-]{6,64})\b", sanitized)))
    orca_team_bin, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
    if not orca_team_bin:
        return
    try:
        if dids:
            did_csv = ",".join(dids)
            py_code = (
                "import subprocess, sys, time; "
                "time.sleep(3.0); "
                "subprocess.run([sys.executable, sys.argv[1], '--path', sys.argv[2], "
                "'release-workers', '--after-dispatch', sys.argv[3], '--quiet'], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)"
            )
            subprocess.Popen(
                [sys.executable, "-c", py_code, orca_team_bin, cwd, did_csv],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            # Executed with a shell variable (e.g. `for d in ...; do orca orchestration worker-release --dispatch "$d"; done`)
            py_code = (
                "import subprocess, sys, time; "
                "time.sleep(3.0); "
                "subprocess.run([sys.executable, sys.argv[1], '--path', sys.argv[2], "
                "'release-workers', '--quiet'], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)"
            )
            subprocess.Popen(
                [sys.executable, "-c", py_code, orca_team_bin, cwd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass


def _ensure_child_worktree_hygiene(payload: dict, args: dict) -> None:
    """
    Ultra-fast (<1ms pure Python) child worktree hygiene check before any Worker command runs:
    If `cwd` is a Git child worktree (`.git` is a file pointing to `<main_root>/.git/worktrees/<slot>`):
    1. Strip `.agents/GEMINI.md` so Leader orchestration directives never leak into Worker context or dirty `git status`.
    2. Ensure `<cwd>/.orca/runs -> <main_root>/.orca/runs` symlink exists (inside real `<cwd>/.orca/` dir) so Pinned Master Plan is always readable.
    3. Ensure `<cwd>/node_modules -> <main_root>/node_modules` symlink exists when `<cwd>/package.json` and `<main_root>/node_modules` exist.
    """
    try:
        raw_cwd = str(args.get("Cwd") or args.get("cwd") or payload.get("cwd") or os.getcwd()).strip()
        if not raw_cwd:
            return
        cwd_path = os.path.realpath(raw_cwd)
        git_dot = os.path.join(cwd_path, ".git")
        if not os.path.isfile(git_dot):
            return
        with open(git_dot, "r", encoding="utf-8", errors="ignore") as gf:
            first_line = gf.readline().strip()
        if "/.git/worktrees/" not in first_line:
            return
        gitdir_val = first_line.split(":", 1)[1].strip() if ":" in first_line else first_line
        if not os.path.isabs(gitdir_val):
            gitdir_val = os.path.normpath(os.path.join(cwd_path, gitdir_val))
        main_root = gitdir_val.split("/.git/worktrees/", 1)[0]
        if not main_root or not os.path.isdir(main_root) or os.path.realpath(main_root) == cwd_path:
            return

        # 1. Strip .agents/GEMINI.md, GEMINI.md, AGENTS.md, CLAUDE.md in child worktree across all Agent CLIs
        for rel_d in (".agents/GEMINI.md", "GEMINI.md", "AGENTS.md", "CLAUDE.md"):
            cand_d = os.path.join(cwd_path, rel_d)
            if not os.path.lexists(cand_d):
                continue
            should_strip = (rel_d == ".agents/GEMINI.md") or os.path.islink(cand_d)
            if not should_strip and os.path.isfile(cand_d):
                try:
                    with open(cand_d, "r", encoding="utf-8", errors="ignore") as df:
                        head_txt = df.read(4096)
                    if (
                        "Orca Multi-Agent Orchestration Directive" in head_txt
                        or "Principal Chief Architect & Lead Orchestrator" in head_txt
                    ):
                        should_strip = True
                except OSError:
                    pass
            if should_strip:
                try:
                    os.unlink(cand_d)
                except OSError:
                    pass
        agents_dir = os.path.join(cwd_path, ".agents")
        try:
            if os.path.isdir(agents_dir) and not os.listdir(agents_dir):
                os.rmdir(agents_dir)
        except OSError:
            pass

        # 2. Ensure <cwd>/.orca/runs -> <main_root>/.orca/runs
        main_runs = os.path.join(main_root, ".orca", "runs")
        if os.path.isdir(main_runs):
            wt_orca = os.path.join(cwd_path, ".orca")
            if os.path.islink(wt_orca):
                try:
                    os.unlink(wt_orca)
                except OSError:
                    pass
            os.makedirs(wt_orca, exist_ok=True)
            wt_runs = os.path.join(wt_orca, "runs")
            if not os.path.lexists(wt_runs):
                try:
                    os.symlink(main_runs, wt_runs)
                except OSError:
                    pass

        # 3. Ensure <cwd>/node_modules -> <main_root>/node_modules
        main_nm = os.path.join(main_root, "node_modules")
        wt_pkg = os.path.join(cwd_path, "package.json")
        wt_nm = os.path.join(cwd_path, "node_modules")
        if os.path.isfile(wt_pkg) and os.path.isdir(main_nm) and not os.path.lexists(wt_nm):
            try:
                os.symlink(main_nm, wt_nm)
            except OSError:
                pass

        # 4. Ensure <main_root>/.git/info/exclude contains `.orca`, `.orca/`, `node_modules`, `node_modules/`
        # (because a gitignore rule with a trailing slash `node_modules/` does NOT match a symlink `node_modules`)
        excl_file = os.path.join(main_root, ".git", "info", "exclude")
        if os.path.isdir(os.path.dirname(excl_file)):
            try:
                existing_excl = ""
                if os.path.isfile(excl_file):
                    with open(excl_file, "r", encoding="utf-8", errors="ignore") as ef:
                        existing_excl = ef.read()
                excl_lines = {ln.strip() for ln in existing_excl.splitlines()}
                needed = [e for e in (".orca", ".orca/", "node_modules", "node_modules/") if e not in excl_lines]
                if needed:
                    with open(excl_file, "a", encoding="utf-8") as ef:
                        if existing_excl and not existing_excl.endswith("\n"):
                            ef.write("\n")
                        for item in needed:
                            ef.write(f"{item}\n")
            except OSError:
                pass
    except Exception:
        pass


def _has_unescaped_backtick_in_double_quoted_prose_flag(cmd: str) -> str:
    """
    Detect unescaped backticks (`...`) inside double-quoted values of prose flags
    (--body, --subject, --question, --summary, --content, --title) on `orca orchestration` or `orca-team plan`.
    In zsh/bash, unescaped backticks inside "..." are executed as command substitution before the CLI runs.
    """
    if "`" not in cmd:
        return ""
    scan_cmd = _strip_quoted_heredocs(_strip_non_piped_cat_tee_heredocs(cmd))
    if "`" not in scan_cmd or not re.search(r"\borca(?:-team)?\b", scan_cmd):
        return ""
    flag_re = re.compile(r"--(body|subject|question|summary|content|title)(?:\s+|=)\"")
    n = len(scan_cmd)
    i = 0
    in_single = False
    in_double = False
    while i < n:
        ch = scan_cmd[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        # Outside quotes
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == "-":
            m = flag_re.match(scan_cmd, i)
            if m:
                flag_name = f"--{m.group(1)}"
                open_dq_idx = m.end() - 1
                if _double_quoted_arg_has_backtick(scan_cmd, open_dq_idx):
                    return flag_name
                end_dq = _find_double_quote_end(scan_cmd, open_dq_idx)
                i = (end_dq + 1) if end_dq != -1 else m.end()
                continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        i += 1
    return ""


def _find_orca_orchestration_db_path() -> str:
    """Locate Orca's SQLite orchestration.db across macOS/Linux/XDG paths."""
    env_p = os.environ.get("ORCA_DB_PATH") or os.environ.get("ORCA_ORCHESTRATION_DB")
    if env_p and os.path.isfile(env_p):
        return env_p
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Library", "Application Support", "orca", "orchestration.db"),
        os.path.join(home, ".config", "orca", "orchestration.db"),
        os.path.join(home, ".local", "share", "orca", "orchestration.db"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _extract_cli_flag_value(cmd_text: str, flag_names: tuple[str, ...]) -> str:
    """
    C-FIX-31: Extract the unquoted value of the first matching `--flag` in `cmd_text`.
    First attempts `shlex.split` so mixed quoting from `shlex.quote` (e.g. `'...'"'"'...'` when JSON contains apostrophes)
    is reconstructed accurately before falling back to regex.
    """
    import shlex

    flag_set = {f"--{f}" for f in flag_names}
    try:
        toks = shlex.split(cmd_text, posix=True)
        for idx, tk in enumerate(toks):
            if tk in flag_set and idx + 1 < len(toks):
                return toks[idx + 1]
            if "=" in tk:
                k_part, v_part = tk.split("=", 1)
                if k_part in flag_set:
                    return v_part
    except Exception:
        pass

    alt = "|".join(re.escape(f) for f in flag_names)
    m = re.search(
        rf"(?:^|\s)--(?:{alt})(?:\s+|=)(?:\"((?:\\.|[^\"])*)\"|'([^']*)'|([^\s;|&\"']+))",
        cmd_text,
    )
    if not m:
        return ""
    if m.group(1) is not None:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    if m.group(2) is not None:
        return m.group(2)
    return m.group(3) or ""


def _load_critic_gate_module():
    """Load scripts/orca_critic_gate.py from the same directory as this guard."""
    import importlib.util

    gate_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orca_critic_gate.py")
    if not os.path.isfile(gate_p):
        raise RuntimeError(f"Missing orca_critic_gate.py at {gate_p}")
    spec = importlib.util.spec_from_file_location("orca_critic_gate", gate_p)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot create spec for orca_critic_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _handle_critic_gate_violation(
    violation_msg: str,
    run_id: str = "",
    cmd_str: str = "",
    plan_hash_val: str = "",
) -> str:
    """
    C-FIX-05..09 / C16: Support ORCA_CRITIC_GATE_MODE=off|shadow|enforce (default: enforce).
    In shadow mode, write to ~/.orca/evidence/critic-gate/would_block.jsonl and return '' (ALLOW).
    """
    if not violation_msg:
        return ""
    mode = os.environ.get("ORCA_CRITIC_GATE_MODE", "enforce").strip().lower()
    if mode == "off":
        return ""
    if mode == "shadow":
        try:
            ev_base = os.environ.get("ORCA_EVIDENCE_DIR", "").strip() or os.path.join(
                os.path.expanduser("~"), ".orca", "evidence"
            )
            cg_dir = os.path.join(ev_base, "critic-gate")
            os.makedirs(cg_dir, exist_ok=True)
            wb_file = os.path.join(cg_dir, "would_block.jsonl")
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "run": run_id or "default",
                "cmd": (cmd_str or "")[:300],
                "planHash": plan_hash_val,
                "violations": [violation_msg],
                "action": "would_block",
            }
            with open(wb_file, "a", encoding="utf-8") as wf:
                wf.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
        _log_guard_decision("would_block", violation_msg, cmd_str, "critic-gate")
        return ""
    return violation_msg


def _resolve_critic_run_dir(real_cwd: str, payload: dict, args: dict, run_id: str) -> tuple[str, str]:
    """Return (project_root, run_dir) for a given run_id and cwd."""
    candidates_root: list[str] = []
    if real_cwd:
        cur = real_cwd
        for _ in range(6):
            if os.path.isfile(os.path.join(cur, "orca-team.json")) or os.path.isdir(os.path.join(cur, ".orca", "runs")):
                if cur not in candidates_root:
                    candidates_root.append(cur)
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
    ws_list = (payload.get("workspacePaths") if isinstance(payload, dict) else None) or []
    for wp in ws_list:
        if isinstance(wp, str) and wp.strip():
            rp = os.path.realpath(wp.strip())
            if rp not in candidates_root:
                candidates_root.append(rp)
    if real_cwd and real_cwd not in candidates_root:
        candidates_root.append(real_cwd)

    eff_run = (run_id or "").strip()
    for root_cand in candidates_root:
        if eff_run:
            rd = os.path.join(root_cand, ".orca", "runs", eff_run)
            if os.path.isdir(rd):
                return root_cand, rd
        else:
            runs_parent = os.path.join(root_cand, ".orca", "runs")
            if os.path.isdir(runs_parent):
                subs = [
                    os.path.join(runs_parent, d)
                    for d in os.listdir(runs_parent)
                    if os.path.isdir(os.path.join(runs_parent, d))
                ]
                if subs:
                    subs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                    return root_cand, subs[0]

    proj_root = candidates_root[0] if candidates_root else (real_cwd or "")
    return proj_root, (os.path.join(proj_root, ".orca", "runs", eff_run or "default") if proj_root else "")


def _mask_shell_quoted_spans(s: str) -> tuple[str, str]:
    """
    C-FIX-31: Linear single-pass POSIX shell quote scanner.
    Returns `(no_sq, no_quotes)`:
      - `no_sq`: all single-quoted spans (`'...'` and `$'...'`) replaced with `'__SQ__'`
      - `no_quotes`: both single-quoted and double-quoted spans replaced with `'__SQ__'` / `"__DQ__"`.
    Correctly handles adjacent/hybrid quotes produced by `shlex.quote` (e.g. `'don'"'"'t'`)
    and multi-line JSON containing apostrophes, double quotes, backticks, and `${}` without splitting inside quotes.
    """
    out_no_sq: list[str] = []
    out_no_q: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            pair = s[i : i + 2]
            out_no_sq.append(pair)
            out_no_q.append(pair)
            i += 2
            continue
        if ch == "'":
            end_sq = _find_single_quote_end(s, i)
            if end_sq == -1:
                out_no_sq.append("'")
                out_no_q.append("'")
                break
            out_no_sq.append("'__SQ__'")
            out_no_q.append("'__SQ__'")
            i = end_sq + 1
            continue
        if ch == '"':
            end_dq = _find_double_quote_end(s, i)
            if end_dq == -1:
                out_no_sq.append('"')
                out_no_q.append('"')
                break
            out_no_sq.append(s[i : end_dq + 1])
            out_no_q.append('"__DQ__"')
            i = end_dq + 1
            continue
        out_no_sq.append(ch)
        out_no_q.append(ch)
        i += 1
    return "".join(out_no_sq), "".join(out_no_q)


def _check_critic_bash_readonly_guard(cmd: str, no_heredoc: str, cwd_slot: str) -> str:
    """
    C-FIX-08, C-FIX-25 & C-FIX-31: Enforce strict read-only command allowlist for critic-worker in Bash commands.
    Only read-only inspection utilities, read-only git subcommands, `orca orchestration ask|send|inbox|status`,
    and `orca-team critic brief|write` / `state` / `card` / `kb` are permitted.
    Any redirection (`>`, `>>`) or `tee` must target `.orca/runs/<run>/critic/` without `..`.
    """
    if not (cwd_slot and cwd_slot.startswith("critic-worker")):
        return ""
    if os.environ.get("ORCA_CRITIC_GATE_MODE", "enforce").strip().lower() == "off":
        return ""

    import shlex

    no_sq, no_quotes = _mask_shell_quoted_spans(no_heredoc)

    # 1. Block any redirection or tee targeting outside .orca/runs/<run>/critic/ (outside quotes)
    for m_red in re.finditer(r"(?:>|>>|\btee\s+(?:-a\s+)?)\s*['\"]?([^\s'\";|&]+)", no_sq):
        target_p = m_red.group(1).strip()
        if target_p in ("/dev/null", "/dev/stderr", "/dev/stdout", "__SQ__") or target_p.startswith("&"):
            continue
        if ".." in target_p or not re.search(r"(?:^|[/\\])\.orca[/\\]runs[/\\][^/\\]+[/\\]critic[/\\]", target_p):
            return "[ORCA CRITIC READ-ONLY GUARD] critic-worker chỉ được ghi dưới .orca/runs/<run>/critic/."

    # 2. Check command substitution `$()` or backticks (outside single quotes)
    for m_sub in re.finditer(r"\$\(([^)]+)\)|`([^`]+)`", no_sq):
        sub_c = (m_sub.group(1) or m_sub.group(2) or "").strip()
        sub_err = _check_critic_bash_readonly_guard(sub_c, sub_c, cwd_slot)
        if sub_err:
            return sub_err

    readonly_bins = {
        "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "ack",
        "find", "fd", "ls", "tree", "stat", "file", "wc", "du", "df", "pwd", "echo", "printf",
        "true", "false", "test", "[", "which", "whereis", "type", "uname", "date", "env",
        "printenv", "id", "whoami", "jq", "yq", "sort", "uniq", "cut", "tr", "awk", "sed",
        "diff", "cmp", "comm", "shasum", "sha256sum", "md5", "md5sum", "xxd", "hexdump",
        "strings", "basename", "dirname", "realpath", "readlink", "tee",
    }
    readonly_git_subcmds = {
        "status", "diff", "log", "show", "rev-parse", "merge-base", "ls-files", "ls-tree",
        "blame", "grep", "cat-file", "describe", "shortlog", "branch", "tag",
    }

    segments = re.split(r"(?:\|\||&&|[;|\n])+", no_quotes)
    for seg in segments:
        seg_clean = seg.strip()
        if not seg_clean:
            continue
        # Remove harmless redirections already checked above
        seg_no_redir = re.sub(r"(?:\d*>>?|\d*<&?\d*)\s*['\"]?[^\s'\";|&]+['\"]?", " ", seg_clean).strip()
        if not seg_no_redir:
            continue
        try:
            toks = shlex.split(seg_no_redir)
        except Exception:
            toks = seg_no_redir.split()
        # Strip leading VAR=val assignments
        while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
            toks.pop(0)
        if not toks:
            continue

        exe = os.path.basename(toks[0]).lower()

        if exe == "git":
            # Parse options (-C <path>, -c <k=v>, --git-dir=..., --work-tree=..., -p, --no-pager) to locate git subcommand
            idx = 1
            subcmd = ""
            while idx < len(toks):
                tk = toks[idx]
                if tk in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                    idx += 2
                    continue
                if tk.startswith("-"):
                    idx += 1
                    continue
                subcmd = tk.lower()
                break
            if not subcmd or subcmd not in readonly_git_subcmds:
                return (
                    f"[ORCA CRITIC READ-ONLY GUARD] critic-worker chỉ được chạy git ở chế độ chỉ-đọc "
                    f"(chặn 'git {subcmd or '<unknown>'}'). critic-worker chỉ được ghi dưới .orca/runs/<run>/critic/."
                )
            if subcmd in ("branch", "tag"):
                for tk in toks[idx + 1 :]:
                    if tk in ("-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force"):
                        return "[ORCA CRITIC READ-ONLY GUARD] critic-worker cấm xoá/đổi tên branch hoặc tag!"
            continue

        if exe == "orca":
            if len(toks) >= 3 and toks[1] == "orchestration" and toks[2] in ("ask", "send", "inbox", "status", "list", "check", "get"):
                continue
            return f"[ORCA CRITIC READ-ONLY GUARD] critic-worker không được phép chạy lệnh '{' '.join(toks[:3])}'!"

        if exe == "orca-team":
            # Strip global --path <dir> if present
            sub_toks = list(toks[1:])
            if len(sub_toks) >= 2 and sub_toks[0] in ("--path", "-p", "--project"):
                sub_toks = sub_toks[2:]
            if not sub_toks:
                continue
            s0 = sub_toks[0].lower()
            if s0 in ("brief", "critic-brief", "blind-pack", "state", "card", "kb", "knowledge", "system-engineer", "status", "list", "show", "spec", "role-spec", "prompt"):
                continue
            if s0 == "critic" and len(sub_toks) >= 2 and sub_toks[1].lower() in ("brief", "blind-pack", "pack", "write"):
                continue
            if s0 == "plan" and len(sub_toks) >= 2 and sub_toks[1].lower() in ("show", "consensus"):
                continue
            return f"[ORCA CRITIC READ-ONLY GUARD] critic-worker không được phép chạy 'orca-team {s0}'!"

        if exe not in readonly_bins:
            return (
                f"[ORCA CRITIC READ-ONLY GUARD] Lệnh '{exe}' không nằm trong allowlist chỉ-đọc của critic-worker! "
                "critic-worker chỉ được ghi dưới .orca/runs/<run>/critic/ (dùng 'orca-team critic write' hoặc công cụ ghi file vào thư mục critic)."
            )

        if exe == "sed":
            for tk in toks[1:]:
                if re.match(r"^-[a-zA-Z]*i", tk) or tk.startswith("--in-place"):
                    return "[ORCA CRITIC READ-ONLY GUARD] critic-worker cấm dùng 'sed -i' sửa file tại chỗ!"
        elif exe == "awk":
            for tk in toks[1:]:
                if tk in ("-i", "inplace") or tk.startswith("-i"):
                    return "[ORCA CRITIC READ-ONLY GUARD] critic-worker cấm dùng 'awk -i inplace'!"
        elif exe == "find":
            for tk in toks[1:]:
                if tk in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls", "-fprintf"):
                    return f"[ORCA CRITIC READ-ONLY GUARD] critic-worker cấm dùng cờ '{tk}' trong find!"

    return ""


def _check_critic_two_way_debate_and_question_guards(
    cmd: str, sanitized_orch: str, payload: dict, args: dict
) -> str:
    """
    Enforce mandatory 2-way debate (`ask` <-> `reply`) and Critic Consensus Gate (`C-FIX-01`..`C-FIX-12`):
    - C-FIX-05: Validate Critic review JSON (coverage, quota, resolution, independence).
    - C-FIX-06: Plan hash & message-log provenance binding on `plan pin` and `worker-start`.
    - C-FIX-07: Deadlock escalation guard.
    - C-FIX-08: Critic read-only enforcement.
    - C-FIX-09: Independent hypothesis before first `ask`.
    - C15: Fail-closed when `question_threads` is missing or internal error occurs on critic/pin/worker-start.
    - C16: Support `ORCA_CRITIC_GATE_MODE=off|shadow|enforce`.
    """
    no_heredoc = _strip_quoted_heredocs(cmd)
    raw_cwd_in, real_cwd, cwd_slot = _detect_caller_slot_safe(payload, args, cmd)

    # C-FIX-08: Bash read-only check for critic-worker (applies even if command does not mention `orca`)
    ro_err = _check_critic_bash_readonly_guard(cmd, no_heredoc, cwd_slot)
    if ro_err:
        return _handle_critic_gate_violation(ro_err, "", cmd)

    if not re.search(r"\borca(?:-team)?\b", sanitized_orch):
        return ""

    # 1a. Block chaining `orca orchestration ask` and `orca orchestration send` in the same command (C13)
    if re.search(r"\borca\s+orchestration\s+ask\b", sanitized_orch) and re.search(
        r"\borca\s+orchestration\s+send\b", sanitized_orch
    ):
        return (
            "[ORCA CRITIC 2-WAY DEBATE GUARD] CẤM nối chuỗi 'orca orchestration ask' và 'orca orchestration send' trong cùng một lệnh! "
            "Quân sư 'critic-worker' BẮT BUỘC phải thực hiện quy trình 2 pha tách biệt: "
            "Pha 1: Chạy duy nhất lệnh 'orca orchestration ask --from <worker_handle> --question \"CRITIC_CHALLENGE vòng 1/3: ...\" --timeout-ms 900000 --json' để gửi phản biện/câu hỏi kiểm chứng cho Leader và chờ Leader trả lời qua 'orca orchestration reply'; "
            "Pha 2: Chỉ SAU KHI lệnh 'ask' trả về câu trả lời của Leader và hai bên đã thống nhất 100%, ở lượt (turn) tiếp theo mới được chạy 'orca orchestration send --type handoff ... && orca orchestration send --type worker_done ...'."
        )

    is_critic_cmd = bool(
        (cwd_slot and cwd_slot.startswith("critic-worker"))
        or re.search(r"--from(?:\s+|=)['\"]?critic-worker\b", no_heredoc, re.IGNORECASE)
        or re.search(r"\b(?:Critic\s+Consensus|Critic\s+Deadlock|CRITIC_VERDICT|CRITIC_CHALLENGE)\b", no_heredoc, re.IGNORECASE)
    )
    is_plan_pin = bool(re.search(r"\borca-team\b[^\n;|&]*\bplan\s+pin\b", sanitized_orch))
    is_worker_start = bool(re.search(r"\borca\s+orchestration\s+worker-start\b", sanitized_orch))

    db_path = _find_orca_orchestration_db_path()
    if not db_path:
        if is_critic_cmd or is_plan_pin:
            return "[ORCA CRITIC FAIL-CLOSED GUARD] Không tìm thấy cơ sở dữ liệu orchestration.db để xác minh Critic Gate!"
        return ""

    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        if is_critic_cmd or is_plan_pin:
            return f"[ORCA CRITIC FAIL-CLOSED GUARD] Không thể kết nối orchestration.db ({exc})!"
        return ""

    try:
        # C-FIX-09 (C09): Require hypothesis.json before Critic's first `orca orchestration ask` (for risk >= medium)
        if re.search(r"\borca\s+orchestration\s+ask\b", sanitized_orch) and is_critic_cmd:
            ask_run_id = _extract_cli_flag_value(no_heredoc, ("run", "run-id"))
            ask_disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
            if not ask_run_id and ask_disp_id:
                r_ask_dc = conn.execute("SELECT run_id FROM dispatch_contexts WHERE id = ? LIMIT 1", (ask_disp_id,)).fetchone()
                if r_ask_dc:
                    ask_run_id = str(r_ask_dc["run_id"] or "")
            if not ask_run_id:
                r_act_r = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                if r_act_r:
                    ask_run_id = str(r_act_r["id"] or "")

            _, ask_run_dir = _resolve_critic_run_dir(real_cwd, payload, args, ask_run_id)
            if ask_run_dir:
                cg_mod = _load_critic_gate_module()
                plan_txt_ask = ""
                for pf_name in ("plan-draft.md", "master-plan.md"):
                    pf_cand = os.path.join(ask_run_dir, pf_name)
                    if os.path.isfile(pf_cand):
                        plan_txt_ask = open(pf_cand, "r", encoding="utf-8", errors="ignore").read()
                        break
                if plan_txt_ask:
                    risk_ask = cg_mod.compute_risk(cg_mod.extract_items(plan_txt_ask), cg_mod.contract_lines(plan_txt_ask))
                else:
                    risk_ask = {"level": "high", "reasons": ["default_high"]}
                if str(risk_ask.get("level") or "high").lower() in ("medium", "high"):
                    hyp_json_p = os.path.join(ask_run_dir, "critic", "hypothesis.json")
                    wt_hyp_p = os.path.join(real_cwd, ".orca", "runs", ask_run_id or "default", "critic", "hypothesis.json") if real_cwd else ""
                    if not os.path.isfile(hyp_json_p) and not (wt_hyp_p and os.path.isfile(wt_hyp_p)):
                        return _handle_critic_gate_violation(
                            f"[ORCA CRITIC INDEPENDENCE GUARD] Quân sư critic-worker bắt buộc phải ghi .orca/runs/{ask_run_id or '<run>'}/critic/hypothesis.md và hypothesis.json TRƯỚC lần 'orca orchestration ask' đầu tiên (risk={risk_ask.get('level')})!",
                            ask_run_id,
                            cmd,
                        )

        # 1b. Critic 2-Way Debate Guard + Review Schema/Coverage/Quota/Resolution/Independence on `orca orchestration send`
        if re.search(r"\borca\s+orchestration\s+send\b", sanitized_orch):
            has_handoff_or_done = bool(
                re.search(r"--type(?:\s+|=)['\"]?(?:handoff|worker_done)\b", no_heredoc)
            )
            is_failed_done_only = bool(
                re.search(r"--outcome(?:\s+|=)['\"]?failed\b", no_heredoc)
                and not re.search(r"--type(?:\s+|=)['\"]?handoff\b", no_heredoc)
                and not re.search(r"CONSENSUS_REACHED|Critic\s+Consensus", no_heredoc, re.IGNORECASE)
            )
            disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
            task_id = _extract_cli_flag_value(no_heredoc, ("task-id", "task"))
            from_handle = _extract_cli_flag_value(no_heredoc, ("from",))
            to_target = _extract_cli_flag_value(no_heredoc, ("to",))

            # Check Guard 3 ([ORCA REPLY GUARD]): Leader sending `orca orchestration send --to ...` when target has a pending question
            if to_target and to_target != "@all":
                target_disp = to_target.split("dispatch:", 1)[1].strip() if to_target.startswith("dispatch:") else ""
                pend_row = None
                if target_disp:
                    pend_row = conn.execute(
                        "SELECT message_id, dispatch_id, asker_handle FROM question_threads "
                        "WHERE dispatch_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                        (target_disp,),
                    ).fetchone()
                elif to_target.startswith("term_"):
                    pend_row = conn.execute(
                        "SELECT message_id, dispatch_id, asker_handle FROM question_threads "
                        "WHERE asker_handle = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                        (to_target,),
                    ).fetchone()
                if pend_row:
                    p_mid = pend_row["message_id"]
                    return (
                        f"[ORCA REPLY GUARD] Worker '{to_target}' đang chặn chờ câu trả lời qua lệnh 'orca orchestration ask' (Question ID: {p_mid})! "
                        f"Nếu bạn dùng 'orca orchestration send --to {to_target}', lệnh 'ask' của Worker sẽ KHÔNG nhận được tín hiệu trả lời và sẽ bị treo timeout. "
                        f"Cách sửa bắt buộc: Dùng lệnh 'orca orchestration reply --id {p_mid} --body \"<Nội dung phản biện/quyết định chi tiết>\" --json' để trả lời trực tiếp vào luồng ask!"
                    )

            if has_handoff_or_done and not is_failed_done_only:
                expanded_send, _ = _expand_inline_cat_substitutions(cmd, real_cwd)
                is_critic_sender = bool(
                    (cwd_slot and cwd_slot.startswith("critic-worker"))
                    or re.search(r"--from(?:\s+|=)['\"]?critic-worker\b", no_heredoc, re.IGNORECASE)
                    or re.search(
                        r"\b(?:Critic\s+Consensus|Critic\s+Deadlock|CRITIC_VERDICT|Worker\s+Done:\s*critic-worker)\b",
                        expanded_send,
                        re.IGNORECASE,
                    )
                )
                resolved_run_id = _extract_cli_flag_value(no_heredoc, ("run", "run-id"))
                if disp_id:
                    r_dc = conn.execute(
                        "SELECT dc.run_id, dc.task_id, dc.assignee_handle, wd.agent_terminal_handle, wd.worktree_id, t.spec "
                        "FROM dispatch_contexts dc "
                        "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                        "LEFT JOIN tasks t ON t.id = dc.task_id "
                        "WHERE dc.id = ? LIMIT 1",
                        (disp_id,),
                    ).fetchone()
                    if r_dc:
                        resolved_run_id = resolved_run_id or str(r_dc["run_id"] or "")
                        task_id = task_id or str(r_dc["task_id"] or "")
                        from_handle = (
                            from_handle
                            or str(r_dc["agent_terminal_handle"] or "")
                            or str(r_dc["assignee_handle"] or "")
                        )
                        wt_s = _extract_canonical_worker_slot(os.path.basename(str(r_dc["worktree_id"] or "")))
                        t_spec = str(r_dc["spec"] or "")
                        if (wt_s and wt_s.startswith("critic-worker")) or "[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC" in t_spec:
                            is_critic_sender = True
                if not is_critic_sender and task_id:
                    r_tk = conn.execute(
                        "SELECT run_id, spec FROM tasks WHERE id = ? LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if r_tk:
                        resolved_run_id = resolved_run_id or str(r_tk["run_id"] or "")
                        if "[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC" in str(r_tk["spec"] or ""):
                            is_critic_sender = True

                if is_critic_sender:
                    is_critic_cmd = True
                    if not disp_id and from_handle:
                        r_fh = conn.execute(
                            "SELECT dc.id, dc.run_id, dc.task_id "
                            "FROM dispatch_contexts dc "
                            "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                            "WHERE dc.assignee_handle = ? OR wd.agent_terminal_handle = ? "
                            "ORDER BY dc.created_at DESC LIMIT 1",
                            (from_handle, from_handle),
                        ).fetchone()
                        if r_fh:
                            disp_id = str(r_fh["id"] or "")
                            resolved_run_id = resolved_run_id or str(r_fh["run_id"] or "")
                            task_id = task_id or str(r_fh["task_id"] or "")

                    q_rows = []
                    if resolved_run_id and disp_id and from_handle:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE (run_id = ? OR run_id IS NULL) AND (dispatch_id = ? OR asker_handle = ?) ORDER BY created_at ASC",
                            (resolved_run_id, disp_id, from_handle),
                        ).fetchall()
                    elif disp_id and from_handle:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE dispatch_id = ? OR asker_handle = ? ORDER BY created_at ASC",
                            (disp_id, from_handle),
                        ).fetchall()
                    elif disp_id:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE dispatch_id = ? ORDER BY created_at ASC",
                            (disp_id,),
                        ).fetchall()
                    elif resolved_run_id and from_handle:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE (run_id = ? OR run_id IS NULL) AND asker_handle = ? ORDER BY created_at ASC",
                            (resolved_run_id, from_handle),
                        ).fetchall()
                    elif from_handle:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE asker_handle = ? ORDER BY created_at ASC",
                            (from_handle,),
                        ).fetchall()
                    elif resolved_run_id:
                        q_rows = conn.execute(
                            "SELECT message_id, status, answered_at, answer_message_id, created_at FROM question_threads "
                            "WHERE run_id = ? ORDER BY created_at ASC",
                            (resolved_run_id,),
                        ).fetchall()

                    total_q = len(q_rows)
                    answered_q = sum(
                        1
                        for r in q_rows
                        if str(r["status"] or "") == "answered"
                        or r["answered_at"] is not None
                        or r["answer_message_id"] is not None
                    )
                    pending_mid = next(
                        (
                            str(r["message_id"])
                            for r in q_rows
                            if str(r["status"] or "") == "pending" and r["answered_at"] is None
                        ),
                        "",
                    )

                    # Fallback check in `messages` table if `question_threads` exists but had 0 rows for this handle
                    if total_q == 0 and (disp_id or from_handle):
                        sender_handles = [h for h in (f"dispatch:{disp_id}" if disp_id else "", from_handle) if h]
                        if sender_handles:
                            ph = ",".join("?" for _ in sender_handles)
                            m_q = conn.execute(
                                f"SELECT id, created_at FROM messages WHERE type = 'question' AND from_handle IN ({ph}) ORDER BY created_at ASC",
                                tuple(sender_handles),
                            ).fetchall()
                            total_q = len(m_q)
                            if total_q > 0:
                                q_ids = [str(x["id"]) for x in m_q]
                                ph_q = ",".join("?" for _ in q_ids)
                                m_ans = conn.execute(
                                    f"SELECT COUNT(*) AS cnt FROM messages WHERE thread_id IN ({ph_q}) AND type != 'question'",
                                    tuple(q_ids),
                                ).fetchone()
                                answered_q = int(m_ans["cnt"] or 0) if m_ans else 0
                                if answered_q == 0:
                                    pending_mid = q_ids[0]

                    from_hint = from_handle or "<worker_handle>"
                    if total_q == 0:
                        return (
                            f"[ORCA CRITIC 2-WAY DEBATE GUARD] CẤM Quân sư 'critic-worker' gửi thẳng 'handoff (Critic Consensus)' hoặc 'worker_done' ở lượt đầu tiên mà BỎ QUA bước tranh biện 2 chiều ('orca orchestration ask') với Leader! "
                            f"Hiến pháp bắt buộc Quân sư phải thực hiện Pha 1 (Cross-Examination) trước: đối chiếu bằng chứng `file:line` độc lập và gửi ít nhất 1 câu hỏi thẩm định/phản biện qua 'orca orchestration ask' để kiểm chứng với Leader (về lỗ hổng logic/race-condition, ranh giới file Disjoint File Ownership giữa các dev-worker*, hoặc phạm vi vai trò dev/devops/qa/sec). "
                            f"Cách sửa bắt buộc ngay lúc này: Chạy lệnh:\n"
                            f"orca orchestration ask --from {from_hint} --question 'CRITIC_CHALLENGE vòng 1/3: [Bằng chứng file:line + 5 trục System Eng] | [Điểm cần chỉnh hoặc câu hỏi xác nhận ranh giới file/edge-case cho [P1..Pn]]' --timeout-ms 900000 --json\n"
                            f"Chỉ SAU KHI Leader trả lời qua 'orca orchestration reply' và hai bên thống nhất, bạn mới được gửi 'CONSENSUS_REACHED' + 'worker_done'!"
                        )
                    if answered_q == 0:
                        resume_flag = f"--resume {pending_mid} " if pending_mid else ""
                        return (
                            f"[ORCA CRITIC 2-WAY DEBATE GUARD] Câu hỏi phản biện '{pending_mid or 'CRITIC_CHALLENGE'}' của bạn đang ở trạng thái 'pending' (Leader chưa trả lời qua 'orca orchestration reply')! "
                            f"CẤM tự ý chốt 'Critic Consensus' hoặc 'worker_done' khi chưa nhận được phản hồi 2 chiều từ Leader. "
                            f"Cách sửa bắt buộc: Chạy lệnh chờ câu trả lời của Leader:\n"
                            f"orca orchestration ask --from {from_hint} {resume_flag}--timeout-ms 900000 --json"
                        )

                    # C-FIX-05: Validate Critic review JSON on handoff / verdict send
                    has_verdict_marker = bool(
                        re.search(
                            r"\b(?:CRITIC_VERDICT|CONSENSUS_REACHED|Critic\s+Consensus|Critic\s+Deadlock|DEADLOCK|CHANGES_REQUIRED)\b",
                            expanded_send,
                            re.IGNORECASE,
                        )
                        or re.search(r"--type(?:\s+|=)['\"]?handoff\b", no_heredoc)
                    )
                    if has_verdict_marker:
                        cg_mod = _load_critic_gate_module()
                        _, c_run_dir = _resolve_critic_run_dir(real_cwd, payload, args, resolved_run_id)
                        plan_text_c = ""
                        for pf_n in ("master-plan.md", "plan-draft.md"):
                            for base_d in (
                                os.path.join(real_cwd, ".orca", "runs", resolved_run_id or "default") if real_cwd else "",
                                c_run_dir,
                            ):
                                if base_d and os.path.isfile(os.path.join(base_d, pf_n)):
                                    plan_text_c = open(os.path.join(base_d, pf_n), "r", encoding="utf-8", errors="ignore").read()
                                    break
                            if plan_text_c:
                                break
                        if not plan_text_c:
                            return _handle_critic_gate_violation(
                                f"[ORCA PLAN BINDING GUARD] Không tìm thấy master-plan.md hoặc plan-draft.md cho Run '{resolved_run_id or 'active'}' (fail-closed)!",
                                resolved_run_id,
                                cmd,
                            )

                        body_unquoted = _extract_cli_flag_value(expanded_send, ("body", "content", "message"))
                        search_text_c = body_unquoted if body_unquoted else expanded_send
                        review_obj = cg_mod.extract_review_json_from_body(search_text_c)
                        if review_obj is None and search_text_c != expanded_send:
                            review_obj = cg_mod.extract_review_json_from_body(expanded_send)
                        if not isinstance(review_obj, dict):
                            return _handle_critic_gate_violation(
                                "[ORCA CRITIC SCHEMA GUARD] Critic handoff bắt buộc phải chứa khối ```json ... ``` chuẩn schema 'orca.critic.review/v1'!",
                                resolved_run_id,
                                cmd,
                            )

                        items_c = cg_mod.extract_items(plan_text_c)
                        contracts_c = cg_mod.contract_lines(plan_text_c)
                        risk_c = cg_mod.compute_risk(items_c, contracts_c)

                        # Load hypothesis.json and verify createdAt < first ask created_at
                        hyp_obj = None
                        for base_d in (
                            os.path.join(real_cwd, ".orca", "runs", resolved_run_id or "default") if real_cwd else "",
                            c_run_dir,
                        ):
                            hp = os.path.join(base_d, "critic", "hypothesis.json") if base_d else ""
                            if hp and os.path.isfile(hp):
                                try:
                                    hyp_obj = json.loads(open(hp, "r", encoding="utf-8", errors="ignore").read())
                                    break
                                except Exception:
                                    pass

                        if isinstance(hyp_obj, dict) and q_rows:
                            first_ask_ts = str(q_rows[0]["created_at"] or "").strip()
                            hyp_ts = str(hyp_obj.get("createdAt") or "").strip()
                            if first_ask_ts and hyp_ts and hyp_ts > first_ask_ts:
                                return _handle_critic_gate_violation(
                                    f"[ORCA CRITIC INDEPENDENCE GUARD] hypothesis.json được tạo SAU lần ask đầu tiên (hypothesis={hyp_ts} > ask={first_ask_ts})! Phải lập giả thuyết độc lập TRƯỚC khi đọc plan/ask.",
                                    resolved_run_id,
                                    cmd,
                                    str(review_obj.get("planHash") or ""),
                                )

                        # Gather valid reply IDs from messages and question_threads
                        valid_reply_ids: set[str] = set()
                        for qr in q_rows:
                            if qr["answer_message_id"]:
                                valid_reply_ids.add(str(qr["answer_message_id"]))
                            if str(qr["status"] or "") == "answered" and qr["message_id"]:
                                valid_reply_ids.add(str(qr["message_id"]))
                        if resolved_run_id:
                            rep_rows = conn.execute(
                                "SELECT id FROM messages WHERE run_id = ? AND (type IN ('reply','answer') OR LOWER(COALESCE(subject,'')) LIKE 're:%' OR LOWER(COALESCE(subject,'')) LIKE '%reply%')",
                                (resolved_run_id,),
                            ).fetchall()
                            for rr in rep_rows:
                                valid_reply_ids.add(str(rr["id"]))

                        viols = cg_mod.validate_review(review_obj, plan_text_c, risk_c, valid_reply_ids=valid_reply_ids)
                        viols.extend(cg_mod.validate_independence(review_obj, plan_text_c, hyp_obj, risk_c))

                        if viols:
                            return _handle_critic_gate_violation(
                                viols[0],
                                resolved_run_id,
                                cmd,
                                str(review_obj.get("planHash") or ""),
                            )

                        # If verdict == DEADLOCK, record deadlock=true in critic/audit.jsonl so C-FIX-07 blocks plan pin
                        if str(review_obj.get("verdict") or "") == "DEADLOCK" and c_run_dir:
                            try:
                                os.makedirs(os.path.join(c_run_dir, "critic"), exist_ok=True)
                                with open(os.path.join(c_run_dir, "critic", "audit.jsonl"), "a", encoding="utf-8") as af:
                                    af.write(
                                        json.dumps(
                                            {
                                                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                "runId": resolved_run_id,
                                                "planHash": review_obj.get("planHash"),
                                                "verdict": "DEADLOCK",
                                                "deadlock": True,
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "\n"
                                    )
                            except Exception:
                                pass

        # 2. Critic Review Depth Guard on Leader's `orca orchestration reply` (C12)
        if re.search(r"\borca\s+orchestration\s+reply\b", sanitized_orch):
            reply_id = _extract_cli_flag_value(no_heredoc, ("id", "message-id", "thread", "thread-id"))
            reply_body = _extract_cli_flag_value(no_heredoc, ("body",))
            if reply_id:
                q_info = conn.execute(
                    "SELECT qt.dispatch_id, m.body AS q_body, wd.worktree_id, t.spec "
                    "FROM question_threads qt "
                    "LEFT JOIN messages m ON m.id = qt.message_id "
                    "LEFT JOIN dispatch_contexts dc ON dc.id = qt.dispatch_id "
                    "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = qt.dispatch_id "
                    "LEFT JOIN tasks t ON t.id = dc.task_id "
                    "WHERE qt.message_id = ? LIMIT 1",
                    (reply_id,),
                ).fetchone()
                is_critic_q = False
                if q_info:
                    wt_s = _extract_canonical_worker_slot(os.path.basename(str(q_info["worktree_id"] or "")))
                    q_body_str = str(q_info["q_body"] or "")
                    t_spec_str = str(q_info["spec"] or "")
                    if (
                        (wt_s and wt_s.startswith("critic-worker"))
                        or "CRITIC_CHALLENGE" in q_body_str
                        or "[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC" in t_spec_str
                    ):
                        is_critic_q = True
                if is_critic_q and len(reply_body.strip()) < 80:
                    return _handle_critic_gate_violation(
                        f"[ORCA CRITIC REVIEW DEPTH GUARD] Nội dung trả lời 'orca orchestration reply --id {reply_id}' cho Quân sư 'critic-worker' quá ngắn ({len(reply_body.strip())} ký tự < 80 ký tự tối thiểu)! "
                        f"CẤM trả lời qua loa ('ok', 'đồng ý') khi tranh biện kiến trúc 2 chiều. "
                        f"Cách sửa bắt buộc: Trả lời chi tiết (>= 80 ký tự) xác nhận rõ: (1) Chấp thuận hay phản biện các điểm `file:line` của Critic, "
                        f"(2) Chốt Checklist `[P1..Pn]` + phân chia Disjoint File Ownership giữa các `dev-worker*`, và (3) Ranh giới vai trò (`dev`/`devops`/`qa`/`sec`).",
                        "",
                        cmd,
                    )

        # 4. Pending-Question Guard + C-FIX-06 Plan Binding + C-FIX-07 Deadlock Guard on Leader (`orca-team plan pin`, `orca orchestration worker-start`, or `wait-events`)
        is_in_main_root = bool(real_cwd and not cwd_slot)
        if is_in_main_root or is_plan_pin or is_worker_start:
            is_wait_events = bool(
                re.search(r"\borca\s+orchestration\s+check\b[^\n;|&]*\s--wait\b", sanitized_orch)
                or re.search(r"\borca-team\b[^\n;|&]*\b(?:wait-events|check-wait)\b", sanitized_orch)
            )
            if is_plan_pin or is_worker_start or is_wait_events:
                explicit_run_id = _extract_cli_flag_value(no_heredoc, ("run", "run-id"))
                ws_disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
                ws_task_id = _extract_cli_flag_value(no_heredoc, ("task-id", "task"))
                if not explicit_run_id and ws_disp_id:
                    r_ws_d = conn.execute("SELECT run_id FROM dispatch_contexts WHERE id = ? LIMIT 1", (ws_disp_id,)).fetchone()
                    if r_ws_d:
                        explicit_run_id = str(r_ws_d["run_id"] or "")
                if not explicit_run_id and ws_task_id:
                    r_ws_t = conn.execute("SELECT run_id FROM tasks WHERE id = ? LIMIT 1", (ws_task_id,)).fetchone()
                    if r_ws_t:
                        explicit_run_id = str(r_ws_t["run_id"] or "")
                if not explicit_run_id:
                    r_active = conn.execute(
                        "SELECT dc.run_id FROM dispatch_contexts dc "
                        "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                        "WHERE (wd.worktree_id LIKE ? OR wd.worktree_id LIKE ?) "
                        "ORDER BY dc.created_at DESC LIMIT 1",
                        (f"{real_cwd}%", f"%{os.path.basename(real_cwd)}%"),
                    ).fetchone()
                    if r_active:
                        explicit_run_id = str(r_active["run_id"] or "")
                if not explicit_run_id:
                    r_latest_run = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                    if r_latest_run:
                        explicit_run_id = str(r_latest_run["id"] or "")

                if explicit_run_id:
                    pend_qs = conn.execute(
                        "SELECT qt.message_id, qt.dispatch_id, qt.asker_handle, m.read AS msg_read, "
                        "m.delivered_at AS msg_delivered_at, m.body AS msg_body, wd.worktree_id, t.spec "
                        "FROM question_threads qt "
                        "LEFT JOIN messages m ON m.id = qt.message_id "
                        "LEFT JOIN dispatch_contexts dc ON dc.id = qt.dispatch_id "
                        "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = qt.dispatch_id "
                        "LEFT JOIN tasks t ON t.id = dc.task_id "
                        "WHERE qt.run_id = ? AND qt.status = 'pending' AND qt.answered_at IS NULL "
                        "ORDER BY qt.created_at ASC",
                        (explicit_run_id,),
                    ).fetchall()
                    if pend_qs:
                        pq = pend_qs[0]
                        p_mid = str(pq["message_id"] or "")
                        p_wt = _extract_canonical_worker_slot(os.path.basename(str(pq["worktree_id"] or "")))
                        if not p_wt and "[ROLE: PRINCIPAL RESEARCH, ADVERSARIAL CRITIC" in str(pq["spec"] or ""):
                            p_wt = "critic-worker"
                        p_wt = p_wt or str(pq["dispatch_id"] or "worker")
                        p_snip = str(pq["msg_body"] or "").strip().replace("\n", " ")[:180]
                        already_delivered = bool(
                            (pq["msg_read"] is not None and int(pq["msg_read"] or 0) == 1)
                            or pq["msg_delivered_at"] is not None
                            or re.search(r"\s--ack(?:\s+|=)", no_heredoc)
                        )
                        if is_plan_pin and p_wt.startswith("critic-worker"):
                            return (
                                f"[ORCA PENDING-QUESTION GUARD] Quân sư '{p_wt}' đang chờ bạn trả lời câu hỏi phản biện (Question ID: {p_mid}) trong Run '{explicit_run_id}'! "
                                f"CẤM chạy 'orca-team plan pin' trước khi trả lời câu hỏi của Critic và chốt CONSENSUS_REACHED. "
                                f"Nội dung câu hỏi: \"{p_snip}\". "
                                f"Cách sửa bắt buộc: Chạy 'orca orchestration reply --id {p_mid} --body \"<Phản hồi chi tiết >= 60 ký tự>\" --json', chờ Critic xác nhận CONSENSUS_REACHED rồi mới ghim 'orca-team plan pin'!"
                            )
                        if is_worker_start:
                            return (
                                f"[ORCA PENDING-QUESTION GUARD] Worker '{p_wt}' đang chặn chờ bạn trả lời câu hỏi 'ask' (Question ID: {p_mid}) trong Run '{explicit_run_id}'! "
                                f"CẤM khởi chạy thêm Worker mới khi câu hỏi phản biện/kiến trúc hiện tại chưa được trả lời. "
                                f"Nội dung câu hỏi: \"{p_snip}\". "
                                f"Cách sửa bắt buộc: Chạy 'orca orchestration reply --id {p_mid} --body \"<Phản hồi chi tiết>\" --json' trước!"
                            )
                        if is_wait_events and already_delivered:
                            return (
                                f"[ORCA PENDING-QUESTION GUARD] Bạn đã nhận được câu hỏi 'question' (ID: {p_mid}) từ '{p_wt}' nhưng CHƯA trả lời qua 'orca orchestration reply'! "
                                f"Nếu bạn gọi 'orca-team wait-events' lúc này, cả Leader và '{p_wt}' sẽ bị DEADLOCK (hai bên cùng chờ nhau)! "
                                f"Nội dung câu hỏi: \"{p_snip}\". "
                                f"Cách sửa bắt buộc: Trả lời ngay bằng lệnh:\n"
                                f"orca orchestration reply --id {p_mid} --body \"<Nội dung phản biện/quyết định chi tiết >= 60 ký tự>\" --json\n"
                                f"Sau khi chạy 'reply' xong, bạn mới gọi 'orca-team wait-events' để chờ kết quả tiếp theo từ '{p_wt}'!"
                            )

                # C-FIX-06 & C-FIX-07: Plan Binding & Deadlock Escalation on `plan pin` and `worker-start` (dev/ui)
                is_starting_critic_or_re = bool(
                    is_worker_start
                    and re.search(r"\b(?:critic-worker|criticSpecialist|critic|re-worker|reSpecialist)\b", no_heredoc)
                )
                if is_plan_pin or (is_worker_start and not is_starting_critic_or_re):
                    cg_mod = _load_critic_gate_module()
                    proj_root_p, pin_run_dir = _resolve_critic_run_dir(real_cwd, payload, args, explicit_run_id)

                    # Legacy override check (only if no critic dispatch exists in run and before cutoff)
                    legacy_env = os.environ.get("ORCA_CRITIC_GATE_LEGACY", "").strip() == "1"
                    if legacy_env and explicit_run_id:
                        has_critic_dc = conn.execute(
                            "SELECT 1 FROM dispatch_contexts dc LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                            "WHERE dc.run_id = ? AND (wd.worktree_id LIKE '%critic%' OR dc.assignee_handle LIKE '%critic%') LIMIT 1",
                            (explicit_run_id,),
                        ).fetchone()
                        if not has_critic_dc:
                            return ""

                    # 1. Check Deadlock flag in audit.jsonl or consensus.json (C-FIX-07 / C07)
                    audit_p = os.path.join(pin_run_dir, "critic", "audit.jsonl") if pin_run_dir else ""
                    if audit_p and os.path.isfile(audit_p):
                        try:
                            audit_lines = [ln.strip() for ln in open(audit_p, "r", encoding="utf-8", errors="ignore") if ln.strip()]
                            if audit_lines:
                                last_aud = json.loads(audit_lines[-1])
                                if isinstance(last_aud, dict) and (
                                    last_aud.get("deadlock") is True or str(last_aud.get("verdict") or "") == "DEADLOCK"
                                ):
                                    unres_list = last_aud.get("unresolvedObjections") or []
                                    claim_hint = unres_list[0].get("claim") if (unres_list and isinstance(unres_list[0], dict)) else "unresolved blocker"
                                    item_hint = unres_list[0].get("item") if (unres_list and isinstance(unres_list[0], dict)) else "P1"
                                    return _handle_critic_gate_violation(
                                        f"[ORCA CRITIC DEADLOCK GUARD] Còn bất đồng chưa giải quyết ([{item_hint}]: {claim_hint}). "
                                        f"Mở human gate: orca-team gate create --from-consensus {explicit_run_id or '<run>'} "
                                        f"--question \"Critic bất đồng [{item_hint}]: {claim_hint}. Chọn hướng?\" --options '[\"A\",\"B\",\"human_review\"]'",
                                        explicit_run_id,
                                        cmd,
                                    )
                        except Exception:
                            pass

                    contracts_p = os.path.join(pin_run_dir, "contracts.json") if pin_run_dir else ""
                    draft_contracts_p = os.path.join(pin_run_dir, "plan-draft.contracts.json") if pin_run_dir else ""
                    contracts_obj = None
                    all_contracts_objs: list[dict] = []
                    for cp_cand in (contracts_p, draft_contracts_p):
                        if cp_cand and os.path.isfile(cp_cand):
                            try:
                                loaded_c = json.loads(open(cp_cand, "r", encoding="utf-8", errors="ignore").read())
                                if isinstance(loaded_c, dict):
                                    all_contracts_objs.append(loaded_c)
                                    if contracts_obj is None:
                                        contracts_obj = loaded_c
                            except Exception:
                                pass
                    if not isinstance(contracts_obj, dict):
                        return _handle_critic_gate_violation(
                            f"[ORCA PLAN BINDING GUARD] Thiếu contracts.json cho Run '{explicit_run_id or 'active'}'! Chạy 'orca-team plan draft' và Critic Consensus trước.",
                            explicit_run_id,
                            cmd,
                        )

                    # Read current plan text (from --file / --content if passed to plan pin, else master-plan.md / plan-draft.md)
                    plan_candidates: list[str] = []
                    pin_file_arg = _extract_cli_flag_value(no_heredoc, ("file", "f"))
                    pin_content_arg = _extract_cli_flag_value(no_heredoc, ("content", "c", "body"))
                    if is_plan_pin and pin_file_arg and os.path.isfile(os.path.expanduser(pin_file_arg)):
                        plan_candidates.append(open(os.path.expanduser(pin_file_arg), "r", encoding="utf-8", errors="ignore").read())
                    elif is_plan_pin and pin_content_arg:
                        plan_candidates.append(pin_content_arg.replace("\\n", "\n"))
                    for pf_name in ("master-plan.md", "plan-draft.md"):
                        pf_p = os.path.join(pin_run_dir, pf_name) if pin_run_dir else ""
                        if pf_p and os.path.isfile(pf_p):
                            plan_candidates.append(open(pf_p, "r", encoding="utf-8", errors="ignore").read())

                    if not plan_candidates:
                        return _handle_critic_gate_violation(
                            f"[ORCA PLAN BINDING GUARD] Không tìm thấy file kế hoạch (master-plan.md / plan-draft.md) cho Run '{explicit_run_id}'!",
                            explicit_run_id,
                            cmd,
                        )

                    cons_p = os.path.join(pin_run_dir, "consensus.json") if pin_run_dir else ""
                    if not (cons_p and os.path.isfile(cons_p)):
                        h_now = cg_mod.plan_hash(plan_candidates[0])
                        return _handle_critic_gate_violation(
                            f"[ORCA PLAN BINDING GUARD] Chưa có Critic Consensus cho plan_hash {h_now} (Run '{explicit_run_id}').",
                            explicit_run_id,
                            cmd,
                            h_now,
                        )

                    try:
                        cons_obj = json.loads(open(cons_p, "r", encoding="utf-8", errors="ignore").read())
                    except Exception:
                        cons_obj = None

                    for p_txt in plan_candidates:
                        for c_dict in all_contracts_objs:
                            c_viols = cg_mod.validate_consensus_file(cons_obj, c_dict, p_txt)
                            if c_viols:
                                return _handle_critic_gate_violation(
                                    c_viols[0],
                                    explicit_run_id,
                                    cmd,
                                    cg_mod.plan_hash(p_txt),
                                )

                    # C-FIX-21 & C-FIX-24: Verify messageIds + question_threads provenance in SQLite DB (no hardcoded 'critic-worker'!)
                    msg_ids = (cons_obj or {}).get("messageIds") or {}
                    ch_ids = [str(x) for x in (msg_ids.get("challenges") or []) if str(x).strip()]
                    rp_ids = [str(x) for x in (msg_ids.get("replies") or []) if str(x).strip()]
                    fin_id = str(msg_ids.get("final") or "").strip()

                    critic_dids_db: set[str] = set()
                    critic_handles_db: set[str] = set()
                    if explicit_run_id:
                        dc_c_rows = conn.execute(
                            "SELECT dc.id, dc.assignee_handle, wd.agent_terminal_handle, wd.worktree_id, t.task_title, t.spec "
                            "FROM dispatch_contexts dc "
                            "INNER JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                            "LEFT JOIN tasks t ON t.id = dc.task_id "
                            "WHERE dc.run_id = ?",
                            (explicit_run_id,),
                        ).fetchall()
                        for r_c in dc_c_rows:
                            t_spec_up = str(r_c["spec"] or "").upper()
                            t_title_low = str(r_c["task_title"] or "").strip().lower()
                            is_critic_role = (
                                "[ROLE: PRINCIPAL RESEARCH" in t_spec_up
                                or "ADVERSARIAL CRITIC" in t_spec_up
                                or bool(re.match(r"^(?:t-)?critic(?:[-_\s\d:]|$)", t_title_low))
                            )
                            t_h = str(r_c["agent_terminal_handle"] or "").strip()
                            if is_critic_role and t_h:
                                critic_dids_db.add(str(r_c["id"]))
                                critic_handles_db.add(t_h)

                    cons_critic_did = str((cons_obj or {}).get("criticDispatchId") or "").strip()
                    if not critic_dids_db or not critic_handles_db or (cons_critic_did and cons_critic_did not in critic_dids_db):
                        return _handle_critic_gate_violation(
                            f"[ORCA CONSENSUS PROVENANCE GUARD] Không tìm thấy dispatch critic hợp lệ cho Run '{explicit_run_id}' (criticDispatchId='{cons_critic_did}').",
                            explicit_run_id,
                            cmd,
                            str((cons_obj or {}).get("planHash") or ""),
                        )

                    qt_rows_db = conn.execute(
                        "SELECT message_id, dispatch_id, asker_handle, status, answer_message_id FROM question_threads WHERE run_id = ?",
                        (explicit_run_id,),
                    ).fetchall()
                    qt_by_challenge: dict[str, dict] = {}
                    for q_r in qt_rows_db:
                        q_did = str(q_r["dispatch_id"] or "").strip()
                        q_asker = str(q_r["asker_handle"] or "").strip()
                        q_st = str(q_r["status"] or "").strip().lower()
                        q_mid = str(q_r["message_id"] or "").strip()
                        q_ans = str(q_r["answer_message_id"] or "").strip()
                        if (
                            q_did in critic_dids_db
                            and q_asker in critic_handles_db
                            and q_st in ("answered", "resolved", "closed")
                            and q_mid
                            and q_ans
                        ):
                            qt_by_challenge[q_mid] = dict(q_r)

                    challenge_created_ats: list[str] = []
                    for cid in ch_ids:
                        m_row = conn.execute("SELECT from_handle, type, body, created_at FROM messages WHERE id = ? AND run_id = ? LIMIT 1", (cid, explicit_run_id)).fetchone()
                        if (
                            not m_row
                            or str(m_row["type"] or "").lower() != "question"
                            or str(m_row["from_handle"] or "") not in critic_handles_db
                            or "CRITIC_CHALLENGE" not in str(m_row["body"] or "")
                            or cid not in qt_by_challenge
                        ):
                            return _handle_critic_gate_violation(
                                f"[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không khớp message log / question_threads (challenge '{cid}' không hợp lệ hoặc không xuất phát từ lệnh ask của critic).",
                                explicit_run_id,
                                cmd,
                                str((cons_obj or {}).get("planHash") or ""),
                            )
                        if m_row["created_at"]:
                            challenge_created_ats.append(str(m_row["created_at"]))

                    expected_ans_to_ch = {
                        str(qt_by_challenge[cid]["answer_message_id"]): cid
                        for cid in ch_ids
                        if cid in qt_by_challenge
                    }

                    for rid in rp_ids:
                        m_row = conn.execute(
                            "SELECT from_handle, to_handle, type, subject, body, thread_id FROM messages WHERE id = ? AND run_id = ? LIMIT 1",
                            (rid, explicit_run_id),
                        ).fetchone()
                        if (
                            not m_row
                            or str(m_row["from_handle"] or "").lower() not in ("leader", "coordinator", "orca")
                            or str(m_row["to_handle"] or "") not in critic_handles_db
                            or rid not in expected_ans_to_ch
                            or str(m_row["thread_id"] or "") != expected_ans_to_ch[rid]
                            or len(str(m_row["body"] or "").strip()) < 60
                        ):
                            return _handle_critic_gate_violation(
                                f"[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không khớp message log (reply '{rid}' không phải phản hồi hợp lệ từ Leader cho thread challenge của Critic).",
                                explicit_run_id,
                                cmd,
                                str((cons_obj or {}).get("planHash") or ""),
                            )

                    f_row = conn.execute("SELECT from_handle, type, body, payload FROM messages WHERE id = ? AND run_id = ? LIMIT 1", (fin_id, explicit_run_id)).fetchone()
                    if (
                        not f_row
                        or str(f_row["type"] or "").lower() not in ("handoff", "worker_done")
                        or str(f_row["from_handle"] or "") not in critic_handles_db
                        or "CRITIC_VERDICT" not in str(f_row["body"] or "")
                    ):
                        return _handle_critic_gate_violation(
                            f"[ORCA CONSENSUS PROVENANCE GUARD] consensus.json không khớp message log (thiếu {fin_id} / sai sender).",
                            explicit_run_id,
                            cmd,
                            str((cons_obj or {}).get("planHash") or ""),
                        )

                    # C-FIX-22 & C-FIX-23: Re-validate review JSON extracted EXCLUSIVELY from f_row["body"] + re-hash hypothesis.md
                    rev_from_msg = cg_mod.extract_review_json_from_body(str(f_row["body"] or ""))
                    if not isinstance(rev_from_msg, dict):
                        return _handle_critic_gate_violation(
                            f"[ORCA CONSENSUS PROVENANCE GUARD] Bản tin final '{fin_id}' không chứa khối JSON review hợp lệ trong body!",
                            explicit_run_id,
                            cmd,
                            str((cons_obj or {}).get("planHash") or ""),
                        )
                    hyp_p = os.path.join(pin_run_dir, "critic", "hypothesis.json") if pin_run_dir else ""
                    hyp_md_p = os.path.join(pin_run_dir, "critic", "hypothesis.md") if pin_run_dir else ""
                    hyp_obj_pin = None
                    if hyp_p and os.path.isfile(hyp_p):
                        try:
                            hyp_obj_pin = json.loads(open(hyp_p, "r", encoding="utf-8", errors="ignore").read())
                        except Exception:
                            hyp_obj_pin = None
                    hyp_md_txt_pin = open(hyp_md_p, "r", encoding="utf-8", errors="ignore").read() if (hyp_md_p and os.path.isfile(hyp_md_p)) else ""
                    first_ch_at = min(challenge_created_ats) if challenge_created_ats else ""

                    for p_txt in plan_candidates:
                        comp_risk = cg_mod.compute_risk(cg_mod.extract_items(p_txt), cg_mod.contract_lines(p_txt))
                        rev_viols = cg_mod.validate_review(rev_from_msg, p_txt, comp_risk, valid_reply_ids=set(rp_ids))
                        rev_viols.extend(
                            cg_mod.validate_independence(
                                rev_from_msg,
                                p_txt,
                                hyp_obj_pin,
                                comp_risk,
                                hypothesis_md_text=hyp_md_txt_pin,
                                first_challenge_at=first_ch_at,
                            )
                        )
                        if rev_viols:
                            return _handle_critic_gate_violation(
                                rev_viols[0],
                                explicit_run_id,
                                cmd,
                                cg_mod.plan_hash(p_txt),
                            )

    except Exception as exc:
        if is_critic_cmd or is_plan_pin or is_worker_start:
            return _handle_critic_gate_violation(
                f"[ORCA CRITIC FAIL-CLOSED GUARD] Lỗi nội bộ khi xác minh Critic Gate ({exc})! Thực thi Fail-Closed.",
                "",
                cmd,
            )
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return ""


def _visual_inspection_cache_path() -> str:
    return f"/tmp/orca-visual-inspected-{os.getuid()}.json"


def _record_visual_image_inspection(payload: dict, args: dict, tool_name: str = "") -> None:
    """Record when an Agent CLI calls `view_file` / `read_file` / `Read` on an image (`.png`, `.jpg`, `.jpeg`, `.webp`)."""
    if not isinstance(args, dict):
        return
    raw_paths: list[str] = []
    for k in ("Path", "AbsolutePath", "file_path", "filePath", "path", "image_path", "imagePath", "TargetFile"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            raw_paths.append(v.strip())
    paths_arr = args.get("paths")
    if isinstance(paths_arr, list):
        for item in paths_arr:
            if isinstance(item, str) and item.strip():
                raw_paths.append(item.strip())
    img_Exts = (".png", ".jpg", ".jpeg", ".webp")
    cand_imgs = [p for p in raw_paths if p.lower().endswith(img_Exts)]
    if not cand_imgs:
        return
    try:
        _, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
        cache_p = _visual_inspection_cache_path()
        data: dict[str, float] = {}
        if os.path.isfile(cache_p):
            try:
                with open(cache_p, "r", encoding="utf-8") as cf:
                    loaded = json.load(cf)
                if isinstance(loaded, dict):
                    data = {str(k): float(v) for k, v in loaded.items() if isinstance(v, (int, float))}
            except Exception:
                data = {}
        now_ts = time.time()
        cutoff = now_ts - 86400.0
        data = {k: v for k, v in data.items() if v >= cutoff}
        for raw_p in cand_imgs:
            abs_p = raw_p if os.path.isabs(raw_p) else os.path.abspath(os.path.join(cwd or os.getcwd(), raw_p))
            real_p = os.path.realpath(abs_p)
            data[real_p] = now_ts
            data[abs_p] = now_ts
            data[os.path.basename(real_p)] = now_ts
        tmp_p = f"{cache_p}.{os.getpid()}.tmp"
        with open(tmp_p, "w", encoding="utf-8") as tf:
            json.dump(data, tf)
        os.replace(tmp_p, cache_p)
    except Exception:
        pass


def _check_png_uniformity(path: str) -> tuple[float, int, int] | None:
    """
    Pure-Python fast PNG scanline uniformity detector (`struct` + `zlib`).
    Returns `(uniform_row_ratio, width, height)` or `None` if not a standard 8-bit PNG.
    Broken black/white/collapsed screenshots score `>= 0.92` (`92%+` uniform scanlines).
    """
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > 15 * 1024 * 1024:
            return None
        with open(path, "rb") as pf:
            data = pf.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        w = h = bit_depth = color_type = 0
        idat = bytearray()
        n = len(data)
        while pos + 8 <= n:
            length = int.from_bytes(data[pos : pos + 4], "big")
            ctype = data[pos + 4 : pos + 8]
            chunk = data[pos + 8 : pos + 8 + length]
            if ctype == b"IHDR" and len(chunk) >= 10:
                w = int.from_bytes(chunk[0:4], "big")
                h = int.from_bytes(chunk[4:8], "big")
                bit_depth = chunk[8]
                color_type = chunk[9]
                if bit_depth != 8 or color_type not in (0, 2, 3, 4, 6) or w <= 0 or h <= 0:
                    return None
            elif ctype == b"IDAT":
                idat.extend(chunk)
            elif ctype == b"IEND":
                break
            pos += 12 + length
        if not idat or w <= 0 or h <= 0:
            return None
        import zlib

        raw = zlib.decompress(bytes(idat))
        bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 4)
        stride = 1 + w * bpp
        if len(raw) < h * stride:
            return None
        zero_row = b"\x00" * (w * bpp)
        uniform_rows = 0
        for y in range(h):
            r_start = y * stride
            ftype = raw[r_start]
            pixels = raw[r_start + 1 : r_start + stride]
            if ftype == 0:
                first_px = pixels[:bpp]
                if pixels == first_px * w:
                    uniform_rows += 1
            elif ftype in (1, 2) and pixels == zero_row:
                uniform_rows += 1
        return (uniform_rows / float(h), w, h)
    except Exception:
        return None


def _get_guard_mode() -> str:
    """Return active guard mode: 'enforce' (default), 'shadow', or 'off'."""
    m = os.environ.get("ORCA_GUARD_MODE", "enforce").strip().lower()
    if m in ("shadow", "off", "enforce"):
        return m
    return "enforce"


def _log_guard_decision(decision: str, reason: str, cmd: str, slot: str = "") -> None:
    """Append guard decision telemetry to ~/.orca/guard-log.jsonl (or ORCA_GUARD_LOG)."""
    try:
        log_p = (os.environ.get("ORCA_GUARD_LOG") or os.environ.get("ORCA_GUARD_LOG_FILE") or "").strip()
        if not log_p:
            log_p = os.path.join(os.path.expanduser("~"), ".orca", "guard-log.jsonl")
        os.makedirs(os.path.dirname(log_p), exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": _get_guard_mode(),
            "slot": slot or "",
            "decision": decision,
            "reason": (reason or "")[:500],
            "cmd": (cmd or "")[:400],
        }
        with open(log_p, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _detect_caller_slot_safe(payload: dict, args: dict, cmd: str = "") -> tuple[str, str, str]:
    """
    Slot-first detector (FIX-01): safely extract (raw_cwd_in, real_cwd, cwd_slot)
    before any regex or project root check so fail-closed logic always knows the caller slot.
    """
    raw_cwd_in = ""
    real_cwd = ""
    cwd_slot = ""
    try:
        if isinstance(args, dict):
            raw_cwd_in = str(args.get("Cwd") or args.get("cwd") or "").strip()
        if not raw_cwd_in and isinstance(payload, dict):
            raw_cwd_in = str(payload.get("cwd") or "").strip()
        _, cwd = _resolve_orca_team_bin_and_cwd(payload if isinstance(payload, dict) else {}, args if isinstance(args, dict) else {})
        real_cwd = os.path.realpath(cwd) if cwd else (os.path.realpath(raw_cwd_in) if raw_cwd_in else "")
    except Exception:
        real_cwd = raw_cwd_in
    try:
        env_slot = os.environ.get("ORCA_SLOT", "").strip()
        cwd_slot = (
            (_extract_canonical_worker_slot(env_slot) if env_slot else "")
            or (_extract_canonical_worker_slot(os.path.basename(raw_cwd_in.rstrip("/"))) if raw_cwd_in else "")
            or (_extract_canonical_worker_slot(os.path.basename(real_cwd.rstrip("/"))) if real_cwd else "")
        )
        if not cwd_slot and cmd:
            no_hd = _strip_quoted_heredocs(cmd)
            m_from = re.search(
                r"(?:^|[;&|()\n])\s*(?:\S*/)?orca\s+orchestration\s+send\b[^\n;|&]*--from(?:\s+|=)['\"]?([a-z]+-worker(?:-\d+)?)\b",
                no_hd,
                re.IGNORECASE,
            )
            if m_from:
                cwd_slot = _extract_canonical_worker_slot(m_from.group(1))
            if not cwd_slot and re.search(r"(?:^|[;&|()\n])\s*(?:\S*/)?orca\s+orchestration\s+send\b", no_hd, re.IGNORECASE):
                disp_q = _extract_cli_flag_value(no_hd, ("dispatch-id", "dispatch"))
                task_q = _extract_cli_flag_value(no_hd, ("task-id", "task"))
                db_p = _find_orca_orchestration_db_path()
                if (disp_q or task_q) and db_p and os.path.isfile(db_p):
                    try:
                        import sqlite3

                        conn_s = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True, timeout=1.5)
                        conn_s.row_factory = sqlite3.Row
                        row_s = None
                        if disp_q:
                            row_s = conn_s.execute(
                                "SELECT dc.assignee_handle, wd.agent_terminal_handle, wd.worktree_id "
                                "FROM dispatch_contexts dc LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                                "WHERE dc.id = ? LIMIT 1",
                                (disp_q,),
                            ).fetchone()
                        if not row_s and task_q:
                            row_s = conn_s.execute(
                                "SELECT dc.assignee_handle, wd.agent_terminal_handle, wd.worktree_id "
                                "FROM dispatch_contexts dc LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                                "WHERE dc.task_id = ? ORDER BY dc.created_at DESC LIMIT 1",
                                (task_q,),
                            ).fetchone()
                        conn_s.close()
                        if row_s:
                            for cand_h in (row_s["assignee_handle"], row_s["agent_terminal_handle"], row_s["worktree_id"]):
                                s_cand = _extract_canonical_worker_slot(str(cand_h or ""))
                                if s_cand:
                                    cwd_slot = s_cand
                                    break
                    except Exception:
                        pass
    except Exception:
        pass
    return raw_cwd_in, real_cwd, cwd_slot


def _expand_inline_cat_substitutions(cmd: str, real_cwd: str) -> tuple[str, list[str]]:
    """
    Expand `$(cat <file>)` or `` `cat <file>` `` inside `cmd` against `real_cwd` (FIX-01)
    so indirect `--body "$(cat QA_REPORT.txt)"` cannot hide report contents from the guard.
    Returns `(expanded_cmd, unresolved_files)`.
    """
    unresolved: list[str] = []
    if not cmd or "cat " not in cmd:
        return cmd, unresolved

    def _repl(m: re.Match) -> str:
        raw_arg = (m.group(1) or "").strip().strip("'\"")
        if not raw_arg or raw_arg.startswith("$"):
            unresolved.append(raw_arg or "$VAR")
            return m.group(0)
        cand_p = raw_arg if os.path.isabs(raw_arg) else os.path.join(real_cwd or os.getcwd(), raw_arg)
        try:
            if os.path.isfile(cand_p) and os.path.getsize(cand_p) <= 512 * 1024:
                with open(cand_p, "r", encoding="utf-8", errors="ignore") as cf:
                    return cf.read()
            unresolved.append(raw_arg)
        except Exception:
            unresolved.append(raw_arg)
        return m.group(0)

    expanded = re.sub(r"\$\(\s*cat\s+([^)\n;|&]+)\)", _repl, cmd)
    expanded = re.sub(r"`\s*cat\s+([^`\n;|&]+)`", _repl, expanded)
    return expanded, unresolved


def _inspect_test_report_file(fp: str, real_cwd: str) -> tuple[bool, str]:
    """
    Multi-format test report inspector (FIX-03):
    Supports Jest/Vitest, Playwright, JUnit XML, pytest lastfailed/JSON, Go test JSONL, TAP, and internal JSON schema.
    Never skips files > 5MB (uses streaming XML iterparse and chunked fallback for oversized JSON).
    Returns `(has_positive_pass_evidence, failure_error_message)`.
    """
    rel_fp = os.path.relpath(fp, real_cwd) if real_cwd else os.path.basename(fp)
    low = os.path.basename(fp).lower()

    def _fmt_err(detail: str) -> str:
        return (
            f"[ORCA QA REPORT-VERDICT MISMATCH GUARD] File báo cáo '{rel_fp}' được tạo/cập nhật trong phiên này đang ghi nhận kết quả THẤT BẠI ({detail}), "
            f"nhưng bạn lại đang gửi tin nhắn báo cáo PASS / succeeded! "
            f"CẤM ngụy tạo kết quả 'QA_VERDICT: PASS' hoặc tự ý bỏ qua lỗi trong file report. "
            f"Cách sửa bắt buộc: Nếu lỗi là bug thật của hệ thống, hãy đổi báo cáo sang 'QA_VERDICT: FAIL' (hoặc '--type escalation' / 'QA_REJECT'); "
            f"nếu file '{rel_fp}' là kết quả nháp cũ trước khi fix, hãy chạy lại test qua 'orca-team verify' để cập nhật '{rel_fp}' đạt PASS 100% trước khi gửi!"
        )

    try:
        fsize = os.path.getsize(fp)
    except OSError:
        return False, ""

    # 1. JUnit XML (`.xml`) via streaming iterparse (no 5MB limit)
    if low.endswith(".xml"):
        try:
            import xml.etree.ElementTree as ET

            total_tests = 0
            total_failures = 0
            total_errors = 0
            failed_cases: list[str] = []
            saw_suite = False
            for _ev, elem in ET.iterparse(fp, events=("end",)):
                tag = elem.tag.split("}")[-1].lower()
                if tag in ("testsuite", "testsuites"):
                    saw_suite = True
                    try:
                        total_tests += int(elem.attrib.get("tests") or 0)
                    except ValueError:
                        pass
                    try:
                        total_failures += int(elem.attrib.get("failures") or 0)
                    except ValueError:
                        pass
                    try:
                        total_errors += int(elem.attrib.get("errors") or 0)
                    except ValueError:
                        pass
                elif tag in ("failure", "error"):
                    total_failures += 1
                elif tag == "testcase":
                    for child in list(elem):
                        ctag = child.tag.split("}")[-1].lower()
                        if ctag in ("failure", "error"):
                            tc_name = elem.attrib.get("name") or elem.attrib.get("classname") or "testcase"
                            if tc_name not in failed_cases:
                                failed_cases.append(tc_name)
                elem.clear()
            if total_failures > 0 or total_errors > 0 or failed_cases:
                return False, _fmt_err(f"JUnit XML failures={total_failures}, errors={total_errors}")
            if saw_suite and total_tests > 0:
                return True, ""
        except Exception:
            pass
        return False, ""

    # 2. TAP format (`.tap`)
    if low.endswith(".tap"):
        try:
            ok_cnt = 0
            not_ok_lines: list[str] = []
            with open(fp, "r", encoding="utf-8", errors="ignore") as tf:
                for raw_ln in tf:
                    ln = raw_ln.strip()
                    if re.match(r"^not\s+ok\b", ln, re.IGNORECASE) and not re.search(r"#\s*(?:TODO|SKIP)\b", ln, re.IGNORECASE):
                        not_ok_lines.append(ln[:80])
                    elif re.match(r"^ok\b", ln, re.IGNORECASE):
                        ok_cnt += 1
            if not_ok_lines:
                return False, _fmt_err(f"TAP not ok ({len(not_ok_lines)} failed: {not_ok_lines[0]})")
            if ok_cnt > 0:
                return True, ""
        except Exception:
            pass
        return False, ""

    # 3. Pytest `.pytest_cache/v/cache/lastfailed`
    if low == "lastfailed":
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as lf:
                lf_obj = json.load(lf)
            if isinstance(lf_obj, dict) and any(bool(v) for v in lf_obj.values()):
                keys = [str(k) for k, v in lf_obj.items() if v][:4]
                return False, _fmt_err(f"pytest lastfailed={len(lf_obj)} ({', '.join(keys)})")
        except Exception:
            pass
        return False, ""

    # 4. JSON / JSONL reports (Jest, Vitest, Playwright, Pytest JSON, Go test -json, Internal schema)
    if low.endswith(".json") or low.endswith(".jsonl"):
        try:
            if fsize > 25 * 1024 * 1024:
                # Stream regex scan for oversized JSON (>25MB) so we never skip large reports
                with open(fp, "r", encoding="utf-8", errors="ignore") as big_f:
                    chunk = big_f.read(2 * 1024 * 1024)
                if re.search(r'"(?:numFailedTests|numFailedTestSuites|numRuntimeErrorTestSuites|unexpected|failed)"\s*:\s*[1-9]\d*', chunk):
                    return False, _fmt_err("oversized JSON report has failed/unexpected > 0")
                if re.search(r'"success"\s*:\s*false\b', chunk) or re.search(r'"Action"\s*:\s*"fail"', chunk):
                    return False, _fmt_err("oversized JSON report has success=false or Action=fail")
                return False, ""

            with open(fp, "r", encoding="utf-8", errors="ignore") as jf:
                raw_txt = jf.read().strip()
            if not raw_txt:
                return False, ""

            # Check Go test JSONL (`{"Action":"fail"}` per line)
            if "\n" in raw_txt and raw_txt.startswith("{") and '"Action"' in raw_txt:
                go_fails: list[str] = []
                go_passes = 0
                is_jsonl = True
                for ln in raw_txt.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                        if isinstance(row, dict) and "Action" in row:
                            act = str(row.get("Action") or "").lower()
                            if act == "fail":
                                go_fails.append(str(row.get("Test") or row.get("Package") or "go_test"))
                            elif act == "pass":
                                go_passes += 1
                        else:
                            is_jsonl = False
                            break
                    except Exception:
                        is_jsonl = False
                        break
                if is_jsonl:
                    if go_fails:
                        return False, _fmt_err(f"go test Action=fail ({', '.join(go_fails[:5])})")
                    if go_passes > 0:
                        return True, ""

            j_obj = json.loads(raw_txt)
            if not isinstance(j_obj, dict):
                return False, ""

            # 4a. Jest / Vitest format (`numFailedTests`, `numFailedTestSuites`, `numRuntimeErrorTestSuites`, `success: false`)
            jest_failed = 0
            for jk in ("numFailedTests", "numFailedTestSuites", "numRuntimeErrorTestSuites"):
                jv = j_obj.get(jk)
                if isinstance(jv, int) and not isinstance(jv, bool) and jv > 0:
                    jest_failed += jv
            if jest_failed > 0 or j_obj.get("success") is False:
                return False, _fmt_err(
                    f"Jest/Vitest numFailedTests={j_obj.get('numFailedTests', 0)}, "
                    f"runtimeErrors={j_obj.get('numRuntimeErrorTestSuites', 0)}, success={j_obj.get('success')}"
                )

            # 4b. Playwright format (`stats.unexpected`, `stats.failed`)
            stats_obj = j_obj.get("stats")
            if isinstance(stats_obj, dict):
                pw_unexp = stats_obj.get("unexpected", 0)
                pw_fail = stats_obj.get("failed", 0)
                if (isinstance(pw_unexp, int) and pw_unexp > 0) or (isinstance(pw_fail, int) and pw_fail > 0):
                    return False, _fmt_err(f"Playwright stats.unexpected={pw_unexp}, stats.failed={pw_fail}")

            # 4c. Internal / Pytest / Generic schema (`verdict`, `status`, `failed`, `summary.failed`, `checks/results/tests/items/suites`)
            v_str = str(j_obj.get("verdict") or j_obj.get("status") or "").strip().upper()
            failed_cnt = 0
            raw_failed = j_obj.get("failed")
            if isinstance(raw_failed, int) and not isinstance(raw_failed, bool):
                failed_cnt = int(raw_failed)
            elif isinstance(j_obj.get("summary"), dict):
                for sk in ("failed", "errors", "error"):
                    sum_f = j_obj["summary"].get(sk)
                    if isinstance(sum_f, int) and not isinstance(sum_f, bool):
                        failed_cnt += int(sum_f)

            failed_ids: list[str] = []
            for arr_k in ("checks", "results", "tests", "items", "suites", "testResults"):
                arr_v = j_obj.get(arr_k)
                if isinstance(arr_v, list):
                    for item in arr_v:
                        if isinstance(item, dict):
                            st = str(item.get("status") or item.get("verdict") or "").strip().upper()
                            if st in ("FAIL", "FAILED", "ERROR", "UNEXPECTED") or item.get("pass") is False or item.get("passed") is False or item.get("ok") is False:
                                failed_ids.append(str(item.get("id") or item.get("name") or item.get("title") or item.get("test") or "check"))

            if v_str in ("FAIL", "FAILED", "ERROR") or failed_cnt > 0 or failed_ids:
                f_detail = f"checks FAIL: {', '.join(failed_ids[:6])}" if failed_ids else f"verdict='{v_str or 'FAIL'}', failed={failed_cnt}"
                return False, _fmt_err(f_detail)

            # Check positive pass evidence in JSON
            has_pass = False
            if v_str in ("PASS", "PASSED", "OK", "SUCCEEDED"):
                has_pass = True
            elif isinstance(j_obj.get("numPassedTests"), int) and j_obj.get("numPassedTests", 0) > 0 and jest_failed == 0:
                has_pass = True
            elif isinstance(stats_obj, dict) and int(stats_obj.get("expected") or 0) > 0:
                has_pass = True
            elif isinstance(j_obj.get("passed"), int) and j_obj.get("passed", 0) > 0 and failed_cnt == 0:
                has_pass = True
            return has_pass, ""
        except Exception:
            pass
    return False, ""


def _check_qa_reject_and_ping_pong_guards(
    expanded_cmd: str, no_heredoc: str, real_cwd: str
) -> str:
    """
    Enforce FIX-08 (Structured Bug Report JSON schema on QA_REJECT) and
    FIX-10 (Block >= 3 QA_REJECT ping-pong loops on the same [Pn], requiring --type escalation).
    """
    is_escalation_type = bool(re.search(r"--type(?:\s+|=)['\"]?escalation\b", no_heredoc))
    p_items = re.findall(r"\[(P\d+)\]", expanded_cmd)
    if not p_items:
        p_items = re.findall(r'"plan_item"\s*:\s*"(P\d+)"', expanded_cmd)

    # 1. FIX-10: Count prior QA_REJECT messages in SQLite `messages` for the same [Pn] in this run
    db_path = _find_orca_orchestration_db_path()
    if db_path and os.path.isfile(db_path):
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
            conn.row_factory = sqlite3.Row
            run_id = _extract_cli_flag_value(no_heredoc, ("run", "run-id"))
            disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
            if not run_id and disp_id:
                r_row = conn.execute("SELECT run_id FROM dispatch_contexts WHERE id = ? LIMIT 1", (disp_id,)).fetchone()
                if r_row:
                    run_id = str(r_row["run_id"] or "")
            if not run_id:
                r_row = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                if r_row:
                    run_id = str(r_row["id"] or "")
            if run_id:
                m_rows = conn.execute(
                    "SELECT subject, body FROM messages WHERE run_id = ? AND type IN ('handoff', 'escalation')",
                    (run_id,),
                ).fetchall()
                reject_msgs = [
                    f"{r['subject'] or ''} {r['body'] or ''}"
                    for r in m_rows
                    if "QA_REJECT" in f"{r['subject'] or ''} {r['body'] or ''}"
                ]
                max_pn_cnt = 0
                hit_pn = ""
                if p_items:
                    for pn in p_items:
                        cnt = sum(1 for txt in reject_msgs if f"[{pn}]" in txt or f'"{pn}"' in txt)
                        if cnt > max_pn_cnt:
                            max_pn_cnt = cnt
                            hit_pn = pn
                else:
                    max_pn_cnt = len(reject_msgs)
                    hit_pn = "RUN"
                if max_pn_cnt >= 3:
                    has_escalation_fields = bool(
                        re.search(r"(?:đã\s+thử|tried)", expanded_cmd, re.IGNORECASE)
                        and re.search(r"(?:vì\s+sao|nguyên\s+nhân|root_cause|why)", expanded_cmd, re.IGNORECASE)
                        and re.search(r"(?:cần\s+quyết\s+định|quyết\s+định\s+gì|decision_needed|decision)", expanded_cmd, re.IGNORECASE)
                    )
                    if not is_escalation_type or not has_escalation_fields:
                        conn.close()
                        return (
                            f"[ORCA PING-PONG ESCALATION GUARD] Hạng mục [{hit_pn}] đã bị QA_REJECT {max_pn_cnt}/3 vòng trong Run '{run_id}'! "
                            f"CẤM tiếp tục bắn P2P reject vòng thứ {max_pn_cnt + 1} giữa Dev↔QA gây lặp vô hạn. "
                            f"Cách sửa bắt buộc: Gửi '--type escalation' về thẳng Lead Coordinator kèm đủ 3 mục: "
                            f"(1) Đã thử gì (tried), (2) Vì sao vẫn fail / nguyên nhân gốc (root_cause), (3) Cần Leader quyết định gì (decision_needed)!"
                        )
            conn.close()
        except Exception:
            pass

    # 2. FIX-08: Validate structured bug report JSON block on QA_REJECT when JSON block is present or ORCA_REQUIRE_STRUCTURED_BUG=1
    req_keys = ("plan_item", "file_path", "line", "error_code", "repro_cmd", "expected", "actual", "evidence")
    m_json = re.search(r"(\{[^{}]*\"(?:plan_item|file_path|error_code|repro_cmd)\"[^{}]*\})", expanded_cmd, re.DOTALL)
    if m_json:
        try:
            bug_obj = json.loads(m_json.group(1))
            if isinstance(bug_obj, dict):
                missing_k = [k for k in req_keys if k not in bug_obj or bug_obj.get(k) in (None, "")]
                if missing_k:
                    return (
                        f"[ORCA STRUCTURED BUG REPORT GUARD] Khối JSON báo lỗi QA_REJECT thiếu các trường bắt buộc: {', '.join(missing_k)}! "
                        f"Schema bắt buộc: {{\"plan_item\":\"P2\",\"file_path\":\"...\",\"line\":41,\"error_code\":\"...\",\"repro_cmd\":\"...\",\"expected\":\"...\",\"actual\":\"...\",\"evidence\":\"...\"}}."
                    )
        except Exception:
            return (
                "[ORCA STRUCTURED BUG REPORT GUARD] Khối JSON trong QA_REJECT bị lỗi cú pháp (malformed JSON)! "
                "Hãy truyền JSON hợp lệ với đủ 8 trường: plan_item, file_path, line, error_code, repro_cmd, expected, actual, evidence."
            )
    elif os.environ.get("ORCA_REQUIRE_STRUCTURED_BUG", "1").strip() != "0":
        return (
            "[ORCA STRUCTURED BUG REPORT GUARD] QA_REJECT bắt buộc phải đính kèm khối JSON cấu trúc chuẩn "
            "({\"plan_item\":...,\"file_path\":...,\"line\":...,\"error_code\":...,\"repro_cmd\":...,\"expected\":...,\"actual\":...,\"evidence\":...})!"
        )
    return ""


def _verify_qa_evidence_ledger_and_ancestry(
    real_cwd: str,
    no_heredoc: str,
    expanded_cmd: str,
    cwd_slot: str,
    dispatch_start_epoch: float,
) -> str:
    """
    Enforce FIX-02 (Machine-recorded Evidence Ledger), FIX-04 (Git Merge Ancestry & Scope-Drift),
    FIX-07 (Contracts & TDD Red->Green), and FIX-09 (Gate Ledger rows) under strict Zero-Trust:
    - Rejects any worktree-local evidence.jsonl (A01) and only accepts 0600 recorder files in ~/.orca/evidence (or ORCA_EVIDENCE_DIR).
    - Requires real_cwd to match dispatch worktree (A10) and be a valid Git repository with clean status (A03).
    - Fails closed if any DB-registered upstream branch was deleted or unmerged (A04, T08).
    - Requires strict dispatch/task/run match (A06), non-empty head_sha == HEAD (A02, T10), tests_total > 0 (A08, A11),
      non-noop command, 100% [P1..Pn] plan item coverage (A05), TDD Red->Green (T11), and contracts.json acceptance re-run.
    """
    import hashlib
    import subprocess

    disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
    task_id = _extract_cli_flag_value(no_heredoc, ("task-id", "task"))
    run_id = _extract_cli_flag_value(no_heredoc, ("run", "run-id"))
    from_handle = _extract_cli_flag_value(no_heredoc, ("from",)) or cwd_slot

    project_root = os.path.dirname(real_cwd) if cwd_slot and real_cwd.endswith(cwd_slot) else real_cwd
    upstream_branches: list[str] = []
    db_upstream_branches: set[str] = set()
    expected_plan_items: set[str] = set()
    db_worktree_path = ""

    db_path = _find_orca_orchestration_db_path()
    if db_path and os.path.isfile(db_path):
        try:
            import sqlite3

            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
            conn.row_factory = sqlite3.Row
            if disp_id:
                row = conn.execute(
                    "SELECT dc.run_id, dc.task_id, wd.worktree_id "
                    "FROM dispatch_contexts dc LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                    "WHERE dc.id = ? LIMIT 1",
                    (disp_id,),
                ).fetchone()
                if row:
                    if not run_id:
                        run_id = str(row["run_id"] or "")
                    if not task_id:
                        task_id = str(row["task_id"] or "")
                    wt_raw = str(row["worktree_id"] or "").strip()
                    if "::" in wt_raw:
                        wt_raw = wt_raw.split("::", 1)[1].strip()
                    if wt_raw.startswith("path:"):
                        wt_raw = wt_raw[5:].strip()
                    if wt_raw:
                        db_worktree_path = wt_raw
            if not run_id and task_id:
                row = conn.execute("SELECT run_id FROM tasks WHERE id = ? LIMIT 1", (task_id,)).fetchone()
                if row:
                    run_id = str(row["run_id"] or "")
            if not run_id:
                row = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
                if row:
                    run_id = str(row["id"] or "")
            if run_id:
                w_rows = conn.execute(
                    "SELECT dc.id AS did, wd.state, wd.worktree_id, wd.agent_terminal_handle "
                    "FROM dispatch_contexts dc "
                    "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                    "WHERE dc.run_id = ?",
                    (run_id,),
                ).fetchall()
                for wr in w_rows:
                    wt_id = str(wr["worktree_id"] or "")
                    if "::" in wt_id:
                        wt_id = wt_id.split("::", 1)[1].strip()
                    if wt_id.startswith("path:"):
                        wt_id = wt_id[5:]
                    if not disp_id and wt_id and os.path.realpath(wt_id) == os.path.realpath(real_cwd):
                        disp_id = str(wr["did"] or "")
                        db_worktree_path = wt_id
                    slot_n = _extract_canonical_worker_slot(os.path.basename(wt_id.rstrip("/"))) or _extract_canonical_worker_slot(str(wr["agent_terminal_handle"] or ""))
                    if slot_n and (slot_n.startswith("dev-worker") or slot_n.startswith("ui-worker")):
                        db_upstream_branches.add(slot_n)
                        if slot_n not in upstream_branches:
                            upstream_branches.append(slot_n)
                t_rows = conn.execute("SELECT task_title, spec FROM tasks WHERE run_id = ?", (run_id,)).fetchall()
                for tr in t_rows:
                    for pk in re.findall(r"\[(P\d+)\]", f"{tr['task_title'] or ''} {tr['spec'] or ''}"):
                        expected_plan_items.add(pk)
            conn.close()
        except Exception:
            pass

    if db_worktree_path:
        project_root = os.path.dirname(db_worktree_path.rstrip("/"))
        if os.path.realpath(real_cwd) != os.path.realpath(db_worktree_path):
            if (".gemini" in real_cwd or not real_cwd) and os.path.isdir(db_worktree_path):
                real_cwd = db_worktree_path
            else:
                return (
                    f"[ORCA WORKTREE-MISMATCH GUARD] Lệnh báo cáo của '{disp_id or cwd_slot}' được thực thi từ thư mục '{real_cwd}', "
                    f"không khớp với worktree được cấp phát '{db_worktree_path}'! "
                    f"Thực thi Fail-Closed: CẤM chạy lệnh gửi báo cáo từ thư mục ngoài worktree của slot!"
                )

    # Reject any self-written evidence.jsonl inside the worktree (A01)
    for forbidden_local_ev in (
        os.path.join(real_cwd, "evidence.jsonl"),
        os.path.join(real_cwd, ".orca", "evidence.jsonl"),
    ):
        if os.path.isfile(forbidden_local_ev):
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Phát hiện file ledger tự ghi trong worktree ('{forbidden_local_ev}')! "
                "Theo quy tắc Zero-Trust P0, Guard CẤM đọc ledger nằm trong worktree mà agent có quyền tự tạo. "
                "Hãy xóa file này và chạy kiểm thử qua 'orca-team verify --run <RUN> --dispatch-id <DID> --task-id <TID> -- <cmd>'!"
            )

    # Item 6: Fail-closed when real_cwd is not a valid Git repository with a HEAD commit (A03)
    current_head_sha = os.environ.get("ORCA_HEAD_SHA", "").strip()
    is_git_repo = False
    try:
        rev_p = subprocess.run(
            ["git", "-C", real_cwd, "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if rev_p.returncode == 0 and (rev_p.stdout or "").strip():
            is_git_repo = True
            if not current_head_sha:
                current_head_sha = (rev_p.stdout or "").strip()
    except Exception:
        pass

    if not is_git_repo or not current_head_sha:
        return (
            f"[ORCA GIT-REPO GUARD] Thư mục worktree '{real_cwd}' không phải là Git repository hợp lệ (hoặc chưa có commit HEAD)! "
            "Thực thi Fail-Closed: CẤM gửi báo cáo QA_VERDICT: PASS từ thư mục ngoài Git!"
        )

    # Check git status --porcelain & merge ancestry (FIX-04, A04, T08)
    try:
        st_p = subprocess.run(
            ["git", "-C", real_cwd, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if st_p.returncode == 0 and (st_p.stdout or "").strip():
            dirty_lines = []
            for ln in (st_p.stdout or "").splitlines():
                if len(ln) < 4:
                    continue
                rel_f = ln[3:].strip().strip('"')
                if rel_f.startswith(".orca"):
                    continue
                dirty_lines.append(rel_f)
            if dirty_lines:
                return (
                    f"[ORCA GIT-CLEAN GUARD] Worktree '{cwd_slot or 'qa-worker'}' đang có file chưa commit hoặc bẩn ({', '.join(dirty_lines[:5])})! "
                    f"Cách sửa bắt buộc: Commit hoặc dọn sạch các file thay đổi ('git status --porcelain' phải rỗng) trước khi gửi QA_VERDICT: PASS!"
                )

        if not upstream_branches:
            br_p = subprocess.run(
                ["git", "-C", real_cwd, "for-each-ref", "--format=%(refname:short)", "refs/heads/dev-worker*", "refs/heads/ui-worker*"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if br_p.returncode == 0:
                upstream_branches = [b.strip() for b in (br_p.stdout or "").splitlines() if b.strip()]

        for br in upstream_branches:
            if br == cwd_slot:
                continue
            v_ref = subprocess.run(
                ["git", "-C", real_cwd, "rev-parse", "--verify", "--quiet", br],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if v_ref.returncode != 0:
                if br in db_upstream_branches:
                    return (
                        f"[ORCA MERGE-ANCESTRY GUARD] Nhánh upstream '{br}' của Run '{run_id or 'active'}' "
                        f"(đã ghi nhận trong DB điều phối) không tồn tại hoặc đã bị xóa khỏi Git repo! "
                        f"Thực thi Fail-Closed: CẤM xóa nhánh dev/ui để bỏ qua kiểm tra merge ancestry!"
                    )
                continue
            anc_p = subprocess.run(
                ["git", "-C", real_cwd, "merge-base", "--is-ancestor", br, "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if anc_p.returncode != 0:
                return (
                    f"[ORCA MERGE-ANCESTRY GUARD] Nhánh upstream '{br}' của Run '{run_id or 'active'}' CHƯA được merge vào HEAD của '{cwd_slot or 'qa-worker'}' "
                    f"('git merge-base --is-ancestor {br} HEAD' thất bại)! "
                    f"CẤM gửi báo cáo PASS khi chưa tích hợp đầy đủ code từ mọi nhánh dev/ui. "
                    f"Cách sửa bắt buộc: Chạy 'orca-team merge-upstream --role qa --run {run_id or '<RUN_ID>'}' rồi chạy lại test qua 'orca-team verify'!"
                )
    except Exception as exc:
        return f"[ORCA GIT-ANCESTRY ERROR] Không thể xác minh trạng thái Git/ancestry ({exc}). Thực thi Fail-Closed!"

    cg = _load_critic_gate_module()
    if cg is None:
        return "[ORCA LEDGER-INTEGRITY GUARD] Không thể tải mô-đun orca_critic_gate. Thực thi Fail-Closed!"

    # Load expected [P1..Pn], test_allowlist, and item test_patterns from master-plan.md & contracts.json (C-FIX-13..15, C-FIX-27..30)
    c_obj: dict = {}
    draft_c_obj: dict = {}
    cfg_obj: dict = {}
    if project_root:
        cfg_p = os.path.join(project_root, "orca-team.json")
        if os.path.isfile(cfg_p):
            try:
                loaded_cfg = json.loads(open(cfg_p, "r", encoding="utf-8", errors="ignore").read())
                if isinstance(loaded_cfg, dict):
                    cfg_obj = loaded_cfg
            except Exception:
                pass
    item_test_patterns: dict[str, str] = {}
    test_allowlist: set[str] = set()
    contract_cmd_hashes: set[str] = set()
    pinned_test_cmds_from_draft: list[tuple[str, list[str]]] = []
    if run_id and project_root:
        plan_md_p = os.path.join(project_root, ".orca", "runs", run_id, "master-plan.md")
        if os.path.isfile(plan_md_p):
            try:
                plan_txt = open(plan_md_p, "r", encoding="utf-8", errors="ignore").read()
                for pk in re.findall(r"\[(P\d+)\]", plan_txt):
                    expected_plan_items.add(pk)
            except Exception:
                pass
        draft_contracts_p = os.path.join(project_root, ".orca", "runs", run_id, "plan-draft.contracts.json")
        if os.path.isfile(draft_contracts_p):
            try:
                loaded_dc = json.loads(open(draft_contracts_p, "r", encoding="utf-8", errors="ignore").read())
                if isinstance(loaded_dc, dict):
                    draft_c_obj = loaded_dc
                    dv = draft_c_obj.get("verify_cmd")
                    if isinstance(dv, list) and dv:
                        pinned_test_cmds_from_draft.append(("test", [str(x) for x in dv]))
                    for d_it in draft_c_obj.get("items") or []:
                        if isinstance(d_it, dict):
                            dv_obj = d_it.get("verify")
                            if isinstance(dv_obj, dict) and isinstance(dv_obj.get("cmd"), list) and dv_obj.get("cmd"):
                                pinned_test_cmds_from_draft.append(("test", [str(x) for x in dv_obj["cmd"]]))
                            elif isinstance(d_it.get("verify_cmd"), list) and d_it.get("verify_cmd"):
                                pinned_test_cmds_from_draft.append(("test", [str(x) for x in d_it["verify_cmd"]]))
            except Exception:
                pass
        contracts_p = os.path.join(project_root, ".orca", "runs", run_id, "contracts.json")
        if os.path.isfile(contracts_p):
            try:
                loaded_c = json.loads(open(contracts_p, "r", encoding="utf-8", errors="ignore").read())
                if isinstance(loaded_c, dict):
                    c_obj = loaded_c
                    for al_f in c_obj.get("test_allowlist") or []:
                        if isinstance(al_f, str) and al_f.strip():
                            test_allowlist.add(al_f.strip().replace("\\", "/").lstrip("./"))
                    for pk in c_obj.get("plan_items") or []:
                        if isinstance(pk, str) and re.match(r"^P\d+$", pk.strip()):
                            expected_plan_items.add(pk.strip())
                    for item in c_obj.get("items") or []:
                        if isinstance(item, dict):
                            ipk = str(item.get("id") or "").strip()
                            if re.match(r"^P\d+$", ipk):
                                expected_plan_items.add(ipk)
                                v_obj = item.get("verify")
                                pat = ""
                                if isinstance(v_obj, dict):
                                    pat = str(v_obj.get("test_pattern") or "").strip()
                                if not pat:
                                    pat = str(item.get("test_pattern") or "").strip()
                                if pat:
                                    pat_ok, pat_err = cg.is_valid_item_test_pattern(pat, ipk)
                                    if not pat_ok:
                                        return pat_err
                                    item_test_patterns[ipk] = pat
                            ihash = str(item.get("cmd_hash") or "").strip()
                            if ihash:
                                contract_cmd_hashes.add(ihash)
                    for ch in c_obj.get("required_cmd_hashes") or []:
                        if isinstance(ch, str) and ch.strip():
                            contract_cmd_hashes.add(ch.strip())
            except Exception:
                pass
    for pk in expected_plan_items:
        if pk not in item_test_patterns:
            item_test_patterns[pk] = rf"(?i)\btest[_\-]?{pk.lower()}\b"

    # Item 1: Only read ledger files from ~/.orca/evidence (or ORCA_EVIDENCE_DIR) — never from worktree or project/.orca
    target_files: list[str] = []
    env_ev_dir = os.environ.get("ORCA_EVIDENCE_DIR", "").strip()
    ev_root = env_ev_dir if env_ev_dir else os.path.join(os.path.expanduser("~"), ".orca", "evidence")
    ev_base_dir = os.path.dirname(ev_root) if os.path.isfile(ev_root) else ev_root
    if os.path.isfile(ev_root):
        target_files.append(ev_root)
    elif os.path.isdir(ev_root):
        for cand in (
            os.path.join(ev_root, run_id or "default", f"{disp_id}.jsonl") if disp_id else "",
            os.path.join(ev_root, f"{disp_id}.jsonl") if disp_id else "",
        ):
            if cand and os.path.exists(cand) and cand not in target_files:
                target_files.append(cand)

    # C-FIX-18: Check stat (not symlink, owner == getuid(), mode == 0600) on ledger files
    ledger_stat_errors: list[str] = []
    ledger_rows: list[dict] = []
    for lf_p in target_files:
        try:
            st = os.lstat(lf_p)
            if stat.S_ISLNK(st.st_mode):
                ledger_stat_errors.append(
                    f"[ORCA LEDGER-INTEGRITY GUARD] File ledger '{lf_p}' là symlink! Thực thi Fail-Closed."
                )
                continue
            if st.st_uid != os.getuid():
                ledger_stat_errors.append(
                    f"[ORCA LEDGER-INTEGRITY GUARD] File ledger '{lf_p}' không thuộc UID hiện tại ({st.st_uid} != {os.getuid()})!"
                )
                continue
            if (st.st_mode & 0o777) != 0o600:
                ledger_stat_errors.append(
                    f"[ORCA LEDGER-INTEGRITY GUARD] File ledger '{lf_p}' có quyền truy cập không an toàn ({oct(st.st_mode & 0o777)} != 0o600)! "
                    "Bắt buộc tạo qua 'orca-team verify'."
                )
        except Exception as exc:
            ledger_stat_errors.append(f"[ORCA LEDGER-INTEGRITY GUARD] Không thể lstat ledger '{lf_p}': {exc}")
            continue
        try:
            with open(lf_p, "r", encoding="utf-8", errors="ignore") as lf:
                for raw_ln in lf:
                    ln = raw_ln.strip()
                    if not ln:
                        continue
                    try:
                        row = json.loads(ln)
                        if isinstance(row, dict):
                            row["_file"] = lf_p
                            ledger_rows.append(row)
                    except Exception:
                        continue
        except Exception:
            continue

    if not ledger_rows and ledger_stat_errors:
        return ledger_stat_errors[0]

    if not ledger_rows:
        return (
            "[ORCA QA EVIDENCE LEDGER GUARD] Không tìm thấy bằng chứng thực thi test trong Evidence Ledger "
            f"('~/.orca/evidence/{run_id or '<run>'}/{disp_id or '<dispatch>'}.jsonl')! "
            "Theo quy tắc Zero-Trust P0, 'không có bằng chứng = BLOCK'. "
            "Cách sửa bắt buộc: Chạy bộ kiểm thử qua 'orca-team verify --run <RUN> --dispatch-id <DID> --task-id <TID> -- <lệnh test>' "
            "để máy ghi nhận exit_code=0 và head_sha vào ledger trước khi gửi QA_VERDICT: PASS!"
        )

    # Item 6 / A06: Strict dispatch_id, task_id, and run_id matching (reject foreign dispatches)
    qa_rows: list[dict] = []
    for r in ledger_rows:
        r_disp = str(r.get("dispatch_id") or r.get("dispatch") or "").strip()
        r_task = str(r.get("task_id") or r.get("task") or "").strip()
        r_run = str(r.get("run_id") or r.get("run") or "").strip()
        r_src = str(r.get("source") or "").strip().lower()
        if disp_id and r_disp != disp_id:
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Phát hiện dòng bằng chứng của dispatch khác ('{r_disp}' != '{disp_id}') trong ledger! "
                "CẤM tái sử dụng bằng chứng của worker/dispatch khác cho QA."
            )
        if task_id and r_task and r_task != task_id:
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Phát hiện dòng bằng chứng của task khác ('{r_task}' != '{task_id}') trong ledger! "
                "CẤM tái sử dụng bằng chứng của task khác cho QA."
            )
        if run_id and r_run and r_run != run_id:
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Phát hiện dòng bằng chứng của Run khác ('{r_run}' != '{run_id}') trong ledger!"
            )
        if r_src not in ("recorder", "verify", "gate"):
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Dòng bằng chứng có nguồn không hợp lệ (source='{r_src}')!"
            )
        qa_rows.append(r)

    if ledger_stat_errors:
        return ledger_stat_errors[0]

    # C-FIX-18: Verify HMAC signature on every ledger row
    secret_bytes, sec_err = cg.get_or_create_hmac_secret(ev_base_dir, create_if_missing=False)
    if sec_err or not secret_bytes:
        return sec_err or "[ORCA LEDGER-INTEGRITY GUARD] Thiếu khóa HMAC .hmac_secret hợp lệ!"
    for r in qa_rows:
        row_copy = {k: v for k, v in r.items() if k != "_file"}
        if not cg.verify_row_hmac(row_copy, secret_bytes):
            return (
                "[ORCA LEDGER-INTEGRITY GUARD] Chữ ký HMAC ('row_hmac') của dòng bằng chứng trong ledger KHÔNG HỢP LỆ hoặc bị sửa tay! "
                "CẤM chỉnh sửa trực tiếp file evidence.jsonl; bắt buộc chạy qua 'orca-team verify'."
            )

    # C-FIX-19 & C-FIX-16: Runner allowlist & kind-aware chronological evaluation
    rows_by_key: dict[str, list[dict]] = {}
    latest_by_key: dict[str, dict] = {}
    history_by_key: dict[str, list[int]] = {}
    for r in qa_rows:
        cmd_v = r.get("cmd")
        if isinstance(cmd_v, list):
            cmd_tokens_r = [str(x) for x in cmd_v if str(x).strip()]
            cmd_str = " ".join(cmd_tokens_r)
        else:
            cmd_str = str(cmd_v or "").strip()
            cmd_tokens_r = cmd_str.split()
        if not cmd_tokens_r:
            return "[ORCA QA EVIDENCE LEDGER GUARD] Dòng bằng chứng trong ledger có trường 'cmd' rỗng!"
        if any(op in cmd_str for op in ("||", ";", "|", "$(", "`")):
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Lệnh ghi trong ledger ('{cmd_str}') chứa toán tử shell che giấu lỗi!"
            )
        r_kind = str(r.get("kind") or "test").strip().lower()
        allowed_ok, allowed_err = cg.validate_runner_allowlist(cmd_tokens_r, r_kind, worktree_dir=real_cwd)
        if not allowed_ok:
            return allowed_err
        expected_hash = cg.canonical_cmd_hash(cmd_tokens_r)
        row_hash = str(r.get("cmd_hash") or "").strip()
        if row_hash != expected_hash:
            return (
                f"[ORCA LEDGER-INTEGRITY GUARD] Trường 'cmd_hash' ({row_hash}) không khớp với mã băm chuẩn ({expected_hash}) của lệnh '{cmd_str}'!"
            )
        key = f"{r_kind}:{expected_hash}"
        raw_exit = r.get("exit_code") if r.get("exit_code") is not None else r.get("exit")
        exit_code = int(raw_exit) if isinstance(raw_exit, (int, float)) and not isinstance(raw_exit, bool) else 1
        rows_by_key.setdefault(key, []).append(r)
        history_by_key.setdefault(key, []).append(exit_code)
        latest_by_key[key] = r

    verified_cmd_hashes: set[str] = set()
    has_test_kind = False

    for key, last_r in latest_by_key.items():
        r_kind = str(last_r.get("kind") or "test").strip().lower()
        if r_kind == "test":
            has_test_kind = True
        raw_exit = last_r.get("exit_code") if last_r.get("exit_code") is not None else last_r.get("exit")
        exit_code = int(raw_exit) if isinstance(raw_exit, (int, float)) and not isinstance(raw_exit, bool) else 1
        failed_n = int(last_r.get("tests_failed") or last_r.get("failed") or 0)
        if exit_code != 0 or failed_n > 0:
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Evidence Ledger ghi nhận kết quả THẤT BẠI ở lần chạy mới nhất "
                f"(exit_code={exit_code}, failed={failed_n}, cmd={last_r.get('cmd')})! "
                f"CẤM gửi báo cáo QA_VERDICT: PASS khi lệnh kiểm thử trong ledger có exit_code != 0."
            )
        if last_r.get("dirty") is not False:
            return (
                "[ORCA QA EVIDENCE LEDGER GUARD] Evidence Ledger ghi nhận worktree đang bẩn (dirty!=false) lúc chạy test! "
                "Hãy commit sạch mọi thay đổi rồi chạy lại 'orca-team verify' trước khi báo PASS."
            )
        # C-FIX-16: tests_total > 0 only required for kind == "test"
        if r_kind == "test":
            raw_total = last_r.get("tests_total")
            if raw_total is None or not isinstance(raw_total, (int, float)) or isinstance(raw_total, bool) or int(raw_total) <= 0:
                return (
                    f"[ORCA QA EVIDENCE LEDGER GUARD] Evidence Ledger thiếu 'tests_total' hoặc 'tests_total={raw_total}' (<= 0, không có test nào chạy)! "
                    "CẤM báo cáo PASS khi chưa có test thực tế nào được thực thi."
                )
        # C-FIX-18: Exact 40-hex SHA match (no prefix match)
        row_sha = str(last_r.get("head_sha") or "").strip().lower()
        if not row_sha or not re.fullmatch(r"[0-9a-f]{40}", row_sha):
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Dòng bằng chứng trong Evidence Ledger có 'head_sha={row_sha}' không phải 40 ký tự hex đầy đủ! "
                "Thực thi Fail-Closed: Cấm rút gọn hoặc bỏ trống commit SHA."
            )
        if row_sha != current_head_sha.lower():
            return (
                f"[ORCA QA EVIDENCE LEDGER GUARD] Bằng chứng trong Evidence Ledger có commit 'head_sha={row_sha}' "
                f"CŨ / KHÁC với HEAD hiện tại ('{current_head_sha}')! "
                f"Mọi lần sửa code hoặc commit mới đều làm vô hiệu hóa kết quả test cũ. "
                f"Cách sửa bắt buộc: Chạy lại 'orca-team verify' trên commit HEAD hiện tại trước khi báo PASS!"
            )
        if last_r.get("cmd_hash"):
            verified_cmd_hashes.add(str(last_r.get("cmd_hash")))

    if not has_test_kind:
        return (
            "[ORCA QA EVIDENCE LEDGER GUARD] Evidence Ledger chỉ có lệnh build/lint/typecheck mà CHƯA có lệnh kiểm thử (kind='test') nào! "
            "Bắt buộc chạy ít nhất 1 bộ kiểm thử thực tế qua 'orca-team verify'."
        )

    # C-FIX-16: TDD Red -> Green required ONLY for kind == "test" (unless ORCA_REQUIRE_TDD=0)
    require_tdd = os.environ.get("ORCA_REQUIRE_TDD", "1").strip() != "0"
    if require_tdd:
        for key, seq in history_by_key.items():
            last_r = latest_by_key[key]
            r_kind = str(last_r.get("kind") or "test").strip().lower()
            if r_kind != "test":
                continue
            if not (len(seq) >= 2 and any(x != 0 for x in seq[:-1]) and seq[-1] == 0):
                return (
                    f"[ORCA TDD GUARD] Bộ kiểm thử '{key}' chưa có đủ chuỗi bằng chứng TDD Red→Green "
                    f"(cần ít nhất 1 dòng exit_code!=0 trước và 1 dòng exit_code=0 sau cùng cmd_hash trong ledger)!"
                )

    # C-FIX-14: Test-Tree Integrity (compare live worktree test_tree_hash with red/green rows & merge-base main HEAD)
    live_test_hash, live_test_files = cg.compute_test_tree_hash(real_cwd)
    if not live_test_hash or not live_test_files:
        return (
            "[ORCA TEST-INTEGRITY GUARD] Không tìm thấy file kiểm thử hợp lệ trong worktree! "
            "Thực thi Fail-Closed: Dự án bắt buộc phải có file test thật."
        )
    for key, r_list in rows_by_key.items():
        green_r = r_list[-1]
        if str(green_r.get("kind") or "test").strip().lower() != "test":
            continue
        green_t_hash = str(green_r.get("test_tree_hash") or "").strip()
        if green_t_hash != live_test_hash:
            return (
                f"[ORCA TEST-INTEGRITY GUARD] Mã băm cây kiểm thử ('test_tree_hash={green_t_hash}') trong ledger "
                f"KHÔNG KHỚP với file test hiện tại trên đĩa ('{live_test_hash}')!"
            )
        red_rows = [rr for rr in r_list[:-1] if int(rr.get("exit_code") if rr.get("exit_code") is not None else 1) != 0]
        for red_r in red_rows:
            red_t_hash = str(red_r.get("test_tree_hash") or "").strip()
            if red_t_hash != live_test_hash:
                changed_test_files: list[str] = []
                red_sha = str(red_r.get("head_sha") or "").strip()
                if red_sha and re.fullmatch(r"[0-9a-f]{40}", red_sha.lower()):
                    try:
                        df_p = subprocess.run(
                            ["git", "-C", real_cwd, "diff", "--name-only", red_sha, current_head_sha],
                            capture_output=True, text=True, timeout=5,
                        )
                        if df_p.returncode == 0:
                            for f_ln in (df_p.stdout or "").splitlines():
                                f_clean = f_ln.strip().replace("\\", "/").lstrip("./")
                                if f_clean and cg.is_test_file_path(f_clean):
                                    changed_test_files.append(f_clean)
                    except Exception:
                        pass
                if not changed_test_files:
                    changed_test_files = list(live_test_files)
                disallowed = [f for f in changed_test_files if f not in test_allowlist]
                if disallowed:
                    return (
                        f"[ORCA TEST-INTEGRITY GUARD] Phát hiện sửa đổi file kiểm thử giữa pha RED và GREEN "
                        f"ngoài danh sách cho phép (test_allowlist): {', '.join(disallowed)}! "
                        f"CẤM làm yếu assert hoặc sửa bài test để ép xanh kết quả."
                    )

    # Also check git diff against merge-base main/master HEAD for pre-existing test files modified outside test_allowlist
    for base_ref in ("main", "master"):
        try:
            mb_p = subprocess.run(
                ["git", "-C", real_cwd, "merge-base", base_ref, "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if mb_p.returncode == 0 and (mb_p.stdout or "").strip():
                mb_sha = (mb_p.stdout or "").strip()
                if mb_sha.lower() != current_head_sha.lower():
                    ls_p = subprocess.run(
                        ["git", "-C", real_cwd, "ls-tree", "-r", "--name-only", mb_sha],
                        capture_output=True, text=True, timeout=5,
                    )
                    base_files = {
                        ln.strip().replace("\\", "/").lstrip("./")
                        for ln in (ls_p.stdout or "").splitlines()
                        if ln.strip()
                    }
                    df_p = subprocess.run(
                        ["git", "-C", real_cwd, "diff", "--name-only", mb_sha, current_head_sha],
                        capture_output=True, text=True, timeout=5,
                    )
                    if df_p.returncode == 0:
                        mod_tests = [
                            ln.strip().replace("\\", "/").lstrip("./")
                            for ln in (df_p.stdout or "").splitlines()
                            if ln.strip()
                            and cg.is_test_file_path(ln.strip())
                            and ln.strip().replace("\\", "/").lstrip("./") in base_files
                            and ln.strip().replace("\\", "/").lstrip("./") not in test_allowlist
                        ]
                        if mod_tests:
                            return (
                                f"[ORCA TEST-INTEGRITY GUARD] Phát hiện file kiểm thử gốc trên nhánh '{base_ref}' bị sửa đổi "
                                f"ngoài 'contracts.json.test_allowlist': {', '.join(mod_tests)}!"
                            )
                break
        except Exception:
            pass

    # C-FIX-13 & C-FIX-27: Resolve canonical gate commands and verify against pinned policy anchor / plan-draft.contracts.json
    if contract_cmd_hashes:
        missing_hashes = contract_cmd_hashes - verified_cmd_hashes
        if missing_hashes:
            return (
                f"[ORCA CONTRACT-HASH GUARD] Evidence Ledger thiếu cmd_hash bắt buộc trong contracts.json ({', '.join(sorted(missing_hashes))})!"
            )

    policy_anchor, anchor_err = cg.load_pinned_policy_anchor(ev_base_dir, run_id or "default", worktree_dir=real_cwd)
    if anchor_err:
        return anchor_err

    if isinstance(policy_anchor, dict):
        anchor_allowlist = {
            str(f).strip().replace("\\", "/").lstrip("./")
            for f in (policy_anchor.get("testAllowlist") or [])
            if isinstance(f, str) and str(f).strip()
        }
        if test_allowlist != anchor_allowlist:
            return (
                f"[ORCA POLICY-ANCHOR GUARD] test_allowlist trong contracts.json ({sorted(test_allowlist)}) "
                f"không khớp với testAllowlist đã ghim trong .policy_anchor.json ({sorted(anchor_allowlist)})! "
                "CẤM tự thêm file test giả vào test_allowlist."
            )
        test_allowlist = anchor_allowlist
        anchor_cmds = policy_anchor.get("acceptanceCommands") or policy_anchor.get("canonicalCommands") or []
        canonical_cmds = [
            (str(ac.get("kind") or "test"), [str(x) for x in (ac.get("cmd") or [])])
            for ac in anchor_cmds
            if isinstance(ac, dict) and ac.get("cmd")
        ]
        anchor_pats = policy_anchor.get("itemTestPatterns")
        if isinstance(anchor_pats, dict):
            for pk, pat_v in anchor_pats.items():
                if isinstance(pat_v, str) and pat_v.strip():
                    item_test_patterns[str(pk)] = pat_v.strip()
        anchor_msha = str(policy_anchor.get("manifestSha256") or "").strip()
        if isinstance(c_obj, dict) and c_obj.get("manifestSha256") and anchor_msha:
            if str(c_obj.get("manifestSha256")).strip() != anchor_msha:
                return (
                    f"[ORCA POLICY-ANCHOR GUARD] manifestSha256 trong contracts.json ('{c_obj.get('manifestSha256')}') "
                    f"không khớp với .policy_anchor.json / acceptance-manifest.json ('{anchor_msha}')!"
                )
        baseline_files = policy_anchor.get("baselineTestFiles")
        if isinstance(baseline_files, dict) and baseline_files:
            cur_baseline = cg.compute_baseline_test_files(real_cwd)
            for b_rel, b_sha in baseline_files.items():
                if b_rel not in test_allowlist and cur_baseline.get(b_rel) != b_sha:
                    return (
                        f"[ORCA TEST-INTEGRITY GUARD] File kiểm thử/cấu hình gốc '{b_rel}' trong baselineTestFiles "
                        f"đã bị sửa đổi hoặc xoá sau khi pin ngoài test_allowlist!"
                    )
            unapproved_new_tests = sorted(set(cur_baseline.keys()) - set(baseline_files.keys()))
            if unapproved_new_tests:
                return (
                    f"[ORCA TEST-INTEGRITY GUARD] Phát hiện file kiểm thử mới thêm vào sau khi pin ngoài "
                    f"baselineTestFiles: {', '.join(unapproved_new_tests)}! "
                    "CẤM QA tự thêm file test giả để khớp tên [P1..Pn]."
                )
        baseline_test_ids = policy_anchor.get("baselineTestIds")
        if isinstance(baseline_test_ids, list) and baseline_test_ids:
            cur_test_ids = set(cg.extract_defined_test_ids(real_cwd))
            unapproved_ids = sorted(cur_test_ids - set(str(x) for x in baseline_test_ids))
            if unapproved_ids:
                return (
                    f"[ORCA TEST-INTEGRITY GUARD] Phát hiện test ID mới thêm vào sau khi pin ngoài baselineTestIds: "
                    f"{', '.join(unapproved_ids[:6])}! CẤM bơm thêm hàm test giả để vượt cổng độ phủ."
                )
    else:
        if project_root and run_id:
            cons_check_p = os.path.join(project_root, ".orca", "runs", run_id, "consensus.json")
            draft_check_p = os.path.join(project_root, ".orca", "runs", run_id, "plan-draft.contracts.json")
            run_pinned_in_db = os.path.isfile(cons_check_p)
            if not run_pinned_in_db and db_path and os.path.isfile(db_path):
                try:
                    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0) as _conn_p:
                        _r_pin = _conn_p.execute(
                            "SELECT 1 FROM messages WHERE run_id = ? AND body LIKE '%CONSENSUS_REACHED%' "
                            "UNION SELECT 1 FROM tasks WHERE run_id = ? LIMIT 1",
                            (run_id, run_id),
                        ).fetchone()
                        if _r_pin:
                            run_pinned_in_db = True
                except Exception:
                    pass
            if run_pinned_in_db and (not os.path.isfile(cons_check_p) or not os.path.isfile(draft_check_p)):
                return (
                    f"[ORCA POLICY-ANCHOR GUARD] Thiếu '.policy_anchor.json' và phát hiện xoá trái phép "
                    f"'consensus.json' / 'plan-draft.contracts.json' của Run '{run_id}' đã được duyệt trong DB! "
                    "CẤM xoá file neo hợp đồng để đổi lệnh kiểm thử."
                )
        canonical_cmds = cg.resolve_canonical_gate_commands(
            real_cwd, project_root or real_cwd, contracts_obj=c_obj, cfg_obj=cfg_obj
        )
        # C-FIX-27: If plan-draft.contracts.json pinned a verify_cmd, block if contracts.json removed/changed it to run something else
        if pinned_test_cmds_from_draft:
            draft_hashes = {cg.canonical_cmd_hash(toks) for _, toks in pinned_test_cmds_from_draft}
            cur_hashes = {cg.canonical_cmd_hash(toks) for k, toks in canonical_cmds if k == "test"}
            if draft_hashes and (cur_hashes != draft_hashes or len(draft_hashes) > 1):
                return (
                    f"[ORCA POLICY-ANCHOR GUARD] Lệnh kiểm thử trong contracts.json / orca-team.json (hash={sorted(cur_hashes)}) "
                    f"không khớp với lệnh kiểm thử đã duyệt lúc đầu trong plan-draft.contracts.json (hash={sorted(draft_hashes)})! "
                    "CẤM xóa/sửa verify_cmd trong contracts.json để đổi sang script/runner khác."
                )

    if require_tdd:
        for key, r_list in rows_by_key.items():
            green_r = r_list[-1]
            if str(green_r.get("kind") or "test").strip().lower() != "test":
                continue
            red_rows = [rr for rr in r_list[:-1] if int(rr.get("exit_code") if rr.get("exit_code") is not None else 1) != 0]
            if red_rows:
                green_sha = str(green_r.get("head_sha") or "").strip().lower()
                green_dirty = bool(green_r.get("dirty"))
                has_code_progression = any(
                    bool(rr.get("dirty")) != green_dirty
                    or str(rr.get("head_sha") or "").strip().lower() != green_sha
                    for rr in red_rows
                )
                if not has_code_progression:
                    return (
                        f"[ORCA TDD-PROGRESSION GUARD] Chuỗi TDD RED→GREEN của '{key}' diễn ra trên CÙNG một commit sạch "
                        f"(head_sha='{green_sha[:12]}', không có thay đổi code giữa pha RED và GREEN)! "
                        "Bài kiểm thử phụ thuộc trạng thái ngoại lai ngoài git thay vì kiểm thử thay đổi code thực tế."
                    )

    for g_k, g_t in canonical_cmds:
        ok_al, err_al = cg.validate_runner_allowlist(g_t, g_k, worktree_dir=real_cwd)
        if not ok_al:
            return err_al
    if not canonical_cmds:
        return (
            "[ORCA CANONICAL-TEST GUARD] Không thể xác định lệnh kiểm thử chuẩn (canonical test command)! "
            "Thực thi Fail-Closed: hãy khai báo 'gateCommands' (dạng list token) trong 'orca-team.json' hoặc 'verify_cmd' trong 'contracts.json'."
        )

    canonical_test_cmds = [(k, toks) for k, toks in canonical_cmds if k == "test"]
    if not canonical_test_cmds:
        return "[ORCA CANONICAL-TEST GUARD] Không tìm thấy lệnh kiểm thử chuẩn (kind='test') trong dự án!"

    for g_kind, g_tokens in canonical_test_cmds:
        c_hash = cg.canonical_cmd_hash(g_tokens)
        if c_hash not in verified_cmd_hashes:
            return (
                f"[ORCA CANONICAL-TEST GUARD] Evidence Ledger chưa ghi nhận kết quả của lệnh kiểm thử chuẩn "
                f"'{' '.join(g_tokens)}' (cmd_hash={c_hash})! "
                f"CẤM chạy script/test tự chế thay cho bộ test chuẩn của dự án."
            )

    # Also include any verified build/lint/typecheck commands from latest_by_key (validated against allowlist above)
    cmds_to_rerun: list[tuple[str, list[str]]] = list(canonical_cmds)
    seen_hashes = {cg.canonical_cmd_hash(toks) for _, toks in cmds_to_rerun}
    for last_r in latest_by_key.values():
        rk = str(last_r.get("kind") or "test").strip().lower()
        if rk in ("build", "lint", "typecheck"):
            cmd_v = last_r.get("cmd")
            toks = [str(x) for x in cmd_v if str(x).strip()] if isinstance(cmd_v, list) else str(cmd_v or "").split()
            ch = cg.canonical_cmd_hash(toks)
            if toks and ch not in seen_hashes:
                seen_hashes.add(ch)
                cmds_to_rerun.append((rk, toks))

    combined_test_outputs: list[str] = []
    total_live_passed = 0

    for g_kind, acc_tokens in cmds_to_rerun:
        exec_tokens = list(acc_tokens)
        # Ensure verbose test name output for C-FIX-15 pattern matching
        if g_kind == "test":
            norm_toks = cg.normalize_cmd_tokens(exec_tokens)
            if (
                (len(norm_toks) >= 3 and norm_toks[0] == "python3" and norm_toks[1] == "-m" and norm_toks[2] in ("unittest", "pytest"))
                or (norm_toks and norm_toks[0] == "pytest")
                or (len(norm_toks) >= 2 and norm_toks[0] == "go" and norm_toks[1] == "test")
            ):
                if "-v" not in exec_tokens and "--verbose" not in exec_tokens:
                    exec_tokens.append("-v")
        acc_cmd_str = " ".join(exec_tokens)
        try:
            acc_p = subprocess.run(
                exec_tokens,
                cwd=real_cwd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=600,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            )
            if acc_p.returncode != 0:
                return (
                    f"[ORCA ACCEPTANCE RERUN GUARD] Guard tự chạy lại lệnh chuẩn '{acc_cmd_str}' (kind={g_kind}) "
                    f"tại thời điểm PASS và nhận kết quả THẤT BẠI (exit_code={acc_p.returncode})! "
                    f"CẤM giả mạo ledger khi kiểm thử/build thực tế đang fail."
                )
            if g_kind != "test":
                continue
            combined_live = (acc_p.stdout or "") + "\n" + (acc_p.stderr or "")
            combined_test_outputs.append(combined_live)
            live_passed = 0
            live_failed = 0
            for m_p in re.finditer(r"\b(\d+)\s+(?:passed|passing)\b", combined_live, re.IGNORECASE):
                live_passed = max(live_passed, int(m_p.group(1)))
            for m_f in re.finditer(r"\b(\d+)\s+(?:failed|failing|failures?|errors?)\b", combined_live, re.IGNORECASE):
                live_failed = max(live_failed, int(m_f.group(1)))
            m_ok = re.search(r"\bRan\s+(\d+)\s+tests?\b[\s\S]{0,200}?\bOK\b", combined_live)
            if m_ok:
                live_passed = max(live_passed, int(m_ok.group(1)))
            go_ok = len(re.findall(r"(?m)^(?:ok|---\s*PASS:)\s+\S+", combined_live))
            if go_ok:
                live_passed = max(live_passed, go_ok)
            tap_ok = len(re.findall(r"(?m)^ok\s+\d+\b", combined_live))
            if tap_ok:
                live_passed = max(live_passed, tap_ok)
            gradle_ok = len(re.findall(r"\bPASSED\b", combined_live))
            if gradle_ok:
                live_passed = max(live_passed, gradle_ok)
            if live_failed > 0 or live_passed <= 0:
                return (
                    f"[ORCA ACCEPTANCE RERUN GUARD] Guard tự chạy lại lệnh kiểm thử chuẩn '{acc_cmd_str}' tại thời điểm PASS "
                    f"nhưng không ghi nhận được test nào PASS (live_passed={live_passed}, live_failed={live_failed})!"
                )
            total_live_passed += live_passed
        except Exception as exc:
            return (
                f"[ORCA ACCEPTANCE RERUN GUARD] Không thể thực thi lệnh chuẩn '{acc_cmd_str}' tại thời điểm PASS ({exc})! "
                f"Thực thi Fail-Closed."
            )

    # C-FIX-15 & C-FIX-33: Verify 100% [P1..Pn] coverage by binding each item's test_pattern to executed test IDs
    if expected_plan_items:
        full_live_out = "\n".join(combined_test_outputs)
        executed_ids = cg.extract_executed_test_ids(full_live_out)
        allowed_base_ids = (
            set(str(x) for x in (policy_anchor.get("baselineTestIds") or []))
            if isinstance(policy_anchor, dict) and policy_anchor.get("baselineTestIds")
            else None
        )
        missing_p: list[str] = []
        for pk in sorted(expected_plan_items, key=lambda x: int(x[1:]) if x[1:].isdigit() else 999):
            pat = item_test_patterns.get(pk) or rf"(?i)\btest[_\-]?{pk.lower()}\b"
            try:
                if executed_ids:
                    matched_ids = [
                        tid
                        for tid in executed_ids
                        if re.search(pat, tid) and (allowed_base_ids is None or tid in allowed_base_ids)
                    ]
                    if not matched_ids:
                        missing_p.append(pk)
                elif not re.search(pat, full_live_out):
                    missing_p.append(pk)
            except re.error:
                if pat not in full_live_out:
                    missing_p.append(pk)
        if missing_p or total_live_passed < len(expected_plan_items):
            missing_label = ", ".join(missing_p) if missing_p else f"số test chạy ({total_live_passed}) < số hạng mục ({len(expected_plan_items)})"
            return (
                f"[ORCA PLAN-COVERAGE GUARD] thiếu test thật cho {missing_label} "
                f"(output kiểm thử live không khớp test_pattern của đủ 100% hạng mục [P1..Pn] trong contracts.json)!"
            )

    return ""


def _check_qa_and_ui_verification_guards(
    cmd: str, sanitized_orch: str, payload: dict, args: dict
) -> str:
    """
    Universal, project-agnostic QA & UI Evidence Guards (`[ORCA QA & UI EVIDENCE GUARD]`):
    - FIX-01: Slot-first detection, fail-closed when `orca-team.json` is missing or guard raises on QA/UI slots,
              and indirect variable/subshell (`--type "$T"`, `--body "$(cat ...)"`) expansion.
    - FIX-02 / FIX-07 / FIX-09: Machine-recorded Evidence Ledger verification (`exit_code==0`, `head_sha==HEAD`,
              `dirty==false`, positive test count, and TDD red->green).
    - FIX-03: Multi-format test report parser (Jest, Playwright, JUnit XML, pytest, Go test JSON, TAP) without 5MB skip.
    - FIX-04: Read-only Git merge ancestry (`git merge-base --is-ancestor`) & clean worktree check.
    - FIX-05: Scan source directories (`src/`, `app/`, `packages/`... up to depth 4) for test reports and verify
              `.png`/`.jpg`/`.jpeg`/`.webp` screenshots.
    - FIX-08 / FIX-10: Structured JSON bug report check and >=3 QA_REJECT ping-pong escalation guard.
    """
    raw_cwd_in, real_cwd, cwd_slot = _detect_caller_slot_safe(payload, args, cmd)
    try:
        if os.environ.get("ORCA_GUARD_FORCE_RAISE", "").strip() == "1":
            raise RuntimeError("Simulated guard internal exception (ORCA_GUARD_FORCE_RAISE=1)")

        managed = _is_orca_managed_project(payload, args)
        if not managed:
            if cwd_slot and (cwd_slot.startswith("qa-worker") or cwd_slot.startswith("ui-worker")):
                return (
                    f"[ORCA GUARD-ERROR] Không xác định được project root (thiếu 'orca-team.json') để verify cho slot '{cwd_slot}'. "
                    f"Thực thi Fail-Closed: CẤM cho qua lệnh báo cáo hoàn tất khi không tìm thấy cấu hình project!"
                )
            return ""

        # 1. [ORCA LOG-TRUNCATION GUARD]
        if cwd_slot and (cwd_slot.startswith("qa-worker") or cwd_slot.startswith("devops-worker")):
            if re.search(
                r"\b(?:docker(?:\s+compose|-compose)?|kubectl)\s+logs\b[^\n;|&]*\s--since(?:\s+|=)['\"]?(?:[1-9]\d?\s*s|[12]\d\d\s*s|[1-5]\s*m)\b",
                sanitized_orch,
            ):
                return (
                    "[ORCA LOG-TRUNCATION GUARD] CẤM dùng '--since <ngắn>' ('--since 30s' / '--since 2m' / '--since 5m') khi kiểm tra runtime logs trong QA/DevOps! "
                    "Cờ '--since' ngắn sẽ bỏ sót hoàn toàn các lỗi khởi tạo (startup/DI crash, schema/migration error) xảy ra lúc container vừa boot. "
                    "Cách sửa bắt buộc: Bỏ cờ '--since' và dùng 'docker compose logs --no-color --tail=300 <service>' (hoặc 'docker logs --tail 300 <container>') để quét toàn bộ lịch sử log từ lúc khởi động!"
                )

        no_heredoc = _strip_quoted_heredocs(cmd)
        expanded_cmd, unresolved_cats = _expand_inline_cat_substitutions(cmd, real_cwd)

        is_qa_slot = bool(
            (cwd_slot and cwd_slot.startswith("qa-worker"))
            or re.search(r"--from(?:\s+|=)['\"]?qa-worker\b", no_heredoc, re.IGNORECASE)
            or re.search(r"\b(?:QA_VERDICT\s*:\s*PASS|QA_FINAL|Worker\s+Done:\s*qa-worker)\b", expanded_cmd, re.IGNORECASE)
        )
        is_ui_slot = bool(
            (cwd_slot and cwd_slot.startswith("ui-worker"))
            or re.search(r"--from(?:\s+|=)['\"]?ui-worker\b", no_heredoc, re.IGNORECASE)
            or re.search(r"\b(?:UI\s+Ready|UI_READY|Worker\s+Done:\s*ui-worker)\b", expanded_cmd, re.IGNORECASE)
        )

        has_orch_send = bool(re.search(r"\borca\s+orchestration\s+send\b", sanitized_orch))
        has_completion_markers = bool(
            re.search(r"\b(?:QA_VERDICT\s*:\s*PASS|QA_FINAL|UI\s+Ready|UI_READY|worker_done)\b", expanded_cmd, re.IGNORECASE)
        )
        if not has_orch_send and not ((is_qa_slot or is_ui_slot) and has_completion_markers):
            return ""
        if not (is_qa_slot or is_ui_slot):
            return ""

        # Check FIX-08 & FIX-10 when sending QA_REJECT
        if is_qa_slot and re.search(r"\bQA_REJECT\b", expanded_cmd, re.IGNORECASE):
            rej_err = _check_qa_reject_and_ping_pong_guards(expanded_cmd, no_heredoc, real_cwd)
            if rej_err:
                return rej_err

        # Detect indirect variable/subshell flags on `orca orchestration send` (e.g. `--type "$T"`, `--body "$(cat ...)"`, `--body "$(jq ...)"`)
        has_indirect_type_or_body = bool(
            re.search(r"--(?:type|outcome|body|subject)(?:\s+|=)['\"]?(?:\$(?:\w+|\{|\()|[^'\"]*\$\()", no_heredoc)
            or unresolved_cats
        )

        # Allow explicit literal questions, explicit literal failed outcomes, or explicit literal FAIL/REJECT verdicts
        # (ONLY when not obfuscated with indirect `--type "$T"` and when escalation includes mandatory context or FAIL marker)
        has_explicit_fail_marker = bool(
            re.search(
                r"\b(?:QA_VERDICT\s*:\s*FAIL|QA_REJECT|INCOMPLETE_PLAN|TEST_FAIL|SEC_BLOCK|SECURITY_VERDICT\s*:\s*BLOCK|PERF_VERDICT\s*:\s*BOTTLENECK_FOUND|BLOCKED_BY_UNKNOWN)\b",
                expanded_cmd,
                re.IGNORECASE,
            )
        )
        is_literal_question = bool(re.search(r"--type(?:\s+|=)['\"]?question\b", no_heredoc))
        is_literal_escalation = bool(re.search(r"--type(?:\s+|=)['\"]?escalation\b", no_heredoc))
        is_literal_failed_outcome = bool(re.search(r"--outcome(?:\s+|=)['\"]?failed\b", no_heredoc))

        if not has_indirect_type_or_body:
            if is_literal_question or is_literal_failed_outcome or has_explicit_fail_marker:
                return ""
            if is_literal_escalation and not re.search(r"\b(?:QA_VERDICT\s*:\s*PASS|QA_FINAL|UI\s+Ready|UI_READY)\b", expanded_cmd, re.IGNORECASE):
                return ""

        is_completion_send = bool(
            has_orch_send
            or has_indirect_type_or_body
            or re.search(r"--type(?:\s+|=)['\"]?(?:handoff|worker_done)\b", no_heredoc)
            or has_completion_markers
        )
        if not is_completion_send:
            return ""
        if not real_cwd or not os.path.isdir(real_cwd):
            return (
                f"[ORCA GUARD-ERROR] Không xác định được thư mục làm việc hợp lệ ('{real_cwd}') cho slot '{cwd_slot or 'qa/ui'}'. "
                f"Thực thi Fail-Closed: CẤM gửi báo cáo PASS!"
            )

        # Determine dispatch_start_epoch from SQLite `dispatch_contexts`
        now_ts = time.time()
        dispatch_start_epoch = now_ts - 1800.0
        db_path = _find_orca_orchestration_db_path()
        if db_path:
            try:
                import sqlite3

                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.5)
                conn.row_factory = sqlite3.Row
                disp_id = _extract_cli_flag_value(no_heredoc, ("dispatch-id", "dispatch"))
                from_handle = _extract_cli_flag_value(no_heredoc, ("from",))
                row_dc = None
                if disp_id:
                    row_dc = conn.execute(
                        "SELECT CAST(strftime('%s', dc.created_at) AS INTEGER) AS c_ts "
                        "FROM dispatch_contexts dc WHERE dc.id = ? LIMIT 1",
                        (disp_id,),
                    ).fetchone()
                if not row_dc and from_handle:
                    row_dc = conn.execute(
                        "SELECT CAST(strftime('%s', dc.created_at) AS INTEGER) AS c_ts "
                        "FROM dispatch_contexts dc "
                        "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                        "WHERE dc.assignee_handle = ? OR wd.agent_terminal_handle = ? "
                        "ORDER BY dc.created_at DESC LIMIT 1",
                        (from_handle, from_handle),
                    ).fetchone()
                if not row_dc:
                    row_dc = conn.execute(
                        "SELECT CAST(strftime('%s', dc.created_at) AS INTEGER) AS c_ts "
                        "FROM dispatch_contexts dc "
                        "LEFT JOIN worker_dispatches wd ON wd.dispatch_id = dc.id "
                        "WHERE wd.worktree_id = ? OR wd.worktree_id = ? ORDER BY dc.created_at DESC LIMIT 1",
                        (real_cwd, f"path:{real_cwd}"),
                    ).fetchone()
                if row_dc and row_dc["c_ts"]:
                    c_val = float(row_dc["c_ts"])
                    if c_val > 1e9:
                        dispatch_start_epoch = c_val
                conn.close()
            except Exception:
                pass

        cutoff_ts = dispatch_start_epoch - 5.0
        try:
            dot_git = os.path.join(real_cwd, ".git")
            git_dir = dot_git
            if os.path.isfile(dot_git):
                with open(dot_git, "r", encoding="utf-8", errors="ignore") as gf:
                    first_ln = gf.readline().strip()
                if first_ln.lower().startswith("gitdir:"):
                    raw_gd = first_ln.split(":", 1)[1].strip()
                    git_dir = raw_gd if os.path.isabs(raw_gd) else os.path.abspath(os.path.join(real_cwd, raw_gd))
            orig_head_p = os.path.join(git_dir, "ORIG_HEAD")
            if os.path.isfile(orig_head_p):
                oh_mtime = os.path.getmtime(orig_head_p)
                if oh_mtime >= dispatch_start_epoch - 5.0:
                    cutoff_ts = max(cutoff_ts, oh_mtime + 2.0)
        except Exception:
            pass

        # FIX-05: Split report_skip_dirs (depth 4, scanning src/app/packages/...) vs image_skip_dirs (depth 2)
        report_skip_dirs = {
            ".git",
            "node_modules",
            ".next",
            "dist",
            "build",
            "target",
            "vendor",
            ".gradle",
            ".idea",
            "__pycache__",
            ".venv",
            "venv",
        }
        image_skip_dirs = report_skip_dirs | {
            "src",
            "app",
            "public",
            "static",
            "assets",
            "res",
            "android",
            "ios",
            "packages",
            "lib",
            "components",
            "pages",
            "views",
            "styles",
            "icons",
            "images",
            "img",
            "media",
            "fonts",
            "fixtures",
            "__snapshots__",
        }
        screenshot_name_re = re.compile(
            r"(?:screen|shot|captur|verif|audit|qa|ui[-_]|page|modal|drawer|dark|light|desktop|mobile|test|after|before|live|clone|orig|rebuilt|parity)",
            re.IGNORECASE,
        )
        non_report_json_names = {
            "orca-team.json",
            "package.json",
            "package-lock.json",
            "tsconfig.json",
            "jsconfig.json",
            "budget.json",
            "contracts.json",
            "composer.json",
            "manifest.json",
        }

        recent_reports: list[str] = []
        recent_images: list[str] = []
        try:
            for root_d, dirs, files in os.walk(real_cwd):
                rel_d = os.path.relpath(root_d, real_cwd)
                depth = 0 if rel_d == "." else len(rel_d.split(os.sep))
                dirs[:] = [
                    d for d in dirs
                    if d not in report_skip_dirs and (not d.startswith(".") or d == ".pytest_cache")
                ]
                if depth >= 4:
                    dirs[:] = []
                rel_parts = set() if rel_d == "." else set(rel_d.split(os.sep))
                in_img_skip = bool(rel_parts & image_skip_dirs) or any(p.startswith(".") for p in rel_parts)
                for fname in files:
                    low = fname.lower()
                    fp = os.path.join(root_d, fname)
                    try:
                        mtime = os.path.getmtime(fp)
                    except OSError:
                        continue
                    if mtime < cutoff_ts:
                        continue
                    if is_qa_slot:
                        if low == "lastfailed" and ".pytest_cache" in rel_d:
                            recent_reports.append(fp)
                        elif low.endswith(".tap"):
                            recent_reports.append(fp)
                        elif low.endswith(".xml") and any(k in low for k in ("junit", "report", "result", "test", "surefire", "failsafe", "xunit")):
                            recent_reports.append(fp)
                        elif low.endswith((".json", ".jsonl")) and low not in non_report_json_names and low != "evidence.jsonl":
                            recent_reports.append(fp)
                    if depth <= 2 and not in_img_skip and low.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        if rel_d != "." or screenshot_name_re.search(low):
                            recent_images.append(fp)
        except Exception:
            pass

        # 2. [ORCA QA REPORT-VERDICT MISMATCH GUARD] (FIX-03 & FIX-05)
        if is_qa_slot and recent_reports:
            for rp in sorted(recent_reports):
                _has_pass, rep_err = _inspect_test_report_file(rp, real_cwd)
                if rep_err:
                    return rep_err

        # Block indirect variable obfuscation (`--type "$T"` or missing `$(cat ...)`) in QA/UI slots (FIX-01)
        if has_indirect_type_or_body:
            return (
                "[ORCA INDIRECT-SEND GUARD] Phát hiện lệnh 'orca orchestration send' trong slot QA/UI dùng biến shell gián tiếp "
                "('--type \"$T\"' hoặc '--body \"$(cat ...)\"' không đọc được file)! "
                "CẤM ẩn loại tin nhắn hoặc nội dung báo cáo qua biến trung gian. "
                "Cách sửa bắt buộc: Truyền tường minh '--type worker_done' / '--type handoff' và nội dung báo cáo trực tiếp!"
            )

        # 2b. [ORCA QA EVIDENCE LEDGER & MERGE ANCESTRY GUARD] (FIX-02, FIX-04, FIX-07, FIX-09)
        if is_qa_slot:
            ledger_or_ancestry_err = _verify_qa_evidence_ledger_and_ancestry(
                real_cwd, no_heredoc, expanded_cmd, cwd_slot, dispatch_start_epoch
            )
            if ledger_or_ancestry_err:
                return ledger_or_ancestry_err

        # 3 & 4. [ORCA VISUAL BLANK-SCREEN GUARD] & [ORCA MULTIMODAL VISUAL INSPECTION GUARD] (FIX-05)
        valid_screenshots: list[str] = []
        for img_p in sorted(recent_images):
            low_img = img_p.lower()
            if low_img.endswith(".png"):
                uni_res = _check_png_uniformity(img_p)
                if uni_res is not None:
                    uni_ratio, w_px, h_px = uni_res
                    if w_px >= 150 and h_px >= 150:
                        valid_screenshots.append(img_p)
                        if uni_ratio >= 0.92:
                            rel_p = os.path.relpath(img_p, real_cwd)
                            return (
                                f"[ORCA VISUAL BLANK-SCREEN GUARD] Ảnh chụp màn hình '{rel_p}' ({w_px}x{h_px}px) vừa tạo có tới {uni_ratio * 100:.1f}% số dòng điểm ảnh đồng nhất (màn hình đen / trắng rỗng / sụp đổ layout)! "
                                f"CẤM gửi báo cáo PASS / UI Ready khi ảnh screenshot thực tế bị đen hoặc trống rỗng. "
                                f"Cách sửa bắt buộc: Dùng tool 'view_file' / 'read_file' / 'Read' để xem trực tiếp '{img_p}', kiểm tra lỗi render/layout và sửa lại UI hoặc báo cáo 'QA_VERDICT: FAIL'!"
                            )
            else:
                try:
                    if os.path.getsize(img_p) >= 1024:
                        valid_screenshots.append(img_p)
                except OSError:
                    pass

        if valid_screenshots:
            inspected_map: dict[str, float] = {}
            cache_p = _visual_inspection_cache_path()
            if os.path.isfile(cache_p):
                try:
                    with open(cache_p, "r", encoding="utf-8") as cf:
                        loaded = json.load(cf)
                    if isinstance(loaded, dict):
                        inspected_map = {str(k): float(v) for k, v in loaded.items() if isinstance(v, (int, float))}
                except Exception:
                    inspected_map = {}

            def _was_inspected(p: str) -> bool:
                rp = os.path.realpath(p)
                bp = os.path.basename(rp)
                min_ts = max(cutoff_ts - 120.0, os.path.getmtime(p) - 5.0)
                return (
                    inspected_map.get(rp, 0.0) >= min_ts
                    or inspected_map.get(p, 0.0) >= min_ts
                    or inspected_map.get(bp, 0.0) >= min_ts
                )

            inspected_imgs = [p for p in valid_screenshots if _was_inspected(p)]
            req_count = min(2, len(valid_screenshots))
            if len(inspected_imgs) < req_count:
                must_view = [os.path.relpath(p, real_cwd) for p in valid_screenshots if not _was_inspected(p)][:3]
                return (
                    f"[ORCA MULTIMODAL VISUAL INSPECTION GUARD] Bạn vừa tạo/cập nhật {len(valid_screenshots)} ảnh chụp màn hình trong phiên này nhưng mới mở xem {len(inspected_imgs)}/{req_count} ảnh tối thiểu bằng mắt thần Multimodal ('view_file' / 'read_file' / 'Read')! "
                    f"CẤM báo cáo PASS / UI Ready nếu chỉ chạy script tự động mà không mở xem trực tiếp file ảnh chụp màn hình. "
                    f"Cách sửa bắt buộc ngay lúc này: Gọi tool 'view_file' / 'read_file' / 'Read' trên ít nhất {req_count - len(inspected_imgs)} file ảnh ({', '.join(must_view)}) để kiểm tra trực quan bố cục & hiển thị, sau đó mới chạy lại lệnh 'orca orchestration send'!"
                )

        return ""
    except Exception as exc:
        # FIX-01: Slot-aware Fail-Closed exception handling
        is_qa_or_ui = bool(
            (cwd_slot and (cwd_slot.startswith("qa-worker") or cwd_slot.startswith("ui-worker")))
            or re.search(r"\b(?:QA_VERDICT\s*:\s*PASS|QA_FINAL|UI\s+Ready|UI_READY|Worker\s+Done:\s*(?:qa|ui)-worker)\b", cmd or "", re.IGNORECASE)
        )
        if is_qa_or_ui:
            return (
                f"[ORCA FAIL-CLOSED GUARD] Lỗi nội bộ trong guard khi xác minh bằng chứng cho slot '{cwd_slot or 'qa/ui'}' ({exc}). "
                f"Thực thi Fail-Closed: CẤM cho qua báo cáo hoàn tất khi guard gặp ngoại lệ!"
            )
        return ""


def _check_orca_cli_protocol_guards(cmd: str, sanitized_orch: str, payload: dict, args: dict) -> str:
    """
    Prevent recurring real-world Orca CLI errors:
    1. Unescaped backticks in double-quoted `--body "..."` / `--subject "..."` / `--question "..."`.
    2. Invalid `orca orchestration task-update` flags (`--spec`, `--task`, or invalid `--status`).
    3. Chaining `orca orchestration check --wait` with `worker-start`, `worker-retain`, or `task-create`.
    4. Duplicate concurrent `orca orchestration check --wait` (`waiter_exists`) in the same workspace.
    5. Mandatory 2-Way Debate (`ask` <-> `reply`) between Leader and `critic-worker`.
    6. Mandatory 3-Layer QA & UI Evidence Verification (`[ORCA QA & UI EVIDENCE GUARD]`).
    """
    bad_bt_flag = _has_unescaped_backtick_in_double_quoted_prose_flag(cmd)
    if bad_bt_flag:
        return (
            f"[ORCA SHELL QUOTING GUARD] Phát hiện dấu backtick (`...`) chưa escape bên trong chuỗi ngoặc kép (\"...\") của cờ '{bad_bt_flag}'! "
            f"Trong zsh/bash, dấu `...` nằm trong ngoặc kép sẽ bị shell thực thi ngầm như lệnh hệ thống trước khi chạy orca "
            f"(gây lỗi 'permission denied' / 'command not found' và làm mất chữ trong nội dung tin nhắn). "
            f"Cách sửa: Bọc giá trị của '{bad_bt_flag}' bằng dấu ngoặc đơn ('...'), hoặc bỏ dấu backtick, hoặc escape thành \\`."
        )

    critic_debate_err = _check_critic_two_way_debate_and_question_guards(cmd, sanitized_orch, payload, args)
    if critic_debate_err:
        return critic_debate_err

    qa_ui_err = _check_qa_and_ui_verification_guards(cmd, sanitized_orch, payload, args)
    if qa_ui_err:
        return qa_ui_err

    # 7. [ORCA SECRET-TOKEN LEAK GUARD] on `git commit`: prevent committing `.env` files or raw session/auth tokens in `capture/**`
    if re.search(r"\bgit\s+commit\b", sanitized_orch) and _is_orca_managed_project(payload, args):
        _, cwd_commit = _resolve_orca_team_bin_and_cwd(payload, args)
        real_commit_cwd = os.path.realpath(cwd_commit) if cwd_commit else ""
        if real_commit_cwd and os.path.isdir(real_commit_cwd):
            try:
                p_staged = subprocess.run(
                    ["git", "-C", real_commit_cwd, "diff", "--cached", "--name-only"],
                    capture_output=True,
                    text=True,
                    timeout=2.5,
                )
                staged_files = [x.strip() for x in (p_staged.stdout or "").splitlines() if x.strip()]
                for sf in staged_files:
                    base_sf = os.path.basename(sf).lower()
                    if (
                        base_sf == ".env"
                        or base_sf.endswith(".env")
                        or (base_sf.startswith(".env.") and not base_sf.endswith((".example", ".sample", ".template", ".test", ".dist")))
                    ):
                        return (
                            f"[ORCA SECRET-TOKEN LEAK GUARD] Phát hiện file biến môi trường nhạy cảm '{sf}' đang nằm trong git staging (`git commit`)! "
                            f"CẤM commit file '.env' chứa secret/token vào git repository. "
                            f"Cách sửa: Chạy `git reset HEAD -- \"{sf}\"` để gỡ file khỏi staging trước khi commit!"
                        )
                    full_sf = os.path.join(real_commit_cwd, sf)
                    if os.path.isfile(full_sf) and os.path.getsize(full_sf) <= 2 * 1024 * 1024:
                        with open(full_sf, "r", encoding="utf-8", errors="ignore") as f_cap:
                            cap_sample = f_cap.read(256 * 1024)
                        if re.search(
                            r"(?:[\"']authorization[\"']\s*:\s*[\"']Bearer\s+eyJ[A-Za-z0-9_-]{20,}|-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----|\b(?:glpat-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|sk-proj-[A-Za-z0-9_-]{30,}|AIzaSy[A-Za-z0-9_-]{33})\b)",
                            cap_sample,
                            re.IGNORECASE,
                        ):
                            return (
                                f"[ORCA SECRET-TOKEN LEAK GUARD] Phát hiện token xác thực / API key / private key sống bên trong file '{sf}' đang chờ commit! "
                                f"CẤM commit trực tiếp Bearer JWT / API Key / Private Key thật vào git. "
                                f"Cách sửa: Thay thế giá trị token trong '{sf}' bằng biến môi trường hoặc chuỗi ẩn `<REDACTED_TOKEN>`, hoặc gỡ file khỏi staging (`git reset HEAD -- \"{sf}\"`) trước khi commit!"
                            )
            except Exception:
                pass

    if "task-update" in sanitized_orch and re.search(r"\borca\s+orchestration\s+task-update\b", sanitized_orch):
        no_heredoc = _strip_quoted_heredocs(cmd)
        for seg_raw in _split_top_level_shell_segments(no_heredoc):
            if not re.search(r"\borca\s+orchestration\s+task-update\b", seg_raw):
                continue
            if re.search(r"\s--spec(?:\s+|=|$)", seg_raw):
                return (
                    "[ORCA CLI SYNTAX GUARD] Lệnh 'orca orchestration task-update' KHÔNG hỗ trợ cờ '--spec'! "
                    "Cú pháp chuẩn của task-update: 'orca orchestration task-update --id <task_id> --status <pending|ready|dispatched|completed|failed|blocked> "
                    "[--result <text>] [--run <run_id>] [--json]'. "
                    "Nếu cần cập nhật kế hoạch chi tiết, hãy dùng 'orca-team plan pin --run <RUN_ID> --content \"...\"' hoặc gửi tin nhắn qua 'orca orchestration send'."
                )
            if re.search(r"\s--task(?:\s+|=|$)", seg_raw) and not re.search(r"\s--id(?:\s+|=)", seg_raw):
                return (
                    "[ORCA CLI SYNTAX GUARD] Lệnh 'orca orchestration task-update' dùng cờ '--id <task_id>' (KHÔNG phải '--task')! "
                    "Cú pháp chuẩn: 'orca orchestration task-update --id <task_id> --status <pending|ready|dispatched|completed|failed|blocked> [--result <text>] [--json]'."
                )
            if re.search(
                r"--status(?:\s+|=)['\"]?(?:canceled|cancelled|done|todo|in_progress|assigned|review)\b",
                seg_raw,
                re.IGNORECASE,
            ):
                return (
                    "[ORCA CLI SYNTAX GUARD] Giá trị '--status' KHÔNG hợp lệ trong 'orca orchestration task-update'! "
                    "Các trạng thái hợp lệ của Orca CLI: pending, ready, dispatched, completed, failed, blocked "
                    "(nếu muốn đánh dấu hoàn tất hoặc đóng task, hãy dùng '--status completed' hoặc '--status failed')."
                )

    has_check_wait = bool(
        re.search(r"\borca\s+orchestration\s+check\b[^\n;|&]*\s--wait\b", sanitized_orch)
        or re.search(r"\borca-team\b[^\n;|&]*\b(?:wait-events|check-wait)\b", sanitized_orch)
    )
    if (
        not has_check_wait
        and re.search(r"(?:^|[;&|()\n]|\b(?:do|then|else|elif)\s+)\s*sleep\s+\d+", sanitized_orch)
        and re.search(
            r"\borca\s+orchestration\s+(?:check|worker-list|task-list|status|worker-read)\b",
            sanitized_orch,
        )
    ):
        return (
            "[ORCA POLLING GUARD] CẤM dùng 'sleep N && orca orchestration ...' để poll trạng thái Worker theo chu kỳ! "
            "Mỗi vòng lặp poll trên bất kỳ Agent CLI nào (Antigravity, Claude Code, Gemini CLI, Kimi, Codex) đều đốt 150,000–200,000 tokens. "
            "Cách sửa: Dùng 'orca-team wait-events [--ack <DELIVERY_ID>] [--run <RUN_ID>]' để chặn chờ sự kiện ở mức 0 token (hoặc để Orca tự động bơm thông báo vào PTY khi rảnh)!"
        )

    if has_check_wait:
        if re.search(r"\borca\s+orchestration\s+(?:worker-start|worker-retain|task-create)\b", sanitized_orch):
            return (
                "[ORCA CHECK-WAIT GUARD] CẤM nối chuỗi '&& orca-team wait-events' hoặc '&& orca orchestration check --wait' chung trong cùng 1 lệnh với "
                "'worker-start', 'worker-retain', hoặc 'task-create'! "
                "Lệnh wait-events là lệnh chặn chờ sự kiện (blocking wait); nối chung sẽ khiến tool call bị treo hoặc tạo trùng waiter. "
                "Cách sửa: Tách riêng phần 'worker-start' / 'worker-retain' / 'task-create' chạy trước, sau đó gọi 'orca-team wait-events' ở một lệnh riêng biệt."
            )
        if not os.environ.get("ORCA_GUARD_SKIP_WAITER_CHECK") and os.environ.get("ORCA_INTERNAL_WAIT") != "1":
            try:
                _, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
                real_cwd = os.path.realpath(cwd) if cwd else ""
                cur_run_m = re.search(r"--run(?:\s+|=)['\"]?(run_[A-Za-z0-9_-]+)", sanitized_orch)
                cur_run_id = cur_run_m.group(1) if cur_run_m else ""
                proc_ps = subprocess.run(
                    ["ps", "-eo", "pid=,ppid=,args="],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                my_pid = os.getpid()
                my_ppid = os.getppid()
                for line in (proc_ps.stdout or "").splitlines():
                    parts = line.strip().split(None, 2)
                    if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                        continue
                    p_id = int(parts[0])
                    p_ppid = int(parts[1])
                    if p_id in (my_pid, my_ppid):
                        continue
                    p_args = parts[2]
                    is_waiter_proc = (
                        (
                            "orca orchestration check" in p_args
                            and "--wait" in p_args
                        )
                        or (
                            "orca-team" in p_args
                            and any(w_sub in p_args for w_sub in ("wait-events", "check-wait"))
                        )
                    ) and not any(sh_tok in p_args for sh_tok in ("zsh -c", "bash -c", "sh -c", "orca-command-guard"))
                    if is_waiter_proc:
                        # Check if the existing waiter belongs to the same cwd
                        proc_lsof = subprocess.run(
                            ["lsof", "-a", "-p", str(p_id), "-d", "cwd", "-Fn"],
                            capture_output=True,
                            text=True,
                            timeout=1.0,
                        )
                        waiter_cwd = ""
                        for l_line in (proc_lsof.stdout or "").splitlines():
                            if l_line.startswith("n"):
                                waiter_cwd = os.path.realpath(l_line[1:].strip())
                                break
                        if waiter_cwd and real_cwd and waiter_cwd == real_cwd:
                            old_run_m = re.search(r"--run(?:\s+|=)['\"]?(run_[A-Za-z0-9_-]+)", p_args)
                            old_run_id = old_run_m.group(1) if old_run_m else ""
                            is_stale_waiter = (p_ppid <= 1) or bool(
                                cur_run_id and old_run_id and cur_run_id != old_run_id
                            )
                            if is_stale_waiter:
                                try:
                                    os.kill(p_id, signal.SIGTERM)
                                except OSError:
                                    pass
                                continue
                            return (
                                f"[ORCA WAITER-EXISTS GUARD] Đã có 1 tiến trình chờ sự kiện ('orca-team wait-events' / 'check --wait') đang chạy ngầm cho workspace này (PID {p_id})! "
                                f"CẤM gọi thêm lệnh wait song song (sẽ bị lỗi 'waiter_exists: A blocking check is already active'). "
                                f"Cách sửa: Không gọi thêm lệnh wait nữa; hãy kết thúc lượt (stop calling tools) để hệ thống tự động đánh thức bạn khi tiến trình wait hiện tại nhận được sự kiện!"
                            )
            except Exception:
                pass

    if _is_orca_managed_project(payload, args) and not _is_in_orca_meta_repo(payload, args):
        # Block shell commands reading internal Agent CLI transcript JSONL files directly
        if re.search(
            r"(?:[/\\]\.system_generated[/\\]logs[/\\]transcript[^/\\\s'\"]*\.jsonl|[/\\]\.kimi(?:-code)?[/\\](?:sessions[/\\][^\s'\"]*[/\\]wire\.jsonl|server[/\\]events[/\\]session_[^\s'\"]*\.jsonl)|[/\\]\.claude[/\\]projects[/\\][^\s'\"]*\.jsonl|[/\\]\.gemini[/\\]tmp[/\\][^\s'\"]*[/\\]chats[/\\]session-[^\s'\"]*\.json|[/\\]\.codex[/\\]sessions[/\\][^\s'\"]*\.jsonl)",
            sanitized_orch,
        ):
            return (
                "[ORCA TOKEN-LEAK GUARD] CẤM dùng lệnh shell đọc file nhật ký hội thoại nội bộ ('transcript.jsonl' / 'wire.jsonl' / session '.jsonl')! "
                "Đọc file này sẽ bơm đệ quy hàng trăm nghìn tokens vào Context Window và làm cạn kiệt Quota ngay lập tức. "
                "Cách sửa: Dùng 'orca-team wait-events' để nhận tin nhắn từ Worker hoặc 'orca orchestration worker-list --run <RUN_ID> --json' để xem trạng thái."
            )

        # Block infinite foreground commands (tail -f, docker logs -f, docker compose up without -d, foreground docker run server.js)
        # unless bounded by timeout/alarm or piped into head / grep -m
        has_timeout_wrapper = bool(
            re.search(r"\b(?:timeout|gtimeout)\s+\d+|\bperl\s+-e\s+['\"]alarm\b", sanitized_orch)
        )
        for seg_fg in _split_top_level_shell_segments(_strip_quoted_heredocs(cmd)):
            if re.search(r"\|\s*(?:head|grep\s+[^\n|]*-m\s*\d+)\b", seg_fg):
                continue
            if not has_timeout_wrapper and re.search(r"(?:^|\s)tail\s+(?:-[a-zA-Z]*[fF]\b|--follow\b)", seg_fg):
                return (
                    "[ORCA INFINITE-STREAM GUARD] CẤM chạy 'tail -f' / 'tail --follow' (treo vĩnh viễn không bao giờ kết thúc)! "
                    "Cách sửa: Dùng 'tail -n 80 <file>' để đọc log tĩnh."
                )
            if not has_timeout_wrapper and re.search(
                r"\bdocker(?:\s+compose|-compose)?\s+logs\b[^\n;|&]*(?:\s-[a-zA-Z]*f\b|\s--follow\b)",
                seg_fg,
            ) and "--until" not in seg_fg:
                return (
                    "[ORCA INFINITE-STREAM GUARD] CẤM chạy 'docker [compose] logs -f' / '--follow' (stream log vô tận làm treo Worker/Leader)! "
                    "Cách sửa: Bỏ cờ '-f' và dùng 'docker [compose] logs --tail 80 <service>'."
                )
            if re.search(r"\bdocker(?:\s+compose|-compose)\b[^\n;|&]*\bup\b", seg_fg):
                if not re.search(
                    r"(?:\s-[a-zA-Z]*d\b|\s--detach\b|\s--abort-on-container-exit\b|\s--exit-code-from\b|\s--help\b)",
                    seg_fg,
                ):
                    return (
                        "[ORCA INFINITE-DAEMON GUARD] CẤM chạy 'docker compose up' ở chế độ foreground (thiếu cờ '-d') vì tiến trình sẽ treo vĩnh viễn! "
                        "Cách sửa: Thêm cờ '-d' ('docker compose up -d')."
                    )
            if not has_timeout_wrapper and re.search(r"\bdocker\s+run\b", seg_fg):
                if not re.search(r"(?:\s-[a-zA-Z]*d\b|\s--detach\b|\s--help\b)", seg_fg):
                    if re.search(
                        r"\b(?:node\s+\S*(?:server|index|app|main)\.m?js|npm\s+(?:run\s+)?(?:start|dev|serve)|uvicorn\s+\S+|gunicorn\s+\S+|php\s+-S\s+\S+)\s*(?:2>&1|\s)*$",
                        seg_fg.strip(),
                    ):
                        return (
                            "[ORCA INFINITE-DAEMON GUARD] CẤM chạy 'docker run' ở chế độ foreground (thiếu cờ '-d') với entrypoint là tiến trình HTTP server chạy vô tận ('node .../server.js', 'npm start', ...) vì lệnh sẽ không bao giờ kết thúc! "
                            "Cách sửa: Thêm cờ '-d' vào 'docker run -d ...' rồi kiểm tra bằng 'docker logs --tail 50 <container>', hoặc bọc giới hạn thời gian bằng 'perl -e \"alarm 8; exec @ARGV\" docker run ...'."
                        )

    return ""


def _maybe_rewrite_raw_check_wait_to_wait_events(cmd: str, sanitized_orch: str) -> str:
    """
    If a Leader calls raw `orca orchestration check ...` (with `--wait`, `--ack`, or `--run`, and without `--terminal`),
    transparently rewrite it to `orca-team wait-events [--ack <ID>] [--run <ID>] [--types "<TYPES>"]`
    so that:
    1) `_keepalive` stderr noise, `"type": "heartbeat"` rows, and duplicate `payload` bodies are stripped (preventing Antigravity `<SYSTEM_MESSAGE>` 4KB/10KB truncation and 230k-token `view_file(task-XX.log)` turns), and
    2) Empty timeout wakeups and intermediate Wave completions while peers are still `working` are coalesced in the wrapper.
    """
    if os.environ.get("ORCA_INTERNAL_WAIT") == "1":
        return ""
    stripped = sanitized_orch.strip()
    if not re.match(r"^(?:\S*/)?orca\s+orchestration\s+check\b", stripped):
        return ""
    if re.search(r"\s--terminal(?:\s+|=|$)", stripped) or re.search(r"\s--peek\b", stripped):
        return ""
    if not (re.search(r"\s--wait\b", stripped) or re.search(r"\s--ack(?:\s+|=)", stripped) or re.search(r"\s--run(?:\s+|=)", stripped)):
        return ""
    if re.search(r"[;|&`\n<>]", stripped):
        return ""
    ack_m = re.search(r"--ack(?:\s+|=)['\"]?((?:dlv|delivery|deliv)_[A-Za-z0-9_-]+)", cmd)
    run_m = re.search(r"--run(?:\s+|=)['\"]?(run_[A-Za-z0-9_-]+)", cmd)
    types_m = re.search(r"--types(?:\s+|=)['\"]?([A-Za-z0-9_,-]+)", cmd)
    parts = ["orca-team", "wait-events"]
    if ack_m:
        parts.extend(["--ack", ack_m.group(1)])
    if run_m:
        parts.extend(["--run", run_m.group(1)])
    if types_m and types_m.group(1) != "worker_done,escalation,question,handoff":
        parts.extend(["--types", f'"{types_m.group(1)}"'])
    return " ".join(parts)


def _maybe_cap_worker_read_limit(cmd: str, sanitized_orch: str) -> str:
    """
    When `orca orchestration worker-read --dispatch <ctx>` is called without `--limit` (or with `--limit > 35`),
    it dumps up to 70KB (100 transcript turns or 120 PTY lines) into stdout, triggering 10KB truncation on every call.
    Transparently inject `--limit 20` (or cap `--limit` at 30) to reduce output by ~95% (70KB -> 2.8KB).
    """
    if not re.search(r"\borca\s+orchestration\s+worker-read\b", sanitized_orch):
        return ""

    def _cap_match(m: re.Match) -> str:
        seg = m.group(0)
        lim_m = re.search(r"(--limit(?:\s+|=))(\d+)", seg)
        if lim_m:
            try:
                val = int(lim_m.group(2))
                if val > 35:
                    return seg[: lim_m.start(2)] + "25" + seg[lim_m.end(2) :]
            except ValueError:
                pass
            return seg
        return re.sub(r"(\borca\s+orchestration\s+worker-read\b)", r"\1 --limit 20", seg, count=1)

    rewritten = re.sub(r"(?:\S*/)?orca\s+orchestration\s+worker-read\b[^\n;|&]*", _cap_match, cmd)
    return rewritten if rewritten != cmd else ""


def _maybe_bound_noisy_shell_commands(cmd: str, sanitized_orch: str) -> str:
    """
    Transparently bound common shell token-leak traps:
    1) `curl ... | head -n <N>` on single-line minified JSON/XML responses dumps 200KB–500KB because `head -n` counts lines, not bytes -> append ` | head -c 4096`.
    2) Standalone `head -n <N> <file.(json|xml|har|min.js|svg)>` on single-line files dumps 45KB–100KB -> append ` | head -c 4096`.
    3) `mvn ... (compile|test-compile|test|package|verify|install)` without `-B`/`Slf4jMavenTransferListener` dumps 1.3MB of download progress -> inject `-B -Dorg.slf4j.simpleLogger.log.org.apache.maven.cli.transfer.Slf4jMavenTransferListener=warn`.
    """
    new_cmd = cmd
    stripped = new_cmd.strip()
    if (
        re.search(r"\bcurl\b", sanitized_orch)
        and re.search(r"\|\s*head\s+(?:-n\s*\d+|-\d+)\s*$", stripped)
        and "head -c" not in stripped
    ):
        new_cmd = f"{stripped} | head -c 4096"
    elif (
        re.match(r"^head\s+(?:-n\s*\d+|-\d+)\s+\S+\.(?:json|xml|har|min\.js|svg)\s*$", stripped, re.IGNORECASE)
        and "head -c" not in stripped
    ):
        new_cmd = f"{stripped} | head -c 4096"
    elif re.match(r"^(?:\S*/)?orca\s+orchestration\s+run-list(?:\s+--json)?\s*$", stripped):
        if "--json" in stripped:
            new_cmd = f"{stripped} | jq '{{ok, result: {{runs: (.result.runs[:5])}}}}'"
        else:
            new_cmd = f"{stripped} | head -n 15"
    elif re.match(r"^(?:\S*/)?orca\s+terminal\s+list\s+--json\s*$", stripped):
        new_cmd = f"{stripped} | jq '{{ok, result: {{terminals: [.result.terminals[] | {{handle, title, worktreePath, branch, orphaned, connected}}]}}}}'"

    if (
        re.search(r"(?:^|[;&|()\n])\s*(?:\./)?mvn\s+(?:clean\s+)?(?:compile|test-compile|test|package|verify|install)\b", sanitized_orch)
        and "Slf4jMavenTransferListener" not in new_cmd
        and " -q" not in new_cmd
    ):
        new_cmd = re.sub(
            r"((?:^|[;&|()\n])\s*(?:\./)?mvn)\s+",
            r"\1 -B -Dorg.slf4j.simpleLogger.log.org.apache.maven.cli.transfer.Slf4jMavenTransferListener=warn ",
            new_cmd,
            count=1,
        )
    return new_cmd if new_cmd != cmd else ""


def _maybe_inject_buildkit_plain_progress(cmd: str, sanitized_orch: str) -> str:
    """
    When a Worker runs `docker compose ... build` / `docker compose ... up ... --build` / `docker build`
    inside Antigravity's PTY without `BUILDKIT_PROGRESS=plain` or `--progress`, BuildKit redraws its
    25-line TUI 10 times per second, inflating `task-XX.log` to 5–15 MB and truncating `<SYSTEM_MESSAGE>`.
    Prepending `export BUILDKIT_PROGRESS=plain; ` switches BuildKit to plain line-by-line logs.
    """
    if "BUILDKIT_PROGRESS" in cmd or "--progress" in cmd:
        return ""
    if not re.search(
        r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo)\s+)\s*(?:\S*/)?(?:docker\s+compose\b[^\n;|&]*\b(?:build\b|up\b[^\n;|&]*--build\b)|docker-compose\b[^\n;|&]*\b(?:build\b|up\b[^\n;|&]*--build\b)|docker\s+(?:buildx\s+)?build\b)",
        sanitized_orch,
    ):
        return ""
    return f"export BUILDKIT_PROGRESS=plain; {cmd}"


def _is_in_orca_meta_repo(payload: dict, args: dict) -> bool:
    """Return True if the current workspace/cwd is the `orca-multi-agent-orchestrator` repo itself."""
    candidates: list[str] = []
    if isinstance(args, dict):
        for k in ("Cwd", "cwd", "Path", "AbsolutePath", "file_path", "filePath", "path", "TargetFile"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
    if isinstance(payload, dict):
        for k in ("cwd",):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
        wps = payload.get("workspacePaths")
        if isinstance(wps, list):
            for wp in wps:
                if isinstance(wp, str) and wp.strip():
                    candidates.append(wp.strip())
    for c in candidates:
        if "orca-multi-agent-orchestrator" in c:
            return True
        try:
            if "orca-multi-agent-orchestrator" in os.path.realpath(c):
                return True
        except Exception:
            pass
    return False


def _is_orca_managed_project(payload: dict, args: dict) -> bool:
    """Return True if workspace/cwd belongs to a project with `orca-team.json` (excluding `orca-multi-agent-orchestrator`)."""
    if _is_in_orca_meta_repo(payload, args):
        return False
    _, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
    check_dirs: list[str] = []
    if cwd:
        check_dirs.append(cwd)
    if isinstance(payload, dict):
        wps = payload.get("workspacePaths")
        if isinstance(wps, list):
            for wp in wps:
                if isinstance(wp, str) and wp.strip():
                    check_dirs.append(wp.strip())
    for d in check_dirs:
        try:
            cur = os.path.realpath(d)
            if os.path.isfile(cur):
                cur = os.path.dirname(cur)
            for _ in range(6):
                if not cur or cur == "/":
                    break
                if os.path.isfile(os.path.join(cur, "orca-team.json")):
                    return True
                git_dot = os.path.join(cur, ".git")
                if os.path.isfile(git_dot):
                    with open(git_dot, "r", encoding="utf-8", errors="ignore") as gf:
                        fl = gf.readline().strip()
                    if "/.git/worktrees/" in fl:
                        g_val = fl.split(":", 1)[1].strip() if ":" in fl else fl
                        m_root = g_val.split("/.git/worktrees/", 1)[0]
                        if m_root and os.path.isfile(os.path.join(m_root, "orca-team.json")):
                            return True
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        except Exception:
            pass
    return False


def _is_leader_main_workspace(payload: dict, args: dict) -> bool:
    """Return True when the caller is the Coordinator/Leader operating on the Main Repository Root of an Orca-managed project (NOT a child worker worktree)."""
    if _is_in_orca_meta_repo(payload, args):
        return False
    if os.environ.get("ORCA_DISPATCH_ID") or os.environ.get("ORCA_WORKER_ROLE"):
        return False
    _, cwd = _resolve_orca_team_bin_and_cwd(payload, args)
    check_dirs: list[str] = []
    if isinstance(payload, dict):
        c_val = payload.get("cwd")
        if isinstance(c_val, str) and c_val.strip():
            check_dirs.append(c_val.strip())
        wps = payload.get("workspacePaths")
        if isinstance(wps, list):
            for wp in wps:
                if isinstance(wp, str) and wp.strip():
                    check_dirs.append(wp.strip())
    if cwd and cwd not in check_dirs:
        check_dirs.append(cwd)
    found_main_orca_root = False
    for d in check_dirs:
        try:
            cur = os.path.realpath(d)
            if os.path.isfile(cur):
                cur = os.path.dirname(cur)
            if "/orca/workspaces/" in cur.replace("\\", "/"):
                return False
            for _ in range(6):
                if not cur or cur == "/":
                    break
                git_dot = os.path.join(cur, ".git")
                if os.path.isfile(git_dot):
                    return False
                if os.path.isdir(git_dot) and os.path.isfile(os.path.join(cur, "orca-team.json")):
                    found_main_orca_root = True
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        except Exception:
            pass
    return found_main_orca_root


def _extract_normalized_tool_call(payload: dict) -> tuple[str, dict, str]:
    """
    Normalize tool name, tool args dict, and shell command across all 22 Agent CLIs supported by Orca:
    - Antigravity (`toolCall.name`, `toolCall.args.CommandLine`)
    - Claude Code / OpenClaude / Qwen / CodeBuddy / Qoder (`tool_name`, `tool_input.command`)
    - Gemini CLI (`tool_name`, `tool_input.command` on `BeforeTool`/`AfterTool`)
    - Kimi Code CLI (`toolName`, `toolInput.command`)
    - Cursor / Codex / Amp / Droid (`tool_name`/`toolName`/`tool`, `tool_input`/`input`/`arguments`/`args`)
    """
    if not isinstance(payload, dict):
        return "", {}, ""
    tc = payload.get("toolCall") or payload.get("tool_call") or {}
    if not isinstance(tc, dict):
        tc = {}
    tool_name = str(
        tc.get("name")
        or tc.get("toolName")
        or tc.get("tool_name")
        or payload.get("tool_name")
        or payload.get("toolName")
        or payload.get("tool")
        or payload.get("name")
        or ""
    ).strip()
    raw_args = (
        tc.get("args")
        or payload.get("tool_input")
        or payload.get("toolInput")
        or payload.get("input")
        or payload.get("arguments")
        or payload.get("args")
        or {}
    )
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            raw_args = parsed if isinstance(parsed, dict) else {}
        except Exception:
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}
    cmd = str(
        args.get("CommandLine")
        or args.get("command")
        or args.get("cmd")
        or payload.get("command")
        or ""
    )
    return tool_name, args, cmd


def _check_unbounded_task_log_view(payload: dict, args: dict, tc: dict | None = None, tool_name_hint: str = "") -> str:
    """
    1. Block internal subagent tools (`invoke_subagent`, `Agent`, `AgentSwarm`, `Task`, `TowerInit`, `dispatch_agent`, `spawn_subagent`, `delegate_task`)
       and interactive `EnterPlanMode` inside Orca-managed projects (forces proper Orca worktree worker orchestration across all Agent CLIs).
    2. Block background task polling tools across all Agent CLIs inside Orca-managed projects:
       - Antigravity: `manage_task(Action="status"|"list")` and `schedule(DurationSeconds=...)`
       - Kimi Code CLI: `TaskOutput` / `TaskList` / `WaitFor` / `CronCreate` (Kimi has `automatic_notification: true` on `run_in_background=true`)
       - Claude Code / OpenClaude / Qwen: `TaskOutput(block=false)` / `BashOutput` (Claude Code delivers `<task-notification>` automatically)
    3. Block `view_file` / `Read` / `read_file` on `.system_generated/logs/transcript*.jsonl`, `wire.jsonl`, and CLI session `.jsonl`/`.json` files.
    4. Block `view_file` / `Read` / `read_file` on in-progress `.system_generated/tasks/task-*.log` (0 bytes or `_keepalive` without `"ok":true`)
       or running Kimi `tasks/bash-*.json`, and block unbounded reads (> 120 lines) on `task-*.log`.
    """
    tc_obj = tc if isinstance(tc, dict) else {}
    tool_name = (
        tool_name_hint
        or str(
            tc_obj.get("name")
            or tc_obj.get("toolName")
            or (payload or {}).get("tool_name")
            or (payload or {}).get("toolName")
            or (payload or {}).get("tool")
            or ""
        )
    ).strip().lower()

    is_subagent_tool = tool_name in (
        "invoke_subagent",
        "agent",
        "agentswarm",
        "agent_swarm",
        "towerinit",
        "tower_init",
        "dispatch_agent",
        "spawn_subagent",
        "delegate_task",
    ) or (
        tool_name == "task"
        and isinstance(args, dict)
        and any(k in args for k in ("subagent_type", "subagentType", "prompt", "description"))
    )
    if is_subagent_tool and _is_orca_managed_project(payload, args):
        return (
            f"[ORCA SUBAGENT GUARD] CẤM gọi tool subagent nội bộ ('{tool_name}') trong dự án điều phối bằng Orca! "
            "Gọi subagent nội bộ tại Leader/Worker làm nhân đôi Context Window (200k+ tokens/lượt) và phá vỡ cơ chế Worktree độc lập của Orca. "
            "Cách sửa: Nếu cần tham vấn kiến trúc liên tầng phức tạp, hãy dùng 'critic-worker' của Orca; nếu là tác vụ cục bộ 1 module hoặc rõ nguyên nhân, hãy ghim thẳng 'orca-team plan pin' và giao 'dev-worker'!"
        )

    if tool_name in ("enterplanmode", "enter_plan_mode") and _is_orca_managed_project(payload, args):
        return (
            f"[ORCA PLAN-MODE GUARD] CẤM gọi '{tool_name}' (chế độ Plan Mode tương tác của CLI sẽ khóa quyền ghi và chặn đứng tiến trình chờ người dùng bấm duyệt 'ExitPlanMode')! "
            "Cách sửa: Nếu bạn là Leader, hãy ghim kế hoạch bằng lệnh 'orca-team plan pin --run <RUN_ID> --title \"...\" --content \"[P1]...\"'; nếu bạn là Worker, hãy thực thi thẳng nhiệm vụ trong Role Spec!"
        )

    if tool_name == "manage_task" and isinstance(args, dict) and _is_orca_managed_project(payload, args):
        act = str(args.get("Action") or args.get("action") or "").strip().lower()
        if act in ("status", "list"):
            return (
                f"[ORCA REACTIVE-WAKEUP GUARD] CẤM gọi 'manage_task(Action=\"{act}\")' để poll trạng thái tác vụ ngầm! "
                "Mỗi lượt poll 'manage_task' ngốn ~200,000 tokens mà không giúp lệnh chạy nhanh hơn. "
                "Cách sửa: DỪNG GỌI TOOL NGAY LẬP TỨC (stop calling tools to end your turn) — hệ thống sẽ TỰ ĐỘNG đánh thức bạn (Reactive Wakeup) kèm toàn bộ kết quả ngay khi tác vụ ngầm hoàn tất!"
            )

    if tool_name in ("schedule", "croncreate", "cron_create") and isinstance(args, dict) and _is_orca_managed_project(payload, args):
        dur = args.get("DurationSeconds") if args.get("DurationSeconds") is not None else args.get("duration_seconds")
        cron = args.get("CronExpression") if args.get("CronExpression") is not None else (args.get("cron_expression") or args.get("cron"))
        if tool_name in ("croncreate", "cron_create") or (dur is not None and not cron):
            return (
                f"[ORCA REACTIVE-WAKEUP GUARD] CẤM gọi '{tool_name}' để hẹn giờ poll tác vụ ngầm hoặc chờ Worker! "
                "Mỗi lần timer nổ khi tác vụ vẫn đang chạy sẽ đánh thức LLM vô ích và đốt 200,000+ tokens/lượt. "
                "Cách sửa: DỪNG GỌI TOOL NGAY LẬP TỨC (stop calling tools to end your turn) — hệ thống sẽ TỰ ĐỘNG đánh thức bạn ngay khi background task hoàn tất!"
            )

    if tool_name in ("taskoutput", "task_output", "tasklist", "task_list", "bashoutput", "bash_output", "waitfor", "wait_for") and isinstance(args, dict) and _is_orca_managed_project(payload, args):
        is_kimi_payload = isinstance(payload, dict) and "toolName" in payload and "toolInput" in payload
        block_param = args.get("block")
        if is_kimi_payload or tool_name in ("tasklist", "task_list", "bashoutput", "bash_output", "waitfor", "wait_for") or block_param is False:
            return (
                f"[ORCA REACTIVE-WAKEUP GUARD] CẤM gọi '{tool_name}' để poll hoặc chờ lặp trạng thái tác vụ ngầm! "
                "Trong Kimi Code CLI / Claude Code và Orca, các tiến trình ngầm (run_in_background=true) và hộp thư Orca đều có cơ chế TỰ ĐỘNG thông báo (automatic_notification / <task-notification> / Orca PTY Mailbox Pointer Delivery) ngay khi hoàn tất. "
                "Cách sửa: DỪNG GỌI TOOL NGAY LẬP TỨC (kết thúc lượt) để hệ thống tự động đánh thức bạn khi tác vụ hoàn tất, hoặc chạy 'orca-team wait-events' ở chế độ đồng bộ!"
            )

    if not isinstance(args, dict):
        return ""

    # Block Leader in main workspace from calling figma:get_design_context or figma:get_screenshot
    # (Each call dumps 50KB-116KB JSX/Tailwind into steps/<N>/output.txt and triggers 15+ read_file turns + CHECKPOINT compaction before Wave 1)
    if tool_name in ("call_mcp_tool", "mcp_figma_get_design_context", "mcp_figma_get_screenshot", "get_design_context", "get_screenshot"):
        is_figma_heavy = tool_name in ("mcp_figma_get_design_context", "mcp_figma_get_screenshot", "get_design_context", "get_screenshot")
        if tool_name == "call_mcp_tool":
            srv_n = str(args.get("ServerName") or args.get("server_name") or "").strip().lower()
            m_tool = str(args.get("ToolName") or args.get("tool_name") or "").strip().lower()
            if srv_n == "figma" and m_tool in ("get_design_context", "get_screenshot"):
                is_figma_heavy = True
        if is_figma_heavy and _is_leader_main_workspace(payload, args):
            return (
                "[ORCA TOKEN-LEAK GUARD] CẤM Leader (Coordinator) gọi trực tiếp `figma:get_design_context` hoặc `figma:get_screenshot`! "
                "Mỗi lệnh này xả 50KB–116KB mã JSX/Tailwind thô vào `.system_generated/steps/<N>/output.txt`, khiến Leader phải đọc phân mảnh hàng chục lượt và làm tràn Context Window (CHECKPOINT compaction) trước cả khi tạo Wave 1. "
                "Cách sửa: Leader chỉ truyền URL Figma / `nodeId` vào Task Spec để `ui-worker` hoặc `dev-worker` trực tiếp gọi `get_design_context` & `get_screenshot` bên trong worktree cô lập của Worker!"
            )

    fp = str(
        args.get("Path")
        or args.get("AbsolutePath")
        or args.get("file_path")
        or args.get("filePath")
        or args.get("path")
        or args.get("TargetFile")
        or ""
    ).strip()
    if not fp:
        return ""

    # Block Leader in main workspace from directly writing/editing project code files instead of delegating to Workers,
    # and enforce C-FIX-08 Read-Only Guard for critic-worker
    if tool_name in (
        "write_file",
        "edit_file",
        "write_to_file",
        "write",
        "edit",
        "multiedit",
        "replace_file_content",
        "multi_replace_file_content",
        "writefile",
        "strreplacefile",
        "apply_patch",
        "create_file",
    ):
        _, real_cwd_f, cwd_slot_f = _detect_caller_slot_safe(payload, args, "")
        norm_fp = (
            os.path.abspath(os.path.expanduser(fp))
            if os.path.isabs(os.path.expanduser(fp))
            else os.path.abspath(os.path.join(real_cwd_f or os.getcwd(), os.path.expanduser(fp)))
        )
        if cwd_slot_f and cwd_slot_f.startswith("critic-worker"):
            if not re.search(r"[/\\]\.orca[/\\]runs[/\\][^/\\]+[/\\]critic[/\\]", norm_fp) and not re.search(
                r"(?:^|[/\\])\.orca[/\\]runs[/\\][^/\\]+[/\\]critic[/\\]", fp
            ):
                return _handle_critic_gate_violation(
                    "[ORCA CRITIC READ-ONLY GUARD] critic-worker chỉ được ghi dưới .orca/runs/<run>/critic/.",
                    "",
                    f"{tool_name} {fp}",
                )
        is_exempt_leader_write = (
            "/orca-multi-agent-orchestrator/" in norm_fp
            or "/.orca/" in norm_fp
            or "/.worktrees/" in norm_fp
            or "/orca/workspaces/" in norm_fp
            or os.path.basename(norm_fp) in ("orca-team.json", ".gitignore")
        )
        if not is_exempt_leader_write and _is_leader_main_workspace(payload, args):
            # Verify norm_fp itself resides inside the Orca main repo root (so scratch files in /tmp/spec.md are allowed)
            in_main_repo_tree = False
            cur_p = os.path.dirname(norm_fp)
            for _ in range(8):
                if not cur_p or cur_p == "/":
                    break
                git_marker = os.path.join(cur_p, ".git")
                if os.path.isfile(git_marker):
                    in_main_repo_tree = False
                    break
                if os.path.isdir(git_marker) and os.path.isfile(os.path.join(cur_p, "orca-team.json")):
                    in_main_repo_tree = True
                    break
                nxt_p = os.path.dirname(cur_p)
                if nxt_p == cur_p:
                    break
                cur_p = nxt_p
            if in_main_repo_tree:
                return (
                    f"[ORCA COORDINATOR DELEGATION GUARD] CẤM Leader trực tiếp ghi/sửa file mã nguồn dự án ('{os.path.basename(norm_fp)}') tại Main Repo! "
                    "Việc Leader tự viết code trực tiếp làm phình Context Window hàng triệu tokens (gây 3–4 lần CHECKPOINT compaction) và phá vỡ quy trình kiểm thử độc lập của Worker + QA. "
                    "Cách sửa: Khởi tạo Orca Run (`orca-project-init -y` / `orca orchestration run-create`), tạo Task Spec và dispatch cho `dev-worker` / `ui-worker` / `devops-worker` thực thi trong git worktree cô lập!"
                )

    # Block reading internal conversation transcript JSONL/JSON files across all Agent CLIs (outside orca-multi-agent-orchestrator)
    if re.search(
        r"(?:[/\\]\.system_generated[/\\]logs[/\\]transcript[^/\\]*\.jsonl|[/\\]\.kimi(?:-code)?[/\\](?:sessions[/\\].*[/\\]wire\.jsonl|server[/\\]events[/\\]session_[^/\\]*\.jsonl)|[/\\]\.claude[/\\]projects[/\\].*\.jsonl|[/\\]\.gemini[/\\]tmp[/\\].*[/\\]chats[/\\]session-[^/\\]*\.json|[/\\]\.codex[/\\]sessions[/\\].*\.jsonl)$",
        fp,
    ):
        if not _is_in_orca_meta_repo(payload, args):
            return (
                f"[ORCA TOKEN-LEAK GUARD] CẤM đọc file nhật ký hội thoại nội bộ ('{os.path.basename(fp)}')! "
                "Đọc file này sẽ bơm đệ quy hàng trăm nghìn tokens lịch sử hội thoại vào Context Window và làm cạn kiệt Quota ngay lập tức. "
                "Cách sửa: Dùng 'orca-team wait-events' để nhận tin nhắn từ Worker hoặc 'orca orchestration worker-list --run <RUN_ID> --json' để xem trạng thái."
            )

    # Block polling running Kimi background bash task files (`~/.kimi-code/sessions/.../tasks/bash-*.json`)
    if re.search(r"[/\\]\.kimi(?:-code)?[/\\]sessions[/\\].*[/\\]tasks[/\\](?:bash|agent)-[^/\\]*\.json$", fp):
        if not _is_in_orca_meta_repo(payload, args) and os.path.isfile(fp):
            try:
                with open(fp, "rb") as kf:
                    k_b = kf.read(4096)
                if b'"status":"running"' in k_b or b'"status": "running"' in k_b:
                    return (
                        f"[ORCA REACTIVE-WAKEUP GUARD] Tác vụ ngầm '{os.path.basename(fp)}' vẫn đang chạy (status=running)! "
                        "CẤM đọc/poll file trạng thái tác vụ ngầm. Hãy kết thúc lượt (stop calling tools) — Kimi sẽ tự động gửi thông báo ngay khi tiến trình ngầm hoàn tất!"
                    )
            except OSError:
                pass

    is_task_log = bool(re.search(r"[/\\]\.system_generated[/\\]tasks[/\\]task-\d+\.log$", fp))
    is_step_output = bool(re.search(r"[/\\]\.system_generated[/\\]steps[/\\]\d+[/\\]output\.txt$", fp))
    if not is_task_log and not is_step_output:
        return ""

    base_log = os.path.basename(fp)
    if is_step_output:
        if _is_in_orca_meta_repo(payload, args) or not os.path.isfile(fp):
            return ""
        try:
            step_f_sz = os.path.getsize(fp)
        except OSError:
            return ""
        if step_f_sz <= 12000:
            return ""
        is_leader_ctx = _is_leader_main_workspace(payload, args)
        max_step_lines = 50 if is_leader_ctx else 90
        max_slice_bytes = 4000 if is_leader_ctx else 6500
        start_ln = args.get("StartLine") if args.get("StartLine") is not None else (args.get("start_line") if args.get("start_line") is not None else (args.get("offset") if args.get("offset") is not None else args.get("line_offset")))
        end_ln = args.get("EndLine") if args.get("EndLine") is not None else args.get("end_line")
        limit_ln = args.get("limit") if args.get("limit") is not None else args.get("n_lines")
        byte_off = args.get("ByteOffset") if args.get("ByteOffset") is not None else args.get("byte_offset")
        s_i = None
        e_i = None
        if byte_off is None and start_ln is not None and end_ln is not None:
            try:
                s_i = int(start_ln)
                e_i = int(end_ln)
            except (ValueError, TypeError):
                s_i, e_i = None, None
        elif byte_off is None and start_ln is not None and limit_ln is not None:
            try:
                s_i = int(start_ln)
                e_i = s_i + int(limit_ln) - 1
            except (ValueError, TypeError):
                s_i, e_i = None, None
        if s_i is not None and e_i is not None and s_i >= 1 and e_i >= s_i and (e_i - s_i) <= max_step_lines:
            slice_bytes = 0
            try:
                with open(fp, "rb") as sf:
                    for idx, raw_ln in enumerate(sf, start=1):
                        if idx < s_i:
                            continue
                        if idx > e_i:
                            break
                        slice_bytes += len(raw_ln)
                        if slice_bytes > max_slice_bytes:
                            break
            except OSError:
                slice_bytes = 0
            if slice_bytes <= max_slice_bytes:
                return ""
            return (
                f"[ORCA TOKEN-LEAK GUARD] Khoảng dòng {s_i}–{e_i} của '.system_generated/steps/.../{base_log}' chứa các dòng rất dài (tổng dung lượng > {max_slice_bytes // 1000}KB, vượt ngưỡng cắt 10KB và sẽ tạo file output.txt đệ quy)! "
                f"Cách sửa: (1) Giảm khoảng dòng xuống <= 25–30 dòng mỗi lần gọi `read_file`, HOẶC (2) dùng lệnh `python3 -c` / `jq` / `rg -n` để trích xuất đúng trường JSON/JSX cần lấy!"
            )
        return (
            f"[ORCA TOKEN-LEAK GUARD] File kết quả '.system_generated/steps/.../{base_log}' có dung lượng lớn ({step_f_sz // 1024}KB)! "
            f"CẤM gọi `read_file`/`view_file` không giới hạn dòng, truyền khoảng dòng > {max_step_lines} dòng, dùng `ByteOffset`, hoặc chỉ truyền `EndLine` mà thiếu `StartLine` (nếu thiếu `StartLine`, công cụ sẽ đọc lặp lại từ dòng 1 và bơm 46KB = ~15,000 tokens rác vào Context mỗi lượt!). "
            f"Cách sửa bắt buộc: (1) Dùng `rg -n` hoặc `grep -n` trên file '{fp}' để tìm chính xác số dòng cần xem, sau đó (2) gọi `read_file` với ĐẦY ĐỦ cả `StartLine` VÀ `EndLine` sao cho `EndLine - StartLine <= {max_step_lines}` (và dung lượng đoạn đọc <= {max_slice_bytes // 1000}KB)!"
        )

    if not _is_in_orca_meta_repo(payload, args) and os.path.isfile(fp):
        try:
            f_sz = os.path.getsize(fp)
            if f_sz == 0:
                return (
                    f"[ORCA TOKEN-LEAK GUARD] File log tác vụ ngầm '{base_log}' đang rỗng (0 bytes — tiến trình ngầm vẫn đang chạy/chờ sự kiện)! "
                    f"CẤM đọc/poll '{base_log}' (mỗi lượt đọc ngốn 200,000+ tokens). "
                    f"Cách sửa: Dừng gọi tool ngay lập tức (stop calling tools) — hệ thống sẽ tự động đánh thức bạn ngay khi tiến trình ngầm hoàn tất!"
                )
            with open(fp, "rb") as lf:
                head_b = lf.read(2048)
                tail_b = head_b
                if f_sz > 2048:
                    lf.seek(max(0, f_sz - 4096))
                    tail_b = lf.read(4096)
            if b'"_keepalive":true' in head_b:
                if b'"ok":true' not in tail_b and b'"ok": true' not in tail_b:
                    return (
                        f"[ORCA TOKEN-LEAK GUARD] Tiến trình chờ sự kiện trong '{base_log}' vẫn đang chạy ngầm chờ Worker (chỉ mới có dòng _keepalive, chưa có kết quả cuối)! "
                        f"CẤM đọc/poll '{base_log}' (mỗi lượt đọc ngốn 200,000+ tokens). "
                        f"Cách sửa: Dừng gọi tool ngay lập tức (stop calling tools) — hệ thống sẽ tự động đánh thức bạn khi Worker gửi tin nhắn hoặc hoàn tất! "
                        f"(Lưu ý: Từ lượt sau hãy dùng 'orca-team wait-events [--ack <DELIVERY_ID>]' thay cho 'orca orchestration check --wait')."
                    )
        except OSError:
            pass

    start_ln = (
        args.get("StartLine")
        if args.get("StartLine") is not None
        else (
            args.get("start_line")
            if args.get("start_line") is not None
            else (args.get("offset") if args.get("offset") is not None else args.get("line_offset"))
        )
    )
    end_ln = args.get("EndLine") if args.get("EndLine") is not None else args.get("end_line")
    limit_ln = args.get("limit") if args.get("limit") is not None else args.get("n_lines")
    if start_ln is not None and end_ln is not None:
        try:
            s_i = int(start_ln)
            e_i = int(end_ln)
            if s_i >= 1 and e_i >= s_i and (e_i - s_i) <= 120:
                return ""
        except (ValueError, TypeError):
            pass
    elif limit_ln is not None:
        try:
            if 1 <= int(limit_ln) <= 120:
                return ""
        except (ValueError, TypeError):
            pass
    elif start_ln is not None:
        try:
            if -120 <= int(start_ln) < 0:
                return ""
        except (ValueError, TypeError):
            pass
    return (
        f"[ORCA TOKEN-LEAK GUARD] CẤM đọc toàn bộ file log tác vụ ngầm '{base_log}' không giới hạn dòng! "
        f"Mỗi lần đọc thẳng '{base_log}' không truyền StartLine/EndLine (hoặc offset/limit / line_offset/n_lines) sẽ bơm 10,000–200,000 tokens rác vào bộ nhớ ngữ cảnh. "
        f"Cách sửa: Chỉ định rõ khoảng dòng ngắn (<= 100 dòng), hoặc dùng lệnh shell `tail -n 50 \"{fp}\"` để chỉ xem phần kết quả cuối!"
    )


KIMI_GUARD_START_MARKER = "# >>> orca-command-guard-hooks (managed by Orca Multi-Agent Orchestrator) >>>"
KIMI_GUARD_END_MARKER = "# <<< orca-command-guard-hooks <<<"


def ensure_all_cli_command_guard_hooks(guard_bin: str | None = None, check_only: bool = False) -> tuple[bool, list[str], list[str]]:
    """
    Ensure `orca-command-guard` is registered across all installed Agent CLIs (without disturbing Orca's native status hooks):
      1. Antigravity (`~/.gemini/config/hooks.json`)
      2. Claude Code (`~/.claude/settings.json`) & OpenClaude / Qwen / CodeBuddy if present
      3. Gemini CLI (`~/.gemini/settings.json`)
      4. Kimi Code CLI (`~/.kimi-code/config.toml`)
      5. Cursor Agent (`~/.cursor/hooks.json`) if present
    Returns `(all_up_to_date, configured_targets, updated_or_missing_targets)`.
    """
    g_bin = guard_bin or os.path.expanduser("~/.local/bin/orca-command-guard")
    home = Path.home()
    configured: list[str] = []
    missing_or_updated: list[str] = []

    pre_cmd_agy = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_EVENT=PreToolUse python3 '{g_bin}' --event PreToolUse; "
        f"else printf '{{\"decision\":\"ask\"}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    post_cmd_agy = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_EVENT=PostToolUse python3 '{g_bin}' --event PostToolUse; "
        f"else printf '{{}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    pre_cmd_generic = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_EVENT=PreToolUse python3 '{g_bin}' --event PreToolUse; "
        f"else printf '{{}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    post_cmd_generic = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_EVENT=PostToolUse python3 '{g_bin}' --event PostToolUse; "
        f"else printf '{{}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    pre_cmd_gemini = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_CLIENT=gemini ORCA_GUARD_EVENT=BeforeTool python3 '{g_bin}' --event BeforeTool; "
        f"else printf '{{}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    post_cmd_gemini = (
        f"if [ -r '{g_bin}' ]; then ORCA_GUARD_CLIENT=gemini ORCA_GUARD_EVENT=AfterTool python3 '{g_bin}' --event AfterTool; "
        f"else printf '{{}}\\n'; cat >/dev/null 2>&1 || :; fi"
    )
    agy_matcher = "run_command|view_file|read_file|Read|write_file|edit_file|Write|Edit|MultiEdit|replace_file_content|multi_replace_file_content|call_mcp_tool|invoke_subagent|send_message|manage_task|schedule"

    # 1. Antigravity (~/.gemini/config/hooks.json)
    agy_path = home / ".gemini" / "config" / "hooks.json"
    try:
        agy_data = {}
        if agy_path.exists():
            agy_data = json.loads(agy_path.read_text(encoding="utf-8"))
        if isinstance(agy_data, dict):
            pre_entry = (agy_data.get("orca-command-guard") or {}).get("PreToolUse", [{}])[0]
            curr_pre = (pre_entry.get("hooks") or [{}])[0].get("command", "")
            curr_m = str(pre_entry.get("matcher") or "")
            if curr_pre == pre_cmd_agy and curr_m == agy_matcher:
                configured.append("antigravity")
            else:
                missing_or_updated.append("antigravity")
                if not check_only:
                    agy_path.parent.mkdir(parents=True, exist_ok=True)
                    agy_data["orca-command-guard"] = {
                        "PreToolUse": [{"matcher": agy_matcher, "hooks": [{"type": "command", "command": pre_cmd_agy, "timeout": 5}]}],
                        "PostToolUse": [{"matcher": "run_command", "hooks": [{"type": "command", "command": post_cmd_agy, "timeout": 5}]}],
                    }
                    tmp_p = agy_path.with_name(f"{agy_path.name}.{os.getpid()}.orca-tmp")
                    tmp_p.write_text(json.dumps(agy_data, indent=2) + "\n", encoding="utf-8")
                    os.replace(tmp_p, agy_path)
                    configured.append("antigravity")
    except Exception:
        missing_or_updated.append("antigravity")

    # 2. Claude Code family (~/.claude/settings.json, ~/.openclaude/settings.json, ~/.qwen/settings.json, ~/.codebuddy/settings.json)
    for cli_label, dir_name in (
        ("claude", ".claude"),
        ("openclaude", ".openclaude"),
        ("qwen-code", ".qwen"),
        ("codebuddy", ".codebuddy"),
    ):
        cfg_dir = home / dir_name
        cfg_path = cfg_dir / "settings.json"
        if not cfg_path.exists():
            continue
        try:
            c_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(c_data, dict):
                continue
            hooks_obj = c_data.get("hooks")
            if not isinstance(hooks_obj, dict):
                hooks_obj = {}
            pre_list = hooks_obj.get("PreToolUse") if isinstance(hooks_obj.get("PreToolUse"), list) else []
            post_list = hooks_obj.get("PostToolUse") if isinstance(hooks_obj.get("PostToolUse"), list) else []

            def _has_exact_guard(arr: list, expected_cmd: str) -> bool:
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    for h in item.get("hooks") or []:
                        if isinstance(h, dict) and h.get("command") == expected_cmd:
                            return True
                return False

            def _strip_old_guard(arr: list) -> list:
                cleaned_arr = []
                for item in arr:
                    if not isinstance(item, dict):
                        cleaned_arr.append(item)
                        continue
                    h_list = item.get("hooks")
                    if isinstance(h_list, list):
                        kept_h = [
                            h for h in h_list
                            if not (isinstance(h, dict) and "orca-command-guard" in str(h.get("command") or ""))
                        ]
                        if kept_h:
                            new_item = dict(item)
                            new_item["hooks"] = kept_h
                            cleaned_arr.append(new_item)
                    elif "orca-command-guard" not in str(item.get("command") or ""):
                        cleaned_arr.append(item)
                return cleaned_arr

            if _has_exact_guard(pre_list, pre_cmd_generic) and _has_exact_guard(post_list, post_cmd_generic):
                configured.append(cli_label)
            else:
                missing_or_updated.append(cli_label)
                if not check_only:
                    new_pre = _strip_old_guard(pre_list)
                    new_pre.append({"matcher": "*", "hooks": [{"type": "command", "command": pre_cmd_generic, "timeout": 10}]})
                    new_post = _strip_old_guard(post_list)
                    new_post.append({"matcher": "*", "hooks": [{"type": "command", "command": post_cmd_generic, "timeout": 10}]})
                    hooks_obj["PreToolUse"] = new_pre
                    hooks_obj["PostToolUse"] = new_post
                    c_data["hooks"] = hooks_obj
                    tmp_p = cfg_path.with_name(f"{cfg_path.name}.{os.getpid()}.orca-tmp")
                    tmp_p.write_text(json.dumps(c_data, indent=2) + "\n", encoding="utf-8")
                    os.replace(tmp_p, cfg_path)
                    configured.append(cli_label)
        except Exception:
            missing_or_updated.append(cli_label)

    # 3. Gemini CLI (~/.gemini/settings.json — uses BeforeTool / AfterTool and timeout in ms)
    gem_path = home / ".gemini" / "settings.json"
    if gem_path.exists():
        try:
            g_data = json.loads(gem_path.read_text(encoding="utf-8"))
            if isinstance(g_data, dict):
                hooks_obj = g_data.get("hooks")
                if not isinstance(hooks_obj, dict):
                    hooks_obj = {}
                bt_list = hooks_obj.get("BeforeTool") if isinstance(hooks_obj.get("BeforeTool"), list) else []
                at_list = hooks_obj.get("AfterTool") if isinstance(hooks_obj.get("AfterTool"), list) else []

                def _has_gem_guard(arr: list, expected_cmd: str) -> bool:
                    for item in arr:
                        if not isinstance(item, dict):
                            continue
                        for h in item.get("hooks") or []:
                            if isinstance(h, dict) and h.get("command") == expected_cmd:
                                return True
                    return False

                def _strip_gem_guard(arr: list) -> list:
                    cleaned_arr = []
                    for item in arr:
                        if not isinstance(item, dict):
                            cleaned_arr.append(item)
                            continue
                        h_list = item.get("hooks")
                        if isinstance(h_list, list):
                            kept_h = [
                                h for h in h_list
                                if not (isinstance(h, dict) and "orca-command-guard" in str(h.get("command") or ""))
                            ]
                            if kept_h:
                                new_item = dict(item)
                                new_item["hooks"] = kept_h
                                cleaned_arr.append(new_item)
                        elif "orca-command-guard" not in str(item.get("command") or ""):
                            cleaned_arr.append(item)
                    return cleaned_arr

                if _has_gem_guard(bt_list, pre_cmd_gemini) and _has_gem_guard(at_list, post_cmd_gemini):
                    configured.append("gemini")
                else:
                    missing_or_updated.append("gemini")
                    if not check_only:
                        new_bt = _strip_gem_guard(bt_list)
                        new_bt.append({"hooks": [{"type": "command", "command": pre_cmd_gemini, "timeout": 10000}]})
                        new_at = _strip_gem_guard(at_list)
                        new_at.append({"hooks": [{"type": "command", "command": post_cmd_gemini, "timeout": 10000}]})
                        hooks_obj["BeforeTool"] = new_bt
                        hooks_obj["AfterTool"] = new_at
                        g_data["hooks"] = hooks_obj
                        tmp_p = gem_path.with_name(f"{gem_path.name}.{os.getpid()}.orca-tmp")
                        tmp_p.write_text(json.dumps(g_data, indent=2) + "\n", encoding="utf-8")
                        os.replace(tmp_p, gem_path)
                        configured.append("gemini")
        except Exception:
            missing_or_updated.append("gemini")

    # 4. Kimi Code CLI (~/.kimi-code/config.toml)
    kimi_dir = Path(os.environ.get("KIMI_CODE_HOME", "").strip() or str(home / ".kimi-code"))
    kimi_cfg = kimi_dir / "config.toml"
    if kimi_cfg.exists():
        try:
            k_txt = kimi_cfg.read_text(encoding="utf-8")
            esc_pre = pre_cmd_generic.replace("\\", "\\\\").replace('"', '\\"')
            esc_post = post_cmd_generic.replace("\\", "\\\\").replace('"', '\\"')
            expected_block = (
                f"{KIMI_GUARD_START_MARKER}\n"
                f"[[hooks]]\n"
                f'event = "PreToolUse"\n'
                f'command = "{esc_pre}"\n'
                f"timeout = 10\n"
                f"[[hooks]]\n"
                f'event = "PostToolUse"\n'
                f'command = "{esc_post}"\n'
                f"timeout = 10\n"
                f"{KIMI_GUARD_END_MARKER}"
            )
            if expected_block in k_txt:
                configured.append("kimi")
            else:
                missing_or_updated.append("kimi")
                if not check_only:
                    cleaned_k = re.sub(
                        re.escape(KIMI_GUARD_START_MARKER) + r".*?" + re.escape(KIMI_GUARD_END_MARKER) + r"\n?",
                        "",
                        k_txt,
                        flags=re.DOTALL,
                    ).rstrip()
                    new_k = f"{cleaned_k}\n\n{expected_block}\n" if cleaned_k else f"{expected_block}\n"
                    tmp_p = kimi_cfg.with_name(f"{kimi_cfg.name}.{os.getpid()}.orca-tmp")
                    tmp_p.write_text(new_k, encoding="utf-8")
                    os.replace(tmp_p, kimi_cfg)
                    configured.append("kimi")
        except Exception:
            missing_or_updated.append("kimi")

    return len(missing_or_updated) == 0, configured, missing_or_updated


def uninstall_all_cli_command_guard_hooks() -> None:
    """Remove `orca-command-guard` hooks from all Agent CLI configs while preserving Orca status hooks and user hooks."""
    home = Path.home()
    agy_path = home / ".gemini" / "config" / "hooks.json"
    if agy_path.exists():
        try:
            data = json.loads(agy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "orca-command-guard" in data:
                del data["orca-command-guard"]
                tmp_p = agy_path.with_name(f"{agy_path.name}.{os.getpid()}.orca-tmp")
                tmp_p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp_p, agy_path)
        except Exception:
            pass

    for cfg_rel in (".claude/settings.json", ".openclaude/settings.json", ".qwen/settings.json", ".codebuddy/settings.json", ".gemini/settings.json"):
        cfg_path = home / cfg_rel
        if not cfg_path.exists():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
                continue
            hooks_obj = data["hooks"]
            changed = False
            for ev_k in list(hooks_obj.keys()):
                arr = hooks_obj.get(ev_k)
                if not isinstance(arr, list):
                    continue
                new_arr = []
                for item in arr:
                    if not isinstance(item, dict):
                        new_arr.append(item)
                        continue
                    h_list = item.get("hooks")
                    if isinstance(h_list, list):
                        kept = [h for h in h_list if not (isinstance(h, dict) and "orca-command-guard" in str(h.get("command") or ""))]
                        if len(kept) != len(h_list):
                            changed = True
                        if kept:
                            ni = dict(item)
                            ni["hooks"] = kept
                            new_arr.append(ni)
                    elif "orca-command-guard" in str(item.get("command") or ""):
                        changed = True
                    else:
                        new_arr.append(item)
                if new_arr:
                    hooks_obj[ev_k] = new_arr
                else:
                    del hooks_obj[ev_k]
            if changed:
                tmp_p = cfg_path.with_name(f"{cfg_path.name}.{os.getpid()}.orca-tmp")
                tmp_p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp_p, cfg_path)
        except Exception:
            pass

    kimi_cfg = Path(os.environ.get("KIMI_CODE_HOME", "").strip() or str(home / ".kimi-code")) / "config.toml"
    if kimi_cfg.exists():
        try:
            k_txt = kimi_cfg.read_text(encoding="utf-8")
            cleaned_k = re.sub(
                r"\n?" + re.escape(KIMI_GUARD_START_MARKER) + r".*?" + re.escape(KIMI_GUARD_END_MARKER) + r"\n?",
                "\n",
                k_txt,
                flags=re.DOTALL,
            )
            if cleaned_k != k_txt:
                tmp_p = kimi_cfg.with_name(f"{kimi_cfg.name}.{os.getpid()}.orca-tmp")
                tmp_p.write_text(cleaned_k.rstrip() + "\n", encoding="utf-8")
                os.replace(tmp_p, kimi_cfg)
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "--stream":
        _run_stream_filter()
        return

    if len(sys.argv) >= 3 and sys.argv[1] == "--watch-dir":
        _run_background_watcher(sys.argv[2])
        return

    if len(sys.argv) >= 2 and sys.argv[1] in ("--install-hooks", "--check-hooks", "--uninstall-hooks"):
        act_flag = sys.argv[1]
        if act_flag == "--uninstall-hooks":
            uninstall_all_cli_command_guard_hooks()
            print(json.dumps({"ok": True, "action": "uninstalled"}))
            return
        g_bin_arg = sys.argv[2] if len(sys.argv) >= 3 else os.path.abspath(__file__)
        ok, conf, miss = ensure_all_cli_command_guard_hooks(
            guard_bin=g_bin_arg,
            check_only=(act_flag == "--check-hooks"),
        )
        print(json.dumps({"ok": ok, "configured": conf, "updatedOrMissing": miss}))
        sys.exit(0 if ok or act_flag == "--install-hooks" else 1)

    raw_event = os.environ.get("ORCA_GUARD_EVENT", "PreToolUse")
    if len(sys.argv) >= 3 and sys.argv[1] == "--event":
        raw_event = sys.argv[2]

    is_pre_event = raw_event in ("PreToolUse", "BeforeTool", "preToolUse", "beforeShellExecution", "tool.call")
    event = "PreToolUse" if is_pre_event else "PostToolUse"
    allow_out = '{"decision":"ask"}\n' if is_pre_event else "{}\n"

    try:
        payload = _read_json_stdin_nonblocking()
        if not isinstance(payload, dict):
            payload = {}

        payload_event = str(
            payload.get("hook_event_name")
            or payload.get("hookEventName")
            or payload.get("event")
            or ""
        ).strip()
        if payload_event in ("BeforeTool", "PreToolUse", "preToolUse", "beforeShellExecution", "tool.call"):
            is_pre_event = True
            event = "PreToolUse"
        elif payload_event in ("AfterTool", "PostToolUse", "PostToolUseFailure", "postToolUse", "tool.result"):
            is_pre_event = False
            event = "PostToolUse"

        is_antigravity_hook = bool(
            "toolCall" in payload
            or "tool_call" in payload
            or os.environ.get("ORCA_ANTIGRAVITY_EVENT")
        )
        is_kimi_hook = bool(
            not is_antigravity_hook
            and "toolName" in payload
            and "toolInput" in payload
        )
        is_gemini_cli_hook = bool(
            not is_antigravity_hook
            and (
                raw_event in ("BeforeTool", "AfterTool")
                or payload_event in ("BeforeTool", "AfterTool")
                or os.environ.get("ORCA_GUARD_CLIENT") == "gemini"
            )
        )

        if not is_antigravity_hook:
            allow_out = "{}\n"

        tc = payload.get("toolCall") or payload.get("tool_call") or {}
        tool_name, args, cmd = _extract_normalized_tool_call(payload)
        _, _, caller_slot = _detect_caller_slot_safe(payload, args, cmd)
        guard_mode = _get_guard_mode()
        if guard_mode == "off":
            sys.stdout.write(allow_out)
            sys.stdout.flush()
            return

        if os.environ.get("ORCA_GUARD_FORCE_RAISE", "").strip() == "1":
            raise RuntimeError("Simulated guard internal exception (ORCA_GUARD_FORCE_RAISE=1)")

        def _deny_json(reason_msg: str) -> str:
            if guard_mode == "shadow":
                _log_guard_decision("would_block", reason_msg, cmd, caller_slot)
                return allow_out
            _log_guard_decision("block", reason_msg, cmd, caller_slot)
            if not is_antigravity_hook:
                hook_ev_name = "BeforeTool" if is_gemini_cli_hook else "PreToolUse"
                return (
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": reason_msg,
                            "message": reason_msg,
                            "hookSpecificOutput": {
                                "hookEventName": hook_ev_name,
                                "permissionDecision": "deny",
                                "permissionDecisionReason": reason_msg,
                                "message": reason_msg,
                            },
                        }
                    )
                    + "\n"
                )
            return json.dumps({"decision": "deny", "reason": reason_msg}) + "\n"

        _ensure_child_worktree_hygiene(payload, args)
        _ensure_watcher_for_payload(payload, event, cmd)
        if not cmd:
            _record_visual_image_inspection(payload, args, tool_name)

        if is_pre_event and not cmd:
            log_view_err = _check_unbounded_task_log_view(payload, args, tc=tc, tool_name_hint=tool_name)
            if log_view_err:
                sys.stdout.write(_deny_json(log_view_err))
                sys.stdout.flush()
                return

        if not is_pre_event or not cmd:
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
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        # Guard 2: Unbounded raw binary dump into stdout or subprocess.check_output
        if _is_raw_binary_cat_risk(cmd[:200000]):
            reason = (
                "[ORCA COMMAND GUARD] Blocked unbounded raw binary dump (`cat` on compiled container binary into stdout/memory). "
                "Dumping multi-megabyte compiled binaries into stdout or check_output() hangs the Worker CLI. "
                "Fix: use bounded queries (`strings /app/binary | grep -i ... | head -n 40`) or escalate to Coordinator to dispatch `re-worker`."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        # Guard 3: Unbounded `while True:` + `.find(` binary search loops in inline Python scripts
        if _is_unbounded_find_loop_risk(cmd[:200000]):
            reason = (
                "[ORCA COMMAND GUARD] Blocked unbounded 'while True:' loop with '.find()' in inline Python script. "
                "If the target pattern is short or empty, 'while True: ... data.find()' loops millions of times and floods stdout. "
                "Fix: replace 'while True:' with a bounded loop 'for _ in range(50):' so matches are safely capped at 50."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        # Guard 4: Wildcard `git for-each-ref ... refs/heads/dev-worker*` merge loop (cross-Run contamination risk)
        if _is_wildcard_dev_branch_merge_risk(cmd[:200000]):
            reason = (
                "[ORCA RUN-SCOPED MERGE GUARD] CẤM dùng vòng lặp wildcard 'git for-each-ref ... refs/heads/dev-worker*' để merge nhánh! "
                "Khi nhiều Leader cùng chạy trên một repo, vòng lặp wildcard sẽ merge nhầm cả nhánh đang làm dở của Leader khác. "
                "Cách sửa: Thay bằng 'orca-team merge-upstream --role <role> --task-id <TASK_ID>' (cho Worker) "
                "hoặc 'orca-team merge-upstream --to-main --run \"$RUN_ID\"' (cho Leader ở Bước 4)."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        # Guard 5: Anti-Token-Leak Guard for `orca orchestration inbox` and `orca orchestration task-create --json`
        sanitized_orch = _strip_quoted_heredocs(_strip_non_exec_prose_flags(cmd[:200000]))
        if re.search(r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo)\s+)\s*(?:\S*/)?orca\s+orchestration\s+inbox\b", sanitized_orch):
            reason = (
                "[ORCA TOKEN-LEAK GUARD] CẤM gọi 'orca orchestration inbox' vì lệnh này xả toàn bộ lịch sử hòm thư đa Run làm phình Context Window! "
                "Cách sửa: "
                "• Nếu là Worker: dùng 'orca orchestration check [--ack <DELIVERY_ID>] --terminal <worker_handle> --json'. "
                "• Nếu là Leader: dùng 'orca-team wait-events [--ack <DELIVERY_ID>] [--run <RUN_ID>]'."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        if re.search(r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo)\s+)\s*(?:\S*/)?orca\s+orchestration\s+task-create\b[^\n;|&]*\s--json\b", sanitized_orch):
            reason = (
                "[ORCA TOKEN-LEAK GUARD] CẤM truyền cờ '--json' vào 'orca orchestration task-create' (vì '--json' sẽ in lặp lại toàn bộ chuỗi Spec 700+ tokens vào Context mỗi lần tạo task)! "
                "Cách sửa: Bỏ cờ '--json' ở cuối lệnh 'orca orchestration task-create' (lệnh vẫn in ra 'Created task_xxxxxxxxxxxx' chỉ tốn 4 tokens)."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        if re.search(r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo)\s+)\s*(?:\S*/)?orca\s+orchestration\s+task-list\b[^\n;|&]*\s--json\b(?!\s*\|\s*(?:jq|python|head|grep|rg|awk|sed)\b)", sanitized_orch):
            reason = (
                "[ORCA TOKEN-LEAK GUARD] CẤM gọi 'orca orchestration task-list ... --json' trực tiếp ra stdout (vì '--json' xả toàn bộ chuỗi Spec dài của mọi task trong Run, ngốn 15,000–25,000 tokens/lần)! "
                "Cách sửa: Bỏ cờ '--json' (`orca orchestration task-list --run <RUN_ID>` chỉ tốn ~150 tokens) hoặc dùng `orca-team plan show --run <RUN_ID>`."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        if re.search(r"(?:^|[;&|()\n]|\b(?:do|then|else|elif|env|command|sudo)\s+)\s*(?:\S*/)?orca\s+skills\s+get\b[^\n;|&]*\s--full\b(?!\s*\|\s*(?:jq|python|head|grep|rg|awk|sed)\b)", sanitized_orch):
            reason = (
                "[ORCA TOKEN-LEAK GUARD] CẤM gọi 'orca skills get ... --full' trực tiếp ra stdout (vì '--full' xả 46,961 bytes = ~15,650 tokens cẩm nang CLI làm vượt ngưỡng cắt 10KB và tràn Context Window)! "
                "Cách sửa: Dùng `orca-team kb` (ví dụ `orca-team kb 01` đến `06`) hoặc dùng cờ `--compact` thay cho `--full`."
            )
            sys.stdout.write(_deny_json(reason))
            sys.stdout.flush()
            return

        # Guard 5b: Orca CLI Protocol & Shell Quoting Guards (backticks in --body "...", task-update --spec/--status canceled, && check --wait, waiter_exists, sleep polling)
        proto_reason = _check_orca_cli_protocol_guards(cmd[:200000], sanitized_orch, payload, args)
        if proto_reason:
            sys.stdout.write(_deny_json(proto_reason))
            sys.stdout.flush()
            return

        def _emit_rewritten_cmd(new_cmd: str, block_on_kimi_reason: str = "") -> bool:
            if is_antigravity_hook and isinstance(args, dict) and "CommandLine" in args:
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "ask",
                            "overwrite": {"CommandLine": new_cmd},
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
                return True
            if is_kimi_hook:
                if block_on_kimi_reason:
                    sys.stdout.write(_deny_json(block_on_kimi_reason))
                    sys.stdout.flush()
                    return True
                return False
            if not is_antigravity_hook and isinstance(args, dict) and ("command" in args or "cmd" in args):
                new_input = dict(args)
                if "command" in new_input:
                    new_input["command"] = new_cmd
                else:
                    new_input["cmd"] = new_cmd
                hook_ev_name = "BeforeTool" if is_gemini_cli_hook else "PreToolUse"
                sys.stdout.write(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": hook_ev_name,
                                "permissionDecision": "allow",
                                "updatedInput": new_input,
                                "tool_input": new_input,
                            }
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
                return True
            return False

        # Auto-Upgrade 1: Transparently rewrite standalone Leader `orca orchestration check ... --wait` -> `orca-team wait-events ...`
        rewritten_wait = _maybe_rewrite_raw_check_wait_to_wait_events(cmd[:200000], sanitized_orch)
        if rewritten_wait:
            kimi_wait_msg = (
                f"[ORCA WAIT-EVENTS GUARD] CẤM gọi trực tiếp 'orca orchestration check --wait' (gây rác _keepalive/heartbeat)! "
                f"Hãy chạy lệnh chuẩn: `{rewritten_wait}`"
            )
            if _emit_rewritten_cmd(rewritten_wait, block_on_kimi_reason=kimi_wait_msg):
                return

        # Auto-Upgrade 1b: Transparently inject `--limit 20` on `orca orchestration worker-read` without `--limit` (saving 67KB/call)
        rewritten_wread = _maybe_cap_worker_read_limit(cmd[:200000], sanitized_orch)
        if rewritten_wread:
            kimi_wread_msg = (
                f"[ORCA TOKEN-LEAK GUARD] CẤM gọi 'orca orchestration worker-read' không có '--limit' (xả tới 70KB = ~23,300 tokens)! "
                f"Hãy thêm '--limit 20': `{rewritten_wread}`"
            )
            if _emit_rewritten_cmd(rewritten_wread, block_on_kimi_reason=kimi_wread_msg):
                return

        # Guard 6: Wave-Order Gate & Run-Scoped Elastic Worktree Guard on `worker-start`
        collision_reason = _check_hardcoded_worker_start_collision(cmd[:200000], payload, args)
        if collision_reason:
            sys.stdout.write(_deny_json(collision_reason))
            sys.stdout.flush()
            return

        # Auto-Upgrade 2: Transparently inject `export BUILDKIT_PROGRESS=plain; ` for `docker compose build` / `docker build`
        rewritten_buildkit = _maybe_inject_buildkit_plain_progress(cmd[:200000], sanitized_orch)
        if rewritten_buildkit:
            if _emit_rewritten_cmd(rewritten_buildkit):
                return

        # Auto-Upgrade 3: Transparently bound single-line JSON/XML `curl|head -n`, `head -n <file.json>`, and noisy `mvn` transfer logs
        rewritten_noisy = _maybe_bound_noisy_shell_commands(cmd[:200000], sanitized_orch)
        if rewritten_noisy:
            if _emit_rewritten_cmd(rewritten_noisy):
                return

        # Trigger automatic post-release tab sweep when Leader runs `orca orchestration worker-release`
        _trigger_post_worker_release_sweep(cmd[:200000], payload, args)

        sys.stdout.write(allow_out)
        sys.stdout.flush()
    except Exception as exc:
        # FIX-01: Slot-aware Fail-Closed exception handling in main()
        try:
            p_fb = payload if "payload" in locals() and isinstance(payload, dict) else {}
            a_fb = args if "args" in locals() and isinstance(args, dict) else {}
            c_fb = cmd if "cmd" in locals() and isinstance(cmd, str) else ""
            _, _, s_fb = _detect_caller_slot_safe(p_fb, a_fb, c_fb)
            is_qa_ui_fb = bool(
                (s_fb and (s_fb.startswith("qa-worker") or s_fb.startswith("ui-worker")))
                or re.search(r"\b(?:QA_VERDICT\s*:\s*PASS|QA_FINAL|UI\s+Ready|UI_READY|Worker\s+Done:\s*(?:qa|ui)-worker)\b", c_fb, re.IGNORECASE)
            )
            if is_qa_ui_fb and _get_guard_mode() != "off":
                fc_msg = (
                    f"[ORCA FAIL-CLOSED GUARD] Lỗi nội bộ trong guard khi xác minh bằng chứng cho slot '{s_fb or 'qa/ui'}' ({exc}). "
                    f"Thực thi Fail-Closed: CẤM cho qua báo cáo hoàn tất khi guard gặp ngoại lệ!"
                )
                if _get_guard_mode() == "shadow":
                    _log_guard_decision("would_block", fc_msg, c_fb, s_fb)
                    sys.stdout.write(allow_out)
                else:
                    _log_guard_decision("block", fc_msg, c_fb, s_fb)
                    sys.stdout.write(json.dumps({"decision": "deny", "reason": fc_msg}) + "\n")
                sys.stdout.flush()
                return
        except Exception:
            pass
        sys.stdout.write(allow_out)
        sys.stdout.flush()


if __name__ == "__main__":
    main()


