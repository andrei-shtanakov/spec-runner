"""CLI plan command: interactive task planning via Claude."""

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import ExecutorConfig, ExecutorLock
from .logging import get_logger
from .prompt import (
    _parse_stage_marker,
    _stage_def,
    build_gated_generation_prompt,
    load_prompt_template,
    render_template,
    template_hash,
)
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
)
from .spec import (
    SpecMeta,
    ancestor_stages,
    atomic_write_bytes,
    read_spec_body,
    read_spec_meta,
    resolve_next_stage,
    stage_path,
    stamp_authoring_links,
    write_spec,
)
from .task import (
    ID_PATTERN,
    TASK_META,
    TASK_STATUS_WORDS,
    parse_tasks,
)
from .validate import (
    format_results,
    validate_spec_stage,
    validate_tasks,
    verdict_from_result,
)

logger = get_logger("cli")


def _harness(config) -> str:
    """Derive a short harness name from the configured CLI command."""
    base = (config.claude_command or "claude").split("/")[-1]
    return base or "claude"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, 'Z' suffix)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_editor(path: Path) -> None:
    """Launch ``$EDITOR`` (falling back to ``vi``) on ``path``, blocking until exit.

    ``$EDITOR`` is shell-word-split so values with arguments (e.g.
    ``"code --wait"``, ``"vim -u NONE"``) invoke correctly instead of being
    passed as a single (invalid) argv element.
    """
    editor = os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)])


def _restore(path: Path, previous: bytes | None) -> None:
    """Put the stage file back the way it was before a rejected candidate.

    A failed generation must not cost the operator the draft they already had,
    and must not leave an invalid document that looks like an artifact (#160).
    """
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        # Atomic: a plain write truncates first, so an interruption here would
        # destroy the draft this rollback exists to protect (Copilot, PR #161).
        atomic_write_bytes(path, previous)


def _generate_stage_draft(
    stage: str,
    description: str,
    config,
    invoke=subprocess.run,
) -> int:
    """Generate one gated spec stage: enforce upstream gate, write DRAFT, validate.

    Every upstream stage must already be APPROVED, else generation is refused
    (no CLI invocation, no file write). On success, writes the generated stage
    as a DRAFT with frontmatter, then runs stage validation and records the
    verdict on the same file.

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        description: Project description used to build the generation prompt.
        config: Executor config providing stage file paths and CLI settings.
        invoke: Injectable subprocess runner (defaults to `subprocess.run`);
            tests pass a fake to avoid spawning a real CLI.

    Returns:
        0 on success (DRAFT written, validated); 1 on generation failure
        (non-zero CLI exit or missing marker); 2 when the upstream gate blocks
        generation.
    """
    profile = config.resolve_spec_profile()
    stage_def = _stage_def(stage, profile)
    stage_names = profile.names()

    # Gate: only the DIRECT requires must be approved before generation runs
    # (requires means direct DAG edges, not the transitive closure — ruling
    # 2026-07-26). This is deliberately separate from the prompt context
    # below: approving a stage's immediate prerequisite doesn't require every
    # ancestor further back to still be approved.
    for upstream in stage_def.upstream:
        meta = read_spec_meta(stage_path(config, upstream), stage_names)
        if meta is None or meta.status != "approved":
            print(f"⛔ cannot generate {stage}: {upstream} must be APPROVED first")
            return 2

    # Context: the full transitive ancestor closure, so e.g. "tasks" still
    # sees "requirements" even though its only direct requirement is "design".
    # Statuses ride along so the prompt can label each block honestly: the
    # gate above clears only the DIRECT requires, so an ancestor further back
    # may still be a draft and must not be presented as approved.
    context: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for ancestor in ancestor_stages(stage, profile):
        context[ancestor] = read_spec_body(stage_path(config, ancestor))
        ancestor_meta = read_spec_meta(stage_path(config, ancestor), stage_names)
        statuses[ancestor] = ancestor_meta.status if ancestor_meta is not None else "unmanaged"

    path = stage_path(config, stage)
    existing = read_spec_meta(path, stage_names)
    version = existing.version if existing is not None else 1
    lock = ExecutorLock(config.spec_lock_file)
    # Bytes to restore when a candidate fails validation, so a rejected
    # generation leaves the stage exactly as it found it (#160).
    previous = path.read_bytes() if path.exists() else None
    budget = max(0, int(getattr(config, "spec_repair_attempts", 2)))
    repair_errors: list[str] | None = None

    for attempt in range(budget + 1):
        prompt = build_gated_generation_prompt(
            stage,
            description,
            context,
            profile=profile,
            spec_context=config.spec_context or None,
            spec_rules=config.spec_rules or None,
            statuses=statuses,
            repair_errors=repair_errors,
        )
        cmd = build_cli_command(
            cmd=config.claude_command,
            prompt=prompt,
            model=config.claude_model,
            template=config.command_template,
            skip_permissions=config.skip_permissions,
        )
        result = invoke(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
        )
        if result.returncode != 0:
            print(f"generation failed at {stage}: {result.stderr[:300]}")
            _restore(path, previous)
            return 1

        body = _parse_stage_marker(result.stdout, stage_def)
        if not body:
            print(f"no {stage} content produced (marker missing)")
            _restore(path, previous)
            return 1

        # Same recoverable generator deviations as the `--full` pipeline
        # (#301): normalizing them here spends no regeneration attempt on a
        # bold meta line. Anything left is still refused by the validator
        # below — the gate does not move, only what reaches it.
        if stage == "tasks":
            body = normalize_task_meta(normalize_task_headers(body))

        meta = SpecMeta(
            spec_stage=stage,
            status="draft",
            version=version,
            generated_by=f"{_harness(config)}@{config.claude_model or 'default'}",
            generated_at=_now_iso(),
            source_prompt_version=template_hash(stage, profile),
            owner_role=existing.owner_role if existing is not None else None,
            extra=dict(existing.extra) if existing is not None else {},
        )
        body = body.rstrip("\n") + "\n"
        # A draft already knows what it was derived from, so the traceability
        # link is stamped here; the upstream pins wait for approval, which is
        # what they record (DEC-008, #135).
        stamp_authoring_links(meta, config, stage, body, profile, pin_upstream=False)

        # The candidate is validated on disk deliberately: this is the same
        # validator the run enforces, and it resolves cross-stage references
        # through the real sibling files — a copy in a scratch directory would
        # be a second, drifting implementation. Nothing invalid survives the
        # call: `_restore` puts back the previous bytes (or removes the file
        # when there were none), so a rejected candidate never persists.
        write_spec(path, meta, body, lock=lock)
        result_v = validate_spec_stage(stage, config, profile)
        verdict = verdict_from_result(result_v)

        if verdict != "fail":
            meta.validation = verdict
            write_spec(path, meta, read_spec_body(path), lock=lock)
            print(f"{stage}.md written as DRAFT — validation={verdict}")
            print(f"  approve with: spec-runner spec approve {stage}")
            return 0

        _restore(path, previous)
        repair_errors = list(result_v.errors)
        remaining = budget - attempt
        print(
            f"{stage}: generated content failed validation "
            f"({len(repair_errors)} error(s)); nothing written"
        )
        for err in repair_errors[:5]:
            print(f"    • {err}")
        if remaining:
            print(f"  regenerating with the errors above ({remaining} attempt(s) left)")

    print(f"⛔ {stage}: no valid document after {budget + 1} attempt(s) — nothing written")
    return 1


def run_gated_stage(
    stage: str,
    description: str,
    config,
    invoke=subprocess.run,
    *,
    interactive: bool = False,
    input_fn: Callable[[str], str] = input,
    editor_fn: Callable[[Path], None] | None = None,
) -> int:
    """Generate one gated spec stage, optionally overlaying the TTY checkpoint menu.

    Delegates the generate/write-DRAFT/validate work to `_generate_stage_draft`.
    When `interactive` is False (the default), behavior is unchanged: generate
    once and return.

    When `interactive` is True, after the DRAFT is written and validated, loop
    over `run_checkpoint_menu` (see `spec_commands.py`):
      - "approved" / "stop" / "abort" → return 0 (caller decides what's next).
      - "edit" → run `editor_fn` (or `_open_editor`) on the stage file, then
        redisplay the menu (which re-validates from disk).
      - "regenerate" → re-run `_generate_stage_draft` for the same stage and
        redisplay the menu.

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        description: Project description used to build the generation prompt.
        config: Executor config providing stage file paths and CLI settings.
        invoke: Injectable subprocess runner (defaults to `subprocess.run`).
        interactive: Show the TTY checkpoint menu after a successful DRAFT.
        input_fn: Injectable input function for the menu (tests never read
            real stdin).
        editor_fn: Injectable editor launcher for the "edit" action (tests
            never launch a real editor); defaults to `_open_editor`.

    Returns:
        0 on a successful DRAFT (non-interactive), or on any terminal menu
        action ("approved"/"stop"/"abort"); the `_generate_stage_draft`
        error code (1 or 2) if generation itself fails.
    """
    rc = _generate_stage_draft(stage, description, config, invoke)
    if rc != 0 or not interactive:
        return rc

    from .spec_commands import run_checkpoint_menu

    while True:
        action = run_checkpoint_menu(stage, config, input_fn=input_fn)
        if action in ("approved", "stop", "abort"):
            return 0
        if action == "edit":
            (editor_fn or _open_editor)(stage_path(config, stage))
            continue
        if action == "regenerate":
            rc = _generate_stage_draft(stage, description, config, invoke)
            if rc != 0:
                return rc
            continue


def _print_gate_status(action: str, stage: str) -> bool:
    """Print the message for a non-"generate" `resolve_next_stage` action.

    Returns True when `action` is terminal (the caller should stop: the
    pipeline is done, a stage is stale, awaits approval, or is dependency-
    blocked); False when `action == "generate"` (the caller should proceed to
    generate it).
    """
    if action == "await_approval":
        print(f"{stage} is DRAFT — approve or edit it before continuing")
        return True
    if action == "stale":
        print(
            f"{stage} is STALE — re-run `plan --gated --stage {stage}` to "
            f"regenerate, or `spec approve {stage}` / `spec reject {stage}`"
        )
        return True
    if action == "done":
        print("all stages approved → spec-runner run")
        return True
    if action == "blocked":
        print(
            f"{stage} is BLOCKED — its upstream stages must be approved first "
            f"(run `spec-runner spec status` to see what's missing)"
        )
        return True
    return False


def _current_metas(config) -> dict[str, SpecMeta | None]:
    """Read the current `SpecMeta` for every stage of the configured profile."""
    names = config.resolve_spec_profile().names()
    return {s: read_spec_meta(stage_path(config, s), names) for s in names}


def resolve_plan_description(
    description: str | None, from_file: str | None, *, required: bool = True
) -> str:
    """Resolve the plan description from --from-file (preferred) or the positional
    argument. Exits with an error if neither is usable.

    Args:
        description: the positional description (may be None when --from-file is used).
        from_file: path to a file whose contents are the description.
        required: when False, "nothing given" resolves to an empty string instead of
            exiting — gated mode decides per stage whether a description is needed
            (a downstream stage inherits it from its approved upstream). An
            unusable ``--from-file`` is still fatal: the caller asked for a file.
    """
    if from_file:
        path = Path(from_file)
        if not path.is_file():
            raise SystemExit(f"plan --from-file: not a readable file: {from_file}")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as e:
            raise SystemExit(f"plan --from-file: not valid UTF-8 text: {from_file}") from e
        except OSError as e:
            raise SystemExit(f"plan --from-file: cannot read {from_file}: {e}") from e
        if not text:
            raise SystemExit(f"plan --from-file: file is empty: {from_file}")
        return text
    if description and description.strip():
        return description
    if not required:
        return ""
    raise SystemExit("plan: provide a description argument or --from-file PATH")


# The stand-in description a downstream stage runs with when the operator gave
# none. It is not a placeholder for missing information: the approved upstream
# document is reproduced in the very same prompt, so this line's whole job is to
# point the model at it instead of letting "## DESCRIPTION" render empty.
_INHERITED_DESCRIPTION = (
    "No separate description was given for this stage. Derive it entirely from the "
    "upstream stage(s) reproduced below ({upstream}) — the approved upstream document "
    "is the source of truth for scope and intent."
)


def _gated_description(description: str, stage: str, profile) -> str:
    """Resolve the description for one gated stage, inheriting it when it is absent.

    README documents `spec-runner plan --gated --stage design` without a
    description, and steward's live V1 run hit the mismatch: every stage demanded
    one (#134 item 2). A stage with an approved upstream does not need it — the
    upstream body is already the richer input — so an absent description is
    inherited there and only the first stage of the chain still insists on one.

    Raises:
        SystemExit: the stage has no upstream to inherit from.
    """
    if description.strip():
        return description
    upstream = _stage_def(stage, profile).upstream
    if not upstream:
        raise SystemExit(
            f"plan --gated: '{stage}' is the first stage — provide a description "
            "argument or --from-file PATH"
        )
    return _INHERITED_DESCRIPTION.format(upstream=", ".join(upstream))


# Generated specs use the TASK- convention, but adopted/edited ones may
# carry a native prefix (#72) — normalize any recognized id shape.
_TASK_HEADER_VARIANT = re.compile(rf"^#{{2,4}} ({ID_PATTERN})\s*[—–:-]\s*(.+)$", re.MULTILINE)


def normalize_task_headers(text: str) -> str:
    """Normalize recoverable task-header variants to the parseable form.

    Governed-run finding H-2b: the generation LLM systematically emits
    variants like ``### TASK-001 — Title`` (em-dash) or an h2 heading despite
    the template. Anything shaped like a task header is rewritten to the
    canonical ``### TASK-NNN: Title`` the run parser requires; genuinely
    unrecoverable output is still caught by validate_generated_tasks.
    """
    return _TASK_HEADER_VARIANT.sub(lambda m: f"### {m.group(1)}: {m.group(2)}", text)


# The meta line is decorated by the generation LLM the same way the headers
# are (#301): bold around the very tokens the parser anchors on — `🔴 **P0**`,
# `Est: **2h**` — and, in the reported file, no status word at all. Anchoring
# on `P\d |` at line start is the same anchor TASK_META uses, so prose and
# checklist items are never mistaken for a meta line.
_PRIORITY_EMOJI = r"(?:🔴|🟠|🟡|🟢)"
_META_LINE_ANCHOR = re.compile(
    rf"^(?:[ \t]*[-*]\s+)?\*{{0,2}}(?:{_PRIORITY_EMOJI}\s+)?\*{{0,2}}P\d\*{{0,2}}\s*\|"
)
_STATUS_EMOJI = r"(?:⬜|🔄|🔍|✅|⏸️)"
_BOLD_PRIORITY = re.compile(rf"\*\*((?:{_PRIORITY_EMOJI}\s+)?P\d)\*\*")
# The bold may wrap the emoji too (`**⬜ TODO**`), and it is the whole segment
# that gets decorated more often than the bare word — matching only the word
# left the segment unrecognized, and the missing-status branch then appended a
# second status while the bolded one stayed behind as a stray line.
_BOLD_STATUS = re.compile(rf"\*\*((?:{_STATUS_EMOJI}\s+)?(?i:{'|'.join(TASK_STATUS_WORDS)}))\*\*")
_BOLD_ESTIMATE = re.compile(r"(Est:\s*)\*\*([^*]+?)\*\*")
_PRIORITY_SEGMENT = re.compile(rf"^((?:[ \t]*[-*]\s+)?(?:{_PRIORITY_EMOJI}\s+)?P\d\s*\|\s*)")


def _split_meta_line(line: str) -> list[str]:
    """Move ``**Field:** value`` segments off the meta line onto their own.

    `parse_tasks` stops reading a meta line once it has the priority, status
    and estimate, so ``🔴 P0 | … | **Depends on:** [TASK-001]`` validates
    cleanly while its ordering is silently dropped — the generated file in
    #301 declared every dependency there. The canonical template puts those
    fields on their own lines, which is where the parser looks for them.
    """
    segments = [s.strip() for s in line.split("|")]
    head = [s for s in segments if not s.startswith("**")]
    fields = [s for s in segments if s.startswith("**")]
    if not fields:
        return [line]
    return [" | ".join(head), *fields]


def normalize_task_meta(text: str) -> str:
    """Normalize recoverable task-meta variants to the parseable form.

    Sibling of `normalize_task_headers` for the line below the header (#301):
    the bold is stripped from priority, status and estimate, a meta line that
    names a priority but no status is given the only status a freshly
    generated task can have — ``⬜ TODO`` — and trailing ``**Field:**``
    segments are moved onto their own lines. A rewrite is adopted only if it
    actually yields a line `TASK_META` recognizes, so a line this cannot bring
    to canonical form is left exactly as written for the validator to refuse.
    """
    out: list[str] = []
    for line in text.split("\n"):
        if not _META_LINE_ANCHOR.match(line):
            out.append(line)
            continue
        fixed = _BOLD_PRIORITY.sub(r"\1", line)
        fixed = _BOLD_STATUS.sub(r"\1", fixed)
        fixed = _BOLD_ESTIMATE.sub(r"\1\2", fixed)
        if not TASK_META.match(fixed):
            fixed = _PRIORITY_SEGMENT.sub(r"\g<1>⬜ TODO | ", fixed, count=1)
        if fixed != line and TASK_META.match(fixed):
            out.extend(_split_meta_line(fixed))
        else:
            out.extend(_split_meta_line(line) if TASK_META.match(line) else [line])
    return "\n".join(out)


def validate_generated_tasks(tasks_file: Path) -> int:
    """Ensure a generated tasks.md passes the validation `run` performs.

    Returns the parsed task count; exits 1 when zero tasks parse or when the
    file carries a validation error (the file is left in place for debugging).
    Guards the plan->run format contract: asking only "does anything parse"
    was strictly weaker than the question `run` asks, so a file every task of
    which `run` refused (#301) was returned as a successful generation.
    """
    parsed = parse_tasks(tasks_file)
    if not parsed:
        logger.error("Generated tasks.md has no parseable tasks", file=str(tasks_file))
        print(
            f"Generated {tasks_file} contains no parseable tasks: headers must "
            f"match '### TASK-NNN: Title' (the exact parser `run` uses). "
            f"The file is kept for inspection; re-run plan."
        )
        sys.exit(1)

    result = validate_tasks(tasks_file)
    if not result.ok:
        logger.error(
            "Generated tasks.md fails the run validator",
            file=str(tasks_file),
            errors=len(result.errors),
        )
        print(f"Generated {tasks_file} does not pass the validation `run` performs:")
        print(format_results(result))
        print("\nThe file is kept for inspection; re-run plan.")
        sys.exit(1)
    return len(parsed)


def apply_plan_confirmation(
    confirm: str,
    task_blocks: list[str],
    config: ExecutorConfig,
    editor_fn: Callable[[Path], None] | None = None,
) -> None:
    """Apply the user's [y/N/edit] choice to the proposed task blocks.

    Both "y" and "edit" persist the proposal by appending it to ``tasks.md``
    (creating the file if needed) — "edit" additionally opens the file in
    ``$EDITOR`` so the draft can be adjusted before ``spec-runner run``.
    Persisting BEFORE the editor launch is the point: the proposal must never
    exist only in scrollback (previously "edit" wrote nothing, so with no
    pre-existing ``tasks.md`` there was nothing to edit and the generated
    tasks were lost). Any other answer cancels without touching the file.

    Args:
        confirm: Normalized (stripped, lowercased) answer to the prompt.
        task_blocks: Task proposal bodies as extracted from the CLI output
            (without the leading ``### ``).
        config: Executor config providing ``tasks_file``.
        editor_fn: Injectable editor launcher for the "edit" action (tests
            never launch a real editor); defaults to `_open_editor`.
    """
    if confirm not in ("y", "edit"):
        print("\n❌ Cancelled")
        return

    tasks_file = config.tasks_file
    content = tasks_file.read_text() if tasks_file.exists() else "# Tasks\n\n"
    for block in task_blocks:
        # Only the appended proposal is normalized (#301) — the same generator
        # decorates the meta line here as in `--full`, and whatever the file
        # already held is the operator's, not ours to rewrite.
        content += f"\n### {normalize_task_meta(block.strip())}\n"

    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(content)
    print(f"\n✅ Added {len(task_blocks)} task(s) to {tasks_file}")
    log_progress(f"✅ Created {len(task_blocks)} tasks")

    if confirm == "edit":
        (editor_fn or _open_editor)(tasks_file)
        print(f"\nEdited {tasks_file} — run 'spec-runner run' when ready")


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude.

    With --full flag, runs a three-stage pipeline to generate
    requirements, design, and tasks files from a description.
    """

    gated = getattr(args, "gated", False)
    # Gated mode resolves "no description given" per stage (see
    # `_gated_description`), so the usage error is deferred until the stage is
    # known; every other mode still needs one up front.
    description = resolve_plan_description(
        args.description, getattr(args, "from_file", None), required=not gated
    )

    if gated:
        profile = config.resolve_spec_profile()
        explicit_stage = getattr(args, "stage", None)
        if explicit_stage:
            # Single-stage request: never auto-continue, never show the menu —
            # this is the same behavior regardless of TTY/--no-interactive.
            raise SystemExit(
                run_gated_stage(
                    explicit_stage,
                    _gated_description(description, explicit_stage, profile),
                    config,
                )
            )

        interactive = sys.stdout.isatty() and not getattr(args, "no_interactive", False)

        if not interactive:
            action, stage = resolve_next_stage(_current_metas(config), profile)
            if _print_gate_status(action, stage):
                return
            raise SystemExit(
                run_gated_stage(stage, _gated_description(description, stage, profile), config)
            )

        # Interactive auto-continue: generate -> checkpoint menu -> next stage.
        # Terminates in at most len(STAGES) generate-iterations: each generated
        # stage flips from missing to draft, so resolve_next_stage can never
        # return "generate" for the same stage twice; a stop/await/stale/done
        # resolves to a terminal action that _print_gate_status breaks on at
        # the top of the loop.
        while True:
            action, stage = resolve_next_stage(_current_metas(config), profile)
            if _print_gate_status(action, stage):
                break
            rc = run_gated_stage(
                stage,
                _gated_description(description, stage, profile),
                config,
                interactive=True,
            )
            if rc != 0:
                break
        return

    if getattr(args, "full", False):
        from .prompt import build_generation_prompt, parse_spec_marker

        stages = ["requirements", "design", "tasks"]
        stage_files = {
            "requirements": config.requirements_file,
            "design": config.design_file,
            "tasks": config.tasks_file,
        }
        marker_names = {
            "requirements": "REQUIREMENTS",
            "design": "DESIGN",
            "tasks": "TASKS",
        }
        context: dict[str, str] = {}

        for stage in stages:
            logger.info("Generating spec", stage=stage)
            prompt = build_generation_prompt(
                stage,
                description,
                context,
                spec_context=config.spec_context or None,
                spec_rules=config.spec_rules or None,
            )

            cmd = build_cli_command(
                cmd=config.claude_command,
                prompt=prompt,
                model=config.claude_model,
                template=config.command_template,
                skip_permissions=config.skip_permissions,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            if result.returncode != 0:
                logger.error(
                    "Generation failed",
                    stage=stage,
                    stderr=result.stderr[:500],
                )
                print(f"Failed at stage: {stage}")
                sys.exit(1)

            content = parse_spec_marker(result.stdout, marker_names[stage])
            if not content:
                logger.error("No spec marker found in output", stage=stage)
                print(f"Claude did not produce {stage} content.")
                sys.exit(1)

            # H-2b (governed-run finding): recoverable task-header variants
            # (em-dash, wrong heading depth) are normalized BEFORE the single
            # write, so the file, the validation and `context` all agree.
            if stage == "tasks":
                normalized = normalize_task_meta(normalize_task_headers(content))
                if normalized != content:
                    logger.info("Task headers/meta normalized", stage=stage)
                content = normalized

            output_file = stage_files[stage]
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(content + "\n")
            logger.info("Spec written", stage=stage, file=str(output_file))
            print(f"Written: {output_file}")

            # H-2: generation must validate its own output against the SAME
            # parser `run` uses — an LLM heading like "## TASK-001 — Title"
            # produced a spec run could not consume.
            if stage == "tasks":
                validate_generated_tasks(output_file)

            context[stage] = content

        print("\nSpec generation complete!")
        print(f"  Requirements: {config.requirements_file}")
        print(f"  Design:       {config.design_file}")
        print(f"  Tasks:        {config.tasks_file}")
        return

    print(f"\n📝 Planning: {description}")
    print("=" * 60)

    # Load context
    requirements_summary = "No requirements.md found"
    if config.requirements_file.exists():
        content = config.requirements_file.read_text()
        # Extract just headers and first lines for summary
        lines = content.split("\n")[:100]
        requirements_summary = "\n".join(lines) + "\n...(truncated)"

    design_summary = "No design.md found"
    if config.design_file.exists():
        content = config.design_file.read_text()
        lines = content.split("\n")[:100]
        design_summary = "\n".join(lines) + "\n...(truncated)"

    # Get existing tasks
    existing_tasks = "No existing tasks"
    if config.tasks_file.exists():
        tasks = parse_tasks(config.tasks_file)
        task_lines = [f"- {t.id}: {t.name} ({t.status})" for t in tasks[-20:]]
        existing_tasks = "\n".join(task_lines) if task_lines else "No tasks yet"

    # Load template
    template = load_prompt_template("plan", prompts_dir=config.prompts_dir)

    if template:
        prompt = render_template(
            template,
            {
                "DESCRIPTION": description,
                "REQUIREMENTS_SUMMARY": requirements_summary,
                "DESIGN_SUMMARY": design_summary,
                "EXISTING_TASKS": existing_tasks,
            },
        )
    else:
        prompt = f"""# Task Planning Request

## Feature Description:
{description}

## Project Context:

### Requirements (excerpt):
{requirements_summary}

### Existing Tasks:
{existing_tasks}

## Instructions:

Create structured tasks for this feature. For each task use format:

### TASK-XXX: <title>
🔴 P0 | ⬜ TODO | Est: Xd

**Checklist:**
- [ ] Implementation items
- [ ] Tests

When done, respond with: PLAN_READY
"""

    log_progress(f"📝 Planning: {description}")

    # Save prompt
    log_file = config.logs_dir / f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        f.write(f"=== PLAN PROMPT ===\n{prompt}\n\n")

    # Interactive loop
    conversation_history = []

    while True:
        # Run Claude
        try:
            cmd = [config.claude_command, "-p", prompt]
            if config.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            print("\n🤖 Claude is analyzing...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            output = result.stdout

            # Save output
            with open(log_file, "a") as f:
                f.write(f"=== OUTPUT ===\n{output}\n\n")

            # Check for API errors
            error_pattern = check_error_patterns(output + result.stderr)
            if error_pattern:
                print(f"\n⚠️  API error: {error_pattern}")
                return

            # Check for QUESTION
            question_match = re.search(r"QUESTION:\s*(.+?)(?:OPTIONS:|$)", output, re.DOTALL)
            if question_match:
                question = question_match.group(1).strip()
                print(f"\n❓ {question}")

                # Extract options
                options_match = re.search(r"OPTIONS:\s*(.+?)(?:$)", output, re.DOTALL)
                if options_match:
                    options_text = options_match.group(1)
                    options = re.findall(r"[-*]\s*(.+)", options_text)
                    if options:
                        print("\nOptions:")
                        for i, opt in enumerate(options, 1):
                            print(f"  {i}. {opt.strip()}")
                        print(f"  {len(options) + 1}. Other (type custom answer)")

                        choice = input("\nYour choice (number or text): ").strip()

                        # Determine answer
                        try:
                            idx = int(choice)
                            if 1 <= idx <= len(options):
                                answer = options[idx - 1].strip()
                            else:
                                answer = input("Enter your answer: ").strip()
                        except ValueError:
                            answer = choice

                        # Add to conversation
                        conversation_history.append(f"Q: {question}\nA: {answer}")
                        prompt = f"{prompt}\n\nPrevious Q&A:\n" + "\n".join(conversation_history)
                        prompt += f"\n\nContinue planning with the answer: {answer}"
                        continue

                # No parseable options, ask for freeform input
                answer = input("\nYour answer: ").strip()
                conversation_history.append(f"Q: {question}\nA: {answer}")
                prompt += f"\n\nAnswer: {answer}\n\nContinue planning."
                continue

            # Check for TASK_PROPOSAL or PLAN_READY
            if "PLAN_READY" in output or "TASK_PROPOSAL" in output:
                print("\n" + "=" * 60)
                print("📋 Proposed Tasks:")
                print("=" * 60)

                # Extract task proposals
                task_blocks = re.findall(
                    rf"### ({ID_PATTERN}:.+?)(?=### [A-Z][A-Z0-9]*-|\Z|PLAN_READY)",
                    output,
                    re.DOTALL,
                )

                for block in task_blocks:
                    print(f"\n### {block.strip()[:500]}")

                print("\n" + "=" * 60)

                # Ask for confirmation
                confirm = input("\nAdd these tasks to tasks.md? [y/N/edit]: ").strip().lower()
                apply_plan_confirmation(confirm, task_blocks, config)
                return

            # No recognizable signal, show output and exit
            print("\n📄 Claude response:")
            print(output[:2000])
            return

        except subprocess.TimeoutExpired:
            print(f"\n⏰ Planning timeout after {config.task_timeout_minutes}m")
            return
        except KeyboardInterrupt:
            print("\n\n❌ Cancelled by user")
            return
        except Exception as e:
            print(f"\n💥 Error: {e}")
            return
