# Orca Multi-Agent Orchestration Directive

> **[ROLE SCOPING]** This directive applies strictly to the **Lead Coordinator**. Supervised workers (Dev, QA, Security) running inside child worktrees must ignore orchestration rules and focus entirely on their specific task prompt.

---

## 👥 Configured Agent Topology

| Role | Agent & Model | Concurrency | Capabilities & Orca Utilities |
| :--- | :--- | :--- | :--- |
| **Lead Coordinator** | `{{COORD_AGENT}}` | 1 | Task DAG, Decision Gates, Release |
| **Full-Stack Devs** | `{{DEV_AGENT}}` | {{DEV_COUNT}} parallel | Child worktrees, implementation, unit tests |
| **QA & Testers** | `{{QA_AGENT}}` | {{QA_COUNT}} parallel | Integration test suites, acceptance verification |
| **Security & Bug Hunter** | `{{SEC_AGENT}}` | {{SEC_COUNT}} parallel | OWASP audit, security scan, diff review |
| **PR Reviewer** | `{{PR_AGENT}}` | On-demand | PR checkout (`--pr`), diff audit (`open-changed`) |
| **Issue Resolver** | `{{ISSUE_AGENT}}` | On-demand | Issue checkout (`--issue`), auto-fix, commit & push |
| **UI/UX Specialist** | `{{UI_AGENT}}` | On-demand | Design Mode (Click-to-code), live inspection |
| **DevOps Specialist** | `{{DEVOPS_AGENT}}` | On-demand | DB migrations, Docker/Orb, SSH worktrees |

- **Execution Host:** `{{EXEC_HOST}}` | **Orca CLI:** `/usr/local/bin/orca`

---

## ⚡ Core Operational Runbook (Anti-Token-Waste Rules)

### 1. Phase 0 Planning Gate
Analyze requirements and produce a concise `plan.md` (see `templates/plan-template.md`). Wait for user approval before dispatching workers.

### 2. STRICT ANTI-POLLING RULE (CRITICAL)
- **NEVER poll in loops** with `orca orchestration check` or `worker-show`.
- After dispatching workers via `worker-start`, **STOP CALLING TOOLS**. Wait reactively for worker completion notifications or explicit user input. Repeated polling burns thousands of tokens per minute.

### 3. Worker Launch & Timeout
Always include `--timeout-ms 300000` (5 minutes watchdog limit) and use child worktrees:
```bash
/usr/local/bin/orca orchestration worker-start \
  --task <taskId> --worktree new-child --name "feature-dev" \
  --agent {{DEV_AGENT}} --setup run --timeout-ms 300000 --json
```

### 4. Concurrent Dual-Track (Hotfix P0)
If a GitHub issue is assigned during feature development, do not disrupt the current branch:
```bash
/usr/local/bin/orca worktree create \
  --name "hotfix-issue-<id>" --issue <id> --no-parent \
  --agent {{ISSUE_AGENT}} --setup run --json
```

### 5. Issue Reassignment & Handoff
If an issue is reassigned: stop worker (`worker-stop`), commit WIP branch `wip/issue-<id>-handover`, post comment on issue, and release worktree.

### 6. Watchdog & Rate-Limit Failover
- **Agent waiting on prompt (`observation.agentWait`):** Unblock with: `orca terminal send --terminal <handle> --text "y" --enter`.
- **Session context overflow:** Spawn fresh worker on same worktree: `worker-start --task <taskId> --retry-of <id> --worktree current`.
- **Account Rate Limit (429):** Do not respawn same provider! Switch provider via `orca-team set dev --agent <fallback_agent>` (e.g. Claude ➔ Kimi, Kimi ➔ Codex).
- **All providers exhausted:** Halt all workers cleanly and report to user (Human Gate).

### 7. Quality Gate & Release
- Verify workers sent `send --type worker_done --outcome succeeded`.
- Release settled workers: `orca orchestration worker-release --dispatch <id> --json`.
- For extended workflows and diagrams, refer to `docs/workflows.md` and `docs/roles.md`.
