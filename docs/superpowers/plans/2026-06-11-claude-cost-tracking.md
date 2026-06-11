# Claude Cost Tracking Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `spec-runner` parse claude's cost/token usage from `claude -p --output-format json` instead of the stale stderr regex, so `costs` / `--budget` / `--task-budget` work for claude.

**Architecture:** Introduce an explicit per-CLI seam in `runner.py`: `build_cli_invocation(...) -> CliInvocation{argv, result_format}` and `parse_cli_result(result_format, stdout, stderr, rc) -> CliResult`. JSON mode is enabled ONLY for an explicit claude binary (`claude`/`claude-code`) with no template; everything else stays text (current behavior). `execution.py` opts into JSON, reads cost from the parsed result, and passes claude's native `--max-budget-usd` hard cap. `build_cli_command` becomes a thin back-compat wrapper so review and existing callers are untouched.

**Tech Stack:** Python 3.10+, dataclasses, `typing.Literal`, json, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-06-11-claude-cost-tracking-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/spec_runner/runner.py` | **Modify** — add `ResultFormat`, `CliResult`, `CliInvocation`, `_is_explicit_claude`, `_fmt_budget`, `_parse_claude_json`, `parse_cli_result`; refactor `build_cli_command` body into `build_cli_invocation`; keep `build_cli_command` as a wrapper |
| `src/spec_runner/execution.py` | **Modify** — use `build_cli_invocation` (json + max_budget) + `parse_cli_result`; `output = cli_result.text`; `classify(combined_output, …)`; fold `is_error` into success |
| `tests/test_runner.py` | **Modify** — tests for `parse_cli_result`, `_parse_claude_json`, `build_cli_invocation` format gating + template/wrapper/unknown edges + `max_budget` |
| `tests/test_execution.py` | **Modify** — convert `build_cli_command` patches to `build_cli_invocation`; add claude-JSON cost test + `is_error` test |

**Verified anchors (read before editing):**
- `build_cli_command(cmd, prompt, model="", template="", skip_permissions=False, prompt_file=None) -> list[str]` at `runner.py:141`; template branch returns `shlex.split(formatted)`; auto-detect branches end with an `else:` "Claude CLI (default)" at `runner.py:236` that also catches **unknown** commands.
- `parse_token_usage(stderr) -> tuple[int|None, int|None, float|None]` at `runner.py:52`.
- `execution.py`: builds the command at ~line 107 (`cmd = build_cli_command(cmd=config.claude_command, prompt=prompt, model=task_model, template=config.command_template, skip_permissions=config.skip_permissions)`), runs `subprocess.run(cmd, …)` at ~124, sets `output = result.stdout` / `combined_output` at ~133, `parse_token_usage(result.stderr)` at ~137, writes the `=== OUTPUT ===` log at ~140, computes `has_complete_marker`/`implicit_success`/`success` at ~180-184, and on failure calls `classify(result.stderr, result.returncode)` at ~274.
- `execution.py` imports `build_cli_command, parse_token_usage` from `.runner` (~line 13-17).
- `state.task_cost(task_id) -> float` (state.py:665) and `state.total_cost() -> float` (state.py:659). `config.budget_usd` and `config.task_budget_usd` (config.py:126-127).
- Many `execute_task` tests use `@patch("spec_runner.execution.build_cli_command", return_value=["echo", "hi"])`.

---

## Task 0: Verify claude `--output-format json` field names

This is an investigation step — no code. The parser in Task 2 must be pinned to the real field names.

- [ ] **Step 1: Make one cheap real call (requires an authenticated claude CLI)**

Run:
```bash
claude -p --output-format json "Reply with the single word PONG"
```
Expected: a single JSON object on stdout. Note the EXACT keys for: the assistant text, total cost, token usage, and the error flag. The spec assumes `result`, `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`, `is_error` (and on error `subtype`/`error`/`message`).

- [ ] **Step 2: Record findings**

If the real keys differ from the assumptions, note the correct names — Task 2's `_parse_claude_json` must use the real keys. If you cannot run claude (no auth), proceed with the assumed keys, but flag in your report that the field names are UNVERIFIED and the manual acceptance step (Task 6) is mandatory before merge. Do NOT skip this — wrong keys mean cost stays `None` and the bug is "fixed" only on paper.

(No commit — this is investigation. Carry the confirmed key names into Task 2.)

---

## Task 1: `CliResult` + `parse_cli_result` (text path) + `ResultFormat`

**Files:**
- Modify: `src/spec_runner/runner.py`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_runner.py` (add `from spec_runner.runner import CliResult, ResultFormat, parse_cli_result` to the existing `from spec_runner.runner import (...)` block):

```python
class TestParseCliResultText:
    def test_text_passthrough_parses_stderr_cost(self):
        res = parse_cli_result(
            "text",
            stdout="work done TASK_COMPLETE",
            stderr="input_tokens: 500\noutput_tokens: 120\ncost: $0.02",
            returncode=0,
        )
        assert res.text == "work done TASK_COMPLETE"
        assert res.input_tokens == 500
        assert res.output_tokens == 120
        assert res.cost_usd == 0.02
        assert res.is_error is False

    def test_text_nonzero_returncode_is_error(self):
        res = parse_cli_result("text", stdout="", stderr="boom", returncode=1)
        assert res.is_error is True
        assert res.cost_usd is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::TestParseCliResultText -v`
Expected: FAIL — `ImportError: cannot import name 'CliResult'`.

- [ ] **Step 3: Implement the data model + text-path parser**

In `src/spec_runner/runner.py`, add to the imports near the top (after `from pathlib import Path`):
```python
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
```
(Replace the existing `from typing import TYPE_CHECKING` line — merge `Literal` in.)

Add, just below the imports / above `parse_token_usage` (or anywhere at module top level):
```python
ResultFormat = Literal["text", "claude_json"]


@dataclass
class CliResult:
    """Extracted result of a CLI invocation: text (for marker detection + log)
    plus usage. `is_error` means the CLI explicitly reported failure."""

    text: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    is_error: bool = False
```

Add this function (it can reference `parse_token_usage`, which is defined in the same module; `_parse_claude_json` is added in Task 2 — define a placeholder forward reference by ordering `parse_cli_result` AFTER `_parse_claude_json`, OR keep `parse_cli_result` here and add `_parse_claude_json` in Task 2 above it):
```python
def parse_cli_result(
    result_format: ResultFormat, stdout: str, stderr: str, returncode: int
) -> CliResult:
    """Per-CLI extraction of (text, usage) from raw process output, keyed on the
    explicit result_format tag from build_cli_invocation."""
    if result_format == "claude_json":
        return _parse_claude_json(stdout, stderr, returncode)
    input_tokens, output_tokens, cost = parse_token_usage(stderr)
    return CliResult(
        text=stdout,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        is_error=returncode != 0,
    )
```

NOTE: `parse_cli_result` references `_parse_claude_json` (added in Task 2). Until Task 2 lands, add a temporary stub so this task's tests pass in isolation:
```python
def _parse_claude_json(stdout: str, stderr: str, returncode: int) -> "CliResult":
    raise NotImplementedError  # implemented in Task 2
```
The text-path tests never hit it, so they pass. Task 2 replaces the stub.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_runner.py::TestParseCliResultText -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/runner.py tests/test_runner.py
git commit -m "feat(runner): CliResult + parse_cli_result text path"
```

---

## Task 2: `_parse_claude_json` (claude_json path)

**Files:**
- Modify: `src/spec_runner/runner.py` (replace the Task 1 stub)
- Test: `tests/test_runner.py`

Use the field names CONFIRMED in Task 0. The code below assumes `result`, `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`, `is_error`, and on error `subtype`/`error`/`message` — adjust if Task 0 found different keys.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py`:
```python
import json as _json

from spec_runner.runner import _parse_claude_json


class TestParseClaudeJson:
    def test_parses_cost_tokens_and_text(self):
        payload = _json.dumps(
            {
                "result": "did the work\nTASK_COMPLETE",
                "total_cost_usd": 0.0123,
                "usage": {"input_tokens": 1200, "output_tokens": 340},
                "is_error": False,
            }
        )
        res = _parse_claude_json(payload, stderr="", returncode=0)
        assert "TASK_COMPLETE" in res.text
        assert res.input_tokens == 1200
        assert res.output_tokens == 340
        assert res.cost_usd == 0.0123
        assert res.is_error is False

    def test_malformed_json_falls_back_to_text_and_stderr(self):
        res = _parse_claude_json(
            "not json at all",
            stderr="input_tokens: 10\noutput_tokens: 2\ncost: $0.01",
            returncode=0,
        )
        assert res.text == "not json at all"
        assert res.cost_usd == 0.01
        assert res.input_tokens == 10

    def test_is_error_folds_message_into_text(self):
        payload = _json.dumps(
            {"result": "", "is_error": True, "subtype": "error_max_turns",
             "error": "too many turns"}
        )
        res = _parse_claude_json(payload, stderr="", returncode=0)
        assert res.is_error is True
        assert "error_max_turns" in res.text
        assert "too many turns" in res.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::TestParseClaudeJson -v`
Expected: FAIL — the stub raises `NotImplementedError`.

- [ ] **Step 3: Replace the stub with the real parser**

In `src/spec_runner/runner.py`, replace the Task 1 `_parse_claude_json` stub with:
```python
def _parse_claude_json(stdout: str, stderr: str, returncode: int) -> CliResult:
    """Parse `claude -p --output-format json` output. Falls back to text +
    stderr parsing when stdout is not valid JSON (CLI format drift, early
    crash, or a templated claude that never got the flag)."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        input_tokens, output_tokens, cost = parse_token_usage(stderr)
        return CliResult(
            text=stdout,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            is_error=returncode != 0,
        )

    usage = data.get("usage") or {}
    is_error = bool(data.get("is_error", False))
    text = str(data.get("result") or "")
    if is_error:
        parts = [data.get("subtype"), data.get("error"), data.get("message"), text]
        text = " | ".join(str(p) for p in parts if p) or "claude reported is_error"

    return CliResult(
        text=text,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=data.get("total_cost_usd"),
        is_error=is_error,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_runner.py::TestParseClaudeJson tests/test_runner.py::TestParseCliResultText -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/runner.py tests/test_runner.py
git commit -m "feat(runner): _parse_claude_json with defensive fallback + is_error payload"
```

---

## Task 3: `CliInvocation` + `build_cli_invocation` + helpers + `build_cli_command` wrapper

**Files:**
- Modify: `src/spec_runner/runner.py`
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runner.py` (add `CliInvocation, build_cli_invocation` to the runner import):
```python
class TestBuildCliInvocation:
    def test_explicit_claude_json_adds_flag_and_tag(self):
        inv = build_cli_invocation("claude", "hi", json_output=True)
        assert "--output-format" in inv.argv and "json" in inv.argv
        assert inv.result_format == "claude_json"

    def test_claude_path_basename_recognized(self):
        inv = build_cli_invocation("/usr/local/bin/claude", "hi", json_output=True)
        assert inv.result_format == "claude_json"

    def test_no_json_when_not_requested(self):
        inv = build_cli_invocation("claude", "hi", json_output=False)
        assert "--output-format" not in inv.argv
        assert inv.result_format == "text"

    def test_template_claude_stays_text(self):
        inv = build_cli_invocation(
            "claude", "hi", template="{cmd} -p {prompt}", json_output=True
        )
        assert "--output-format" not in inv.argv
        assert inv.result_format == "text"

    def test_wrapper_name_is_text_not_claude_json(self):
        inv = build_cli_invocation("my-claude-wrapper", "hi", json_output=True)
        assert "--output-format" not in inv.argv
        assert inv.result_format == "text"

    def test_unknown_command_is_text(self):
        inv = build_cli_invocation("some-unknown-cli", "hi", json_output=True)
        assert inv.result_format == "text"
        assert "--output-format" not in inv.argv

    def test_codex_ignores_json_output(self):
        inv = build_cli_invocation("codex", "hi", json_output=True)
        assert inv.result_format == "text"
        assert inv.argv[:2] == ["codex", "exec"]

    def test_max_budget_added_for_claude_json(self):
        inv = build_cli_invocation("claude", "hi", json_output=True, max_budget_usd=0.003)
        assert "--max-budget-usd" in inv.argv
        i = inv.argv.index("--max-budget-usd")
        assert inv.argv[i + 1] == "0.003"   # not rounded to 0.00

    def test_build_cli_command_wrapper_returns_argv(self):
        argv = build_cli_command("claude", "hi")
        assert argv == ["claude", "-p", "hi"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner.py::TestBuildCliInvocation -v`
Expected: FAIL — `ImportError: cannot import name 'CliInvocation'`.

- [ ] **Step 3: Add the dataclass + helpers, refactor the builder**

In `src/spec_runner/runner.py`, add near `CliResult`:
```python
@dataclass
class CliInvocation:
    """A built CLI command plus how to parse its output."""

    argv: list[str]
    result_format: ResultFormat


def _is_explicit_claude(cmd: str) -> bool:
    """True ONLY for an explicit claude binary (`claude` / `claude-code`), never
    the unknown-command fallback. `/usr/local/bin/claude` matches;
    `my-claude-wrapper` and `codex` do not."""
    return Path(cmd).name in ("claude", "claude-code")


def _fmt_budget(amount: float) -> str:
    """Format a USD cap without collapsing small values to 0.00."""
    return f"{amount:.6f}".rstrip("0").rstrip(".") or "0"
```

Now rename the existing `build_cli_command` to `build_cli_invocation`, add the two new params, and change EVERY `return [...]` to `return CliInvocation([...], "text")`, except the claude `else` branch. Concretely:

Change the signature (`runner.py:141`) to:
```python
def build_cli_invocation(
    cmd: str,
    prompt: str,
    model: str = "",
    template: str = "",
    skip_permissions: bool = False,
    prompt_file: Path | None = None,
    json_output: bool = False,
    max_budget_usd: float | None = None,
) -> CliInvocation:
```

Template branch: `return shlex.split(formatted)` → `return CliInvocation(shlex.split(formatted), "text")`.

Each auto-detect branch (`llama-cli`, `llama-server`/curl, `ollama`, `opencode`, `codex`, `pi`): change `return result` / `return [...]` → `return CliInvocation(result, "text")` (or `CliInvocation([...], "text")` for the inline-list returns).

The final `else:` (Claude default + unknown fallback) becomes:
```python
    else:
        # Claude CLI (default) — also the fallback for unknown commands.
        result = [cmd, "-p", prompt]
        if skip_permissions:
            result.append("--dangerously-skip-permissions")
        if model:
            result.extend(["--model", model])
        # JSON cost mode: ONLY for an explicit claude binary (not unknown
        # fallback / wrapper), and only when requested. `template` is empty here
        # (the template branch returns earlier).
        if json_output and _is_explicit_claude(cmd):
            result.extend(["--output-format", "json"])
            if max_budget_usd is not None:
                result.extend(["--max-budget-usd", _fmt_budget(max_budget_usd)])
            return CliInvocation(result, "claude_json")
        return CliInvocation(result, "text")
```

Add the back-compat wrapper directly below `build_cli_invocation`:
```python
def build_cli_command(
    cmd: str,
    prompt: str,
    model: str = "",
    template: str = "",
    skip_permissions: bool = False,
    prompt_file: Path | None = None,
) -> list[str]:
    """Back-compat wrapper returning just argv (review + existing callers)."""
    return build_cli_invocation(
        cmd, prompt, model, template, skip_permissions, prompt_file
    ).argv
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
uv run pytest tests/test_runner.py -v
```
Expected: PASS — the new `TestBuildCliInvocation` plus the pre-existing `TestBuildCliCommand` (which uses the wrapper) all green.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/runner.py tests/test_runner.py
git commit -m "feat(runner): build_cli_invocation with explicit result_format; build_cli_command wrapper"
```

---

## Task 4: Wire `execution.py` to the seam

**Files:**
- Modify: `src/spec_runner/execution.py`
- Test: `tests/test_execution.py`

This switches `execute_task` from `build_cli_command`/`parse_token_usage` to `build_cli_invocation`/`parse_cli_result`. It **breaks** execute_task tests that patch `spec_runner.execution.build_cli_command` — Task 5 fixes those. Keep this task's edits to `src/` + the two NEW tests; run the targeted new tests here, and do the full-suite green in Task 5.

- [ ] **Step 1: Write the failing tests (new behavior)**

Append to `tests/test_execution.py` (mirror the existing token-test decorator stack, but patch `build_cli_invocation` returning a `CliInvocation`, and import it):
```python
from spec_runner.runner import CliInvocation


class TestClaudeJsonCost:
    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["claude", "-p", "x", "--output-format", "json"], "claude_json"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="p")
    @patch("spec_runner.execution.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution.subprocess.run")
    def test_cost_parsed_from_claude_json(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_inv, mock_log, mock_status, tmp_path
    ):
        import json as _json

        mock_run.return_value = MagicMock(
            stdout=_json.dumps(
                {
                    "result": "done TASK_COMPLETE",
                    "total_cost_usd": 0.05,
                    "usage": {"input_tokens": 900, "output_tokens": 210},
                    "is_error": False,
                }
            ),
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        assert execute_task(task, config, state) is True
        att = state.get_task_state("TASK-001").attempts[-1]
        assert att.cost_usd == 0.05
        assert att.input_tokens == 900
        assert att.output_tokens == 210

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["claude", "-p", "x", "--output-format", "json"], "claude_json"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="p")
    @patch("spec_runner.execution.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution.subprocess.run")
    def test_is_error_json_forces_failure(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_inv, mock_log, mock_status, tmp_path
    ):
        import json as _json

        mock_run.return_value = MagicMock(
            stdout=_json.dumps({"result": "TASK_COMPLETE", "is_error": True, "subtype": "error"}),
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        # is_error must override the TASK_COMPLETE marker → not a success.
        assert execute_task(task, config, state) is not True
```

NOTE: confirm `_make_task`/`_make_config`/`_make_state` exist in `tests/test_execution.py` (they do — used by the existing token tests). Match the exact decorator/parameter ordering of the existing token tests when you write these.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestClaudeJsonCost -v`
Expected: FAIL — `AttributeError: ... does not have the attribute 'build_cli_invocation'` (execution doesn't import it yet).

- [ ] **Step 3: Edit `execution.py`**

In `src/spec_runner/execution.py`, find the `from .runner import (...)` block
(~line 13-17). It currently lists `build_cli_command` and `parse_token_usage`
among the runner imports. Remove those two names and add `build_cli_invocation`
and `parse_cli_result`. Leave every other name in that import block unchanged.
After the edit, confirm `build_cli_command` / `parse_token_usage` are no longer
referenced anywhere in `execution.py` (`grep -n 'build_cli_command\|parse_token_usage' src/spec_runner/execution.py` → no hits).

Replace the command build (~line 107-114):
```python
        task_model = config.get_model_for_role("implementer")
        # Native hard cap = tightest of remaining task / global budgets.
        caps: list[float] = []
        if config.task_budget_usd is not None:
            caps.append(config.task_budget_usd - state.task_cost(task_id))
        if config.budget_usd is not None:
            caps.append(config.budget_usd - state.total_cost())
        max_budget = max(0.0, min(caps)) if caps else None

        invocation = build_cli_invocation(
            cmd=config.claude_command,
            prompt=prompt,
            model=task_model,
            template=config.command_template,
            skip_permissions=config.skip_permissions,
            json_output=True,
            max_budget_usd=max_budget,
        )
```

Change `subprocess.run(cmd, …)` (~line 124) to `subprocess.run(invocation.argv, …)`.

Replace the output/usage block (~line 133-137):
```python
        duration = (datetime.now() - start_time).total_seconds()
        cli_result = parse_cli_result(
            invocation.result_format, result.stdout, result.stderr, result.returncode
        )
        output = cli_result.text                      # parsed text, NOT raw stdout
        combined_output = output + "\n" + result.stderr
        input_tokens = cli_result.input_tokens
        output_tokens = cli_result.output_tokens
        cost_usd = cli_result.cost_usd
```
(The `=== OUTPUT ===` log block right below now writes this `output` — verify it does; no change needed there beyond `output` being the parsed text.)

Update the success logic (~line 180-184):
```python
        has_complete_marker = "TASK_COMPLETE" in output
        has_failed_marker = "TASK_FAILED" in output
        implicit_success = (
            result.returncode == 0 and not has_failed_marker and not cli_result.is_error
        )
        success = (
            has_complete_marker and not has_failed_marker and not cli_result.is_error
        ) or implicit_success
```

Update the failure-branch classify (~line 274):
```python
                error_kind, error = classify(combined_output, result.returncode)
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_execution.py::TestClaudeJsonCost -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/execution.py tests/test_execution.py
git commit -m "feat(execution): parse claude cost from JSON via the CLI seam (+ is_error, max-budget)"
```

---

## Task 5: Repair the `build_cli_command` patches in `test_execution.py`

Task 4 made `execute_task` call `build_cli_invocation` instead of `build_cli_command`, so every execute_task test that patches `spec_runner.execution.build_cli_command` now patches a function the code no longer calls (and `build_cli_command` may no longer be imported there → AttributeError).

**Files:**
- Modify: `tests/test_execution.py`

- [ ] **Step 1: Find the broken patches**

Run: `grep -n 'spec_runner.execution.build_cli_command' tests/test_execution.py`
Each hit is a decorator on an execute_task test.

- [ ] **Step 2: Convert each to `build_cli_invocation`**

For EACH such test:
- Change the decorator
  `@patch("spec_runner.execution.build_cli_command", return_value=["echo", "hi"])`
  to
  `@patch("spec_runner.execution.build_cli_invocation", return_value=CliInvocation(["echo", "hi"], "text"))`.
- The mock parameter (e.g. `mock_cmd`) stays in the same position (still one decorator) — only its meaning changes; rename to `mock_inv` for clarity if you like, but keep the positional alignment intact.
- Ensure `from spec_runner.runner import CliInvocation` is imported at the top of `tests/test_execution.py` (added in Task 4).

Using `result_format="text"` keeps these tests' behavior identical: `parse_cli_result("text", stdout, stderr, rc)` → `parse_token_usage(stderr)`, exactly the old path. So the existing token-from-stderr assertions keep passing.

- [ ] **Step 3: Run the full suite to verify green**

Run:
```bash
uv run pytest tests/test_execution.py -v
uv run pytest tests/ -m "not slow" -q
uv run ruff format . && uv run ruff check .
uv run mypy src
```
Expected: all green, no regressions. If a test still fails because it asserted stderr-cost while now routed through `claude_json`, check that its patch uses `"text"` (it should).

- [ ] **Step 4: Commit**

```bash
git add tests/test_execution.py
git commit -m "test(execution): patch build_cli_invocation (CliInvocation) after seam switch"
```

---

## Task 6: Contract + doctor regression + manual acceptance

**Files:**
- (verification only; no `src/` change expected)

- [ ] **Step 1: Maestro contract still holds**

Run: `uv run pytest tests/test_json_result_contract.py -v`
Expected: PASS. If a golden fixture asserted `cost_usd: null` for a claude scenario, that's expected to still hold (fixtures are synthetic and don't run real claude). Investigate any failure before proceeding.

- [ ] **Step 2: doctor fakes unaffected**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: PASS — the doctor fake CLIs are not named `claude`/`claude-code`, so `build_cli_invocation` gives them `result_format="text"` and they parse stderr as before.

- [ ] **Step 3: Manual acceptance (real claude)**

With an authenticated claude:
```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/spec-runner
uv run spec-runner doctor --cli=claude --yes
```
Expected: `cost_tracking ok cost=$<nonzero>` and **Verdict: READY** (previously DEGRADED). If still `warn`, the JSON field names were wrong — fix `_parse_claude_json` (Task 2) per the actual payload and re-run. Record the observed JSON shape.

- [ ] **Step 4: (no commit)** — verification only.

---

## Task 7: Documentation + memory

**Files:**
- Modify: `CHANGELOG.md`, `CLAUDE.md`
- Modify: `TODO.md` (mark the cost bug fixed)

- [ ] **Step 1: CHANGELOG**

Under `## [Unreleased]` → `### Fixed`, add:
```markdown
- **Cost tracking for the claude CLI.** `execute_task` now invokes claude with
  `--output-format json` and parses `total_cost_usd` / `usage` from the result
  (the old stderr regex no longer matches modern claude), so `costs`,
  `--budget`, and `--task-budget` work for claude again. Implemented behind a
  per-CLI result seam (`build_cli_invocation` → `CliInvocation{argv,
  result_format}`, `parse_cli_result`); JSON mode is gated to an explicit claude
  binary, so other CLIs / templates / wrappers are unaffected. claude's native
  `--max-budget-usd` is passed as a hard cap. Review-stage cost is still not
  tracked (follow-up).
```

- [ ] **Step 2: CLAUDE.md**

In the `runner.py` module-table row, append a note that it now exposes
`build_cli_invocation`/`CliInvocation`/`parse_cli_result`/`CliResult` and that
claude runs use `--output-format json` for cost. In the `execution.py` row, note
cost now comes from `parse_cli_result`.

- [ ] **Step 3: TODO.md**

Mark the "Cost tracking сломан для современного claude CLI" backlog item as
`✅ ИСПРАВЛЕНО (<this PR/commit>)` with `[x]` on its sub-items, mirroring the
DONE-status entry style.

- [ ] **Step 4: Verify + commit**

Run: `uv run ruff check . && uv run pytest tests/ -m "not slow" -q`
```bash
git add CHANGELOG.md CLAUDE.md TODO.md
git commit -m "docs: document claude cost-tracking fix; mark TODO item done"
```

---

## Final verification

- [ ] **Run everything:**
```bash
uv run pytest tests/ -v
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run spec-runner doctor --cli=claude --yes   # expect READY (manual, real claude)
```
Expected: full suite green; doctor READY with non-zero cost.
