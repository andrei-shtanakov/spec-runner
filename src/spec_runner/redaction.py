"""Redactor for everything published outside the machine (NFR-05, design Q-08).

A published record leaves the project for a store the operator controls but
spec-runner does not, so secrets are removed *before* serialisation, not
hoped-away afterwards. Two sources feed one pass:

- the **environment denylist** -- values of variables whose names look like
  credentials, matched literally wherever they appear;
- **patterns** -- well-known token shapes, private-key blocks and
  ``key=value`` pairs whose key is in the vocabulary shared with
  ``obs._DEFAULT_REDACT_KEYS``.

The placeholder ``[REDACTED:kind:hash8]`` keeps the loss legible: ``kind``
says what was removed and ``hash8`` (first 8 hex of the SHA-256 of the secret)
lets two occurrences of the same secret be matched without revealing it.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping

from .obs import _DEFAULT_REDACT_KEYS

#: Same vocabulary of names as the log redactor -- one list, not two.
REDACT_KEY_NAMES = _DEFAULT_REDACT_KEYS

#: Environment names that mark a variable as a credential.
_SECRET_ENV_NAME = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIAL)", re.IGNORECASE
)

#: A value shorter than this is not treated as a secret: redacting "1" or
#: "true" would shred every record while protecting nothing.
MIN_SECRET_LENGTH = 8

_KEY_ALTERNATION = "|".join(sorted(re.escape(k) for k in REDACT_KEY_NAMES))

#: (kind, pattern); the whole match is replaced.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=\-]{16,}")),
    # userinfo of a URL (`https://user:password@host`), e.g. a git remote.
    ("url_credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
)

_KEY_VALUE = re.compile(
    r"(?P<key>\b[\w.\-]*(?:" + _KEY_ALTERNATION + r")[\w.\-]*)"
    r"(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"',;]{" + str(MIN_SECRET_LENGTH) + r",})",
    re.IGNORECASE,
)


def placeholder(kind: str, secret: str) -> str:
    """``[REDACTED:kind:hash8]`` for one removed secret."""
    digest = hashlib.sha256(secret.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"[REDACTED:{kind}:{digest}]"


def env_denylist(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Credential-looking environment values, keyed by variable name."""
    env = os.environ if environ is None else environ
    return {
        name: value
        for name, value in env.items()
        if _SECRET_ENV_NAME.search(name) and len(value) >= MIN_SECRET_LENGTH
    }


def _redact_pair(match: re.Match[str]) -> str:
    value = match.group("value")
    if value.startswith("[REDACTED:"):
        return match.group(0)
    return match.group("key") + match.group("sep") + placeholder("secret", value)


def _replacer(kind: str) -> Callable[[re.Match[str]], str]:
    """A substitution that stands a match for its placeholder."""
    return lambda match: placeholder(kind, match.group(0))


def redact(text: str, *, environ: Mapping[str, str] | None = None) -> str:
    """``text`` with every recognised secret replaced by its placeholder.

    Idempotent: a placeholder contains no secret, so a second pass is a no-op.
    """
    if not text:
        return text
    # Longest first, so a secret that contains another is removed whole.
    for value in sorted(set(env_denylist(environ).values()), key=len, reverse=True):
        text = text.replace(value, placeholder("env", value))
    for kind, pattern in _PATTERNS:
        text = pattern.sub(_replacer(kind), text)
    return _KEY_VALUE.sub(_redact_pair, text)


__all__ = ["REDACT_KEY_NAMES", "env_denylist", "placeholder", "redact"]
