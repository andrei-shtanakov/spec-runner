# pi-loop demo

A self-contained mini-project showing spec-runner driving a full **dev → review → test**
loop with the [`pi`](https://pi.dev) coding agent — using only config, pi-native skills and a
small script. **No spec-runner core code is involved.**

## What it shows

| Stage | Driven by | pi invocation | Tools | Marker |
|---|---|---|---|---|
| **Develop** | `execute_task()` | `command_template` | `read,bash,edit,write,grep,find,ls` | `TASK_COMPLETE` / `TASK_FAILED` |
| **Test** | `run_tests` gate | `python -m pytest -q` | — | pytest exit code |
| **Review** | `run_review` gate | `review_command_template` | `read,grep,find` (read-only) | `REVIEW_PASSED` / `REVIEW_FAILED` |

The implementer pi writes `slugify.py` **and** `test_slugify.py`, runs the tests itself, then
spec-runner re-runs them as a gate and a read-only pi reviewer judges the diff. See
[`../../docs/pi-workflow.md`](../../docs/pi-workflow.md) for the full explanation.

## Layout

```
examples/pi-loop/
├── spec-runner.config.yaml          # pi wired into every stage via command templates
├── .pi/skills/{pi-implementer,pi-reviewer,pi-tester}/SKILL.md
├── spec/{requirements,design,tasks}.md
├── spec/prompts/review.pi.md        # read-only review prompt (PASSED/FAILED only)
├── spec/plugins/pi-tester/plugin.yaml   # optional, documented pattern (see caveat)
└── scripts/pi-author-tests.sh       # the recommended pi test-hardening step
```

## Run it

Requires `pi` on PATH, authenticated for a provider. Run `pi --list-models` to see what your
install can reach and set `claude_model` / `review_model` accordingly (the config defaults to
`openai-codex/gpt-5.4`).

**Run it as its own git repo.** The review gate diffs `git diff HEAD~1` at the project root,
so it needs the task's changes to land as a commit (`auto_commit`) in a *local* repo. Copy the
demo out of this repo and initialise git, otherwise the reviewer will diff the parent
spec-runner repo instead of your task:

```bash
cp -r examples/pi-loop /tmp/pi-loop && cd /tmp/pi-loop
git init -q && git add -A && git commit -qm "scaffold"   # gives HEAD~1 a baseline
# enable per-task commits so review sees the task diff:
#   in spec-runner.config.yaml set  create_git_branch: true  and  auto_commit: true

# Dry run — see the plan without calling pi:
spec-runner run --all --dry-run

# Real run (calls pi → costs tokens; the task is trivial = cheap):
spec-runner run --all

# Optional extra test-hardening pass with pi:
scripts/pi-author-tests.sh slugify.py
```

Running it **in place** inside the spec-runner repo still exercises the develop (pi writes
`slugify.py` + tests) and test (`pytest`) stages, but the review gate will diff this repo's
recent commits rather than your task — expect a `REVIEW_FAILED` there. That is a property of
how the diff is scoped, not of the pi wiring.

> The `pi-tester` plugin is illustrative only: spec-runner runs plugin hooks with a fixed 60s
> timeout, too short for a real pi authoring pass — run the script directly instead.
