"""Runner utilities for spec-runner.

Contains logging, error checking, callback, and CLI command building
functions used by the executor and hooks modules.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .events import EventBus

from .config import ERROR_PATTERNS, PROGRESS_FILE

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


@dataclass
class CliInvocation:
    """A built CLI command plus how to parse its output."""

    argv: list[str]
    result_format: ResultFormat


def _is_explicit_claude(cmd: str) -> bool:
    """True ONLY for an explicit claude binary (`claude` / `claude-code`), never
    the unknown-command fallback."""
    return Path(cmd).name in ("claude", "claude-code")


def _fmt_budget(amount: float) -> str:
    """Format a USD cap without collapsing small values to 0.00."""
    return f"{amount:.6f}".rstrip("0").rstrip(".") or "0"


def _coerce_int(value: object) -> int | None:
    """Coerce a JSON value to int, or None if absent/non-numeric — keeps token
    invariants stable even if claude reports usage as strings."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    """Coerce a JSON value to float, or None if absent/non-numeric — keeps cost
    invariants (sums, budget checks) stable even if claude reports cost as a string."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_claude_json(stdout: str, stderr: str, returncode: int) -> CliResult:
    """Parse `claude -p --output-format json`. Falls back to text + stderr when
    stdout is not valid JSON (format drift / early crash / templated claude)."""
    try:
        data = json.loads(stdout)
        if not isinstance(data, dict):
            raise ValueError("claude JSON is not an object")
    except (json.JSONDecodeError, ValueError):
        input_tokens, output_tokens, cost = parse_token_usage(stderr)
        return CliResult(
            text=stdout,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            is_error=returncode != 0,
        )
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    is_error = bool(data.get("is_error", False))
    text = str(data.get("result") or "")
    if is_error:
        parts = [data.get("subtype"), data.get("error"), data.get("message"), text]
        text = " | ".join(str(p) for p in parts if p) or "claude reported is_error"
    return CliResult(
        text=text,
        input_tokens=_coerce_int(usage.get("input_tokens")),
        output_tokens=_coerce_int(usage.get("output_tokens")),
        cost_usd=_coerce_float(data.get("total_cost_usd")),
        is_error=is_error,
    )


def parse_cli_result(
    result_format: ResultFormat, stdout: str, stderr: str, returncode: int
) -> CliResult:
    """Per-CLI extraction of (text, usage), keyed on the explicit result_format
    tag from build_cli_invocation."""
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


def log_progress(message: str, task_id: str | None = None):
    """Log progress message with timestamp to progress file and structlog."""
    from .logging import get_logger

    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{task_id}] " if task_id else ""
    line = f"[{timestamp}] {prefix}{message}\n"

    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line)

    # Structured log (replaces print)
    logger = get_logger("runner")
    if task_id:
        logger.info(message, task_id=task_id)
    else:
        logger.info(message)


def check_error_patterns(output: str) -> str | None:
    """Check output for API error patterns. Returns matched pattern or None."""
    output_lower = output.lower()
    for pattern in ERROR_PATTERNS:
        if pattern.lower() in output_lower:
            return pattern
    return None


def parse_token_usage(stderr: str) -> tuple[int | None, int | None, float | None]:
    """Extract (input_tokens, output_tokens, cost_usd) from Claude CLI stderr.

    Parses common patterns like "input_tokens: 12,500" and "cost: $0.12".
    Returns None for any field that can't be parsed. Never raises.
    """

    def _parse_int(pattern: str) -> int | None:
        m = re.search(pattern, stderr, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
        return None

    def _parse_float(pattern: str) -> float | None:
        m = re.search(pattern, stderr, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return None
        return None

    input_tokens = _parse_int(r"input[_ ]tokens?[:\s]+(\d[\d,]*)")
    output_tokens = _parse_int(r"output[_ ]tokens?[:\s]+(\d[\d,]*)")
    cost = _parse_float(r"(?:total[_ ])?cost[:\s]+\$?([\d.]+)")
    return input_tokens, output_tokens, cost


def send_callback(
    callback_url: str,
    task_id: str,
    status: str,
    duration: float | None = None,
    error: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Send task status callback to orchestrator.

    Uses urllib to avoid adding dependencies. Errors are silently
    ignored — callback is best-effort, state file is the fallback.

    Args:
        callback_url: URL to POST status to.
        task_id: Task identifier.
        status: Task status (started, success, failed).
        duration: Execution duration in seconds.
        error: Error message if failed.
        input_tokens: Input tokens consumed (if available).
        output_tokens: Output tokens consumed (if available).
        cost_usd: Cost in USD (if available).
    """
    if not callback_url:
        return

    import urllib.request

    payload: dict[str, str | float | int] = {
        "task_id": task_id,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }
    if duration is not None:
        payload["duration_seconds"] = duration
    if error:
        payload["error"] = error
    if input_tokens is not None:
        payload["input_tokens"] = input_tokens
    if output_tokens is not None:
        payload["output_tokens"] = output_tokens
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            callback_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        from .logging import get_logger

        get_logger("runner").debug("callback_failed", url=callback_url, exc_info=True)


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
    """Build CLI command from template or auto-detect based on command name.

    Args:
        cmd: CLI command name (e.g., "claude", "codex", "opencode", "pi",
            "ollama", "llama-cli")
        prompt: The prompt text
        model: Model name (optional)
        template: Command template with placeholders (optional)
        skip_permissions: Add --dangerously-skip-permissions for Claude (optional)
        prompt_file: Path to file containing prompt (optional, for large prompts)
        json_output: Request JSON output (only honoured for explicit claude binary)
        max_budget_usd: Per-invocation spend cap (only used with json_output+claude)

    Returns:
        CliInvocation with argv ready for subprocess and result_format tag.

    Template placeholders:
        {cmd} - CLI command
        {model} - Model name
        {prompt} - Prompt text (shell-escaped)
        {prompt_file} - Path to prompt file
    """
    # Use template if provided — templates bypass json_output (unknown format)
    if template:
        prompt_escaped = shlex.quote(prompt)
        prompt_file_str = str(prompt_file) if prompt_file else ""
        formatted = template.format(
            cmd=cmd,
            model=model,
            prompt=prompt_escaped,
            prompt_file=prompt_file_str,
        )
        return CliInvocation(shlex.split(formatted), "text")

    # Auto-detect based on command name
    cmd_lower = cmd.lower()
    # "pi" is too short for substring matching — match on basename only to
    # avoid false positives like "/usr/local/bin/anti-pi" or "opencode-pi-cli".
    cmd_basename = Path(cmd).name.lower()

    if "llama-cli" in cmd_lower or "llama.cpp" in cmd_lower:
        # llama.cpp CLI
        result = [cmd, "-p", prompt, "--no-display-prompt"]
        if model:
            result.extend(["-m", model])
        return CliInvocation(result, "text")

    elif "llama-server" in cmd_lower or "localhost:8080" in cmd_lower:
        # llama.cpp server via curl
        payload = json.dumps({"prompt": prompt})
        return CliInvocation(
            ["curl", "-s", "http://localhost:8080/completion", "-d", payload], "text"
        )

    elif "ollama" in cmd_lower:
        # Ollama CLI
        return CliInvocation([cmd, "run", model or "llama3", prompt], "text")

    elif "opencode" in cmd_lower:
        # sst/opencode: `opencode run [--model provider/id] <prompt>`
        # Prompt is positional, model accepts "provider/model" form
        # (e.g. "anthropic/claude-3-5-sonnet").
        result = [cmd, "run"]
        if model:
            result.extend(["--model", model])
        result.append(prompt)
        return CliInvocation(result, "text")

    elif "codex" in cmd_lower:
        # codex: `codex exec [-m MODEL] [PROMPT]`
        # NOTE: codex's `-p` is `--profile`, not the prompt — DO NOT use it here.
        result = [cmd, "exec"]
        if model:
            result.extend(["-m", model])
        result.append(prompt)
        return CliInvocation(result, "text")

    elif cmd_basename == "pi" or cmd_basename.startswith("pi."):
        # earendil-works/pi: `pi -p [--model X] <prompt>` (non-interactive mode)
        # Model accepts "provider/id" or bare model name; defaults driven by
        # `~/.config/pi/config.yaml`. Match on basename to avoid short-name
        # collisions (see cmd_basename comment above).
        result = [cmd, "-p"]
        if model:
            result.extend(["--model", model])
        result.append(prompt)
        return CliInvocation(result, "text")

    else:
        # Claude CLI (default) — also the fallback for unknown commands.
        result = [cmd, "-p", prompt]
        if skip_permissions:
            result.append("--dangerously-skip-permissions")
        if model:
            result.extend(["--model", model])
        if json_output and _is_explicit_claude(cmd):
            result.extend(["--output-format", "json"])
            if max_budget_usd is not None:
                result.extend(["--max-budget-usd", _fmt_budget(max_budget_usd)])
            return CliInvocation(result, "claude_json")
        return CliInvocation(result, "text")


def build_cli_command(
    cmd: str,
    prompt: str,
    model: str = "",
    template: str = "",
    skip_permissions: bool = False,
    prompt_file: Path | None = None,
) -> list[str]:
    """Back-compat wrapper returning just argv (review + existing callers)."""
    return build_cli_invocation(cmd, prompt, model, template, skip_permissions, prompt_file).argv


async def run_claude_async(
    cmd: list[str],
    timeout: float,
    cwd: str,
    event_bus: EventBus | None = None,
    task_id: str = "",
) -> tuple[str, str, int]:
    """Run CLI command asynchronously with optional event streaming.

    When event_bus is provided, stdout is streamed line-by-line as TaskEvents
    for live TUI updates. Otherwise, stdout is collected in bulk (original behavior).

    Args:
        cmd: Command arguments.
        timeout: Timeout in seconds.
        cwd: Working directory.
        event_bus: Optional EventBus for streaming stdout lines as events.
        task_id: Task ID for event attribution (required if event_bus is set).

    Returns:
        (stdout, stderr, returncode).

    Raises:
        asyncio.TimeoutError: If command exceeds timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    if event_bus is not None and proc.stdout is not None:
        # Stream stdout line-by-line while collecting full output
        from .events import TaskEvent

        stdout_lines: list[str] = []

        async def _stream_stdout():
            assert proc.stdout is not None
            async for line_bytes in proc.stdout:
                line = line_bytes.decode(errors="replace")
                stdout_lines.append(line)
                event_bus.publish(
                    TaskEvent(task_id=task_id, event_type="output_line", data=line.rstrip())
                )

        async def _collect_stderr():
            assert proc.stderr is not None
            return await proc.stderr.read()

        try:
            _, stderr_bytes = await asyncio.wait_for(
                asyncio.gather(_stream_stdout(), _collect_stderr()),
                timeout=timeout,
            )
            await proc.wait()
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            raise

        stdout = "".join(stdout_lines)
        stderr = stderr_bytes.decode(errors="replace") if isinstance(stderr_bytes, bytes) else ""
        return stdout, stderr, proc.returncode or 0

    # Non-streaming path (original behavior)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        raise
    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode or 0
