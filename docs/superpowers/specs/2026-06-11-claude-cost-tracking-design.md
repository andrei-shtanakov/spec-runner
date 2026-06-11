# Design: Fix cost tracking for the modern claude CLI

**Date:** 2026-06-11
**Status:** Approved (brainstorm), pending implementation plan
**Author:** brainstormed with Claude

## Problem

`spec-runner doctor --cli=claude` against real **claude 2.1.173** reports
`cost_tracking=warn` → DEGRADED. `runner.parse_token_usage()` regex-scrapes
**stderr** for `input_tokens: …` / `cost: $…`, but the current `claude -p` does
not emit those in plain text. So `attempt.cost_usd`/tokens come back `None`, and
`spec-runner costs`, `--budget`, and `--task-budget` silently do nothing for
claude (zeros, no enforcement). This is the "false confidence" case doctor was
built to catch.

claude exposes the data via `--output-format json` (single result), which
carries `result` (the assistant's final text), `total_cost_usd`, and `usage`.
The fix is to invoke claude with `--output-format json` on the execution path and
parse cost from the JSON instead of stderr.

## Key decisions (from brainstorm)

1. **Scope: claude only, behind an extensible seam.** Introduce a small per-CLI
   result parser so codex/pi/ollama can be added later without touching
   `execution.py`; implement only claude now.
2. **Mode: `--output-format json` (single-shot).** Simple, reliable parsing and
   accurate cost. Accepted trade-off: live line-by-line TUI streaming is lost for
   claude (the response arrives as one JSON blob at the end).
3. **Opt-in per call.** `--output-format json` is added only on the execution
   path. The review path is left untouched (it builds the same command via
   `build_cli_command` and detects `REVIEW_PASSED` in text; it needs no cost).
4. **Defensive parsing.** If stdout is not valid JSON, fall back to the previous
   behavior (text + `parse_token_usage(stderr)`) — never crash a run.

## Architecture — the per-CLI result seam

Today `execution.py` hard-codes: `output = stdout`, `cost = parse_token_usage(stderr)`.
Replace that with a single per-CLI extraction seam in `runner.py`:

```python
@dataclass
class CliResult:
    text: str                  # for TASK_COMPLETE marker detection + the task log
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    is_error: bool = False     # CLI explicitly reported an error (claude is_error)


def parse_cli_result(cmd: str, stdout: str, stderr: str, returncode: int) -> CliResult:
    """Per-CLI extraction of (text, usage) from raw process output."""
    if _is_claude(cmd):
        return _parse_claude_json(stdout, stderr, returncode)
    # All other CLIs keep the previous behavior: text = stdout, cost from stderr.
    it, ot, cost = parse_token_usage(stderr)
    return CliResult(text=stdout, input_tokens=it, output_tokens=ot, cost_usd=cost)
```

- `_is_claude(cmd)` — the same detection already used inside `build_cli_command`,
  extracted into a shared helper so the two stay in sync.
- `parse_token_usage` stays (non-claude branches + fallback).
- Adding another CLI later = one more branch in `parse_cli_result`; `execution.py`
  is unchanged.

**Files:** `runner.py` (CliResult + `parse_cli_result` + `_parse_claude_json` +
`_is_claude` + `json_output` param on `build_cli_command`), `execution.py`
(call the seam instead of direct stdout/`parse_token_usage`). Docs as needed.

## claude JSON parsing (`_parse_claude_json`)

`claude -p --output-format json` returns one JSON object. Expected fields
(**verify exact names against the real CLI during implementation**):

```json
{
  "result": "…assistant final text incl. TASK_COMPLETE…",
  "total_cost_usd": 0.0123,
  "usage": { "input_tokens": 1200, "output_tokens": 340 },
  "is_error": false,
  "subtype": "success"
}
```

```python
def _parse_claude_json(stdout: str, stderr: str, returncode: int) -> CliResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        # claude did not emit valid JSON (errored early / mixed output) — fall back.
        it, ot, cost = parse_token_usage(stderr)
        return CliResult(
            text=stdout, input_tokens=it, output_tokens=ot, cost_usd=cost,
            is_error=returncode != 0,
        )
    usage = data.get("usage") or {}
    return CliResult(
        text=str(data.get("result") or ""),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=data.get("total_cost_usd"),
        is_error=bool(data.get("is_error", False)),
    )
```

- **Defensive:** invalid JSON → text + stderr fallback (covers CLI format drift
  and the custom-`command_template` case where the flag isn't added).
- **Marker** `TASK_COMPLETE` is detected in `data["result"]` (the assistant's
  final text, where it is printed).
- **Cost** = `total_cost_usd` directly; tokens from `usage`. Now non-None for
  claude → `cost_tracking=ok`, budgets enforceable.
- **`is_error`** propagated for the success determination (below).

## Integration (opt-in JSON for execution only)

`build_cli_command` gains a `json_output` flag; only the claude branch honors it:

```python
def build_cli_command(cmd, prompt, model="", template="", skip_permissions=False,
                      prompt_file=None, json_output=False) -> list[str]:
    ...
    else:  # claude (default)
        result = [cmd, "-p", prompt]
        if skip_permissions:
            result.append("--dangerously-skip-permissions")
        if model:
            result.extend(["--model", model])
        if json_output:
            result.extend(["--output-format", "json"])
        return result
```

- Default `json_output=False` → existing callers (**including review**) are
  unchanged: claude returns text, `REVIEW_PASSED` is detected as before.
- Only the auto-detect claude branch adds the flag. A `command_template`
  override stays as-is (cost stays unfixed for custom templates — a conscious
  user choice; the defensive fallback keeps it from crashing).

`execution.py` opts in and uses the seam:

```python
cmd = build_cli_command(..., json_output=True)   # passing it is safe: only claude honors it
...
cli_result = parse_cli_result(
    config.claude_command, result.stdout, result.stderr, result.returncode
)
output = cli_result.text
combined_output = output + "\n" + result.stderr   # for check_error_patterns / classify
input_tokens = cli_result.input_tokens
output_tokens = cli_result.output_tokens
cost_usd = cli_result.cost_usd
has_complete_marker = "TASK_COMPLETE" in cli_result.text
```

`review.py` is **not** changed (text mode; no cost needed).

## Streaming / TUI and error handling

**Streaming (accepted degradation for claude):** `run_claude_async` streams
stdout lines to the `event_bus`. In JSON mode claude emits one blob at the end,
so live line-by-line streaming is lost for claude. To keep the task log readable
(not raw JSON), the executor writes `cli_result.text` to the per-task log.
`run_claude_async` itself is unchanged; the degradation is inherent to JSON mode
and will be documented. (Optionally emitting `cli_result.text` to the event bus
post-hoc is out of scope — YAGNI.)

**Error handling:**
- Invalid JSON / nonzero exit → defensive fallback (text + stderr); the existing
  `returncode` / `classify(stderr)` / `check_error_patterns(combined_output)`
  handling runs as before.
- `is_error=true` (claude reported an error, possibly with `returncode==0`) forces
  a non-success so it classifies and retries:
  ```python
  implicit_success = (
      result.returncode == 0 and not has_failed_marker and not cli_result.is_error
  )
  success = (
      has_complete_marker and not has_failed_marker and not cli_result.is_error
  ) or implicit_success
  ```
- `parse_token_usage` stays (non-claude + fallback).
- After the fix, `doctor --cli=claude` should report `cost_tracking=ok` → READY.

## Backward compatibility

- **`--json-result` (Maestro contract):** `cost_usd` is now populated for claude
  — more accurate, schema unchanged. Verify golden tests don't assert `cost=None`.
- **`parse_token_usage`** retained (non-claude + fallback).
- **Review** unchanged (text mode).
- **Other CLIs** (codex/pi/ollama/llama) unchanged — `json_output=True` is ignored
  by their branches, and `parse_cli_result` passes their output through.
- **Existing token tests** use plain-text fakes with a command not containing
  "claude", so `parse_cli_result` passes through / falls back — they keep passing.

## Testing

- `tests/test_runner.py`:
  - `parse_cli_result` for claude JSON (cost + tokens from JSON, text from
    `result`); malformed JSON → fallback to text + `parse_token_usage(stderr)`;
    `is_error=true`; non-claude passthrough.
  - `build_cli_command(json_output=True)` adds `--output-format json` for claude;
    `json_output=False` and `command_template` overrides do not.
- `tests/test_execution.py`: a fake "claude" emitting a JSON object → `cost_usd`
  and tokens recorded, marker detected; `is_error` forces non-success / retry.
- `tests/test_doctor.py`: the doctor fakes (e.g. `ok.sh`) have no "claude" in the
  command → seam passthrough, existing doctor tests unaffected.
- Review tests unchanged (proving review is untouched).
- `tests/test_json_result_contract.py`: run to confirm the contract still holds.
- Manual: one real `doctor --cli=claude --yes` → expect `cost_tracking=ok`, READY.

**Implementation prerequisite:** before finalizing `_parse_claude_json`, verify
the actual field names of `claude -p --output-format json` (one cheap real call
or a doctor run), since the design assumes `result` / `total_cost_usd` /
`usage.input_tokens` / `usage.output_tokens` / `is_error`.

## Out of scope / future

- Cost parsing for codex / pi / ollama (the seam makes these additive — one
  branch each in `parse_cli_result`, plus their own command flags).
- Preserving live TUI streaming for claude (would require `--output-format
  stream-json` and JSONL event parsing).
- Cost for the review path.
