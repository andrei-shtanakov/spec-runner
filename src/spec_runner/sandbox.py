"""Executor write boundary: an OS sandbox around every agent call (#600).

An agent runs with `skip_permissions` and may write anywhere by absolute path —
devtools#445 is a planted PASS in a sibling repo's run directory. The harness
guard sees only the harness surface inside `project_root`. This module wraps the
agent's argv so the OS refuses every write outside the task's tree, a per-call
temp dir, and the state its own CLI needs.

Design and measurements: docs/superpowers/specs/2026-09-29-executor-write-boundary-design.md.
Phase 1 is macOS (`sandbox-exec`, Seatbelt); on a platform without a backend,
`on` runs unwrapped with a warning and `required` refuses at startup.

The seam is `sandboxed()`: every site that launches an agent passes its
invocation through it and hands the returned argv/env to `subprocess`. Under
`off` it returns them unchanged, so nothing else moves.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import ExecutorConfig
from .logging import get_logger
from .runner import CliInvocation

logger = get_logger("sandbox")

SANDBOX_MODES = ("off", "on", "required")

#: Writable state each CLI needs to answer at all, measured per preset on
#: macOS 26.7 (design §3). Keyed by the executable's basename. A CLI with an
#: empty tuple was measured and needs nothing beyond the base set.
STATE_DIRS: dict[str, tuple[str, ...]] = {
    "claude": (),
    "codex": ("~/.codex",),
    "opencode": ("~/.local/share/opencode",),
    "pi": ("~/.pi",),
    "ollama": (),
    "qwen": (),
}

#: Presets not measured (design §3): run with the base set and a warning.
UNMEASURED = frozenset({"copilot", "llama-cli"})

#: codex runs its own shell commands in Seatbelt, which cannot nest inside ours
#: (`sandbox_apply: Operation not permitted`, measured). Its documented mode for
#: "environments that are externally sandboxed" is this flag.
_CODEX_BYPASS = "--dangerously-bypass-approvals-and-sandbox"

_warned: set[str] = set()


class SandboxUnavailable(RuntimeError):
    """`executor_sandbox: required` on a platform with no backend."""


@dataclass
class SandboxedCall:
    """What a launch site hands to `subprocess`, and what to clean up."""

    argv: list[str]
    env: dict[str, str] | None
    tmpdir: Path | None = field(default=None)

    def cleanup(self) -> None:
        """Remove the per-call temp dir; a no-op when there is none."""
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)


def backend() -> str | None:
    """The sandbox backend this platform offers, or None."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    return None


def require_backend(config: ExecutorConfig) -> None:
    """Refuse at startup, before any paid call, when `required` cannot hold.

    Raises:
        SandboxUnavailable: `executor_sandbox: required` and no backend.
    """
    if getattr(config, "executor_sandbox", "off") == "required" and backend() is None:
        raise SandboxUnavailable(
            f"executor_sandbox is 'required' but no sandbox backend exists on {sys.platform} "
            "(phase 1 supports macOS sandbox-exec only)"
        )


def sandboxed(
    config: ExecutorConfig,
    invocation: CliInvocation,
    env: dict[str, str] | None = None,
) -> SandboxedCall:
    """The invocation wrapped in the platform sandbox, or unchanged under `off`.

    Args:
        config: The run's config (mode, project root, allowlist, commands).
        invocation: The agent argv as `build_cli_invocation` built it.
        env: The environment the site would pass (`None` = inherit).

    Raises:
        SandboxUnavailable: `required` and no backend (startup should have
            refused already; this is the last line).
    """
    # getattr: plan paths are driven with duck-typed configs in tests and by
    # callers that predate the key; absent means off.
    mode = getattr(config, "executor_sandbox", "off")
    if mode == "off":
        return SandboxedCall(argv=list(invocation.argv), env=env)
    if backend() is None:
        if mode == "required":
            require_backend(config)
        _warn_once("no-backend", f"executor_sandbox: on, but no backend on {sys.platform}")
        return SandboxedCall(argv=list(invocation.argv), env=env)

    name = PurePosixPath(invocation.argv[0]).name if invocation.argv else ""
    if name in UNMEASURED:
        _warn_once(name, f"{name}: writable state not measured; running with the base set")
    tmpdir = Path(tempfile.mkdtemp(prefix="spec-runner-agent-")).resolve()
    call_env = dict(env if env is not None else os.environ)
    call_env["TMPDIR"] = str(tmpdir)
    # claude's Bash tool ignores TMPDIR and needs this, or runs no command.
    call_env["CLAUDE_CODE_TMPDIR"] = str(tmpdir)
    writable = writable_paths(config, name, tmpdir)
    profile = seatbelt_profile(writable)
    logger.info(
        "Agent call sandboxed", backend="seatbelt", cli=name, writable=[str(p) for p in writable]
    )
    argv = ["sandbox-exec", "-p", profile, *_adapt_argv(name, list(invocation.argv))]
    return SandboxedCall(argv=argv, env=call_env, tmpdir=tmpdir)


def writable_paths(config: ExecutorConfig, cli_name: str, tmpdir: Path) -> list[Path]:
    """Every path the agent may write under the sandbox (design §4.2)."""
    paths = [config.project_root.resolve(), tmpdir]
    common = _git_common_dir(config.project_root)
    if common is not None and not common.is_relative_to(config.project_root.resolve()):
        paths.append(common)
    paths += [_expand(p) for p in STATE_DIRS.get(cli_name, ())]
    uv_cache = _uv_cache_dir(config)
    if uv_cache is not None:
        paths.append(uv_cache)
    paths += [_expand(p) for p in config.sandbox_allow]
    return list(dict.fromkeys(paths))


def seatbelt_profile(writable: list[Path]) -> str:
    """A Seatbelt profile: everything allowed except writes outside `writable`."""
    allowed = " ".join(f'(subpath "{_sbpl_escape(str(p))}")' for p in writable)
    return (
        "(version 1)\n(allow default)\n(deny file-write*)\n"
        f'(allow file-write* {allowed} (regex #"^/dev/"))\n'
    )


def _adapt_argv(cli_name: str, argv: list[str]) -> list[str]:
    if cli_name == "codex" and len(argv) > 1 and argv[1] == "exec" and _CODEX_BYPASS not in argv:
        return [argv[0], "exec", _CODEX_BYPASS, *argv[2:]]
    return argv


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _sbpl_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _git_common_dir(root: Path) -> Path | None:
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0 or not found.stdout.strip():
        return None
    return Path(found.stdout.strip()).resolve()


def _uv_cache_dir(config: ExecutorConfig) -> Path | None:
    """uv's cache when the project's own commands run uv (measured need)."""
    commands = " ".join(c for c in (config.test_command, config.sync_command or "") if c)
    if "uv" not in commands.split():
        return None
    try:
        found = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode != 0 or not found.stdout.strip():
        return None
    return Path(found.stdout.strip()).resolve()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message)
    print(f"⚠️  {message}", file=sys.stderr)
