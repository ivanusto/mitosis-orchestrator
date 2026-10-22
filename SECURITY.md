# Security Policy

## Supported versions

Only the latest minor release line of `mitosis-orchestrator` receives security fixes. Please update to the newest release before reporting, and see [CHANGELOG.md](./CHANGELOG.md) for what changed.

## Reporting a vulnerability

Do not open a public issue for a vulnerability.

Use GitHub private vulnerability reporting: open the **Security** tab of [ivanusto/mitosis-orchestrator](https://github.com/ivanusto/mitosis-orchestrator), choose **Report a vulnerability** (Security Advisories), and describe the problem, the platform (agy, Claude Code or Codex), the plugin version and steps to reproduce.

## Hook permission semantics

These are the behaviours that matter for a security review. They are documented in [README.md](./README.md) and [rules/AGENTS.md](./rules/AGENTS.md).

- **Workspace Guard is opt-in.** It is enabled with `AGY_MITOSIS_AUTO_ALLOW=1`, `CLAUDE_MITOSIS_AUTO_ALLOW=1` or `CODEX_MITOSIS_AUTO_ALLOW=1` (`*_MITOSIS_AUTO_ALLOW=1`). When enabled it auto-approves file and command tools that stay inside the workspace. It prompts (`ask`) for paths outside the workspace, privilege escalation, root deletes, force pushes and shell-piped downloads. Codex has no `ask`, so the same cases are denied there.
- **Default behaviour gives no verdict.** With the guard disabled, the Claude Code hook returns `{}`, so your normal permission rules and prompts apply.
- **1.5.0 and earlier skipped permission prompts.** Those versions answered `Bash`, `Read`, `Write`, `Edit` and `SendMessage` calls with `allow`, which Claude Code treats as "skip the permission prompt". This was fixed in 1.5.1. If you ran 1.5.0 or earlier, update.
- **Scout read-only layer.** A `flash-lite-scout` or Codex `explorer` cannot use `Write`, `Edit` or `apply_patch`, and runs only allowlisted read-only shell commands, whatever its prompt says. The Codex MCP server also starts scouts with `--sandbox read-only` and workers with `--sandbox workspace-write`.
- **Codex hook trust.** Codex skips untrusted plugin hooks silently. Enable the hooks feature and approve the plugin hooks in `/hooks`; a matcher change in a new version needs approval again. Until then the guard does not run.
- **`MITOSIS_CODEX_YOLO=1` bypasses the sandbox.** It runs Codex subagents with `--dangerously-bypass-approvals-and-sandbox`. Leave it unset unless you deliberately want that.
- **The optional judge sends prompts out.** It is off by default. When `MITOSIS_JUDGE_URL` is set, low-confidence routing decisions send the task prompt to that URL. The prompt is cut to 2000 characters, and secrets, e-mail and IP addresses and home directories are masked before sending. The bearer token (`MITOSIS_JUDGE_KEY`) goes only over https or to a loopback or private-network host unless `MITOSIS_JUDGE_ALLOW_HTTP_KEY=1`. Point it only at an endpoint you trust.
