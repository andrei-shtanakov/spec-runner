"""
Prompt building for spec-runner task execution.

Builds prompts from task specs, templates, and previous attempt context
for use with Claude CLI and other LLM tools.
"""

import hashlib
import re
from importlib import resources
from pathlib import Path

from .config import ExecutorConfig
from .spec import LITE, StageDef, StageProfile
from .state import RetryContext, TaskAttempt
from .task import Task

PROMPTS_DIR = Path("spec/prompts")


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


def load_prompt_template(name: str, cli_name: str = "") -> str | None:
    """Load prompt template from spec/prompts/ directory.

    Tries to load CLI-specific template first (e.g., review.codex.md),
    then falls back to generic template (e.g., review.md or review.txt).

    Args:
        name: Template name without extension (e.g., 'task', 'review')
        cli_name: CLI name for CLI-specific templates (e.g., 'codex', 'claude')

    Returns:
        Template content, or None if not found.
    """
    # Try CLI-specific template first
    if cli_name:
        cli_lower = cli_name.lower()
        # Extract base CLI name (e.g., "codex" from "/usr/bin/codex")
        cli_base = cli_lower.split("/")[-1]

        # Try different CLI-specific patterns
        for pattern in [f"{name}.{cli_base}.md", f"{name}.{cli_base}.txt"]:
            template_path = PROMPTS_DIR / pattern
            if template_path.exists():
                return _read_template(template_path)

    # Try generic templates
    for ext in [".md", ".txt"]:
        template_path = PROMPTS_DIR / f"{name}{ext}"
        if template_path.exists():
            return _read_template(template_path)

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


_PRIOR_FOR: dict[str, list[str]] = {
    "requirements": [],
    "design": ["requirements"],
    "tasks": ["requirements", "design"],
}


def build_gated_generation_prompt(
    stage: str,
    description: str,
    context: dict[str, str],
    profile: StageProfile = LITE,
    spec_context: str | None = None,
    spec_rules: dict[str, list[str]] | None = None,
) -> str:
    """Build a rich, template-driven generation prompt for one gated stage.

    Combines role instructions, the full bundled template for the stage,
    the project description, any approved upstream stage outputs (e.g.
    requirements when generating design), and the ``SPEC_<STAGE>_READY``/
    ``_END`` markers the caller uses to extract the result.

    Args:
        stage: One of 'requirements', 'design', 'tasks'.
        description: Project description from the user.
        context: Approved upstream stage outputs, keyed by stage name
            (e.g. {'requirements': '...'}).
        profile: Stage profile supplying the marker prefix and template
            (default lite).
        spec_context: Optional project-wide context, injected as a
            ``<context>`` block after the header (M0). Falsy → omitted.
        spec_rules: Optional per-stage rules; only the entry matching
            ``stage`` is injected as a ``<rules>`` block (M0).

    Returns:
        The assembled prompt string.
    """
    marker = _stage_def(stage, profile).marker_prefix
    template = load_bundled_template(stage, profile)

    prior_parts = [
        f"## Approved {prior}\n\n{context[prior]}"
        for prior in _PRIOR_FOR[stage]
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


def build_task_prompt(
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
            f"**What was tried:** {retry_context.what_was_tried}\n"
            f"**Error:** {retry_context.previous_error}\n"
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
                    error_text = attempt.error[:2000]
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
    template = load_prompt_template("task")

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

{attempts_section}
Begin implementation:
"""

    return prompt
