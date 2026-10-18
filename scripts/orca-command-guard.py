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
    """Verify the interior of $(orca-team spec|worktree-arg|merge-upstream|plan|kb ...) has no unescaped backticks, nested $(), or command chaining."""
    if not re.match(
        r"^\s*(?:\S*/)?orca-team(?:\s+(?:--path|-C)\s+\S+|\s+--path=\S+)*\s+(?:spec|role-spec|prompt|worktree-arg|wt-arg|slot-arg|merge-upstream|merge-run|run-branches|release-workers|plan|pin-plan|master-plan|kb|knowledge|system-engineer)\b",
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
            if w in ("current", "new-child", "new", "new-top-level", "parent"):
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

    # Wave-Order Check 1: Prevent starting Wave 1 (dev/ui/fix) and Wave 2 (devops/sec/qa/bench) simultaneously in one command
    if len(started_roles_in_cmd) > 1:
        w1_roles = sorted({r for r, _, _ in started_roles_in_cmd if r in ("dev", "ui", "fix")})
        w2_roles = sorted({r for r, _, _ in started_roles_in_cmd if r in ("devops", "sec", "qa", "bench")})
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

    # Wave-Order Check 2: Verify in orchestration.db that no upstream worker in the same Run is still working/unstarted/failed
    if orca_team_bin:
        for seg_role, seg_tid, seg_rid in started_roles_in_cmd:
            if seg_role not in ("devops", "sec", "qa", "bench"):
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
        is_claude_or_gemini_hook = (
            isinstance(payload, dict)
            and "tool_input" in payload
            and "toolCall" not in payload
            and "tool_call" not in payload
        )
        if is_claude_or_gemini_hook:
            allow_out = "{}\n"

        def _deny_json(reason_msg: str) -> str:
            if is_claude_or_gemini_hook:
                return (
                    json.dumps(
                        {
                            "decision": "block",
                            "reason": reason_msg,
                            "hookSpecificOutput": {
                                "hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": reason_msg,
                            },
                        }
                    )
                    + "\n"
                )
            return json.dumps({"decision": "deny", "reason": reason_msg}) + "\n"

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

        # Guard 5: Wave-Order Gate & Run-Scoped Elastic Worktree Guard on `worker-start`
        collision_reason = _check_hardcoded_worker_start_collision(cmd[:200000], payload, args)
        if collision_reason:
            sys.stdout.write(_deny_json(collision_reason))
            sys.stdout.flush()
            return

        # Trigger automatic post-release tab sweep when Leader runs `orca orchestration worker-release`
        _trigger_post_worker_release_sweep(cmd[:200000], payload, args)

        sys.stdout.write(allow_out)
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(allow_out)
        sys.stdout.flush()


if __name__ == "__main__":
    main()

