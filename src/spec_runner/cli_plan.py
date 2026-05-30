"""CLI plan command: interactive task planning via Claude."""

import re
import subprocess
import sys
from datetime import datetime

from .config import ExecutorConfig
from .logging import get_logger
from .prompt import (
    load_prompt_template,
    render_template,
)
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
)
from .task import (
    parse_tasks,
)

logger = get_logger("cli")


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude.

    With --full flag, runs a three-stage pipeline to generate
    requirements, design, and tasks files from a description.
    """

    description = args.description

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

            output_file = stage_files[stage]
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(content + "\n")
            logger.info("Spec written", stage=stage, file=str(output_file))
            print(f"Written: {output_file}")

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
