"""MCP launch scope: holder, contradiction predicate, and the parent side of
the startup handshake (spec-runner#485).

Deliberately does not import the ``mcp`` SDK: ``mcp`` stays an optional
dependency (see `mcp_server.py`'s module docstring), and every symbol here
must be testable without it installed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .cli import _build_parser
from .config import (
    ConfigError,
    ExecutorConfig,
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)

if TYPE_CHECKING:
    import subprocess


@dataclass(frozen=True)
class LaunchScope:
    """The config + resolved identity a server (or `_build_config("")`
    fallback, #485 §1.2) is serving."""

    config: ExecutorConfig
    project_root: Path
    namespace: dict[str, str]
    config_path: Path

    @classmethod
    def of(cls, config: ExecutorConfig) -> LaunchScope:
        if config.change_id:
            namespace: dict[str, str] = {"kind": "change", "id": config.change_id}
        elif config.spec_prefix:
            namespace = {"kind": "prefix", "prefix": config.spec_prefix}
        else:
            namespace = {"kind": "flat"}
        return cls(
            config=config,
            project_root=config.project_root,
            namespace=namespace,
            config_path=_resolve_config_path(config.project_root),
        )


class ScopeContradiction(Exception):
    """A tool-level `spec_prefix` contradicts the launch namespace (FR-02)."""

    def __init__(self, namespace: dict[str, str], requested_spec_prefix: str) -> None:
        self.namespace = namespace
        self.requested_spec_prefix = requested_spec_prefix
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"tool-level spec_prefix {self.requested_spec_prefix!r} contradicts "
            f"the launch namespace {self.namespace!r}"
        )

    def to_dict(self) -> dict:
        return {
            "status": "error",
            "error": str(self),
            "launch_namespace": self.namespace,
            "requested_spec_prefix": self.requested_spec_prefix,
        }


def _rebuild_for_namespace(scope: LaunchScope, *, spec_prefix: str) -> ExecutorConfig:
    """Rebuild the launch config under a different `spec_prefix` -- the
    flat-launch "refinement" branch (§1.3, Q-01 working assumption).

    Goes through the same serializer/parser pair as the child (design §1.3,
    DT-02): the launch config's representable overrides (`--no-tests`,
    `--budget`, `--strict`, ...) are serialized by `config_flags`, the
    namespace flag is appended, and `_build_parser()` + `build_config`
    rebuild the config from that argv and the YAML the launch scope read
    (by `project_root`, not CWD). A hand-built `argparse.Namespace` with
    every flag at "not set" silently dropped those overrides (review of the
    DT-02 integration PR) -- the refined config was a degraded copy, and
    the reproducibility check then validated that copy against itself.
    """
    yaml_config = load_config_from_yaml(scope.config_path)
    argv = ["run", "--task", "TASK-000", *config_flags(scope.config), "--spec-prefix", spec_prefix]
    args = _build_parser().parse_args(argv)
    return build_config(yaml_config, args)


def resolve_tool_config(scope: LaunchScope, spec_prefix: str = "") -> ExecutorConfig:
    """The one predicate every tool goes through (§1.3, BEH-05/06/07).

    - empty prefix -> the launch scope's config, always.
    - `change` scope + any prefix -> contradiction.
    - `prefix` scope + matching prefix -> the launch scope's config;
      mismatching prefix -> contradiction.
    - `flat` scope + non-empty prefix -> refinement: rebuild for that prefix
      under the same `project_root` (working assumption Q-01).
    """
    if not spec_prefix:
        return scope.config

    kind = scope.namespace["kind"]
    if kind == "change":
        raise ScopeContradiction(scope.namespace, spec_prefix)
    if kind == "prefix":
        if spec_prefix == scope.namespace["prefix"]:
            return scope.config
        raise ScopeContradiction(scope.namespace, spec_prefix)

    return _rebuild_for_namespace(scope, spec_prefix=spec_prefix)


# === Startup handshake (§3.2) ===


@dataclass(frozen=True)
class Ready:
    pid: int


@dataclass(frozen=True)
class Exited:
    returncode: int
    log_tail: str


@dataclass(frozen=True)
class TimedOut:
    pid: int
    terminated: bool


WaitOutcome = Ready | Exited | TimedOut


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_ready_pid(ready_file: Path) -> int | None:
    try:
        content = ready_file.read_text()
    except OSError:
        return None
    for line in content.splitlines():
        if line.startswith("PID:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def clear_stale_ready_file(ready_file: Path) -> None:
    """Remove a ready file left by a dead process, before `Popen` (§3.2)."""
    pid = _read_ready_pid(ready_file)
    if pid is not None and not _pid_alive(pid):
        with contextlib.suppress(OSError):
            ready_file.unlink()


def _read_log_tail(log_file: Path | None, lines: int = 20) -> str:
    if log_file is None or not log_file.exists():
        return ""
    try:
        text = log_file.read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _terminate(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=grace_seconds)
    except Exception:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace_seconds)


def wait_for_ready(
    proc: subprocess.Popen,
    ready_file: Path,
    timeout: float,
    *,
    log_file: Path | None = None,
    poll_interval: float = 0.01,
) -> WaitOutcome:
    """Poll for the child publishing `ready_file`, or dying, or timing out.

    `ready_file` is published right after `clear_stop_file` and removed by
    `cmd_run`'s own `finally` as the child's very last step -- for a fast
    task (no hooks, an instant fake CLI) that whole window measured as
    narrow as ~30ms (#485, BEH-15 live measurement). A 0.05s poll interval
    straddles that window about as often as it catches it (measured ~50%
    `Exited` on a real child); 0.01s catches it reliably (measured 0/50
    misses) at a negligible added cost (a few thousand extra stats over the
    full `mcp_ready_timeout_seconds` budget).

    (а) `ready_file` exists with `PID == proc.pid` -> unlink -> `Ready`.
    (б) `proc.poll()` is not `None` -> `Exited(returncode, log_tail)`.
    (в) elapsed > timeout -> terminate (SIGTERM -> SIGKILL) -> `TimedOut`.

    In (б)/(в), a `ready_file` written under our own pid is removed too
    (§3.2) -- the caller must never see a `started`-shaped ready file after
    an error response.
    """
    deadline = time.monotonic() + timeout
    while True:
        if ready_file.exists():
            pid = _read_ready_pid(ready_file)
            if pid == proc.pid:
                with contextlib.suppress(OSError):
                    ready_file.unlink()
                return Ready(pid=proc.pid)

        returncode = proc.poll()
        if returncode is not None:
            _cleanup_own_ready_file(ready_file, proc.pid)
            return Exited(returncode=returncode, log_tail=_read_log_tail(log_file))

        if time.monotonic() >= deadline:
            _terminate(proc)
            _cleanup_own_ready_file(ready_file, proc.pid)
            return TimedOut(pid=proc.pid, terminated=True)

        time.sleep(poll_interval)


def _cleanup_own_ready_file(ready_file: Path, pid: int) -> None:
    if _read_ready_pid(ready_file) == pid:
        with contextlib.suppress(OSError):
            ready_file.unlink()


# === Serializer & reproducibility check (§2, DT-02) ===


@dataclass(frozen=True)
class RepresentableField:
    """One row of the `ExecutorConfig` -> argv representability table
    (§2.1). `common_dests` names the `_COMMON_DEFAULTS` / `common`-parser
    dest(s) this field is carried through -- two for the single namespace
    row (`change_id` | `spec_prefix`), one for everything else.
    """

    config_field: str
    common_dests: tuple[str, ...]


#: Declarative table: config field -> `common` flag(s) -- the parity
#: contract of the serializer (BEH-10). `config_flags` below emits one
#: rule per row (value / negated store-true / namespace), and
#: `tests/test_mcp_serializer.py` holds the two in step with
#: `_COMMON_DEFAULTS`; the table is not itself walked at runtime. Order
#: matches design §2.1.
REPRESENTABLE: tuple[RepresentableField, ...] = (
    RepresentableField("project_root", ("project_root",)),
    RepresentableField("change_id", ("change",)),
    RepresentableField("spec_prefix", ("spec_prefix",)),
    RepresentableField("max_retries", ("max_retries",)),
    RepresentableField("task_timeout_minutes", ("timeout",)),
    RepresentableField("run_tests_on_done", ("no_tests",)),
    RepresentableField("create_git_branch", ("no_branch",)),
    RepresentableField("auto_commit", ("no_commit",)),
    RepresentableField("run_review", ("no_review",)),
    RepresentableField("integration_pr", ("integration_pr",)),
    RepresentableField("hitl_review", ("hitl_review",)),
    RepresentableField("budget_usd", ("budget",)),
    RepresentableField("task_budget_usd", ("task_budget",)),
    RepresentableField("callback_url", ("callback_url",)),
    RepresentableField("log_level", ("log_level",)),
)

#: `_COMMON_DEFAULTS` keys the serializer deliberately does not forward, with
#: why -- no silent gaps (BEH-10, second `And`).
NOT_FORWARDED: dict[str, str] = {
    "log_json": (
        "not an ExecutorConfig field -- a parameter of the parent process's "
        "own log renderer (cli.py), invisible to the child's effective config"
    ),
}

#: Run-only representable field: `spec_governance` lives on the `run`
#: subparser's own `--strict`/`--no-strict`, not on `common` (§2.1).
REPRESENTABLE_RUN: dict[str, tuple[str, ...]] = {
    "spec_governance": ("strict", "no_strict"),
}

#: Fields the child never consumes, excluded from the reproducibility diff
#: (§2.2). `config_found` is stamped by the CLI's own loader after
#: `build_config` returns, not by `build_config` itself; `mcp_ready_timeout_seconds`
#: is parent-only -- the child never waits on its own ready file.
PARENT_ONLY_FIELDS: frozenset[str] = frozenset({"config_found", "mcp_ready_timeout_seconds"})

_REPRESENTABLE_FIELD_NAMES: frozenset[str] = frozenset(
    row.config_field for row in REPRESENTABLE
) | frozenset(REPRESENTABLE_RUN)


def child_entry() -> list[str]:
    """The interpreter-bound argv prefix a child is launched with (§2.3,
    BEH-11): `[sys.executable, "-m", "spec_runner"]`, resolving inside the
    parent's own venv via `src/spec_runner/__main__.py` -- never a
    `spec-runner` console script, which could resolve to a different venv or
    nothing at all on a trimmed `PATH`. A seam: E2E BEH-09 replaces it with a
    test entry point that writes its resolved `ExecutorConfig` to a file
    instead of running the executor.
    """
    return [sys.executable, "-m", "spec_runner"]


def child_argv(config: ExecutorConfig, task_id: str) -> list[str]:
    """Serialize `config` into the argv a child `run` invocation carries to
    reproduce it (§2.1): `run --task <id>` + `config_flags(config)`.

    This list is BOTH what `simulate_child_config` validates and what
    `mcp_server.spec_runner_run_task` hands to `Popen` -- one list, never
    two (review of the DT-02 integration PR: a check that certifies an argv
    the child never receives is fail-open for every representable field).
    """
    return ["run", "--task", task_id, *config_flags(config)]


def config_flags(config: ExecutorConfig) -> list[str]:
    """The representable part of `config` as `run` flags (§2.1): one
    emission rule per `REPRESENTABLE` row, then the run-only
    `--strict`/`--no-strict` (`REPRESENTABLE_RUN`).
    """
    argv = ["--project-root", str(config.project_root)]
    if config.change_id:
        argv += ["--change", config.change_id]
    elif config.spec_prefix:
        argv += ["--spec-prefix", config.spec_prefix]
    argv += ["--max-retries", str(config.max_retries)]
    argv += ["--timeout", str(config.task_timeout_minutes)]
    if not config.run_tests_on_done:
        argv.append("--no-tests")
    if not config.create_git_branch:
        argv.append("--no-branch")
    if not config.auto_commit:
        argv.append("--no-commit")
    if not config.run_review:
        argv.append("--no-review")
    if config.integration_pr:
        argv.append("--integration-pr")
    if config.hitl_review:
        argv.append("--hitl-review")
    if config.budget_usd is not None:
        argv += ["--budget", str(config.budget_usd)]
    if config.task_budget_usd is not None:
        argv += ["--task-budget", str(config.task_budget_usd)]
    if config.callback_url:
        argv += ["--callback-url", config.callback_url]
    argv += ["--log-level", config.log_level]
    argv.append("--strict" if config.spec_governance == "strict" else "--no-strict")
    return argv


def _jsonable(value: object) -> object:
    """Render a config field value into something `json.dumps` accepts."""
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _normalized(value: object) -> object:
    """`Path` fields compare resolved (§2.2) -- everything else as-is."""
    return value.resolve() if isinstance(value, Path) else value


@dataclass(frozen=True)
class FieldDiff:
    """One field the child would rebuild differently from the parent (§2.2)."""

    field: str
    parent_value: object
    child_value: object
    reason: str


class Irreproducible(Exception):
    """`scope.config` cannot be reproduced by a child rebuilt from `argv` +
    the YAML the parent read (BEH-13) -- the one check that also clears
    BEH-14 when it finds nothing.

    The rendered text and dict name the field and the reason only, never
    the values: `ExecutorConfig` carries secrets (`telegram_bot_token`,
    `webhook_headers`, ...) and an MCP error response is not the place for
    them (Copilot review of the DT-02 integration PR). `diffs` keeps the
    values in-process for callers that need them.
    """

    def __init__(self, diffs: list[FieldDiff]) -> None:
        self.diffs = diffs
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [f"{d.field}: {d.reason}" for d in self.diffs]
        return "config is not reproducible by the child: " + "; ".join(parts)

    def to_dict(self) -> dict:
        return {
            "status": "error",
            "error": str(self),
            "fields": [{"field": d.field, "reason": d.reason} for d in self.diffs],
        }


def simulate_child_config(scope: LaunchScope, argv: list[str]) -> ExecutorConfig | Irreproducible:
    """Build the config a child would get from `argv` + the YAML the parent
    read, and compare it with `scope.config` field by field (§2.2) -- the one
    check that answers both BEH-13 (refuse) and BEH-14 (proceed): a non-empty
    diff is `Irreproducible`, naming every field, both values, and why.

    Runs entirely in-process: `detect_subdir=False` skips `build_config`'s
    own `git rev-parse` subprocess (config.py's `_detect_subdir_repo`) --
    this check must answer *before* the child is spawned without spawning
    anything itself (BEH-13/14: zero or exactly one `Popen` call, and that
    call is the child's, never a side effect of checking). Safe to skip: the
    only values that detection can produce (`create_git_branch`/`auto_commit`
    forced to `False`) are always carried to the simulated child explicitly
    via `--no-branch`/`--no-commit` in `argv` already, whatever produced them
    on the parent's side.

    Never lets a broken input crash the check itself: an unreadable YAML
    (`ConfigError`), an argv `_build_parser()` itself rejects (`SystemExit`
    -- e.g. a `log_level` value outside the `run` subparser's `choices`,
    which `ExecutorConfig.log_level` does not otherwise restrict), or a
    config `build_config` refuses to build (`ConfigError`) are each reported
    as `Irreproducible` like any other mismatch, never raised -- the whole
    point of this check is to answer before `Popen`, not to replace one
    crash with another.
    """
    yaml_missing = not scope.config_path.exists()
    try:
        yaml_config = load_config_from_yaml(scope.config_path)
    except ConfigError as exc:
        return Irreproducible(
            [
                FieldDiff(
                    "<yaml>",
                    None,
                    None,
                    f"the parent's own config file at {scope.config_path} could not "
                    f"be re-read: {exc}",
                )
            ]
        )
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit:
        return Irreproducible(
            [
                FieldDiff(
                    "<argv>",
                    None,
                    None,
                    "the serialized argv was rejected by the child's own CLI parser "
                    f"(argv={argv!r}) -- a field's current value cannot be represented "
                    "as a valid CLI flag for the child to parse",
                )
            ]
        )
    try:
        child_config = build_config(yaml_config, args, detect_subdir=False)
    except ConfigError as exc:
        return Irreproducible(
            [
                FieldDiff(
                    "<config>",
                    None,
                    None,
                    f"the child could not build a config from argv + YAML: {exc}",
                )
            ]
        )

    diffs: list[FieldDiff] = []
    for f in dataclasses.fields(ExecutorConfig):
        if f.name in PARENT_ONLY_FIELDS:
            continue
        parent_value = getattr(scope.config, f.name)
        child_value = getattr(child_config, f.name)
        if _normalized(parent_value) == _normalized(child_value):
            continue
        # The class of the field decides the reason (BEH-13: "no CLI flag"
        # vs "flag cannot express the value"); a missing YAML is a remark on
        # top, never the headline -- `yaml_missing` cannot tell "deleted
        # after the parent read it" from "never existed" (review of the
        # DT-02 integration PR), so it is worded for both.
        if f.name in _REPRESENTABLE_FIELD_NAMES:
            reason = (
                "this field has a CLI flag, but the flag cannot express the "
                "parent's current value (e.g. a one-directional --no-* flag)"
            )
        else:
            reason = (
                "no CLI flag carries this field to the child -- only the YAML "
                "the child reads by project_root can set it, and that YAML "
                "does not agree with the parent's value"
            )
        if yaml_missing:
            reason += (
                f"; no YAML config exists at {scope.config_path} now, so the "
                "child would read class defaults for it (if the parent read "
                "one there, it has since vanished)"
            )
        diffs.append(FieldDiff(f.name, parent_value, child_value, reason))

    if diffs:
        return Irreproducible(diffs)
    return child_config
