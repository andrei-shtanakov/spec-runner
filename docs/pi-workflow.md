# Driving the dev → review → test loop with `pi` (no core code)

[`pi`](https://pi.dev) (earendil-works/pi) is a coding agent prized for how configurable it
is: native **skills** (the [Agent Skills standard](https://agentskills.io/specification)),
per-run **system-prompt** injection, fine-grained **tool control**, **thinking** levels and
multi-provider model routing. spec-runner can lean on all of that to run a complete
development → review → testing loop **without changing any spec-runner Python** — purely
through config, pi-native skills and a small shell script.

A runnable example lives in [`examples/pi-loop/`](../examples/pi-loop/). Portable copies of
the skills + a sample config ship in the bundled `spec-generator-skill` under
`templates/pi/`, so `spec-runner-init` drops them into any project.

## The one lever: command templates

spec-runner builds the agent command in `runner.build_cli_command()`. When you set
`command_template` (and/or `review_command_template`), it does
`template.format(cmd=…, model=…, prompt=…)` and then `shlex.split` — so **any literal pi flag
you put in the template passes straight through**. That's the whole trick: you bake pi's
power flags into the template per stage.

```yaml
executor:
  claude_command: "pi"
  claude_model: "openai-codex/gpt-5.4"   # run `pi --list-models` for ids your install can reach
  command_template:        "{cmd} -p --model {model} --tools read,bash,edit,write,grep,find,ls --skill .pi/skills/pi-implementer {prompt}"
  review_command: "pi"
  review_model:  "openai-codex/gpt-5.4"
  review_command_template: "{cmd} -p --model {model} --tools read,grep,find --skill .pi/skills/pi-reviewer {prompt}"
```

Placeholders available: `{cmd}`, `{model}`, `{prompt}` (already shell-escaped),
`{prompt_file}`. Everything else is literal.

## Stage → pi mapping

| Stage | Driven by | pi flags | Tools | Exit marker |
|---|---|---|---|---|
| **Develop** | `execute_task()` → `command_template` | `--skill pi-implementer` | `read,bash,edit,write,grep,find,ls` (full) | `TASK_COMPLETE` / `TASK_FAILED` |
| **Test** | `run_tests` hook (`commands.test`) | — (runs `pytest`) | — | pytest exit code |
| **Review** | `run_review` hook → `review_command_template` | `--skill pi-reviewer` | `read,grep,find` (**read-only**) | `REVIEW_PASSED` / `REVIEW_FAILED` |

The implementer runs with the **full** tool set so it can write code, write tests, and run
them itself via `bash`. The reviewer runs with a **read-only** allowlist (`--tools
read,grep,find`, no `edit`/`write`/`bash`) so it physically cannot mutate the code — a clean,
enforced review gate. spec-runner then re-runs the suite (`commands.test`) as an independent
gate, and the read-only reviewer judges the diff with the sharpened
`review.pi.md` prompt (only `REVIEW_PASSED` / `REVIEW_FAILED`).

### Hook order caveat (where the "+ pi test generator" goes)

`post_done_hook` runs **tests → lint → review → plugin hooks** in that order. A `post_done`
plugin therefore fires *after* pytest — too late to author tests before the gate. So:

- **Primary:** the `pi-implementer` skill writes the tests in the develop stage and runs them
  itself; spec-runner's `run_tests` is the gate. Fully in-loop, no extra step.
- **Hardening:** run [`pi-tester`](../examples/pi-loop/.pi/skills/pi-tester/SKILL.md) as a
  standalone step via `scripts/pi-author-tests.sh` (or a Makefile target). Don't rely on the
  plugin form for real authoring — plugin hooks have a fixed **60s** timeout.

## How pi finds the skills

Per pi's [skills docs](https://agentskills.io/specification), pi discovers skills from:

- `~/.pi/agent/skills/` and `~/.agents/skills/` (global)
- `.pi/skills/` and `.agents/skills/` (project, walked up to the git root)
- `--skill <path>` (repeatable, additive — what the templates use)
- a `skills` array in `settings.json` — which can even point at Claude Code skills:
  ```json
  { "skills": ["../.claude/skills", "~/.claude/skills"] }
  ```

**Progressive disclosure caveat:** pi only puts skill *descriptions* in the system prompt and
loads the full `SKILL.md` on demand — and in headless `-p` mode it may not bother. So we make
the role instructions reliable by **also** carrying them as spec-runner `personas`
(`Persona.system_prompt` is always injected into the prompt text). Belt and suspenders: the
persona guarantees the role is in context; the `.pi/skills/` skill adds the reusable,
pi-native workflow + any helper scripts. `constitution.md` is the third layer for inviolable
project rules.

## Gotchas

- **Blank model breaks the template.** With `--model {model}` and an empty `claude_model`,
  the command becomes `… --model  …`; `shlex.split` drops the empty value and pi errors.
  Either always set `claude_model` / `review_model`, or remove `--model {model}` from the
  template and let pi use its own default (`--provider`, `PI_*` env, or
  `~/.pi/agent/settings.json`).
- **Keep output mode `text`.** spec-runner parses `TASK_COMPLETE` / `REVIEW_PASSED` markers
  from stdout. Don't add `--mode json` unless you also adapt how markers are detected.
- **Reviewer must stay read-only.** The `review.pi.md` shipped here drops `REVIEW_FIXED` on
  purpose; pair it with `--tools read,grep,find` so the gate can't silently rewrite code.

## The review gate needs a local commit

The reviewer prompt is filled from `git diff HEAD~1` taken at the project root (see
`review.py`). So the review gate only sees a task's changes when those changes are **committed**
in the project's *own* git repo — i.e. run with `auto_commit: true` (and a baseline commit so
`HEAD~1` exists). If you run a project that is itself a subdirectory of another git repo, the
diff resolves against the *outer* repo and the reviewer will (correctly) report that the task's
files aren't in the diff. Keep each spec-runner project its own repo.

## Try it

```bash
spec-runner-init                       # installs templates/pi skills under .claude/skills

# Copy the demo out so it's its own git repo (the review gate needs local commits):
cp -r examples/pi-loop /tmp/pi-loop && cd /tmp/pi-loop
git init -q && git add -A && git commit -qm scaffold

spec-runner run --all --dry-run        # plan only, no pi call
spec-runner run --all                  # real run (calls pi)
scripts/pi-author-tests.sh slugify.py  # optional pi test-hardening pass
```
