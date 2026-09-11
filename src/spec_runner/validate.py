"""Validation for tasks.md — field checks, dependency refs, cycle detection, and config validation."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from spec_runner.config import (
    KNOWN_EXECUTOR_KEYS,
    ConfigError,
    ExecutorConfig,
    mixed_shape_error,
)
from spec_runner.logging import get_logger
from spec_runner.requirements import parse_requirements
from spec_runner.spec import LITE, StageProfile, load_profile, stage_path, strip_frontmatter
from spec_runner.task import TASK_STATUS_WORDS, Task, parse_tasks

log = get_logger("validate")

VALID_STATUSES = {"todo", "in_progress", "review", "done", "blocked"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}

_REQ_ID = re.compile(r"\bREQ-\d+\b")
_REQ_HEADING = re.compile(r"^#+\s*REQ-(\d+)\b", re.MULTILINE)
_DESIGN_HEADING = re.compile(r"^#+\s*DESIGN-(\d+)\b", re.MULTILINE)

# `KNOWN_EXECUTOR_KEYS` is imported from `config` above rather than rebuilt
# here: the loader decides what counts as a setting, and a second copy of that
# list is how validation and loading come to disagree (#182).


@dataclass
class ValidationResult:
    """Collects errors and warnings from validation checks."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return len(self.errors) == 0

    def merge(self, other: "ValidationResult") -> None:
        """Merge another result into this one."""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def verdict_from_result(result: ValidationResult) -> str:
    """Map a ValidationResult to a stage-gate verdict.

    Args:
        result: The validation result to classify.

    Returns:
        ``"fail"`` if there are errors, ``"warn"`` if only warnings, else ``"pass"``.
    """
    if result.errors:
        return "fail"
    if result.warnings:
        return "warn"
    return "pass"


def validate_requirements(path: Path) -> ValidationResult:
    """Validate a requirements doc: unique REQ ids, scope, acceptance criteria.

    Args:
        path: Path to the requirements.md file (may carry frontmatter).

    Returns:
        ValidationResult with errors/warnings found.
    """
    result = ValidationResult()
    body = strip_frontmatter(path.read_text())

    ids = _REQ_HEADING.findall(body)
    seen: set[str] = set()
    for rid in ids:
        if rid in seen:
            result.errors.append(f"REQ-{rid}: duplicate requirement ID")
        seen.add(rid)
    if not ids:
        result.errors.append("no REQ-XXX requirements found")
    if "out of scope" not in body.lower():
        result.errors.append("missing 'Out of Scope' section")
    if "acceptance criteria" not in body.lower():
        result.warnings.append("no 'Acceptance Criteria' found")

    # Per-requirement structural checks (M1): warn on a functional requirement
    # with no acceptance-criteria section (NFRs, being tables, are exempt to
    # avoid noise), and report duplicate NFR ids — the REQ dup check above only
    # scans REQ-* headings, so NFR-* uniqueness relies on the parsed list.
    seen_nfr: set[str] = set()
    for req in parse_requirements(body):
        if req.kind == "functional" and not req.acceptance_criteria:
            result.warnings.append(f"{req.id}: no acceptance criteria")
        if req.kind == "non-functional":
            if req.id in seen_nfr:
                result.errors.append(f"{req.id}: duplicate requirement ID")
            seen_nfr.add(req.id)
    return result


def validate_design(path: Path) -> ValidationResult:
    """Validate a design doc: unique DESIGN ids, no dangling REQ references.

    Args:
        path: Path to the design.md file (may carry frontmatter).

    Returns:
        ValidationResult with errors/warnings found.
    """
    result = ValidationResult()
    body = strip_frontmatter(path.read_text())

    ids = _DESIGN_HEADING.findall(body)
    seen: set[str] = set()
    for did in ids:
        if did in seen:
            result.errors.append(f"DESIGN-{did}: duplicate design ID")
        seen.add(did)
    if not ids:
        result.errors.append("no DESIGN-XXX components found")

    req_path = path.parent / path.name.replace("design.md", "requirements.md")
    known_reqs: set[str] = set()
    if req_path.exists():
        known_reqs = set(_REQ_ID.findall(strip_frontmatter(req_path.read_text())))
    for ref in _REQ_ID.findall(body):
        if known_reqs and ref not in known_reqs:
            result.errors.append(f"design references unknown {ref}")
    return result


def validate_spec_stage(
    stage: str, config: ExecutorConfig, profile: StageProfile = LITE
) -> ValidationResult:
    """Dispatch stage validation via the profile's validator registry.

    The stage's :class:`~spec_runner.spec.StageDef` supplies a serializable
    ``validator_key`` that is looked up in :data:`VALIDATORS` (DESIGN-304),
    replacing the previous hard-coded ``if/elif`` chain.

    Args:
        stage: A stage name in ``profile`` (default lite: requirements/design/tasks).
        config: Executor config providing the stage file paths.
        profile: Stage profile supplying the validator key (default lite).

    Returns:
        ValidationResult for the requested stage.

    Raises:
        ValueError: If ``stage`` is not a stage of ``profile``, or if its
            ``validator_key`` is not registered in :data:`VALIDATORS`.
    """
    stagedef = next((s for s in profile.stages if s.name == stage), None)
    if stagedef is None:
        raise ValueError(f"unknown stage: {stage}")
    try:
        validator = VALIDATORS[stagedef.validator_key]
    except KeyError:
        raise ValueError(
            f"unknown validator_key: {stagedef.validator_key!r}; available: {sorted(VALIDATORS)}"
        ) from None
    return validator(stage_path(config, stage))


def validate_task_fields(tasks: list[Task]) -> ValidationResult:
    """Check that every task has a valid status, priority, and dependency refs.

    Also emits warnings for missing estimates, blocked tasks without
    dependencies, and tasks without traceability references.

    Args:
        tasks: Parsed task list.

    Returns:
        ValidationResult with errors and warnings found.
    """
    result = ValidationResult()
    task_ids = {t.id for t in tasks}

    # Check for duplicate task IDs
    seen_ids: set[str] = set()
    for task in tasks:
        if task.id in seen_ids:
            result.errors.append(f"{task.id}: duplicate task ID")
        seen_ids.add(task.id)

    # Build blocks/depends_on maps for symmetry check
    blocks_map: dict[str, set[str]] = {}
    depends_map: dict[str, set[str]] = {}
    for task in tasks:
        blocks_map[task.id] = set(task.blocks) if hasattr(task, "blocks") and task.blocks else set()
        depends_map[task.id] = set(task.depends_on)

    for task in tasks:
        # --- Errors ---
        # #133: no line under this header matched TASK_META, so priority and
        # status below are parse defaults (p0/todo) — which read as a perfectly
        # ready task. Left as a warning the run would start, resolve
        # dependencies off invented statuses and die on the first task at the
        # 2.22.0 reconciliation gate; the generator emitting an unparseable
        # ordering must be caught before an agent is paid to run.
        meta_parsed = getattr(task, "has_meta", True)
        if not meta_parsed:
            result.errors.append(
                f"{task.id}: meta line not recognized — expected "
                f"`🔴 P0 | ⬜ TODO | Est: 1d` (priority, then status, "
                f"one of {', '.join(TASK_STATUS_WORDS)}); "
                f"priority/status are defaults, not what the spec says"
            )

        # Only meaningful against a meta line that was actually read: on a
        # default the values would always be the valid `todo`/`p0` and the
        # checks would silently vouch for a task nobody described. Everything
        # below this point comes from other lines and still applies.
        if meta_parsed and task.status not in VALID_STATUSES:
            result.errors.append(
                f"{task.id}: invalid status '{task.status}' "
                f"(expected one of {sorted(VALID_STATUSES)})"
            )

        if meta_parsed and task.priority not in VALID_PRIORITIES:
            result.errors.append(
                f"{task.id}: invalid priority '{task.priority}' "
                f"(expected one of {sorted(VALID_PRIORITIES)})"
            )

        for dep in task.depends_on:
            if dep not in task_ids:
                result.errors.append(f"{task.id}: dependency '{dep}' not found in task list")

        # #372 round 2: a **Verifies:** declaration `parse_tasks` could not
        # make sense of (e.g. a comma inside an unclosed `[...]`) is marked
        # on the task rather than raised, so this is the one surface that
        # turns it into a named, quoted error — before `run`, per FR-03, and
        # without a traceback, per NFR-03.
        if task.verifies_error:
            result.errors.append(f"{task.id}: {task.verifies_error}")

        # --- Warnings ---
        if not task.estimate:
            result.warnings.append(f"{task.id}: missing estimate")

        if task.status == "blocked" and not task.depends_on:
            result.warnings.append(f"{task.id}: status is blocked but has no dependencies")

        if not task.traces_to:
            result.warnings.append(f"{task.id}: no traceability references")

    # Symmetry check: if A blocks B, then B should depend on A
    for tid, blocked_ids in blocks_map.items():
        for blocked in blocked_ids:
            if blocked in depends_map and tid not in depends_map[blocked]:
                result.warnings.append(
                    f"{tid} blocks {blocked}, but {blocked} does not list {tid} in depends_on"
                )

    return result


def _detect_cycle(tasks: list[Task]) -> ValidationResult:
    """DFS cycle detection on the dependency graph.

    Args:
        tasks: Parsed task list.

    Returns:
        ValidationResult with an error per cycle found.
    """
    result = ValidationResult()

    # Build adjacency: task -> list of tasks it depends on
    adj: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks}
    all_ids = set(adj.keys())

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(all_ids, WHITE)

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbour in adj.get(node, []):
            if neighbour not in all_ids:
                continue  # dangling ref handled by validate_task_fields
            if color[neighbour] == GRAY:
                # Found a cycle — extract the cycle portion of the path
                cycle_start = path.index(neighbour)
                cycle = path[cycle_start:] + [neighbour]
                result.errors.append(f"Dependency cycle detected: {' -> '.join(cycle)}")
                # Do NOT return early — continue so path.pop()/BLACK always execute
                continue
            elif color[neighbour] == WHITE:
                dfs(neighbour, path)
        path.pop()
        color[node] = BLACK

    for tid in all_ids:
        if color[tid] == WHITE:
            dfs(tid, [])

    return result


def _levenshtein(s1: str, s2: str) -> int:
    """Compute the Levenshtein (edit) distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if not s2:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Insertion, deletion, substitution
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
        prev_row = curr_row

    return prev_row[-1]


def _suggest_key(unknown: str, known: set[str]) -> str | None:
    """Suggest the closest known key if Levenshtein distance <= 2.

    Args:
        unknown: The unrecognised key.
        known: Set of valid key names.

    Returns:
        The best suggestion, or None if nothing is close enough.
    """
    best: str | None = None
    best_dist = 3  # threshold: only suggest if distance <= 2
    for k in sorted(known):  # sorted for deterministic results
        d = _levenshtein(unknown, k)
        if d < best_dist:
            best = k
            best_dist = d
    return best


def validate_config(config_path: Path) -> ValidationResult:
    """Validate an executor config YAML file.

    Checks:
    - File exists (missing = ok, use defaults)
    - YAML is parseable
    - Keys are checked against known config keys (flat v2.0 and legacy
      ``executor:``-wrapped formats)

    Args:
        config_path: Path to the YAML config file.

    Returns:
        ValidationResult with any errors found.
    """
    result = ValidationResult()

    if not config_path.exists():
        return result  # missing config is fine — defaults apply

    raw = config_path.read_text()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        result.errors.append(f"Failed to parse YAML in {config_path}: {exc}")
        return result

    if not isinstance(data, dict):
        return result

    # Canonical v2.0 is flat (no executor: wrapper); legacy uses the wrapper.
    # Mixing them discards every top-level setting, which is an error and not a
    # warning: the failure mode is "your settings did nothing and the run went
    # to the defaults", and a warning scrolls past (#182).
    mixed = mixed_shape_error(config_path, data)
    if mixed:
        result.errors.append(mixed)
        # Not a return: `validate` exists to report everything wrong at once,
        # and the `executor:` section is still readable and still worth checking.

    executor_section = data.get("executor", data)
    if not isinstance(executor_section, dict):
        return result

    # Top-level sections that are parsed but not processed — warned, not errored.
    _DEAD_SECTIONS = {"execution_order", "skip_tasks", "environment"}

    for key in executor_section:
        if key not in KNOWN_EXECUTOR_KEYS and key not in _DEAD_SECTIONS:
            suggestion = _suggest_key(key, KNOWN_EXECUTOR_KEYS)
            msg = f"Unknown config key 'executor.{key}'"
            if suggestion:
                msg += f" — did you mean '{suggestion}'?"
            result.errors.append(msg)

    # Warn about top-level sections that are not processed
    for key in data:
        if key in _DEAD_SECTIONS:
            result.warnings.append(f"Top-level key '{key}' is not supported and will be ignored")

    _validate_tdd_runner(executor_section, result)
    _validate_spec_context_rules(executor_section, result)
    _validate_review_policy(executor_section, result)
    _validate_execution_mode(executor_section, result)

    return result


def _validate_tdd_runner(section: dict, result: "ValidationResult") -> None:
    """Refuse a runner that does not exist, or one the command cannot carry (#198).

    Both are the same failure at different distances: a red confirmed by
    reading one runner's exit codes as another's. A declaration chooses the
    semantics and cannot prove the command can carry them, so a mismatch is an
    error rather than a logged note — letting it through would be #198
    returning through an explicit config key.
    """
    from .tdd_runners import ADAPTERS, adapter_for

    declared = (section.get("tdd_runner") or "").strip()
    if not declared:
        return
    adapter = adapter_for(declared)
    if adapter is None:
        available = ", ".join(sorted(ADAPTERS))
        result.errors.append(f"unknown tdd_runner: {declared!r}; available: {available}")
        return
    commands = section.get("commands") or {}
    test_command = commands.get("test") if isinstance(commands, dict) else None
    if test_command is None:
        # Absent means the built-in default applies, and that default is a
        # pytest command. Present-but-empty is a different thing and must be
        # refused here, or `validate` would pass a config that `run` then
        # stops at — the same config judged differently by two surfaces
        # (Copilot, PR #202).
        return
    refusal = adapter.validate_command(str(test_command))
    if refusal is not None:
        result.errors.append(f"tdd_runner is {declared!r} but {refusal}")


def _validate_execution_mode(section: dict, result: "ValidationResult") -> None:
    """Refuse an unrecognised `execution_mode` before a run starts (#141).

    A typo silently meaning `standard` is how a project believes it is under
    the TDD contract while running without it.
    """
    from .config import EXECUTION_MODES

    mode = section.get("execution_mode")
    if mode is not None and mode not in EXECUTION_MODES:
        result.errors.append(
            f"execution_mode must be one of {', '.join(EXECUTION_MODES)} (got {mode!r})"
        )


#: `review_policy` values the runtime understands (#157).
REVIEW_POLICIES = ("advisory", "required")


def _validate_review_policy(section: dict, result: "ValidationResult") -> None:
    """Catch a `review_policy` that cannot mean what it says.

    ``required`` with ``run_review: false`` demands a review while switching
    review off. The honest moment to say so is before a run starts — not at the
    merge gate, after the work is already done and the only remaining options
    are bad ones.
    """
    policy = section.get("review_policy")
    if policy is None:
        return
    if policy not in REVIEW_POLICIES:
        result.errors.append(
            f"review_policy must be one of {', '.join(REVIEW_POLICIES)} (got {policy!r})"
        )
        return
    if policy != "required":
        return

    post_done = section.get("hooks", {}).get("post_done", {}) if isinstance(section, dict) else {}
    run_review = post_done.get("run_review") if isinstance(post_done, dict) else None
    if run_review is False:
        result.errors.append(
            "review_policy is 'required' but hooks.post_done.run_review is false — "
            "a required review that never runs can only ever block"
        )


# OpenSpec caps injected context at 50KB; mirror that (M0).
_SPEC_CONTEXT_LIMIT_BYTES = 50 * 1024


def _validate_spec_context_rules(section: dict, result: "ValidationResult") -> None:
    """Validate M0 ``spec_context`` and ``spec_rules``.

    Wrong types are errors (so a mis-typed config is caught before ``plan``):
    ``spec_context`` must be a string, ``spec_rules`` a mapping of stage name
    to a list of rules. ``spec_context`` over 50KB is an error; ``spec_rules``
    keyed by a stage absent from the configured profile is a warning (mirrors
    OpenSpec's "unknown artifact ID in rules" behaviour).
    """
    spec_context = section.get("spec_context")
    if spec_context is not None:
        if not isinstance(spec_context, str):
            result.errors.append("spec_context must be a string")
        elif len(spec_context.encode("utf-8")) > _SPEC_CONTEXT_LIMIT_BYTES:
            result.errors.append("spec_context exceeds the 50KB limit; summarise or link out")

    spec_rules = section.get("spec_rules")
    if spec_rules is None:
        return
    if not isinstance(spec_rules, dict):
        result.errors.append("spec_rules must be a mapping of stage name to a list of rules")
        return

    profile_name = section.get("spec_profile", "lite")
    try:
        stage_names = set(load_profile(profile_name).names())
    except Exception:
        stage_names = set(LITE.names())
    for stage_key, stage_rules in spec_rules.items():
        if stage_key not in stage_names:
            result.warnings.append(
                f"spec_rules references unknown stage '{stage_key}' "
                f"(profile '{profile_name}' stages: {', '.join(sorted(stage_names))})"
            )
        if not isinstance(stage_rules, list):
            result.errors.append(f"spec_rules['{stage_key}'] must be a list of rules")


def validate_tasks(tasks_file: Path) -> ValidationResult:
    """Orchestrate all validation checks on a tasks file.

    Checks performed (in order):
    1. File exists
    2. File parses to at least one task
    3. Task fields are valid (status, priority, dep refs)
    4. No dependency cycles

    Args:
        tasks_file: Path to the tasks.md file.

    Returns:
        ValidationResult aggregating all checks.
    """
    result = ValidationResult()

    # 1. File exists
    if not tasks_file.exists():
        result.errors.append(f"Tasks file does not exist: {tasks_file}")
        return result

    # 2. Parse tasks (parse_tasks calls sys.exit on missing file,
    #    but we already checked existence above)
    tasks = parse_tasks(tasks_file)

    if not tasks:
        result.errors.append(f"No tasks found in {tasks_file}")
        return result

    # 3. Field validation (status, priority, dep refs)
    result.merge(validate_task_fields(tasks))

    # 4. Cycle detection
    result.merge(_detect_cycle(tasks))

    log.info(
        "validation_complete",
        file=str(tasks_file),
        errors=len(result.errors),
        warnings=len(result.warnings),
    )

    return result


#: Registry of stage validators keyed by :attr:`StageDef.validator_key`
#: (DESIGN-304). Profiles reference these string keys rather than the callables
#: themselves so that :class:`~spec_runner.spec.StageProfile` stays serializable.
VALIDATORS: dict[str, Callable[[Path], ValidationResult]] = {
    "requirements": validate_requirements,
    "design": validate_design,
    "tasks": validate_tasks,
}


def _config_for_validation(
    config_file: Path | None, *, project_root: Path | None = None
) -> ExecutorConfig:
    """The config a verify-first declaration is judged against (#367 FR-03).

    Built from YAML alone, no CLI args: neither `execution_mode` nor
    `tdd_runner` is ever overridden by a CLI flag (see `build_config`), so
    this agrees with what `run`/`watch` actually resolve those two to.
    `None` (no config file given) mirrors `validate_config`'s own behaviour
    of doing nothing rather than guessing a project's config path.

    `project_root` is the one exception to "YAML alone" (sr397 review): it is
    NOT covered by the rationale above — that rationale is scoped to
    `execution_mode`/`tdd_runner` agreeing with what `run`/`watch` resolve,
    and says nothing about which tree a file-existence check runs against.
    Left unset, `ExecutorConfig()`'s own default resolves to cwd — correct
    for a bare `spec-runner validate` invoked from the project root, but
    wrong under `--project-root` (or `validate_all` called from a directory
    other than the project root): every declared path would then be checked
    against the wrong tree, giving false warnings and false
    `outside_repository` errors alike. Callers pass the already-built
    config's `project_root` (itself CLI-`--project-root`-aware,
    `config.py`'s `build_config`) so this file-existence check judges the
    same tree the run itself will.
    """
    if config_file is None:
        kwargs: dict = {}
    else:
        from spec_runner.config import load_config_from_yaml

        try:
            yaml_config = load_config_from_yaml(config_file)
            kwargs = {k: v for k, v in yaml_config.items() if v is not None}
        except ConfigError:
            # Already reported by validate_config (mixed flat/executor:
            # shape) — fall back to defaults so this check still runs
            # against something.
            kwargs = {}
    if project_root is not None:
        kwargs["project_root"] = project_root
    return ExecutorConfig(**kwargs)


def _validate_verify_first_declarations(
    tasks: list[Task], config: ExecutorConfig
) -> ValidationResult:
    """An invalid verify-first declaration refuses before any execution
    (#367 FR-03/BEH-04/BEH-05): an unresolvable `**Mode:**`, a verify-first
    task with no declared `**Verifies:**` group, an empty declared group, a
    selector the project's adapter refuses, or a declared group on a task
    whose *resolved* mode is not verify_first (that group would never run —
    a plain `**Mode:**`-less task under a project-wide verify_first default
    is not this case, since it resolves to verify_first itself).

    Each declared group element is read through `parse_group_element`
    (DT-01's declared-group-element vocabulary), not `parse_selector` (a
    RED-checkpoint vocabulary about exactly one test) — a bare file target is
    a legal group member here. Only what `validate` can decide without a run
    is an error: a `parse_group_element` refusal is an error, except a file
    missing from the *working tree*, which is a warning — `validate` judges
    the tree in hand, not a commit a live run will later replay, so that
    file's existence there is a fact for the run to establish, not one this
    static check can assume fixed (#367 follow-up, BEH-09).

    Args:
        tasks: Parsed task list.
        config: The config to resolve each task's mode and adapter against —
            see `_config_for_validation`.

    Returns:
        ValidationResult with one error per defective declaration and one
        warning per file target absent from the working tree.
    """
    from spec_runner.tdd_runners import SelectorRefusal, adapter_for, parse_group_element

    result = ValidationResult()

    adapter_error: str | None = None
    try:
        adapter_name = config.resolve_tdd_runner()
    except ConfigError as exc:
        adapter_name = None
        adapter_error = str(exc)
    adapter = adapter_for(adapter_name) if adapter_name else None

    for task in tasks:
        if task.verifies_error:
            # Already reported by validate_task_fields — a different defect
            # (unparseable declaration) from the ones checked here.
            continue

        try:
            mode = config.resolve_execution_mode(task)
        except ConfigError as exc:
            result.errors.append(f"{task.id}: {exc}")
            continue

        # #429: the marker is cross-validated against the mode by the same
        # precedent as `**Verifies:**` below — a declaration that could never
        # take effect is an operator error, not a no-op. The resolver is the
        # single place that decides; here we only surface its refusal with the
        # task named, at config time rather than mid-run.
        try:
            config.resolve_waiver(task)
        except ConfigError as exc:
            result.errors.append(f"{task.id}: {exc}")

        if mode != "verify_first":
            if task.verifies is not None:
                result.errors.append(
                    f"{task.id}: **Verifies:** {task.verifies!r} is declared "
                    f"but the resolved execution mode is {mode!r}, not "
                    "verify_first — this group would never run"
                )
            continue

        if task.verifies is None:
            result.errors.append(
                f"{task.id}: mode is verify_first but no **Verifies:** group is declared"
            )
            continue
        if not task.verifies:
            result.errors.append(
                f"{task.id}: mode is verify_first but the declared **Verifies:** group is empty"
            )
            continue

        if adapter is None:
            result.errors.append(
                f"{task.id}: mode is verify_first but the project's test "
                "adapter cannot be resolved" + (f": {adapter_error}" if adapter_error else "")
            )
            continue

        for raw in task.verifies:
            # `parse_group_element` (DT-01's declared-group vocabulary), not
            # `parse_selector` (a RED-checkpoint vocabulary about exactly one
            # test): a bare file-path element is a legal group member here,
            # and `parse_selector` alone would keep refusing it as "not a
            # node id" (the retired boundary BEH-09 lifts).
            parsed = parse_group_element(adapter, raw, config.project_root)
            if not isinstance(parsed, SelectorRefusal):
                continue
            if parsed.code == "not_a_regular_file":
                # `validate` judges the working tree, not the commit a live
                # run will replay — existence there is a fact for that run to
                # establish, not one static validation can assume is fixed.
                result.warnings.append(
                    f"{task.id}: mode is verify_first, declared group "
                    f"{task.verifies!r} — file {raw!r} does not exist in the "
                    f"working tree yet ({adapter.name} adapter): {parsed.message}"
                )
                continue
            result.errors.append(
                f"{task.id}: mode is verify_first, declared group "
                f"{task.verifies!r} — selector {raw!r} refused by the "
                f"{adapter.name} adapter ({parsed.code}): {parsed.message}"
            )

    return result


def validate_all(
    tasks_file: Path | None = None,
    config_file: Path | None = None,
    *,
    project_root: Path | None = None,
) -> ValidationResult:
    """Run all validation checks.

    Args:
        tasks_file: Path to tasks.md (optional).
        config_file: Path to executor config YAML (optional).
        project_root: The tree a declared file target's existence is judged
            against (sr397 review) — pass the caller's already-built
            `ExecutorConfig.project_root` (CLI `--project-root`-aware) so
            this agrees with the tree `run`/`watch` actually operate on.
            Unset falls back to `_config_for_validation`'s own default
            (cwd) — correct only when the caller's cwd already is the
            project root.

    Returns:
        Merged ValidationResult from all checks.
    """
    result = ValidationResult()
    tasks: list[Task] = []
    if tasks_file:
        result.merge(validate_tasks(tasks_file))
        if tasks_file.exists():
            tasks = parse_tasks(tasks_file)
    if config_file:
        result.merge(validate_config(config_file))
    if tasks:
        result.merge(
            _validate_verify_first_declarations(
                tasks, _config_for_validation(config_file, project_root=project_root)
            )
        )
    return result


def format_results(result: ValidationResult) -> str:
    """Format validation results for terminal output.

    Args:
        result: ValidationResult to format.

    Returns:
        Human-readable string with errors, warnings, and summary.
    """
    lines: list[str] = []
    if result.errors:
        for e in result.errors:
            lines.append(f"  x {e}")
    if result.warnings:
        if lines:
            lines.append("")
        for w in result.warnings:
            lines.append(f"  ! {w}")
    n_err = len(result.errors)
    n_warn = len(result.warnings)
    err_word = "error" if n_err == 1 else "errors"
    warn_word = "warning" if n_warn == 1 else "warnings"
    lines.append(f"\n{n_err} {err_word}, {n_warn} {warn_word}")
    return "\n".join(lines)
