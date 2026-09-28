# `**Touches:**` — declared write scope, checked against the oracle before a run

Status: accepted by the owner 2026-09-28 (harness-guard-companions #4, TODO.md).

## Problem

Under `harness_guard: strict` an agent may not change the verification
harness (`HARNESS_CANDIDATES`, `harness_files`, the spec-runner config). A task
whose job *is* to change one of those files — TASK-022 had to register an entry
point in `pyproject.toml` — is an unfulfillable contract from the start: every
attempt is refused by the guard after it has been paid for. The conflict is
static and could be found in seconds, before any agent runs.

`tasks.md` had no place where a task states which files it will change, and
`preflight` never guesses. Reading `pyproject.toml` out of a task's prose is a
guess, and a wrong one on "do not touch pyproject.toml".

## Decision

A new optional task field, stated by the author, checked by `preflight`:

```markdown
**Touches:** src/app/entry.py, pyproject.toml, docs/
```

- Comma-separated, project-root-relative paths. A path names that file, or —
  as a directory — everything under it. No globs: the check must be exact.
- Refused by `validate` (named, quoted, no traceback — the `**Scenarios:**`
  contract): an empty line, an absolute path, a `..` segment, a glob character.
- Declares scope; it is not a permission. `harness_allow` stays the only
  exemption, and the spec-runner config is never exempt (#596).

`preflight` gains one check, `harness.touches`:

| condition | status | blocking |
|---|---|---|
| `harness_guard` is not `strict` | `skipped` | no — nothing would refuse the edit |
| no open task declares `**Touches:**` | `skipped` | no — nothing was declared, nothing is guessed |
| an open task names a harness file, or a path inside a harness directory, and no `harness_allow` exempts it (definite) | `broken` | yes |
| only possible reach: a declared directory *contains* a harness path (`spec/` holds the legacy config), or a declared harness directory whose exemptions depend on the file names written under it | `unavailable` | no — cannot be told from a directory |
| otherwise | `ok` | no |

The exemption is matched, like the guard's, against concrete file paths, and
never reaches the spec-runner config. A blocker must be something the guard
would certainly refuse: a directory says only that the task *may* write there.
Open = any status but `done`. An empty `harness_files`/`harness_allow` entry is
refused when the config loads (`Path.match("")` raises — in the guard too).

The `--json` shape is unchanged: a new check id is data, not a schema change.

## Not in scope

- Enforcing the declared scope at run time (an agent writing outside
  `**Touches:**`). This check is about the contract being fulfillable, not
  about the agent keeping to it.
- Inferring scope from prose. If a text heuristic is ever added it belongs in
  `validate` as a warning, never in `preflight`.
- Emitting the field from devtools' bridge: requested there as an inbox issue.
