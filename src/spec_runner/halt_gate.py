"""DarkFactory halt check before NEW work (opt-in, halt D2b).

spec-runner is a product used outside DarkFactory, so the check runs only
when ``DARKFACTORY_HALT_CHECK=1`` is set — DarkFactory launchers and agent
environments set it. The rule is github-checker's
``contracts/halt-admission/v1`` (vendored with a pin; ``decide`` and
``is_github_origin`` are tested against its vectors):

- a checkout whose origin is not github.com (or has no origin) → admit;
- ruleset ``darkfactory-halt`` absent or ``disabled`` → admit;
- ``active``, a duplicate name, another enforcement, anything unreadable →
  refuse.

Reads with the ambient ``gh`` profile. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ENV_FLAG = "DARKFACTORY_HALT_CHECK"
HALT_RULESET = "darkfactory-halt"
#: Exit codes, as devtools merge-pr.sh: 6 halted, 2 halt unread (retry fits).
EXIT_HALTED = 6
EXIT_UNREAD = 2

Decision = tuple[bool, str, str]
_GITHUB_HOSTS = ("github.com", "www.github.com")


def enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the opt-in flag is set."""
    return (env if env is not None else os.environ).get(ENV_FLAG) == "1"


def decide(listing: list[dict[str, Any]] | None, detail: dict[str, Any] | None) -> Decision:
    """(admit, code, reason) — the halt-admission/v1 table."""
    if listing is None:
        return False, "refuse_unknown", "rulesets could not be listed"
    named = [r for r in listing if r.get("name") == HALT_RULESET]
    if not named:
        return True, "admit_missing", "no darkfactory-halt ruleset (not armed)"
    if len(named) > 1:
        return False, "refuse_duplicate", f"{len(named)} rulesets named {HALT_RULESET}"
    if detail is None:
        return False, "refuse_unknown", "the halt ruleset could not be read"
    enforcement = detail.get("enforcement")
    if enforcement == "disabled":
        return True, "admit_off", "halt is off"
    if enforcement == "active":
        return False, "refuse_on", "the DarkFactory halt is ON for this repository"
    return False, "refuse_enforcement", f"halt enforcement {enforcement!r}"


def is_github_origin(url: str) -> bool:
    """Whether an origin URL's host is exactly github.com (admit_not_github)."""
    url = url.strip()
    if url.startswith("git@"):
        host = url[4:].split(":", 1)[0]
    elif "://" in url:
        rest = url.split("://", 1)[1]
        host = rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    else:
        return False
    return host.lower() in _GITHUB_HOSTS


def _slug(url: str) -> str | None:
    """owner/name of a github.com origin, or None when it cannot be parsed."""
    url = url.strip()  # `git remote get-url` ends with a newline
    path = url.split(":", 1)[1] if url.startswith("git@") else url
    if "://" in path:
        path = path.split("://", 1)[1].split("/", 1)[-1]
    parts = [p for p in path.removesuffix(".git").split("/") if p]
    return f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else None


def _run(*argv: str, cwd: Path | None = None) -> str | None:
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def check(project_root: Path) -> Decision:
    """Read the halt of the repository at *project_root* and decide."""
    origin = _run("git", "-C", str(project_root), "remote", "get-url", "origin")
    if origin is None or not is_github_origin(origin):
        return True, "admit_not_github", "the checkout has no github.com origin"
    slug = _slug(origin)
    if slug is None:
        return False, "refuse_unknown", f"cannot parse owner/name from {origin.strip()!r}"
    out = _run(
        "gh",
        "api",
        "--paginate",
        f"repos/{slug}/rulesets?includes_parents=false",
        "--jq",
        ".[] | [.id, .name] | @json",
    )
    listing: list[dict[str, Any]] | None = None
    if out is not None:
        try:
            pairs = [json.loads(line) for line in out.splitlines() if line.strip()]
            listing = [{"id": rid, "name": name} for rid, name in pairs]
        except (json.JSONDecodeError, TypeError, ValueError):
            listing = None
    detail = None
    if listing is not None:
        named = [r for r in listing if r.get("name") == HALT_RULESET]
        if len(named) == 1:
            body = _run("gh", "api", f"repos/{slug}/rulesets/{named[0]['id']}")
            try:
                parsed = json.loads(body) if body is not None else None
            except json.JSONDecodeError:
                parsed = None
            detail = parsed if isinstance(parsed, dict) else None
    return decide(listing, detail)
