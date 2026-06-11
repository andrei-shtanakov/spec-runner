# Design: Fix cost tracking for the modern claude CLI

**Date:** 2026-06-11
**Status:** Approved (brainstorm, revised after code-grounded review), pending implementation plan
**Author:** brainstormed with Claude

## Problem

`spec-runner doctor --cli=claude` against real **claude 2.1.173** reports
`cost_tracking=warn` → DEGRADED. `runner.parse_token_usage()` regex-scrapes
**stderr** for `input_tokens: …` / `cost: $…`, but the current `claude -p` does
not emit those in plain text. So `attempt.cost_usd`/tokens come back `None`, and
`spec-runner costs`, `--budget`, and `--task-budget` silently do nothing for
claude (zeros, no enforcement) — the "false confidence" case doctor was built to
catch.

claude exposes the data via `--output-format json` (single result): `result`
(the assistant's final text), `total_cost_usd`, `usage`, `is_error`. The fix is
to invoke claude with `--output-format json` on the execution path and parse cost
from the JSON instead of stderr.

## Key decisions (from brainstorm + review)

1. **Scope: claude only, behind an extensible seam.** A per-CLI result parser so
   codex/pi/ollama can be added later without touching `execution.py`; implement
   only claude now.
2. **Mode: `--output-format json` (single-shot).** Simple, reliable parsing and
   accurate cost.
3. **Explicit result-format tag, not command-guessing.** The command builder
   returns *both* the argv and a `result_format` tag (`"claude_json"` | `"text"`).
   `parse_cli_result` switches on that tag — it never re-derives the CLI from the
   command string. This matters because `build_cli_command`'s `else` branch
   (runner.py:236) treats **any unrecognized command as claude**, so a
   `_is_claude(cmd)` guess would mis-parse custom CLIs as claude-JSON.
4. **Opt-in per call.** The JSON flag (and the format tag) is requested only on
   the execution path; the review path is untouched.
5. **Defensive parsing.** Invalid JSON → fall back to text + `parse_token_usage`.
6. **Native hard cap.** When a task budget is set, also pass claude's
   `--max-budget-usd` as a defense-in-depth guard on the CLI call itself.

## Architecture — explicit `CliInvocation` + result seam

Today `execution.py` hard-codes `output = stdout`, `cost = parse_token_usage(stderr)`.
Replace with an explicit invocation+parse pair in `runner.py`.

```python
@dataclass
class CliResult:
    text: str                  # for TASK_COMPLETE detection + the task log
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    is_error: bool = False


ResultFormat = Literal["text", "claude_json"]


@dataclass
class CliInvocation:
    argv: list[str]
    result_format: ResultFormat


def _is_explicit_claude(cmd: str) -> bool:
    """True ONLY for an explicitly-recognized claude binary — never the
    unknown-command fallback. `Path(cmd).name` must equal `claude` or
    `claude-code` (so `/usr/local/bin/claude` matches; `my-claude-wrapper`,
    `codex`, etc. do not)."""
    return Path(cmd).name in ("claude", "claude-code")


def build_cli_invocation(
    cmd: str, prompt: str, model: str = "", template: str = "",
    skip_permissions: bool = False, prompt_file: Path | None = None,
    json_output: bool = False, max_budget_usd: float | None = None,
) -> CliInvocation:
    # ...existing per-CLI argv construction (llama/ollama/opencode/codex/pi/claude)...
    # JSON mode is enabled ONLY for an explicit claude binary, with json_output
    # set, and no template override:
    #     if json_output and not template and _is_explicit_claude(cmd):
    #         argv += ["--output-format", "json"]
    #         if max_budget_usd is not None:
    #             argv += ["--max-budget-usd", _fmt_budget(max_budget_usd)]
    #         result_format = "claude_json"
    #     else:
    #         result_format = "text"
    # The unknown-command fallback still builds claude-style argv (back-compat)
    # but stays result_format="text" with NO --output-format json, so custom
    # wrappers never receive an unexpected format. (A wrapper that needs JSON is a
    # future explicit config flag, not a heuristic.)
    return CliInvocation(argv=argv, result_format=result_format)


def build_cli_command(*args, **kwargs) -> list[str]:
    """Back-compat wrapper — existing callers (review, tests) keep getting argv."""
    return build_cli_invocation(*args, **kwargs).argv


def parse_cli_result(
    result_format: ResultFormat, stdout: str, stderr: str, returncode: int
) -> CliResult:
    if result_format == "claude_json":
        return _parse_claude_json(stdout, stderr, returncode)
    it, ot, cost = parse_token_usage(stderr)   # "text": unchanged behavior
    return CliResult(text=stdout, input_tokens=it, output_tokens=ot,
                     cost_usd=cost, is_error=returncode != 0)
```

- `result_format` is `"claude_json"` **only** for an explicitly-recognized claude
  binary (`Path(cmd).name in {"claude", "claude-code"}`) with `json_output` set
  and no template. The unknown-command fallback, templated claude, and custom
  wrappers (`my-claude-wrapper`) all get `"text"` and no `--output-format json` →
  never mis-parsed. This closes the false-positive class entirely (the builder's
  `else` branch treats unknown commands as claude-style argv, but they stay
  `"text"`).
- `build_cli_command` stays as a thin wrapper returning `.argv`, so **review and
  all existing tests are unchanged**.
- Adding another CLI later = one branch in `build_cli_invocation` (its
  result_format) + one in `parse_cli_result`. `execution.py` untouched.

**Files:** `runner.py` (CliResult, CliInvocation, `build_cli_invocation`,
`_parse_claude_json`, wrapper, `parse_cli_result`), `execution.py` (use the
invocation + seam). Docs as needed.

## claude JSON parsing (`_parse_claude_json`)

`claude -p --output-format json` returns one JSON object. **Field names below are
assumed and MUST be verified against the real CLI before coding** (see
prerequisite). `claude --help` confirms the flags (`--output-format json`,
`--max-budget-usd`); it does *not* confirm payload field names.

```json
{ "result": "…incl TASK_COMPLETE…", "total_cost_usd": 0.0123,
  "usage": { "input_tokens": 1200, "output_tokens": 340 },
  "is_error": false, "subtype": "success" }
```

```python
def _parse_claude_json(stdout: str, stderr: str, returncode: int) -> CliResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        it, ot, cost = parse_token_usage(stderr)        # graceful fallback
        return CliResult(text=stdout, input_tokens=it, output_tokens=ot,
                         cost_usd=cost, is_error=returncode != 0)

    usage = data.get("usage") or {}
    is_error = bool(data.get("is_error", False))
    result_text = str(data.get("result") or "")
    if is_error:
        # Build a meaningful message so the failure path / classify isn't fed an
        # empty string (stderr is often empty in JSON mode).
        parts = [data.get("subtype"), data.get("error"), data.get("message"), result_text]
        result_text = " | ".join(str(p) for p in parts if p) or "claude reported is_error"

    return CliResult(
        text=result_text,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=data.get("total_cost_usd"),
        is_error=is_error,
    )
```

- **Defensive:** invalid JSON → text + stderr fallback.
- **Marker** `TASK_COMPLETE` detected in `text` (= `result`).
- **Cost** = `total_cost_usd`; tokens from `usage`. Non-None for claude →
  `cost_tracking=ok`, budgets enforceable.
- **Richer error payload:** on `is_error`, fold `subtype`/`error`/`message`/`result`
  into `text` so the failure is classifiable even when stderr is empty.

## Integration (execution path only)

`execution.py` builds the invocation with JSON requested + the native cap, runs
it (still `subprocess.run`), and parses via the explicit tag:

```python
# Native hard cap = the tightest of the remaining task and global budgets.
caps = []
if config.task_budget_usd is not None:
    caps.append(config.task_budget_usd - state.task_cost(task_id))
if config.budget_usd is not None:
    caps.append(config.budget_usd - state.total_cost())
max_budget = max(0.0, min(caps)) if caps else None     # None = no cap

invocation = build_cli_invocation(
    config.claude_command, prompt, model=..., template=config.command_template,
    skip_permissions=..., json_output=True, max_budget_usd=max_budget,
)
result = subprocess.run(invocation.argv, ...)          # unchanged runner mechanics

cli_result = parse_cli_result(
    invocation.result_format, result.stdout, result.stderr, result.returncode
)
output = cli_result.text                               # NOT result.stdout
combined_output = output + "\n" + result.stderr        # for check_error_patterns / classify
input_tokens, output_tokens, cost_usd = (
    cli_result.input_tokens, cli_result.output_tokens, cli_result.cost_usd
)
has_complete_marker = "TASK_COMPLETE" in cli_result.text
implicit_success = result.returncode == 0 and not has_failed_marker and not cli_result.is_error
success = (
    has_complete_marker and not has_failed_marker and not cli_result.is_error
) or implicit_success
```

Three integration details the plan must make explicit:

- **Log write uses `output` (= `cli_result.text`), not `result.stdout`.** The
  existing `=== OUTPUT ===` log block (execution.py:140) must run *after*
  `parse_cli_result` and write the parsed text, so the per-task log is readable
  prose, not a raw JSON blob.
- **`classify` input becomes `combined_output`.** The failure branch
  (execution.py:274) currently calls `classify(result.stderr, result.returncode)`.
  In JSON mode the error meaning lives in `cli_result.text` (stderr is often
  empty), so change it to `classify(combined_output, result.returncode)` —
  otherwise the richer `is_error` payload reaches the log but not the
  classification.
- **`--max-budget-usd` = `min` of remaining task and global budgets** (whichever
  are set), via `state.task_cost(task_id)` and `state.total_cost()`. Format with
  `_fmt_budget()` that preserves small limits (e.g. `f"{x:.6f}".rstrip("0").rstrip(".")`)
  — a naive `:.2f` rounds tiny caps to `0.00` and would block the call.

Other notes:

- `json_output=True` / `max_budget_usd` are honored **only** by the explicit
  claude branch; other CLIs and templated/wrapper commands get `result_format="text"`.
- `review.py` is **not** changed (text mode; `REVIEW_PASSED` as before).
- `--max-budget-usd` is a hard guard on the CLI call — protects spend even if JSON
  parsing breaks or the process overspends before state is written.

## Streaming / TUI (corrected)

There is **no live-streaming regression**: the execution path uses blocking
`subprocess.run` (execution.py:124) and collects stdout in bulk — it never streamed
claude tokens live. `runner.run_claude_async` (the EventBus streaming variant) is
defined and exported but **not called anywhere** in the codebase, so switching
claude to single-shot JSON loses nothing currently in use. The TUI shows
task/stage-level progress, not live claude output. (If `run_claude_async` is ever
wired into a TUI path, claude-JSON would then warrant `--output-format stream-json`
— out of scope here.) The executor writes `cli_result.text` (parsed result, not
raw JSON) to the per-task log for readability.

## Error handling

- Invalid JSON / nonzero exit → defensive fallback (text + stderr); existing
  `returncode` / `check_error_patterns(combined_output)` handling runs as before.
  The failure-branch `classify(...)` call switches from `result.stderr` to
  `combined_output` (= parsed text + stderr) so a JSON `is_error` payload is
  classifiable even when stderr is empty.
- `is_error=true` (possibly with `returncode==0`) forces non-success → classified
  and retried; its message is folded into `text`.
- `parse_token_usage` retained (text branch + fallback).
- After the fix, `doctor --cli=claude` should report `cost_tracking=ok` → READY.

## Backward compatibility & a budget caveat

- **`--json-result` (Maestro contract):** `cost_usd` now populated for claude —
  more accurate, schema unchanged. Verify golden tests don't assert `cost=None`.
- **`build_cli_command`** keeps returning `list[str]` (wrapper) → review and
  existing tests unchanged.
- **Other CLIs** unchanged (`json_output`/`max_budget_usd` ignored; `result_format="text"`).
- **Budget caveat (honesty):** this fix makes the **executor attempt** cost
  trackable for claude, so `--budget`/`--task-budget` enforce against it. But
  **review-stage cost is still not counted** (review.py stays text mode), so
  `task_budget_usd` — documented as "total per-task budget" — remains *partial*
  until review cost is also captured. `--max-budget-usd` only caps each CLI call,
  not the cumulative task. Tracking review cost is listed as follow-up.

## Testing

- `tests/test_runner.py`:
  - `build_cli_invocation` format gating: `cmd="claude"`, `json_output=True` →
    `--output-format json` present, `result_format=="claude_json"`;
    `cmd="/path/to/claude"` → same (`"claude_json"`); `json_output=False` → no
    flag, `"text"`; **template edge:** `cmd="claude"`, `template="{cmd} -p {prompt}"`
    → no JSON flag, no crash, `"text"`; **wrapper:** `cmd="my-claude-wrapper"` →
    NO flag, `"text"` (explicitly NOT claude_json); **unknown fallback:**
    `cmd="some-unknown-cli"` → claude-style argv but `"text"`, no flag.
  - `max_budget_usd`: adds `--max-budget-usd` only for explicit-claude+json;
    `_fmt_budget` keeps a tiny cap (e.g. `0.003`) from collapsing to `0.00`.
  - `parse_cli_result("claude_json", …)`: cost + tokens from JSON, text from
    `result`; malformed JSON → fallback to text + `parse_token_usage(stderr)`;
    `is_error=true` folds `subtype`/`error` into text; `parse_cli_result("text", …)`
    passthrough.
- `tests/test_execution.py`: a fake "claude" emitting a JSON object → `cost_usd`
  and tokens recorded, marker detected; `is_error` forces non-success / retry.
- `tests/test_doctor.py`: doctor fakes have no JSON → seam `"text"` path,
  existing doctor tests unaffected.
- Review tests unchanged (proving review is untouched).
- `tests/test_json_result_contract.py`: run to confirm the contract holds.
- Manual acceptance: one real `doctor --cli=claude --yes` → `cost_tracking=ok`,
  READY.

**Implementation prerequisite (do FIRST):** verify the real field names of
`claude -p --output-format json` with one cheap real call (or a doctor run) and
pin `_parse_claude_json` to them — the design assumes `result`, `total_cost_usd`,
`usage.input_tokens`, `usage.output_tokens`, `is_error` (and, on error, `subtype`
/ `error` / `message`).

## Out of scope / future

- Cost parsing for codex / pi / ollama (additive: one `build_cli_invocation`
  branch + one `parse_cli_result` branch each).
- **Review-stage cost** (would complete `task_budget_usd` semantics).
- Preserving live TUI streaming for claude via `--output-format stream-json`
  (only relevant if `run_claude_async` is wired into a TUI path).
