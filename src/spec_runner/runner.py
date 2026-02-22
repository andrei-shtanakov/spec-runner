"""Runner utilities for spec-runner.

Contains logging, error checking, callback, and CLI command building
functions used by the executor and hooks modules.
"""

import json
import shlex
from datetime import datetime
from pathlib import Path

from .config import ERROR_PATTERNS, PROGRESS_FILE


def log_progress(message: str, task_id: str | None = None):
    """Log progress message with timestamp to progress file."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{task_id}] " if task_id else ""
    line = f"[{timestamp}] {prefix}{message}\n"

    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line)

    # Also print to stdout
    print(line.rstrip())


def check_error_patterns(output: str) -> str | None:
    """Check output for API error patterns. Returns matched pattern or None."""
    output_lower = output.lower()
    for pattern in ERROR_PATTERNS:
        if pattern.lower() in output_lower:
            return pattern
    return None


def _send_callback(
    callback_url: str,
    task_id: str,
    status: str,
    duration: float | None = None,
    error: str | None = None,
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
    """
    if not callback_url:
        return

    import urllib.request

    payload: dict[str, str | float] = {
        "task_id": task_id,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }
    if duration is not None:
        payload["duration_seconds"] = duration
    if error:
        payload["error"] = error

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
        pass  # Best-effort, state file is the fallback


def build_cli_command(
    cmd: str,
    prompt: str,
    model: str = "",
    template: str = "",
    skip_permissions: bool = False,
    prompt_file: Path | None = None,
) -> list[str]:
    """Build CLI command from template or auto-detect based on command name.

    Args:
        cmd: CLI command name (e.g., "claude", "codex", "llama-cli")
        prompt: The prompt text
        model: Model name (optional)
        template: Command template with placeholders (optional)
        skip_permissions: Add --dangerously-skip-permissions for Claude (optional)
        prompt_file: Path to file containing prompt (optional, for large prompts)

    Returns:
        List of command arguments ready for subprocess.

    Template placeholders:
        {cmd} - CLI command
        {model} - Model name
        {prompt} - Prompt text (shell-escaped)
        {prompt_file} - Path to prompt file
    """
    # Use template if provided
    if template:
        # Replace placeholders
        prompt_escaped = shlex.quote(prompt)
        prompt_file_str = str(prompt_file) if prompt_file else ""

        formatted = template.format(
            cmd=cmd,
            model=model,
            prompt=prompt_escaped,
            prompt_file=prompt_file_str,
        )
        # Parse the formatted string into arguments
        return shlex.split(formatted)

    # Auto-detect based on command name
    cmd_lower = cmd.lower()

    if "llama-cli" in cmd_lower or "llama.cpp" in cmd_lower:
        # llama.cpp CLI
        result = [cmd, "-p", prompt, "--no-display-prompt"]
        if model:
            result.extend(["-m", model])
        return result

    elif "llama-server" in cmd_lower or "localhost:8080" in cmd_lower:
        # llama.cpp server via curl
        payload = json.dumps({"prompt": prompt})
        return ["curl", "-s", "http://localhost:8080/completion", "-d", payload]

    elif "ollama" in cmd_lower:
        # Ollama CLI
        return [cmd, "run", model or "llama3", prompt]

    elif "codex" in cmd_lower:
        # Codex CLI
        result = [cmd, "-p", prompt]
        if model:
            result.extend(["--model", model])
        return result

    else:
        # Claude CLI (default)
        result = [cmd, "-p", prompt]
        if skip_permissions:
            result.append("--dangerously-skip-permissions")
        if model:
            result.extend(["--model", model])
        return result
