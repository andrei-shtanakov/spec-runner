"""GitHub Issues sync — create, update, close issues from tasks.md."""

import json
import re
import subprocess
from pathlib import Path

from .task import (
    STATUS_EMOJI,
    Task,
    update_task_status,
)


def _gh_run(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run a gh CLI command. Raises FileNotFoundError if gh is missing."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=capture,
        text=True,
        check=False,
    )


def _get_existing_issues() -> dict[str, dict]:
    """Fetch existing [TASK-XXX] issues from GitHub. Returns {task_id: issue_dict}."""
    result = _gh_run(
        ["issue", "list", "--state", "all", "--json", "number,title,state,labels", "--limit", "200"]
    )
    if result.returncode != 0:
        return {}
    issues = json.loads(result.stdout)
    mapping: dict[str, dict] = {}
    for issue in issues:
        m = re.match(r"\[(TASK-\d+)\]", issue["title"])
        if m:
            mapping[m.group(1)] = issue
    return mapping


def _task_labels(task: Task) -> list[str]:
    """Build label list for a task."""
    return [f"priority:{task.priority}", f"status:{task.status}"]


def _task_body(task: Task) -> str:
    """Build issue body from task."""
    parts: list[str] = []
    if task.estimate:
        parts.append(f"**Estimate:** {task.estimate}")
    if task.checklist:
        parts.append("**Checklist:**")
        for item, checked in task.checklist:
            mark = "x" if checked else " "
            parts.append(f"- [{mark}] {item}")
    if task.depends_on:
        parts.append(f"\n**Depends on:** {', '.join(task.depends_on)}")
    if task.traces_to:
        parts.append(f"**Traces to:** {', '.join(task.traces_to)}")
    return "\n".join(parts)


def cmd_sync_to_gh(args, tasks: list[Task]):
    """Sync tasks to GitHub Issues. Creates, updates, or closes issues."""
    dry_run = getattr(args, "dry_run", False)

    try:
        existing = _get_existing_issues()
    except FileNotFoundError:
        print("Error: 'gh' CLI not found. Install from https://cli.github.com/")
        return

    created, updated, closed = 0, 0, 0

    for task in tasks:
        issue = existing.get(task.id)
        labels = _task_labels(task)
        label_str = ",".join(labels)

        if task.status == "done":
            if issue and issue["state"] == "OPEN":
                if not dry_run:
                    _gh_run(["issue", "close", str(issue["number"])])
                closed += 1
            continue

        if issue:
            if not dry_run:
                _gh_run(["issue", "edit", str(issue["number"]), "--add-label", label_str])
                if issue["state"] == "CLOSED":
                    _gh_run(["issue", "reopen", str(issue["number"])])
            updated += 1
        else:
            title = f"[{task.id}] {task.name}"
            body = _task_body(task)
            if not dry_run:
                _gh_run(
                    [
                        "issue",
                        "create",
                        "--title",
                        title,
                        "--body",
                        body,
                        "--label",
                        label_str,
                    ]
                )
            created += 1

    action = "Would" if dry_run else "Done"
    print(f"{action}: created={created}, updated={updated}, closed={closed}")


def _status_from_issue(issue: dict) -> str:
    """Derive task status from GitHub issue state + labels."""
    if issue["state"] == "CLOSED":
        return "done"
    for label in issue.get("labels", []):
        name = label["name"] if isinstance(label, dict) else label
        if name.startswith("status:"):
            status = name.split(":", 1)[1]
            if status in STATUS_EMOJI:
                return status
    return "todo"


def cmd_sync_from_gh(args, tasks: list[Task], tasks_file: Path):
    """Sync GitHub Issues state back to tasks.md."""
    try:
        result = _gh_run(
            [
                "issue",
                "list",
                "--state",
                "all",
                "--json",
                "number,title,state,labels",
                "--limit",
                "200",
            ]
        )
    except FileNotFoundError:
        print("Error: 'gh' CLI not found. Install from https://cli.github.com/")
        return

    if result.returncode != 0:
        print(f"Error: gh issue list failed: {result.stderr}")
        return

    issues = json.loads(result.stdout)

    status_map: dict[str, str] = {}
    for issue in issues:
        m = re.match(r"\[(TASK-\d+)\]", issue["title"])
        if m:
            status_map[m.group(1)] = _status_from_issue(issue)

    updated = 0
    for task in tasks:
        new_status = status_map.get(task.id)
        if (
            new_status
            and new_status != task.status
            and update_task_status(tasks_file, task.id, new_status)
        ):
            updated += 1
            print(f"  {task.id}: {task.status} -> {new_status}")

    print(f"Updated {updated} task(s) from GitHub Issues.")


def export_gh(args, tasks: list[Task]):
    """Export to GitHub Issues format."""
    print("# GitHub Issues Export\n")
    print("Execute commands to create issues:\n")
    print("```bash")

    for task in tasks:
        if task.status == "done":
            continue

        labels = f"priority:{task.priority}"
        if task.milestone:
            labels += f",milestone:{task.milestone.lower().replace(' ', '-')}"

        body = f"**Estimate:** {task.estimate or 'TBD'}\\n\\n"
        if task.checklist:
            body += "**Checklist:**\\n"
            for item, checked in task.checklist:
                mark = "x" if checked else " "
                body += f"- [{mark}] {item}\\n"

        if task.depends_on:
            body += f"\\n**Depends on:** {', '.join(task.depends_on)}"

        cmd = f'gh issue create --title "{task.id}: {task.name}" --body "{body}" --label "{labels}"'
        print(cmd)

    print("```")
