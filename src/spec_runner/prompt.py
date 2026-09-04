"""
Prompt building for spec-runner task execution.

Builds prompts from task specs, templates, and previous attempt context
for use with Claude CLI and other LLM tools.
"""

import hashlib
import re
from importlib import resources
from pathlib import Path, PurePosixPath

from .config import ExecutorConfig
from .logging import get_logger
from .spec import LITE, StageDef, StageProfile, ancestor_stages
from .state import RetryContext, TaskAttempt
from .task import Task

logger = get_logger("prompt")


def _stage_def(stage: str, profile: StageProfile = LITE) -> StageDef:
    """Return the :class:`StageDef` for ``stage`` in ``profile`` (default lite).

    Args:
        stage: Stage name (e.g. 'requirements').
        profile: Stage profile to look up in; defaults to the ``lite`` profile.

    Raises:
        KeyError: If ``stage`` is not a stage of ``profile``.
    """
    for s in profile.stages:
        if s.name == stage:
            return s
    raise KeyError(stage)


def load_bundled_template(stage: str, profile: StageProfile = LITE) -> str:
    """Load the bundled rich template for a stage (importlib.resources).

    The template filename is read from the stage's :class:`StageDef`
    (``StageDef.template``) rather than a local map (DESIGN-305).

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        profile: Stage profile supplying the template filename (default lite).

    Returns:
        Template content as a string.
    """
    fname = _stage_def(stage, profile).template
    return (
        resources.files("spec_runner")
        .joinpath("skills", "spec-generator-skill", "templates", fname)
        .read_text(encoding="utf-8")
    )


def template_hash(stage: str, profile: StageProfile = LITE) -> str:
    """Return 'sha256:<hex>' content hash of the stage template.

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        profile: Stage profile supplying the template filename (default lite).

    Returns:
        SHA256 hash prefixed with 'sha256:'.
    """
    digest = hashlib.sha256(load_bundled_template(stage, profile).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


#: Backward-compatible export, now derived from the ``lite`` profile: keys,
#: order, markers, and instruction text all come from ``LITE.stages``
#: (DESIGN-302/DESIGN-305). Deprecated in favour of reading the ``StageDef``
#: fields directly for profile-aware callers.
SPEC_STAGES: dict[str, dict[str, str]] = {
    s.name: {"marker": s.marker_prefix, "instruction": s.prompt_text} for s in LITE.stages
}


def load_prompt_template(
    name: str,
    cli_name: str = "",
    *,
    prompts_dir: Path | None = None,
) -> str | None:
    """Load a project-owned prompt template from ``prompts_dir``.

    Precedence: a CLI-specific template (``review.codex.md``) beats the generic
    one (``review.md``/``review.txt``); a project template found here beats the
    caller's built-in prompt, wholesale — there is no merging of the two.

    ``prompts_dir`` is explicit and has **no default search path** (#153).
    Passing None means "this caller has no project directory to consult", not
    "look somewhere sensible": the previous module-level ``Path("spec/prompts")``
    resolved against the process CWD, so running against one project from
    inside another silently used the wrong templates. Callers pass
    ``config.prompts_dir``.

    Args:
        name: Template name without extension (e.g., 'task', 'review').
        cli_name: CLI name or path for CLI-specific templates (e.g. 'codex').
        prompts_dir: Directory to look in; None disables the lookup.

    Returns:
        Template content, or None when there is no project template.
    """
    if prompts_dir is None:
        return None

    candidates: list[Path] = []
    if cli_name:
        # Last path component only, so "/usr/bin/codex" matches review.codex.md.
        # Suffixes are deliberately kept rather than taken as `Path.stem`:
        # versioned binaries are the common case here and `claude-3.5` would
        # become `claude-3`, silently missing the template it names. (The
        # package is POSIX-only — `config.py` imports fcntl unconditionally —
        # so a Windows-style separator never reaches this.)
        cli_base = PurePosixPath(cli_name.lower()).name
        candidates += [
            prompts_dir / f"{name}.{cli_base}.md",
            prompts_dir / f"{name}.{cli_base}.txt",
        ]
    candidates += [prompts_dir / f"{name}{ext}" for ext in (".md", ".txt")]

    for path in candidates:
        if path.exists():
            # Which template answered decides what the agent was told, so it
            # belongs in the run's record, not just in the author's head.
            logger.info("Prompt template resolved", template=name, source=str(path))
            return _read_template(path)

    logger.info("Prompt template resolved", template=name, source="built-in")
    return None


def _read_template(path: Path) -> str:
    """Read and process template file."""
    content = path.read_text()

    # Strip comment lines (lines starting with #) only for .txt files
    if path.suffix == ".txt":
        lines = []
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("#"):
                lines.append(line)
        return "\n".join(lines).strip()

    return content.strip()


def render_template(template: str, variables: dict[str, str]) -> str:
    """Render template with variable substitution.

    Supports both {{VARIABLE}} and ${VARIABLE} placeholder syntax.

    Args:
        template: Template string with placeholders
        variables: Dict of variable names to values

    Returns:
        Rendered template string.
    """
    result = template
    for name, value in variables.items():
        # Support both {{VAR}} and ${VAR} syntax
        result = result.replace(f"{{{{{name}}}}}", value)
        result = result.replace(f"${{{name}}}", value)
    return result


#: U+200B ZERO WIDTH SPACE, written as an escape rather than as the character
#: itself (Copilot, PR #269). An invisible literal in source survives no
#: careless edit, no reformatting tool and no copy-paste through a terminal —
#: and losing it would silently put the marker tokens back into prompts, which
#: is the failure this constant exists to prevent.
ZERO_WIDTH_SPACE = "\u200b"


def neutralise_markers(text: str) -> str:
    """Quote terminal-marker tokens so a prompt cannot teach them back (#266).

    The trap in that finding is self-priming: an attempt is failed for a marker
    it only *mentioned*, the failure text quoting that token goes into the next
    attempt's prompt, and an honest agent summarising what came before utters
    it again. Line-anchored parsing breaks the first half; this breaks the
    second, because the tokens reach the agent only as history to describe.

    Both vocabularies (#270): a review's own findings are quoted into later
    prompts too, and a reviewer reading `REVIEW_FAILED` in the history it is
    given is in exactly the position the implementation agent was in.

    Split rather than removed: an operator reading the prompt still sees which
    marker is being talked about, and the agent still reads the word — it just
    cannot be echoed back as a line the parser would count. Applied only where
    **previous** output is quoted into a prompt; the instruction that asks for
    a marker is written by us and must stay verbatim.
    """
    from .runner import REVIEW_MARKERS, TERMINAL_MARKERS

    for marker in (*TERMINAL_MARKERS, *REVIEW_MARKERS):
        text = text.replace(marker, f"{marker[:-1]}{ZERO_WIDTH_SPACE}{marker[-1]}")
    return text


def format_error_summary(error: str, output: str | None = None, max_lines: int = 10) -> str:
    """Format a concise error summary for display.

    Args:
        error: Error message/type
        output: Full output (optional)
        max_lines: Max lines to show from output

    Returns:
        Formatted error summary string.
    """
    lines = [f"  ❌ Error: {error}"]

    if output:
        # Try to extract the most relevant part
        relevant_lines = []

        for line in output.split("\n"):
            line_lower = line.lower()
            # Look for error indicators
            if any(
                kw in line_lower
                for kw in [
                    "error",
                    "failed",
                    "exception",
                    "traceback",
                    "assert",
                    "expected",
                    "actual",
                    "typeerror",
                    "nameerror",
                    "valueerror",
                    "keyerror",
                    "attributeerror",
                ]
            ):
                relevant_lines.append(line.strip())

        if relevant_lines:
            lines.append("  📋 Key issues:")
            for line in relevant_lines[:max_lines]:
                if line:
                    lines.append(f"     • {line[:100]}")
            if len(relevant_lines) > max_lines:
                lines.append(f"     ... and {len(relevant_lines) - max_lines} more")
        else:
            # No specific errors found, show last few lines
            output_lines = [ln.strip() for ln in output.split("\n") if ln.strip()]
            if output_lines:
                lines.append("  📋 Last output:")
                for line in output_lines[-5:]:
                    lines.append(f"     {line[:100]}")

    return "\n".join(lines)


def extract_test_failures(output: str) -> str:
    """Extract relevant test failure info from pytest output."""
    lines = output.split("\n")
    result_lines = []
    in_failure = False
    failure_count = 0
    max_failures = 5  # Limit to avoid huge prompts

    for line in lines:
        # Capture FAILED lines
        if "FAILED" in line or "ERROR" in line:
            result_lines.append(line)
            failure_count += 1
            if failure_count >= max_failures:
                result_lines.append(f"... and more (showing first {max_failures})")
                break
        # Capture assertion errors
        elif "AssertionError" in line or "assert" in line.lower():
            result_lines.append(line)
        # Capture short summary
        elif "short test summary" in line.lower():
            in_failure = True
        elif in_failure and line.strip():
            result_lines.append(line)

    return "\n".join(result_lines[-30:]) if result_lines else output[-500:]


def _context_lines(spec_context: str | None) -> list[str]:
    """Return ``<context>`` block lines, or empty when no context (M0).

    Coerces to ``str`` so a mis-typed config value (e.g. ``spec_context: 123``)
    cannot crash prompt assembly at ``"\\n".join(...)``.
    """
    if not spec_context:
        return []
    return ["<context>", str(spec_context), "</context>", ""]


def _stage_rule_list(stage: str, spec_rules: dict[str, list[str]] | None) -> list[str]:
    """Return the coerced list of rules for ``stage``, tolerant of mis-typing.

    Guards against config that bypasses validation (e.g. ``plan`` without a
    prior ``validate``): a non-dict ``spec_rules`` yields no rules, and a
    stage whose value is a single string becomes one rule rather than one
    bullet per character.
    """
    if not isinstance(spec_rules, dict):
        return []
    rules = spec_rules.get(stage)
    if not rules:
        return []
    if not isinstance(rules, list):
        # A single string or any scalar (123, True) → one rule, never
        # iterated (a str would become one bullet per character; a non-str
        # scalar is not iterable at all).
        return [str(rules)]
    return [str(rule) for rule in rules]


def _rules_lines(stage: str, spec_rules: dict[str, list[str]] | None) -> list[str]:
    """Return ``<rules>`` block lines for ``stage`` only, or empty (M0)."""
    rules = _stage_rule_list(stage, spec_rules)
    if not rules:
        return []
    return ["<rules>", *[f"- {rule}" for rule in rules], "</rules>", ""]


def build_generation_prompt(
    stage: str,
    description: str,
    context: dict[str, str] | None = None,
    profile: StageProfile = LITE,
    spec_context: str | None = None,
    spec_rules: dict[str, list[str]] | None = None,
) -> str:
    """Build prompt for spec generation stage.

    The instruction text comes from the stage's :class:`StageDef`
    (``StageDef.prompt_text``) rather than a local map (DESIGN-305).

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        description: Project description from user.
        context: Previous stage outputs (e.g., {'requirements': '...'}).
        profile: Stage profile supplying the instruction text (default lite).
        spec_context: Optional project-wide context, injected as a
            ``<context>`` block after the instruction (M0). Falsy → omitted.
        spec_rules: Optional per-stage rules; only the entry matching
            ``stage`` is injected as a ``<rules>`` block (M0).
    """
    ctx = context or {}
    parts: list[str] = [_stage_def(stage, profile).prompt_text, ""]
    parts.extend(_context_lines(spec_context))
    parts.extend(_rules_lines(stage, spec_rules))
    parts.append(f"Project description: {description}")

    if "requirements" in ctx:
        parts.extend(["", "## Requirements (already generated)", ctx["requirements"]])
    if "design" in ctx:
        parts.extend(["", "## Design (already generated)", ctx["design"]])

    return "\n".join(parts)


def _prior_heading(stage: str, status: str) -> str:
    """Heading for one ancestor's context block, honest about its status.

    ``approved`` keeps the historical wording so existing prompts (and the
    C1 zero-behaviour goldens) are unchanged; anything else is marked
    unapproved with its real status.
    """
    if status == "approved":
        return f"## Approved {stage}"
    return f"## Unapproved {stage} ({status})"


def build_gated_generation_prompt(
    stage: str,
    description: str,
    context: dict[str, str],
    profile: StageProfile = LITE,
    spec_context: str | None = None,
    spec_rules: dict[str, list[str]] | None = None,
    statuses: dict[str, str] | None = None,
    repair_errors: list[str] | None = None,
) -> str:
    """Build a rich, template-driven generation prompt for one gated stage.

    Combines role instructions, the full bundled template for the stage,
    the project description, the outputs of the stage's transitive ancestors
    (e.g. requirements when generating design), and the
    ``<MARKER_PREFIX>_READY``/``_END`` markers the caller uses to extract the
    result.

    Args:
        stage: Any stage name of ``profile`` — not limited to the ``lite``
            chain ('requirements'/'design'/'tasks'); custom profiles supply
            their own stage names.
        description: Project description from the user.
        context: Upstream stage outputs, keyed by stage name
            (e.g. {'requirements': '...'}). Ancestors absent from the mapping,
            or mapped to an empty body, are skipped.
        profile: Stage profile supplying the marker prefix and template
            (default lite).
        spec_context: Optional project-wide context, injected as a
            ``<context>`` block after the header (M0). Falsy → omitted.
        spec_rules: Optional per-stage rules; only the entry matching
            ``stage`` is injected as a ``<rules>`` block (M0).
        statuses: Optional ``{stage: status}`` for the context entries, used
            to label each block honestly. A stage that is missing here is
            assumed approved, so callers that do not track statuses render
            exactly as before.
        repair_errors: Validation errors from a previous attempt at this same
            stage (#160). Rendered verbatim: "try again" is not a diagnostic,
            and the model needs the specific rule it broke. Omitted entirely on
            a first attempt, so that prompt is byte-identical to before.

    Returns:
        The assembled prompt string.
    """
    stage_def = _stage_def(stage, profile)
    marker = stage_def.marker_prefix
    template = load_bundled_template(stage, profile)

    # The prompt context is the full transitive ancestor closure (every stage
    # this one depends on, directly or through another), not just the direct
    # `requires` gated by the caller — a "tasks" stage needs to trace back to
    # "requirements" even though its only direct requirement is "design".
    # Each block is labelled by the ancestor's ACTUAL status. The gate checks
    # only a stage's direct `requires`, so an ancestor further back can be a
    # draft (reachable via `spec reject`, which does not cascade stale). Its
    # body still belongs here — dropping it would lose the traceability the
    # template asks for — but presenting a draft as "Approved" would lie to
    # the model. Callers that pass no ``statuses`` render exactly as before.
    prior_parts = [
        f"{_prior_heading(prior, (statuses or {}).get(prior, 'approved'))}\n\n{context[prior]}"
        for prior in ancestor_stages(stage, profile)
        if context.get(prior)
    ]
    prior_block = "\n\n".join(prior_parts)

    header = (
        f"You are generating the '{stage}' spec document. Fill the TEMPLATE below "
        "from the DESCRIPTION" + (" and any approved upstream stages" if prior_block else "") + ". "
        "Do not invent or drop sections. Out of Scope is mandatory; acceptance "
        "criteria use GIVEN-WHEN-THEN"
        + (
            "; add [REQ-XXX]/[DESIGN-XXX] traceability where the template calls for it."
            if prior_block
            else "."
        )
    )

    parts = [header]
    if spec_context:
        parts.append(f"<context>\n{spec_context}\n</context>")
    stage_rules = _stage_rule_list(stage, spec_rules)
    if stage_rules:
        parts.append("<rules>\n" + "\n".join(f"- {rule}" for rule in stage_rules) + "\n</rules>")
    parts.append(f"## DESCRIPTION\n\n{description}")
    if repair_errors:
        parts.append(
            "## PREVIOUS ATTEMPT FAILED VALIDATION\n\n"
            "Your previous attempt at this stage was rejected and NOT saved. "
            "Fix exactly these problems; everything else about the task is "
            "unchanged:\n\n" + "\n".join(f"- {err}" for err in repair_errors)
        )
    if prior_block:
        parts.append(prior_block)
    parts.append(f"## TEMPLATE\n\n{template}")
    parts.append(
        "When done, output ONLY the finished document between markers:\n"
        f"{marker}_READY\n<document>\n{marker}_END"
    )
    return "\n\n".join(parts)


def parse_spec_marker(output: str, marker_name: str) -> str | None:
    """Extract content between SPEC_{NAME}_READY and SPEC_{NAME}_END markers.

    Args:
        output: Raw Claude CLI output.
        marker_name: One of REQUIREMENTS, DESIGN, TASKS.

    Returns:
        Extracted content or None if markers not found.
    """
    start = f"SPEC_{marker_name}_READY"
    end = f"SPEC_{marker_name}_END"
    start_idx = output.find(start)
    if start_idx == -1:
        return None
    start_idx += len(start)
    end_idx = output.find(end, start_idx)
    if end_idx == -1:
        return None
    return output[start_idx:end_idx].strip()


def _parse_stage_marker(output: str, stage: StageDef) -> str | None:
    """Extract content between a stage's own ``{marker_prefix}_READY``/``_END``.

    ``StageDef.marker_prefix`` is the complete prefix (e.g. ``SPEC_TASKS``),
    unlike :func:`parse_spec_marker`, which prepends ``SPEC_`` to a bare name
    and stays unchanged for backward compatibility.
    """
    start_marker = f"{stage.marker_prefix}_READY"
    end_marker = f"{stage.marker_prefix}_END"
    start = output.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    end = output.find(end_marker, start)
    if end == -1:
        return None
    return output[start:end].strip()


def _evidential_file(task: "Task", config: "ExecutorConfig") -> str:
    """The path the RED pass is asked to write, named by the **adapter** (#252).

    Not a shared heuristic: a Python-shaped guess told an Elixir project to
    write `tests/test_x_red.py`, which `mix test` never collects — the same
    class of mistake as #198's selector and #220's linter.

    The sentence the prompt builds from this and the check that follows say the
    same thing. The prompt names a file that does not exist; the check refuses
    a claimed file that existed at the baseline. What it cannot do is state a
    path when no adapter is resolved — there the rule is stated without an
    example, because inventing one would be the heuristic this avoids.
    """
    from .tdd import resolve_adapter, resolve_namespace

    adapter = resolve_adapter(config)
    if adapter is None:
        return "(a new test file this project's runner collects)"
    return f"`{adapter.evidential_file(task.id, namespace=resolve_namespace(config))}`"


def _selector_instruction(config: "ExecutorConfig") -> str:
    """How to ask this project's runner for a selector (#198).

    The prompt used to hardcode pytest's node id. On an ExUnit project the
    agent would comply with that *shape* — and `path::name` is exactly what the
    ExUnit adapter refuses, so every RED would have died `unverifiable` before
    a line of implementation. Found before the first paid pilot run, by reading
    the prompt the agent would receive.
    """
    from .tdd import resolve_adapter

    adapter = resolve_adapter(config)
    if adapter is None:
        return (
            "TDD_SELECTOR: <the selector your test runner accepts>\n"
            "\n"
            "   (This project's runner is not one spec-runner can verify, so the\n"
            "   red cannot be confirmed — see `tdd_runner` in the docs.)"
        )
    return adapter.selector_instruction


def build_red_prompt(task: "Task", config: "ExecutorConfig") -> str:
    """Prompt for the RED authoring pass, with the frozen-files block appended.

    The authoring pass is told too, not only the implementation: a namespace
    can hold another workstream's claims, and `check_claims` judges all of
    them. A red authored on top of a neighbour's frozen test is a candidate
    the gate will refuse just as surely.
    """
    from .claims import append_frozen_files

    return append_frozen_files(_render_red_prompt(task, config), config, task)


def _render_red_prompt(task: "Task", config: "ExecutorConfig") -> str:
    """The RED prompt's body (#141): write the failing test only.

    Two instructions carry the contract. **Write no implementation** — a red
    that passes because the code was written alongside it demonstrates
    nothing. And **report the full node id** via a marker: the selector is the
    checkpoint's most load-bearing field, and inferring it from a diff would
    make it a heuristic. The claim is replayed either way, so a wrong or
    missing selector fails verification rather than sliding through.
    """
    requirements = ""
    if config.requirements_file.exists():
        requirements = config.requirements_file.read_text()

    related = []
    for ref in task.traces_to:
        if ref.startswith("REQ-"):
            match = re.search(rf"#### {ref}:.*?(?=####|\Z)", requirements, re.DOTALL)
            if match:
                related.append(match.group(0).strip())

    checklist = "\n".join(f"- {'[x]' if done else '[ ]'} {item}" for item, done in task.checklist)
    context = "\n\n".join(related)

    return f"""# RED phase: {task.id} — {task.name}

You are writing **one failing test** and nothing else.

## Task

{task.description or task.name}

{f"## Checklist{chr(10)}{chr(10)}{checklist}" if checklist else ""}

{f"## Requirements{chr(10)}{chr(10)}{context}" if context else ""}

## Rules

1. Write **exactly one** test that fails because the behaviour does not exist yet.
2. Write **no implementation**. A test that passes because you also wrote the
   code proves nothing, and this test will be replayed on its own commit.
3. The test must fail for the *right* reason — an assertion about the missing
   behaviour, not an import error or a syntax error. A test that cannot be
   collected demonstrates nothing.
4. Write it in a **new file that does not exist yet**: {_evidential_file(task, config)}
   The file this test lives in is frozen byte-for-byte until the task finishes,
   so a test added to an existing file would freeze work the implementation
   still needs. A red written into a file that already existed is refused.
5. Report the test's selector on its own line, exactly:

   {_selector_instruction(config)}

The test command is: `{config.test_command}`
"""


def build_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
    retry_context: RetryContext | None = None,
) -> str:
    """The implementation prompt, with the frozen-files block appended (#214).

    The body is rendered first — built-in or custom template, it makes no
    difference — and the block is appended to whatever came out. The pilot's
    GREEN pass received exactly the prompt a `standard` task gets, wrote four
    more tests into the file the red had frozen, and the claims gate refused
    the merge: $1.3 of implementation spent on a candidate rejected for a rule
    the agent was never given.

    A single wrapper rather than a line in each branch, because "remember to
    append it in the template path too" is precisely the kind of instruction
    that gets forgotten when a branch is added.
    """
    from .claims import append_frozen_files

    body = _render_task_prompt(task, config, previous_attempts, retry_context)
    return append_frozen_files(body, config, task)


def _render_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
    retry_context: RetryContext | None = None,
) -> str:
    """Build prompt for Claude with task context and previous attempt info."""

    # Read specifications
    requirements = ""
    if config.requirements_file.exists():
        requirements = config.requirements_file.read_text()

    design = ""
    if config.design_file.exists():
        design = config.design_file.read_text()

    # Find related requirements
    related_reqs = []
    for ref in task.traces_to:
        if ref.startswith("REQ-"):
            # Extract requirement from requirements.md
            pattern = rf"#### {ref}:.*?(?=####|\Z)"
            match = re.search(pattern, requirements, re.DOTALL)
            if match:
                related_reqs.append(match.group(0).strip())

    # Find related design
    related_design = []
    for ref in task.traces_to:
        if ref.startswith("DESIGN-"):
            pattern = rf"### {ref}:.*?(?=###|\Z)"
            match = re.search(pattern, design, re.DOTALL)
            if match:
                related_design.append(match.group(0).strip())

    # Checklist
    checklist_text = "\n".join(
        [f"- {'[x]' if done else '[ ]'} {item}" for item, done in task.checklist]
    )

    # Build retry context section
    attempts_section = ""
    if retry_context:
        attempts_section = (
            f"\n## \u26a0\ufe0f RETRY \u2014 Attempt {retry_context.attempt_number}"
            f" of {retry_context.max_attempts}\n\n"
            f"**Error type:** {retry_context.previous_error_code.value}\n"
            f"**What was tried:** {neutralise_markers(retry_context.what_was_tried)}\n"
            f"**Error:** {neutralise_markers(retry_context.previous_error)}\n"
        )
        if retry_context.test_failures:
            attempts_section += f"\n**Test failures:**\n```\n{retry_context.test_failures}\n```\n"
        attempts_section += (
            "\n**IMPORTANT:** Review the error above and fix the issue. "
            "Do not repeat the same mistake.\n\n"
        )
    elif previous_attempts:
        # Fallback: keep existing raw attempts logic for backward compat
        failed_attempts = [a for a in previous_attempts if not a.success]
        if failed_attempts:
            max_attempts_context = 2
            max_attempts_chars = 30_000
            recent = failed_attempts[-max_attempts_context:]
            attempts_section = (
                f"\n## \u26a0\ufe0f PREVIOUS ATTEMPTS FAILED "
                f"({len(failed_attempts)} total, showing last "
                f"{len(recent)}):\n\n"
            )
            for i, attempt in enumerate(recent, len(failed_attempts) - len(recent) + 1):
                attempts_section += f"### Attempt {i} (failed):\n"
                if attempt.error:
                    error_text = neutralise_markers(attempt.error[:2000])
                    attempts_section += f"**Error:** {error_text}\n\n"
                if attempt.claude_output:
                    failures = extract_test_failures(attempt.claude_output)
                    if failures:
                        attempts_section += f"**Test failures:**\n```\n{failures}\n```\n\n"

            if len(attempts_section) > max_attempts_chars:
                attempts_section = attempts_section[:max_attempts_chars] + "\n...(truncated)\n"

            attempts_section += (
                "**IMPORTANT:** Review the errors above and fix the issues. "
                "Do not repeat the same mistakes.\n\n"
            )

    # Load constitution guardrails (if present)
    constitution = ""
    if config.constitution_file.exists():
        constitution = config.constitution_file.read_text().strip()

    # Load implementer persona system prompt (if configured)
    persona_prompt = ""
    implementer = config.get_persona("implementer")
    if implementer and implementer.system_prompt:
        persona_prompt = implementer.system_prompt.strip()

    # Try to load custom template
    template = load_prompt_template("task", prompts_dir=config.prompts_dir)

    if template:
        # Use custom template with variable substitution
        variables = {
            "TASK_ID": task.id,
            "TASK_NAME": task.name,
            "PRIORITY": task.priority.upper(),
            "ESTIMATE": task.estimate or "TBD",
            "MILESTONE": task.milestone or "N/A",
            "CHECKLIST": checklist_text,
            "RELATED_REQS": "\n".join(related_reqs)
            if related_reqs
            else f"See {config.requirements_file}",
            "RELATED_DESIGN": "\n".join(related_design)
            if related_design
            else f"See {config.design_file}",
            "PREVIOUS_ATTEMPTS": attempts_section,
            "CONSTITUTION": constitution,
            "PERSONA_PROMPT": persona_prompt,
        }
        return render_template(template, variables)

    # Fallback to built-in prompt
    prompt = f"""{persona_prompt + chr(10) + chr(10) if persona_prompt else ""}# Task Execution Request

## Task: {task.id} — {task.name}

**Priority:** {task.priority.upper()}
**Estimate:** {task.estimate}
**Milestone:** {task.milestone}

## Checklist (implement ALL items):

{checklist_text}

## Related Requirements:

{chr(10).join(related_reqs) if related_reqs else f"See {config.requirements_file}"}

## Related Design:

{chr(10).join(related_design) if related_design else f"See {config.design_file}"}

{"## Constitution (Inviolable Rules):" + chr(10) + chr(10) + constitution + chr(10) + chr(10) if constitution else ""}## Instructions:

1. Implement ALL checklist items for this task
2. Write unit tests for new code (coverage ≥80%)
3. Follow the design patterns from {config.design_file}
4. Use existing code style and conventions
5. Create/update files as needed

## Dependencies:

- To add a new dependency: `uv add <package>`
- To add a dev dependency: `uv add --dev <package>`
- NEVER edit pyproject.toml manually for dependencies
- After adding dependencies, they are available immediately

## Success Criteria:

- All checklist items implemented
- All tests pass (`uv run pytest`)
- No lint errors (`uv run ruff check .`)
- Code follows project conventions

## Output:

When complete, respond with:
- Summary of changes made
- Files created/modified
- Any issues or notes
- "TASK_COMPLETE" if successful, or "TASK_FAILED: <reason>" if not

If the task cannot be done within the rules you were given — the only correct
next step is forbidden to you and needs a human — say "TASK_BLOCKED: <reason>"
instead of "TASK_FAILED". That is not a lesser outcome: it is the honest one,
it is not retried, and the reason goes to the operator verbatim. Use it rather
than looking for a way around a constraint you were told to respect.

{attempts_section}
Begin implementation:
"""

    return prompt
