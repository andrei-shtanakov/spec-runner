"""One writer for every paid call's prompt (#282, and the review half after it).

Three stages of a task cost money — RED authoring, the implementation pass, and
review — and until #282 only the implementation pass left a record of what it
had asked. The RED prompt decides which file is frozen for the rest of the
task; the review prompt decides what a reviewer was looking for. Both were
gone the moment the call returned.

The asymmetry mattered in practice: #198 (a pytest-shaped selector demanded of
an ExUnit project) and #220 (a Python linter applied to an Elixir file) were
both wrong *instructions*, and both were found by reading source rather than by
reading a run. A published artifact could not be checked against its own
prompt at all.

What is written, and what deliberately is not:

- **the prompt as sent**, after template rendering and after every block that
  is appended to it (the frozen-files block, the retry context). A prompt
  logged before rendering answers a different question than the one an
  operator asks;
- **nothing else**. No argv, no environment, no model or command name — those
  are the runner's business and the place secrets would live. The prompt is
  text this tool composed from the project's own files.

Bounded, because a review of five roles writes five of these per attempt and a
`watch` loop runs unattended. The cap keeps the head *and* the tail: the head
carries the task, the tail carries the appended blocks — the frozen-files list
is the last thing added, and truncating to the head would drop exactly the part
a claims refusal sends you to read.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig

logger = get_logger("prompts_log")

#: Bytes kept from each end. 64 KiB either side is far above any prompt this
#: tool builds today (the largest observed is a few KiB with requirements and
#: design context inlined), so the cap is a bound on pathology rather than a
#: routine truncation.
KEEP_EACH_END = 64 * 1024

#: `review:security` is a provenance, not a filename. A role name comes from
#: config, so the rule is deliberately a whitelist and excludes `.` as well as
#: the separators: no traversal, and no file that starts with a dot and hides
#: from the operator looking for it.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def provenance_slug(provenance: str) -> str:
    """`review:security` → `review-security`, and nothing that escapes a path."""
    return _UNSAFE.sub("-", provenance).strip("-") or "prompt"


def bound(text: str) -> str:
    """The prompt, or its head and tail with a marker naming what was dropped."""
    if len(text) <= KEEP_EACH_END * 2:
        return text
    dropped = len(text) - KEEP_EACH_END * 2
    return (
        f"{text[:KEEP_EACH_END]}\n\n"
        f"=== {dropped} characters omitted from the middle ===\n\n"
        f"{text[-KEEP_EACH_END:]}"
    )


def append_output(
    path: Path | None,
    output: str,
    stderr: str = "",
    *,
    returncode: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Append what the call answered to the file its prompt went to.

    The review path has always kept the two together, and they belong
    together: a prompt without its answer says what was asked and not what
    came back. Same bound, same silence-on-failure rule.
    """
    if path is None:
        return
    try:
        with path.open("a") as handle:
            handle.write(f"\n=== OUTPUT ===\n{bound(output)}\n")
            if stderr.strip():
                handle.write(f"\n=== STDERR ===\n{bound(stderr)}\n")
            if returncode is not None:
                handle.write(f"\n=== RETURN CODE: {returncode} ===\n")
            if cost_usd is not None or returncode is not None:
                # `unknown`, never 0.0: an unreported cost is the thing the
                # budget guard refuses to spend against, and writing a zero
                # here would put the opposite claim in the record (#213).
                shown = "unknown" if cost_usd is None else f"{cost_usd}"
                handle.write(f"=== COST: {shown} ===\n")
    except OSError as exc:
        logger.warning("Could not log the call's output", path=str(path), error=str(exc))


def log_prompt(config: ExecutorConfig, task_id: str, provenance: str, prompt: str) -> Path | None:
    """Write one paid call's prompt to the log directory.

    Returns the path written, or None — so a caller that also has an answer to
    record appends to the same file instead of deciding a second filename.

    Never fatal. A prompt that could not be written is worth a warning; it is
    not worth failing a task over, and least of all *before* the call it
    describes — the same rule the harness applies to every other piece of
    bookkeeping that runs beside work rather than deciding it.
    """
    try:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = provenance_slug(provenance)
        path = config.logs_dir / f"{task_id}-{slug}-{stamp}.log"
        path.write_text(f"=== {provenance.upper()} PROMPT ===\n{bound(prompt)}\n")
        return path
    except OSError as exc:
        logger.warning(
            "Could not log the prompt", task_id=task_id, provenance=provenance, error=str(exc)
        )
        return None


__all__ = ["KEEP_EACH_END", "append_output", "bound", "log_prompt", "provenance_slug"]
