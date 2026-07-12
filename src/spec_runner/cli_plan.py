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
    build_gated_generation_prompt,
    load_prompt_template,
    parse_spec_marker,
    render_template,
    template_hash,
)
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
)
from .spec import (
    STAGES,
    SpecMeta,
    read_spec_body,
    read_spec_meta,
    resolve_next_stage,
    stage_path,
    write_spec,
)
from .task import (
    parse_tasks,
)
from .validate import validate_spec_stage, verdict_from_result

logger = get_logger("cli")

_MARKER = {"requirements": "REQUIREMENTS", "design": "DESIGN", "tasks": "TASKS"}
_UPSTREAM: dict[str, list[str]] = {
    "requirements": [],
    "design": ["requirements"],
    "tasks": ["requirements", "design"],
}


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
    context: dict[str, str] = {}
    for upstream in _UPSTREAM[stage]:
        meta = read_spec_meta(stage_path(config, upstream))
        if meta is None or meta.status != "approved":
            print(f"⛔ cannot generate {stage}: {upstream} must be APPROVED first")
            return 2
        context[upstream] = read_spec_body(stage_path(config, upstream))

    prompt = build_gated_generation_prompt(stage, description, context)
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
        return 1

    body = parse_spec_marker(result.stdout, _MARKER[stage])
    if not body:
        print(f"no {stage} content produced (marker missing)")
        return 1

    path = stage_path(config, stage)
    existing = read_spec_meta(path)
    version = existing.version if existing is not None else 1
    meta = SpecMeta(
        spec_stage=stage,
        status="draft",
        version=version,
        generated_by=f"{_harness(config)}@{config.claude_model or 'default'}",
        generated_at=_now_iso(),
        source_prompt_version=template_hash(stage),
    )
    lock = ExecutorLock(config.spec_lock_file)
    write_spec(path, meta, body.rstrip("\n") + "\n", lock=lock)

    verdict = verdict_from_result(validate_spec_stage(stage, config))
    meta.validation = verdict
    write_spec(path, meta, read_spec_body(path), lock=lock)

    print(f"{stage}.md written as DRAFT — validation={verdict}")
    if verdict == "fail":
        print("  fix the errors, then `spec approve` (approve will re-validate)")
    else:
        print(f"  approve with: spec-runner spec approve {stage}")
    return 0


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
    pipeline is done, a stage is stale, or a stage awaits approval); False
    when `action == "generate"` (the caller should proceed to generate it).
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
    return False


def _current_metas(config) -> dict[str, SpecMeta | None]:
    """Read the current `SpecMeta` for every gated-pipeline stage."""
    return {s: read_spec_meta(stage_path(config, s)) for s in STAGES}


def resolve_plan_description(description: str | None, from_file: str | None) -> str:
    """Resolve the plan description from --from-file (preferred) or the positional
    argument. Exits with an error if neither is usable.

    Args:
        description: the positional description (may be None when --from-file is used).
        from_file: path to a file whose contents are the description.
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
    raise SystemExit("plan: provide a description argument or --from-file PATH")


_TASK_HEADER_VARIANT = re.compile(r"^#{2,4} (TASK-\d+)\s*[—–:-]\s*(.+)$", re.MULTILINE)


def normalize_task_headers(text: str) -> str:
    """Normalize recoverable task-header variants to the parseable form.

    Governed-run finding H-2b: the generation LLM systematically emits
    variants like ``### TASK-001 — Title`` (em-dash) or an h2 heading despite
    the template. Anything shaped like a task header is rewritten to the
    canonical ``### TASK-NNN: Title`` the run parser requires; genuinely
    unrecoverable output is still caught by validate_generated_tasks.
    """
    return _TASK_HEADER_VARIANT.sub(lambda m: f"### {m.group(1)}: {m.group(2)}", text)


def validate_generated_tasks(tasks_file: Path) -> int:
    """Ensure a generated tasks.md parses with the runner's own parser.

    Returns the parsed task count; exits 1 when zero tasks parse (the file is
    left in place for debugging). Guards the plan->run format contract:
    task headers must match ``^### (TASK-\\d+): `` (task.py TASK_HEADER).
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
    return len(parsed)


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude.

    With --full flag, runs a three-stage pipeline to generate
    requirements, design, and tasks files from a description.
    """

    description = resolve_plan_description(args.description, getattr(args, "from_file", None))

    if getattr(args, "gated", False):
        explicit_stage = getattr(args, "stage", None)
        if explicit_stage:
            # Single-stage request: never auto-continue, never show the menu —
            # this is the same behavior regardless of TTY/--no-interactive.
            raise SystemExit(run_gated_stage(explicit_stage, description, config))

        interactive = sys.stdout.isatty() and not getattr(args, "no_interactive", False)

        if not interactive:
            action, stage = resolve_next_stage(_current_metas(config))
            if _print_gate_status(action, stage):
                return
            raise SystemExit(run_gated_stage(stage, description, config))

        # Interactive auto-continue: generate -> checkpoint menu -> next stage.
        # Terminates in at most len(STAGES) generate-iterations: each generated
        # stage flips from missing to draft, so resolve_next_stage can never
        # return "generate" for the same stage twice; a stop/await/stale/done
        # resolves to a terminal action that _print_gate_status breaks on at
        # the top of the loop.
        while True:
            action, stage = resolve_next_stage(_current_metas(config))
            if _print_gate_status(action, stage):
                break
            rc = run_gated_stage(stage, description, config, interactive=True)
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
            prompt = build_generation_prompt(stage, description, context)

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
                normalized = normalize_task_headers(content)
                if normalized != content:
                    logger.info("Task headers normalized", stage=stage)
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
    template = load_prompt_template("plan")

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
                    r"### (TASK-\d+:.+?)(?=### TASK-|\Z|PLAN_READY)",
                    output,
                    re.DOTALL,
                )

                for block in task_blocks:
                    print(f"\n### {block.strip()[:500]}")

                print("\n" + "=" * 60)

                # Ask for confirmation
                confirm = input("\nAdd these tasks to tasks.md? [y/N/edit]: ").strip().lower()

                if confirm == "y":
                    # Append tasks to tasks.md
                    tasks_file = config.tasks_file
                    content = tasks_file.read_text() if tasks_file.exists() else "# Tasks\n\n"

                    for block in task_blocks:
                        content += f"\n### {block.strip()}\n"

                    tasks_file.write_text(content)
                    print(f"\n✅ Added {len(task_blocks)} task(s) to {tasks_file}")
                    log_progress(f"✅ Created {len(task_blocks)} tasks")

                elif confirm == "edit":
                    print(f"\nEdit {config.tasks_file} manually, then run 'spec-runner run'")

                else:
                    print("\n❌ Cancelled")

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
