"""
Prompt building for spec-runner task execution.

Builds prompts from task specs, templates, and previous attempt context
for use with Claude CLI and other LLM tools.
"""

import re
from pathlib import Path

from .config import ExecutorConfig
from .state import TaskAttempt
from .task import Task

PROMPTS_DIR = Path("spec/prompts")


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


def build_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
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

    # Build previous attempts section (keep last 2 to avoid prompt overflow)
    attempts_section = ""
    max_attempts_context = 2
    max_attempts_chars = 30_000
    if previous_attempts:
        failed_attempts = [a for a in previous_attempts if not a.success]
        if failed_attempts:
            # Only include the most recent failures
            recent = failed_attempts[-max_attempts_context:]
            attempts_section = (
                f"\n## ⚠️ PREVIOUS ATTEMPTS FAILED "
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

            # Hard cap on total attempts context size
            if len(attempts_section) > max_attempts_chars:
                attempts_section = attempts_section[:max_attempts_chars] + "\n...(truncated)\n"

            attempts_section += (
                "**IMPORTANT:** Review the errors above and fix the issues. "
                "Do not repeat the same mistakes.\n\n"
            )

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
        }
        return render_template(template, variables)

    # Fallback to built-in prompt
    prompt = f"""# Task Execution Request

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

## Instructions:

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
