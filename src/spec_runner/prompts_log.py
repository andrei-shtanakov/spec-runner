"""One writer for the prompts of the paid calls (#282, and the review half after it).

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

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig

logger = get_logger("prompts_log")

#: **Characters** kept from each end — `bound` slices a `str`, so the unit is
#: codepoints, not bytes (Copilot, PR #287). 65_536 either side is far above
#: any prompt this tool builds today (the largest observed is a few thousand
#: characters with requirements and design context inlined), so the cap bounds
#: pathology rather than truncating routinely.
KEEP_EACH_END = 65_536

#: `review:security` is a provenance, not a filename. A role name comes from
#: config, so the rule is deliberately a whitelist and excludes `.` as well as
#: the separators: no traversal, and no file that starts with a dot and hides
#: from the operator looking for it.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def provenance_slug(provenance: str) -> str:
    """`review:security` → `review-security`, and nothing that escapes a path.

    Applied to the **task id** as well as the provenance: both reach a filename,
    and neither may name a file outside the log directory or one that hides
    itself from `ls`.
    """
    return _UNSAFE.sub("-", provenance).strip("-") or "prompt"


def bound(text: str) -> str:
    """The text, or its head and tail with a marker that makes the loss legible.

    A truncation an operator cannot reason about is worse than a long file. The
    marker therefore carries three things (owner's review of PR #287):

    - that it happened, in words nothing else in the file says;
    - the **original size**, so "is this most of it or a tenth of it" has an
      answer;
    - the **SHA-256 of the whole text**, so a truncated log can still be
      matched against a full prompt reproduced later — the question an
      operator actually asks is "was the agent sent *this*", and a hash answers
      it where a middle-less copy cannot.
    """
    if len(text) <= KEEP_EACH_END * 2:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    dropped = len(text) - KEEP_EACH_END * 2
    return (
        f"{text[:KEEP_EACH_END]}\n\n"
        f"=== TRUNCATED: {dropped} of {len(text)} characters omitted from the middle; "
        f"sha256 of the full text {digest} ===\n\n"
        f"{text[-KEEP_EACH_END:]}"
    )


def append_output(
    path: Path | None,
    output: str,
    stderr: str = "",
    *,
    returncode: int | None = None,
    cost_usd: float | None = None,
    note: str | None = None,
) -> None:
    """Append what the call answered to the file its prompt went to.

    The review path has always kept the two together, and they belong
    together: a prompt without its answer says what was asked and not what
    came back. Same bound, same silence-on-failure rule.

    `note` states why a call that *ran* has nothing to show — a timeout is the
    case that exists. Without it the artefact of a timed-out call is an empty
    output block, which is true and unreadable: the call ran, it was billed for
    as long as it ran, and the file should say that rather than leave it to be
    inferred from a synthetic return code.
    """
    if path is None:
        return
    try:
        with path.open("a") as handle:
            if note:
                handle.write(f"\n=== NO RESULT: {bound(note)} ===\n")
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


def append_not_started(path: Path | None, reason: str) -> None:
    """Close an artefact whose call never launched, saying so (#296).

    The prompt is written before the call, so a guard that refuses afterwards
    used to leave a file holding a complete prompt and nothing else — the same
    bytes a call that started and died leaves behind. An operator listing the
    log directory saw a review prompt for a review that was never bought, and
    the artefact could not tell them which of the two had happened.

    Deliberately **not** an `=== OUTPUT ===` block with an empty body: the
    per-role path's own comment had it right — "a log claiming an answer would
    say money was spent that was not". This claims the opposite, explicitly,
    and keeps the prompt, which is what an operator reads when deciding whether
    to raise the ceiling that refused it.

    With this, an artefact that ends without a terminal section means exactly
    one thing: the call launched and the runner died before it could record the
    result.
    """
    if path is None:
        return
    try:
        with path.open("a") as handle:
            handle.write(f"\n=== NOT STARTED: {bound(reason)} ===\n")
    except OSError as exc:
        logger.warning("Could not log the refusal", path=str(path), error=str(exc))


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
        # The **task id** goes through the same whitelist as the role (owner's
        # review of PR #287). Today's parser constrains ids to
        # `[A-Z][A-Z0-9]*-\d+`, so nothing hostile arrives through tasks.md —
        # but a writer that composes a path must not depend on a caller's
        # regex. Measured before fixing: `log_prompt(cfg, "../../escaped", …)`
        # wrote outside `logs_dir`.
        path = config.logs_dir / f"{provenance_slug(task_id)}-{slug}-{stamp}.log"
        # The header comes from the **slug**, not the raw provenance (Copilot,
        # PR #287): a role name is config, and a newline in it would rewrite
        # the file's structure rather than appear in it.
        path.write_text(f"=== {slug.upper()} PROMPT ===\n{bound(prompt)}\n")
        return path
    except OSError as exc:
        logger.warning(
            "Could not log the prompt", task_id=task_id, provenance=provenance, error=str(exc)
        )
        return None


__all__ = [
    "KEEP_EACH_END",
    "append_not_started",
    "append_output",
    "bound",
    "log_prompt",
    "provenance_slug",
]
