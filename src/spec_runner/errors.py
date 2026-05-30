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
        regex=re.compile(
            r"hit your usage limit.*?try again at ([\d:]+\s*[AP]M)", re.S
        ),
        template="OpenAI usage limit — try again at {0}",
    ),
]


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
