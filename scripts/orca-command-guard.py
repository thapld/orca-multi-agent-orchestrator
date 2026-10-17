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
    r"(?P<opts>(?:\s+(?:-(?:W|X|r|C|I)\s+\S+|--?[a-zA-Z0-9_=./-]+))*)\s+(?P<flag>-(?:[a-zA-Z0-9]*[ce]|p)|--eval)(?:\s+|=)?\""
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
    r"(?:--(?:spec|body|question|objective|task-title|display-name|comment|resolution|result|subject|message|notes|title|prompt|summary|description|reason)|-m)(?:\s+|=)"
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
    """Verify the interior of $(orca-team spec ...) has no unescaped backticks, nested $(), or command chaining."""
    if not re.match(r"^\s*(?:\S*/)?orca-team(?:\s+(?:--path|-C)\s+\S+|\s+--path=\S+)*\s+(?:spec|role-spec|prompt)\b", inner):
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
        if re.search(r"(?:^|[\s;(&|])#", pre_hd):
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
    """Strip non-executable prose flag values (--spec, --body, --question, -m, etc.),
    direct `orca-team spec <role> '...'` task literals, top-level shell comments, and non-piped cat/tee quoted heredocs."""
    cleaned = _strip_non_piped_cat_tee_heredocs(cmd)
    out: list[str] = []
    i = 0
    n = len(cleaned)
    orca_spec_direct_re = re.compile(
        r"(?:\S*/)?orca-team(?:\s+(?:--path|-C)\s+\S+|\s+--path=\S+)*\s+(?:spec|role-spec|prompt)(?:\s+(?:--role|-r)\s+\S+|\s+[a-zA-Z0-9_-]+)?(?:\s+(?:--task|-t))?\s+"
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
    # 2. Direct shell `docker exec ... cat /app/binary` (with optional quotes around path args)
    shell_sanitized = _strip_non_shell_quoted_heredocs(sanitized)
    for m in re.finditer(
        r"(?:^|[;&|()\n]|\b(?:sudo|env|time|nohup|command)\s+|-[a-zA-Z]*c\s+['\"])\s*"
        r"docker\s+exec\s+[^\n|>&;]+?\b['\"]?(?:(?:/usr)?/bin/)?cat['\"]?(?:\s+--?[a-zA-Z0-9_-]*)*((?:\s+(?:['\"][^'\n\"|>&;]+['\"]|[^\s|>&;\"')]+))+)"
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


def _is_unbounded_find_loop_risk(cmd: str) -> bool:
    """Detect unbounded `while True:` / `while 1:` + `.find(` loops in inline/heredoc/piped Python scripts."""
    sanitized = _strip_non_exec_prose_flags(cmd)
    if not _WHILE_TRUE_RE.search(sanitized) or not re.search(
        r"(?:\bpython3?(?:\.\d+)?(?:\s+(?:-(?:W|X)\s+\S+|--?[a-zA-Z0-9_=.-]+))*(?:\s+-[a-zA-Z0-9]*c\b|[^\n|;&]*<<)|\bpython3?(?:\.\d+)?\b[^\n|;&]*<<|\|\s*(?:\S*/)?python3?(?:\.\d+)?\b)",
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
    return 0


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
    s = raw_cmd.strip()
    if len(s) >= 158 and " " in s:
        s = s.rsplit(None, 1)[0]
    prev = None
    while s and s != prev:
        prev = s
        s = re.sub(
            r"^(?:\s*(?:set\s+-[a-zA-Z0-9\s-]+|cd\s+[^;&|\n]+|export\s+[^;&|\n]+|source\s+[^;&|\n]+|\.\s+[^;&|\n]+)\s*(?:&&|;)\s*)+",
            "",
            s,
        ).strip()
        s = re.sub(
            r"^(?:\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+",
            "",
            s,
        ).strip()
    s = re.sub(r"<<-?\s*['\"\\]?[A-Za-z0-9_.-]+['\"]?", "", s)
    s = re.sub(r"\b\d*>&\d+\b", "", s)
    s = re.sub(r"\b\d*>>?\s*\S+", "", s)
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
        # lấy PID của leaf agent thuộc conversation này từ `.orca-agent-pids.json` hoặc `lsof` trên `.system_generated`.
        if not holder_pids:
            ordered_agents = _load_agent_pids_ordered(tasks_dir)
            for apid in ordered_agents:
                if apid in proc_table and _looks_like_agent(proc_table[apid][2]):
                    holder_pids.append(apid)
                    break
            if not holder_pids and tasks_dir.parent.name == ".system_generated":
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
                                if p_cand in proc_table and _looks_like_agent(proc_table[p_cand][2]):
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
                    max_etime = max([etime_map.get(dp, 0) for dp in desc_pids] + [etime_map.get(ch_pid, 0)])
                    if log_idle_s >= 5.0 and max_etime > 0 and (max_etime + 2.0) < log_idle_s:
                        continue
                    if require_jest and not re.search(r"\b(?:jest|vitest)\b", full_low):
                        continue
                    candidate_children.append((ch_pid, ch_pgid, full_args))

                # Chỉ kill khi xác định ĐƠN NHẤT được tiến trình chạy lệnh của log này.
                # Mơ hồ (0 hoặc >1 ứng viên) ➔ KHÔNG kill gì (chỉ cắt log) để không giết nhầm dev server / MCP server / lệnh song song.
                if cmd_hints:
                    matched: list[tuple[int, int, str]] = []
                    for hint in reversed(cmd_hints):
                        norm_hint = _normalize_cmd_hint(hint)
                        exact = [
                            c
                            for c in candidate_children
                            if (hint and hint in c[2]) or (norm_hint and norm_hint in c[2])
                        ]
                        if len(exact) == 1:
                            matched = exact
                            break
                        hint_tokens = _extract_hint_tokens(hint)
                        tok_match = [
                            c
                            for c in candidate_children
                            if (hint and hint in c[2])
                            or (norm_hint and norm_hint in c[2])
                            or (hint_tokens and all(t in c[2] for t in hint_tokens))
                        ]
                        if len(tok_match) == 1:
                            matched = tok_match
                            break
                    if not matched and len(candidate_children) == 1:
                        # Khi đã lọc `max_etime >= log_idle_s - 2` (và `require_jest` hoặc PTY session tách biệt `ch_pgid != h_pgid`),
                        # nếu chỉ có duy nhất 1 tiến trình con đang chạy dưới agent này thì chính là nó.
                        if require_jest or candidate_children[0][1] != h_pgid:
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
                                elif idle_age >= 5.0:
                                    # Đánh dấu đã kiểm tra để không gọi lsof/ps lặp lại mỗi 1.5s hoặc giết nhầm lần chạy Jest sau
                                    with open(log_file, "ab") as jf_out:
                                        jf_out.write(
                                            b"\n[ORCA AUTO-EXIT: Jest open-handle warning noted (process already exited). Tip: pass --forceExit to jest.]\n"
                                        )
                    except Exception:
                        pass
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
        ordered_agents = _load_agent_pids_ordered(tasks_dir)
        if not ordered_agents:
            return False
        proc_table = _get_process_table()
        leaf_agent = None
        for apid in ordered_agents:
            if apid in proc_table and _looks_like_agent(proc_table[apid][2]):
                leaf_agent = apid
                break
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
            non_comment_lines = [
                ln.strip()
                for ln in cmd.splitlines()
                for ln in [ _normalize_cmd_hint(ln) or ln.strip() ]
                if ln.strip() and not ln.strip().startswith("#")
            ]
            meaningful_lines = [
                ln
                for ln in non_comment_lines
                if not re.match(r"^(?:set\s+-[a-zA-Z]+|cd\s+[^;&|\n]+|export\s+\w+=[^;&|\n]*)$", ln)
            ] or non_comment_lines
            first_line_cmd = meaningful_lines[0][:160] if meaningful_lines else ""
            if str(step_idx).isdigit() and first_line_cmd:
                base_idx = int(step_idx)
                for key in (f"task-{base_idx}.log", f"task-{base_idx + 1}.log", f"task-{base_idx + 2}.log"):
                    prev = cmap.get(key)
                    if isinstance(prev, list):
                        if first_line_cmd not in prev:
                            cmap[key] = (prev + [first_line_cmd])[-4:]
                    elif isinstance(prev, str) and prev and prev != first_line_cmd:
                        cmap[key] = [prev, first_line_cmd]
                    else:
                        cmap[key] = [first_line_cmd]
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

        sys.stdout.write(allow_out)
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(allow_out)
        sys.stdout.flush()


if __name__ == "__main__":
    main()

