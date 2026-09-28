# Executor write boundary — an OS sandbox around every agent call

Status: draft for the owner, 2026-09-29. Inbox spec-runner#600 (from devtools#469),
TODO `executor-write-boundary`.

## 1. Problem

Executors run with `skip_permissions: true` by default (`claude
--dangerously-skip-permissions`; the other presets are no stricter) and may write
anywhere by absolute path. devtools#445 is the concrete case: a write into
`devtools/out/governance-runs/<run>/edge-check/` plants a PASS that suppresses a
paid re-check. `harness_guard` watches only the harness surface *inside*
`project_root`; nothing sees or bounds a write outside it.

Goal: an agent call can write only to the task's tree, a per-call temp dir, and the
state its own CLI needs to work — enforced by the OS, the same way for every
preset, not by the agent's cooperation.

## 2. Decision: OS sandbox, not per-CLI allowlists

Per-CLI permission modes are eight different mechanisms (claude's permission modes,
codex's `--sandbox`, nothing reliable in opencode/pi/ollama/llama-cli/qwen/copilot)
and hold only as far as each agent honours them. An OS sandbox wraps the process
tree regardless of the CLI.

- **macOS:** `sandbox-exec` with a Seatbelt profile. Deprecated by Apple but
  shipped and working on macOS 26.7 (measured below).
- **Linux/VPS:** `bubblewrap` (no daemon, lighter than a container). **Not measured
  yet** — no Linux host in this session; phase 2 (§7).

## 3. Measurements (macOS 26.7, 2026-09-29)

Method: each preset's argv exactly as `build_cli_invocation` builds it, prompt
"Reply with the single word OK.", run in a fresh `git init` project under a Seatbelt
profile `(allow default) (deny file-write*) (allow file-write* <allowlist>)`, 180 s
cap. Denials are not visible in the unified log without privileges, so the allowlist
was found by iteration: run, read the CLI's own EPERM message, add the path, repeat
until the real call answers. Base allowlist in every row: the project, a fresh
per-call `TMPDIR`, `/dev/*`. Nothing else under `/tmp` is needed (measured with the
per-call dir only).

| preset | version | extra writable paths needed | result |
|---|---|---|---|
| claude | 2.1.284 | none (writes to `~/.claude` fail silently, sessions are not logged) — but its Bash tool needs `CLAUDE_CODE_TMPDIR` pointed at the per-call dir, else it cannot create its work dir under `/private/tmp/claude-<uid>/` and runs no command | `OK` |
| codex | (homebrew) | `~/.codex` — without it: `failed to initialize in-process app-server client: Operation not permitted` | `OK` |
| opencode | (homebrew) | `~/.local/share/opencode` — without it: `FileSystem.open …/opencode/log/opencode.log` | `OK` |
| pi | (npm) | `~/.pi` — without it: `EPERM mkdir ~/.pi/agent/sessions/…` | past the filesystem; the call then fails 401 on the local API key, identically **without** the sandbox — not a sandbox effect |
| ollama | (homebrew) | none (the server is a separate process) | `OK` |
| qwen | (homebrew) | none | `OK` |
| copilot | 0.0.353 | **not measured** — the local install predates the preset's `-s`/`--no-ask-user` and hangs for 90 s without the sandbox too | — |
| llama-cli | — | **not measured** — not installed | — |

Tools the agent runs itself, same method (a uv project, `uv run pytest`, `git commit`):

| tool | extra writable paths needed | without it |
|---|---|---|
| uv | `~/.cache/uv` (`uv cache dir`) | `Failed to initialize cache at ~/.cache/uv` |
| git (repo = project root) | none | — |

The boundary itself, proven with real agents writing `inside.txt` in the project and
`~/sbx_outside_probe.txt` outside it:

| preset | inside | outside |
|---|---|---|
| claude (+`CLAUDE_CODE_TMPDIR`) | written | `operation not permitted`, no file |
| codex (+`--dangerously-bypass-approvals-and-sandbox`) | written | `operation not permitted`, no file |
| opencode | written | `operation not permitted`, no file |
| qwen | written | not attempted — the agent declined on its own; the OS refusal is not proven by qwen |

**Nested sandbox.** codex runs its own shell commands in Seatbelt; inside ours that
fails (`sandbox_apply: Operation not permitted`) and the agent can run nothing. Under
our sandbox codex must be started with `--dangerously-bypass-approvals-and-sandbox`
— its documented mode for "environments that are externally sandboxed". Measured:
works, and the boundary holds.

## 4. Design

### 4.1 Config

```yaml
executor_sandbox: off        # off | on | required   (default off in the first release)
sandbox_allow: []            # extra writable paths, ~ expanded (tool caches: ~/.npm, ~/.mix, ~/.hex …)
```

- `off` — today's behaviour.
- `on` — wrap when the platform has a backend; otherwise one warning per run and
  run unwrapped.
- `required` — no backend ⇒ refuse before the first paid call (instrument error,
  exit 2), the same fail-closed shape as the other gates.

Default `off` in the first release: turning it on changes what every agent can
write, and a project's tool caches (§3) must be declared first. Revisit the default
after a release of use.

### 4.2 The writable set, per call

1. `project_root`;
2. a fresh per-call temp dir, exported as `TMPDIR` and `CLAUDE_CODE_TMPDIR`,
   removed after the call;
3. `/dev/*`;
4. the git common dir when it lies outside `project_root` (a subdir project, a
   worktree) — `git rev-parse --git-common-dir`; otherwise an agent's `git commit`
   fails;
5. the CLI's state dirs from a measured table keyed by the executable's basename
   (§3): `codex → ~/.codex`, `opencode → ~/.local/share/opencode`, `pi → ~/.pi`;
   none for claude, ollama, qwen; copilot and llama-cli unmeasured → none, and a
   warning names them as unmeasured;
6. the uv cache dir when the project's `test_command`/`sync_command` runs `uv`
   (measured necessity), resolved by `uv cache dir`;
7. `sandbox_allow`.

Everything else, including sibling repos and `devtools/out/`, is read-only.

Per-CLI argv under the sandbox: codex gets
`--dangerously-bypass-approvals-and-sandbox` (nested Seatbelt fails, §3). No other
preset needed an argv change.

### 4.3 One seam

Five sites build and launch an agent argv today (`execution`, `review`, `tdd`, two
in `review_pr`), and `build_cli_invocation` knows neither `project_root` nor the
config. So the seam is one new function over the finished invocation, not a change
inside the builder:

```python
def sandboxed(config: ExecutorConfig, invocation: CliInvocation) -> SandboxedCall:
    """argv wrapped in the platform backend + env (TMPDIR, CLAUDE_CODE_TMPDIR)
    + the temp dir to remove; or the invocation unchanged under `off`."""
```

Every launch site passes its invocation through it and hands `argv`/`env` to
`subprocess`. A structural test pins that no module starts an agent argv without
going through `sandboxed` — the drift this repo has paid for before (#270, #241:
one reading, several call sites).

### 4.4 Proof the boundary holds

A test with a fake CLI (a shell script, no agent, no cost) that writes one file
inside the project and one outside: under `on` on macOS the inside file exists and
the outside one does not, and the call's exit status reflects the refusal.
Skipped where `sandbox-exec` is absent. Plus the §3 table re-run by hand before the
default changes.

## 5. Residual risk (stated, not solved)

- **The allowed state dirs are writable.** An agent can edit `~/.codex/config.toml`
  or `~/.pi` — the CLI's own behaviour on later calls, not spec-runner's policy
  (which #596 guards inside the project).
- **Reads and network are open.** This bounds writes (#600's case). Exfiltration and
  reads of secrets are out of scope.
- **`sandbox-exec` is deprecated.** If Apple removes it, `on` degrades to a warning
  and `required` refuses — loudly, never silently open.
- **claude's own session logs are lost** under the sandbox (`~/.claude` not
  writable). spec-runner's own logs are written by spec-runner, outside the sandbox,
  and are unaffected.

## 6. Not in scope

- Per-CLI permission modes as the boundary (§2).
- Enforcing `**Touches:**` at run time (a narrower, per-task scope inside the
  project; possible later on top of this backend).

## 7. Phases

1. macOS backend, `executor_sandbox`/`sandbox_allow`, the seam at all five sites,
   the measured state-dir table, codex's argv, the fake-CLI proof. Default `off`.
2. Linux `bubblewrap` backend — after measuring the same table on a VPS; until
   then `required` on Linux refuses before the first call.
3. Default to `on` after a release of use, if the measured table holds.

## 8. Open questions for the owner

1. Default `off` for phase 1 — agreed?
2. The uv cache added automatically when the test command runs uv (§4.2 item 6),
   or only via `sandbox_allow`?
3. copilot/llama-cli unmeasured: warn and run with the base set (current proposal),
   or refuse under `required`?
