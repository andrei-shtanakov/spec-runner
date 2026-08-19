"""Error classification for CLI agent stderr (v2.3.0).

Adds short, human-readable reasons to failures (previously surfaced as
"Unknown error"). Pattern library + last-N-lines stderr fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STDERR_TAIL_LINES = 5


@dataclass(frozen=True)
class ErrorPattern:
    """One classification pattern.

    `template` supports {0}, {1}, ... substitutions from regex groups;
    if the template uses no groups, regex match-only is enough.
    """

    kind: str
    regex: re.Pattern[str]
    template: str


PATTERNS: list[ErrorPattern] = [
    # codex / OpenAI quota — captures the "try again at <time>" hint
    ErrorPattern(
        kind="rate_limit",
        regex=re.compile(r"hit your usage limit.*?try again at ([\d:]+\s*[AP]M)", re.S),
        template="OpenAI usage limit — try again at {0}",
    ),
    # Provider exhaustion with a reset time — claude prints "You've hit your
    # session limit · resets 5:30pm", "Claude usage limit reached. Your limit
    # will reset at 3pm", "5-hour limit reached ∙ resets 3pm". None of those
    # contains "rate limit", so before #229 they all fell through to the
    # "unknown" tail and were reported as whatever the CLI happened to print.
    # The reset time is the one thing an operator needs, so it is captured.
    ErrorPattern(
        kind="rate_limit",
        regex=re.compile(
            r"(?:session|usage|\d+-hour)\s+limit[^\n]*?reset[s]?\s+(?:at\s+)?"
            r"([\d:]+\s*(?:[ap]\.?m\.?)?)",
            re.I,
        ),
        template="Provider limit reached — resets {0}",
    ),
    # …and without one. Separate rather than an optional group: a template that
    # says "resets " followed by nothing reads like a truncated message.
    ErrorPattern(
        kind="rate_limit",
        regex=re.compile(r"(?:session limit|usage limit reached|\d+-hour limit)", re.I),
        template="Provider limit reached",
    ),
    # generic rate-limit (claude, generic providers)
    ErrorPattern(
        kind="rate_limit",
        regex=re.compile(r"rate[_\s-]?limit", re.I),
        template="Rate limit hit",
    ),
    # auth failures
    ErrorPattern(
        kind="auth",
        regex=re.compile(r"unauthor|invalid api key|forbidden", re.I),
        template="Authentication failed",
    ),
    # network failures
    ErrorPattern(
        kind="network",
        regex=re.compile(
            r"ECONNREFUSED|timed out|name or service not known|dns",
            re.I,
        ),
        template="Network error",
    ),
    # generic CLI error line (last resort before unknown fallback)
    ErrorPattern(
        kind="cli_error",
        regex=re.compile(r"^error:\s*(.+)$", re.M),
        template="{0}",
    ),
]


#: Every value `attempts.error_kind` may carry, in one place (#301).
#:
#: The column had a **closed enum** in `schemas/executor-state.schema.json`
#: listing the five kinds this module's patterns produce — while the execution
#: path was already writing `blocked` and `api_error`, so a consumer validating
#: rows spec-runner itself writes would have rejected them. The enum drifted
#: because nothing compared it to the code; `TestTheDeclaredVocabularyIsTheOneOnDisk`
#: now does, against this set.
#:
#: The three refusal kinds are `RefusalKind`'s own values, not synonyms of
#: them: a gate that answered "no" (`policy`), an instrument that could not
#: answer (`instrument`), and no money left to find out (`budget`) are the
#: distinction the exit code already draws, and inventing a second vocabulary
#: for the same three states is how #230 happened.
ERROR_KINDS: frozenset[str] = frozenset(
    {
        # classifier (this module)
        "rate_limit",
        "auth",
        "network",
        "cli_error",
        "unknown",
        # written by the execution path
        "api_error",
        "blocked",
        "policy",
        "instrument",
        "budget",
        "hook_failure",
        "harness_guard",
        "timeout",
        "interrupted",
        "internal_error",
    }
)


def classify(stderr: str, returncode: int) -> tuple[str, str]:
    """Return (kind, human_message) for a failed CLI invocation.

    - Tries each pattern in PATTERNS order; first match wins.
    - Falls back to ("unknown", last N lines of stderr) when nothing matches.
    - When stderr is empty, falls back to ("unknown", "CLI exited with code N").
    """
    for p in PATTERNS:
        m = p.regex.search(stderr)
        if m:
            try:
                return p.kind, p.template.format(*m.groups())
            except IndexError:
                return p.kind, p.template
    tail = "\n".join(stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
    return "unknown", tail or f"CLI exited with code {returncode}"
