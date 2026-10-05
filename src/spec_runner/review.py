"""Code review module for spec-runner.

Contains review role definitions, prompt building, single and parallel
code review execution, and HITL approval gate functions.
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .budget import budget_is_active
from .config import ExecutorConfig, command_has_executable, format_check_instrument_error
from .git_ops import stage_all_except_runtime
from .harness import harness_violations, snapshot_harness
from .logging import get_logger
from .paid_call import _LEDGER_LOCK as paid_call_lock
from .paid_call import scope as paid_call_scope
from .prompt import load_prompt_template, neutralise_markers, render_template
from .prompts_log import append_not_started, append_output, log_prompt
from .runner import (
    AgentAnswer,
    CliResult,
    agent_env,
    check_error_patterns,
    classify_agent_answer,
    log_progress,
    review_markers,
)
from .state import ReviewVerdict
from .task import Task

logger = get_logger("review")


def _review_fix_format_failure(config: ExecutorConfig) -> str | None:
    """Run the read-only format gate before a reviewer mutation is committed."""
    if not config.run_lint_on_done or not config.format_check_command:
        return None
    if not command_has_executable(config.format_check_command):
        return "Format check command has no executable"
    result = subprocess.run(
        config.format_check_command,
        shell=True,
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0:
        return None
    if result.returncode == 1 and not config.lint_blocking:
        logger.warning("Format drift after review fix is advisory")
        return None
    detail = (result.stdout + result.stderr)[-500:]
    kind = (
        "infrastructure error"
        if format_check_instrument_error(result.returncode)
        else "formatting drift"
    )
    return f"Format check {kind} after review fix: {detail}"


#: What a reviewer's output says, once. Both review paths ask this — the
#: sequential one and the per-role one — because they used to answer it
#: differently: the first tried PASSED → FIXED → FAILED, the second FAILED →
#: FIXED → PASSED. A reviewer that stated two of them was therefore recorded
#: *passed* when roles ran one at a time and *failed* when they ran together —
#: and what makes them run one at a time is an active budget (#213). The
#: verdict depended on how much money was left (#270).
@dataclass(frozen=True)
class ReviewSignal:
    """The distinct verdict markers a review stated, in the order stated."""

    stated: tuple[str, ...]

    @property
    def conflicting(self) -> bool:
        """More than one **different** verdict. A repeat is not a conflict:
        saying `REVIEW_PASSED` in a heading and again in a summary is one
        statement made twice."""
        return len(self.stated) > 1

    @property
    def only(self) -> str | None:
        return self.stated[0] if len(self.stated) == 1 else None


def read_review(output: str) -> ReviewSignal:
    """The one place a reviewer's output is turned into a verdict claim (#270).

    Deliberately **not** a precedence rule. Ranking the markers would have
    removed the sequential/parallel divergence while keeping the ambiguity: a
    reviewer that says both `REVIEW_PASSED` and `REVIEW_FAILED` has not decided,
    and picking one for it is the tool inventing a verdict. Two different
    verdicts are an error, and an error is something a person resolves.
    """
    return ReviewSignal(tuple(review_markers(output)))


def _resolve_review_template(config: ExecutorConfig, review_cmd: str) -> str:
    """Return the command template to use for the review stage.

    Resolution order:
    1. Explicit ``review_command_template`` — always wins.
    2. ``command_template`` — only when ``review_cmd`` is the *same binary* as
       ``config.claude_command`` (i.e. the exec CLI).  Prevents an exec-CLI
       template (e.g. a templated codex/qwen/copilot preset) from bleeding into
       a different review CLI.
    3. Empty string — let the runner auto-detect argv for the review CLI.
    """
    if config.review_command_template:
        return config.review_command_template
    if review_cmd == config.claude_command:
        return config.command_template
    return ""


# Review role definitions for parallel review agents
REVIEW_ROLES: dict[str, str] = {
    "quality": (
        "You are a Quality Review Agent. Focus exclusively on:\n"
        "- Bugs and logic errors\n"
        "- Security vulnerabilities (injection, auth bypass, data leaks)\n"
        "- Error handling gaps and uncaught exceptions"
    ),
    "implementation": (
        "You are an Implementation Review Agent. Focus exclusively on:\n"
        "- Whether the code achieves the stated task goals\n"
        "- Whether all checklist items are properly implemented\n"
        "- Edge cases and boundary conditions"
    ),
    "testing": (
        "You are a Testing Review Agent. Focus exclusively on:\n"
        "- Whether new code has adequate test coverage\n"
        "- Whether tests are meaningful (not trivial pass-through)\n"
        "- Missing test scenarios and edge case tests"
    ),
    "simplification": (
        "You are a Simplification Review Agent. Focus exclusively on:\n"
        "- Unnecessary complexity that can be simplified\n"
        "- Dead code or unused imports\n"
        "- Opportunities for clearer, more concise implementations"
    ),
    "docs": (
        "You are a Documentation Review Agent. Focus exclusively on:\n"
        "- Missing or outdated docstrings on public APIs\n"
        "- Misleading comments or variable names\n"
        "- README or changelog updates needed"
    ),
}


def build_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> str:
    """The review prompt, with the frozen-files block appended (#214).

    Review is not exempt. It fixes findings by editing files — that is what
    `REVIEW_FIXED` means — so it is the second pass that can violate a claim it
    was never told about, and in the pilot run the reviewer had started editing
    the claimed file when it died. The block is appended to the base prompt, so
    every parallel role carries it too: a role's prompt is this one with a
    focus prepended.
    """
    from .claims import ESCAPE_REVIEW, append_frozen_files

    body = _render_review_prompt(task, config, cli_name, test_output, lint_output, previous_error)
    body = append_waiver_obligation(body, config, task)
    return append_frozen_files(body, config, task, escape=ESCAPE_REVIEW)


#: Header of the waiver block, so a test can look for the section rather than
#: for a sentence that may be rephrased.
WAIVER_HEADER = "## Addressed TDD waiver — what you must verify"


def append_waiver_obligation(prompt: str, config: ExecutorConfig, task: Task) -> str:
    """Append the reviewer's waiver obligation, or return the prompt unchanged.

    Appended AFTER rendering, exactly like `append_frozen_files`, and for the
    same reason: a project `review.txt` replaces the built-in prompt whole
    (`prompt.py`), and its own variables are only TASK_ID/TASK_NAME/
    CHANGED_FILES/GIT_DIFF. Put this inside the built-in text and the one
    configuration that matters here — ours, which ships a custom template —
    would be the one that never sees it. Appending is what makes the block
    independent of the template.

    Fires only for a VALID addressed-waived task: `resolve_waiver` returns
    None without a marker, so an ordinary `standard` task is untouched, and a
    marker the resolver cannot read produces no block rather than a
    half-written one.

    This site does NOT refuse such a task, and no longer says it does
    (spec-runner#435): the `ConfigError` is caught and the prompt returned
    unchanged. The refusal lives where the task is run — `execute_task`
    resolves the waiver before anything paid happens and stops the task
    there — and `validate` reports the same defect statically. A task whose
    marker is unreadable therefore never reaches review at all; inventing
    obligation terms for a declaration we cannot read would be the only
    thing raising here could add.

    The obligation text comes from the CLASS (`WAIVER_REVIEW_OBLIGATIONS`),
    never from the marker's own words: the declaration names a class, it does
    not get to write the terms it is judged by.

    Why the reviewer at all, after #428: the harness now EXECUTES the
    negative control before this call (`_run_negative_control_before_review`)
    and only a satisfied verdict reaches the reviewer. What the machine cannot
    judge is whether the mutant corresponds to the property the task claims —
    a mutant that breaks the build produces a red that proves nothing about
    the test. That correspondence is what the obligation now asks for; the
    old text ("you are the only check") would state as absent a check that
    has already happened.
    """
    from .config import WAIVER_REVIEW_OBLIGATIONS, ConfigError

    try:
        waiver = config.resolve_waiver(task)
    except ConfigError:
        # A declaration the resolver cannot read is refused where the task is
        # executed; here we simply say nothing rather than invent terms.
        return prompt
    if waiver is None:
        return prompt
    obligation = WAIVER_REVIEW_OBLIGATIONS[waiver.node_class]
    return (
        f"{prompt.rstrip()}\n\n{WAIVER_HEADER}\n"
        f"Class: {waiver.node_class}\nSanction: {waiver.sanction}\n\n"
        f"{obligation}\n"
    )


#: How much of the patch the prompt carries; the reviewer reads the rest
#: itself from the base the prompt names.
MAX_PROMPT_PATCH = 30_000


@dataclass(frozen=True)
class TaskDiff:
    """The task's changes: from where the task began to the tree under review.

    ``base`` is the merge-base of HEAD with the branch the task merges into
    (the integration branch during a run), so every commit of the task is in
    it — WIP commits, master merged in, the agent's own commits. `HEAD~1`
    saw only the last one: TASK-002 of #480 passed a required review on a
    one-line diff of tasks.md while ~3,900 lines went unread.

    The diff is what merging this branch delivers. A branch that carries
    another task's unmerged work (reused from a stopped run) delivers that
    too, so the reviewer sees it — deliberately.
    """

    base: str
    #: None when git could not say (not a repository, no such base): unknown,
    #: never "nothing changed".
    files: list[str] | None
    #: New files git does not track yet: work `git diff` cannot show
    #: (`auto_commit: false`), named so the reviewer reads them.
    untracked: list[str]
    stat: str
    patch: str


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=config.project_root)


def task_base(config: ExecutorConfig) -> str:
    """Where the task under review began (see ``TaskDiff``).

    The merge-base with the main branch. On a task branch whose HEAD *is*
    the merge-base — no commit of its own — HEAD itself, so only uncommitted
    work is in the diff (`HEAD~1` there handed the reviewer someone else's
    commit, review of #655). `HEAD~1` when the work sits on the main branch
    itself or the merge-base cannot be computed, as before.

    An empty diff is not refused: with the base right it means the task
    changed nothing, which #97 completes as a no-op.
    """
    from .git_ops import current_branch, get_main_branch

    # Without a branch per task nothing marks where the task began: its work
    # is the candidate commit, and a merge-base would be the fork point of a
    # long-lived branch, carrying every earlier task (acceptance of #655).
    if not config.create_git_branch:
        return "HEAD~1"
    main = get_main_branch(config)
    merge_base = _git(config, "merge-base", "HEAD", main)
    head = _git(config, "rev-parse", "HEAD")
    base = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not base or head.returncode != 0:
        return "HEAD~1"
    if base != head.stdout.strip():
        return base
    if current_branch(config) == main:
        return "HEAD~1"
    return "HEAD"


def task_diff(config: ExecutorConfig) -> TaskDiff | None:
    """The task's diff, or None when git automation is off for this project.

    Off means no per-task isolation — a subdir of a larger repo, or
    `--no-branch --no-commit` — where a diff would run against the parent
    repo and drown the reviewer in unrelated changes.
    """
    if not (config.create_git_branch or config.auto_commit):
        return None
    base = task_base(config)
    # `--relative`: paths as the project sees them.
    names = _git(config, "diff", "--relative", "--name-only", base)
    files = names.stdout.splitlines() if names.returncode == 0 else None
    stat = _git(config, "diff", base, "--stat")
    patch = _git(config, "diff", "-p", base)
    others = _git(config, "ls-files", "--others", "--exclude-standard")
    return TaskDiff(
        base=base,
        files=files,
        untracked=others.stdout.splitlines() if others.returncode == 0 else [],
        stat=stat.stdout.strip() if stat.returncode == 0 else "",
        patch=patch.stdout if patch.returncode == 0 else "",
    )


def _render_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> str:
    """Build code review prompt for the specified CLI.

    Args:
        task: Task that was completed
        config: Executor configuration
        cli_name: CLI name for CLI-specific prompt template (e.g., 'codex', 'claude')
        test_output: Test run output to include in review context
        lint_output: Lint check output to include in review context
        previous_error: Error from previous attempt (retry context)
    """
    diff = task_diff(config)
    if diff is not None:
        changed_files = (
            "Unable to get changed files" if diff.files is None else "\n".join(diff.files)
        )
        if diff.untracked:
            changed_files += (
                "\n\nNew files not yet committed (not in the diff below — read them):\n"
                + "\n".join(diff.untracked)
            )
        git_diff_stat = diff.stat
        full_diff = diff.patch[:MAX_PROMPT_PATCH]
        if len(diff.patch) > MAX_PROMPT_PATCH:
            full_diff += (
                f"\n... (diff truncated at {MAX_PROMPT_PATCH} of {len(diff.patch)} chars — "
                f"read the rest with `git diff {diff.base}`)"
            )
        base_note = f"Base of the task: `{diff.base}` (the diff below is `git diff {diff.base}`)"
    else:
        changed_files = "(git diff unavailable: git automation disabled for this project)"
        git_diff_stat = ""
        full_diff = ""
        base_note = ""

    # Try to load CLI-specific or custom template
    template = load_prompt_template("review", cli_name=cli_name, prompts_dir=config.prompts_dir)

    if template:
        variables = {
            "TASK_ID": task.id,
            "TASK_NAME": task.name,
            "CHANGED_FILES": changed_files,
            "GIT_DIFF": git_diff_stat,
            # The base, so a project's own template can name it too.
            "TASK_BASE": diff.base if diff is not None else "",
        }
        return render_template(template, variables)

    # Build additional context sections for fallback prompt
    # Task checklist
    checklist_section = ""
    if task.checklist:
        items = "\n".join(f"- {item}" for item, _checked in task.checklist)
        checklist_section = f"\n## Task Checklist\n{items}\n"

    # Test results
    test_section = ""
    if test_output:
        test_section = f"\n## Test Results\n{test_output[:2048]}\n"

    # Lint status
    lint_section = ""
    if lint_output:
        lint_section = f"\n## Lint Status\n{lint_output[:200]}\n"

    # Previous errors
    error_section = ""
    if previous_error:
        # Quoted history reaches the reviewer with the marker tokens broken
        # (#266/#270): a reviewer that reads `REVIEW_FAILED` in the errors it
        # was handed is one summary away from stating it, and the parser cannot
        # tell a quotation from a verdict.
        quoted = neutralise_markers(previous_error[:1024])
        error_section = f"\n## Previous Errors (from retry)\n{quoted}\n"

    # Reviewer persona system prompt
    persona_section = ""
    reviewer_persona = config.get_persona("reviewer")
    if reviewer_persona and reviewer_persona.system_prompt:
        persona_section = f"\n## Reviewer Role\n{reviewer_persona.system_prompt.strip()}\n"

    # Constitution guardrails
    constitution_section = ""
    if config.constitution_file.exists():
        constitution_text = config.constitution_file.read_text().strip()
        if constitution_text:
            constitution_section = f"\n## Constitution (Inviolable Rules)\n{constitution_text}\n"

    # Fallback to built-in prompt
    return f"""{persona_section}# Code Review Request

## Task Completed: {task.id} — {task.name}

{base_note}

## Changed Files:
{changed_files}

## Full Diff:
{full_diff}

## Diff Summary:
{git_diff_stat}
{checklist_section}{test_section}{lint_section}{error_section}{constitution_section}
## Review Instructions:

Launch the following review agents in parallel using the Task tool:

### 1. Quality Agent
Review the code changes for:
- Bugs and logic errors
- Security vulnerabilities
- Error handling gaps

### 2. Implementation Agent
Verify the implementation:
- Code achieves the stated task goals
- All checklist items are properly implemented
- Edge cases are handled

### 3. Testing Agent
Review test coverage:
- New code has adequate test coverage
- Tests are meaningful and not trivial

## Output:

For each issue found, describe it briefly.
Then end your reply with EXACTLY ONE verdict marker on its own line,
plain text — no markdown, no quotes, no heading, nothing else on the line:
- issues were found and fixed: REVIEW_FIXED
- no issues found: REVIEW_PASSED
A reply without that final marker line is invalid and will be discarded.
"""


#: Ledger provenance for the single-pass reviewer.
REVIEW_PROVENANCE = "review"


def role_provenance(role: str) -> str:
    """Ledger provenance for one parallel review role.

    Per role, never one aggregate row: five roles are five paid calls, and a
    single approximate total cannot say which one was expensive or which one
    was never measured.
    """
    return f"review:{role}"


@dataclass(frozen=True)
class ReviewCall:
    """One reviewer subprocess: what it said, and what it cost.

    `text` is the CLI's *result*, extracted by `parse_cli_result` — the same
    authority the RED pass uses — rather than raw stdout, so an explicit
    claude reviewer can be asked for JSON (and thus for its cost) without the
    markers disappearing into a JSON envelope.
    """

    text: str
    stderr: str
    returncode: int
    cost_usd: float | None
    #: The CLI said it failed. For a text CLI that is the return code; for
    #: claude's JSON it is the payload flag, which can arrive with **exit 0**.
    #: Carried because the exec path already decides on all three signals
    #: together (#167), and a review that dropped this one turned a rate-limit
    #: payload into "no marker" — NOT_RUN, which reads as "the reviewer said
    #: nothing", when in fact the reviewer said it had failed (Copilot, #216).
    is_error: bool = False
    timed_out: bool = False
    #: Set when the call was refused before it started, because the budget
    #: guard could not prove there was anything left to spend (#213). Not an
    #: error and not a verdict: nothing was asked, so nothing was learned.
    budget_refusal: str = ""


def _run_reviewer(
    config: ExecutorConfig,
    task_id: str,
    provenance: str,
    prompt: str,
    review_cmd: str,
    review_model: str,
    review_template: str,
    pending_cost: float | None = 0.0,
) -> ReviewCall:
    """Run one reviewer subprocess and **record what it cost**.

    Every call that happened gets a ledger row: passed, failed, timed out, or
    killed by an account limit. Money spent on a call that produced nothing
    usable is still spent — the rule the RED pass already follows.

    A call that never launched (missing binary, no permission) gets no row and
    the error is re-raised for the caller's existing handler: no subprocess, no
    spend, and a row would assert one. Re-raised rather than reported as a
    return value so the operator still reads *which* binary was missing.

    Until this existed, review cost was recorded **nowhere** — not on the
    attempt row, not in the ledger — so `spec-runner costs`, `task_budget_usd`
    and `budget_usd` were all blind to a third of a TDD attempt's calls, and to
    one call per role of every parallel review (#213).
    """
    from .runner import build_cli_invocation

    # #213: the third of a TDD attempt's paid calls, and the one whose refusal
    # costs least — the candidate commit stands either way, and an unreviewed
    # candidate is a state the tool already models.
    refusal = _budget_refusal(config, task_id, provenance, pending_cost)
    if refusal is not None:
        log_progress(f"⛔ {refusal.reason}", task_id)
        return ReviewCall(
            text="", stderr="", returncode=-1, cost_usd=None, budget_refusal=refusal.reason
        )

    invocation = build_cli_invocation(
        cmd=review_cmd,
        prompt=prompt,
        model=review_model,
        template=review_template,
        skip_permissions=config.skip_permissions,
        json_output=True,
    )
    from . import paid_call

    scope = paid_call.current_scope()
    try:
        # The seam writes the ledger row (open before the store ack, closed
        # after the result): a timed-out call is a row with an unknown cost,
        # never a zero, and a call that never launched is closed
        # `not_started`.
        outcome = paid_call.execute(
            config,
            None,
            paid_call.PaidCall(
                invocation=invocation,
                provenance=provenance,
                prompt=prompt,
                timeout_seconds=config.review_timeout_minutes * 60,
                call_id=paid_call.new_call_id(),
                task_id=task_id,
                attempt=_attempt_number(config, task_id),
                env=agent_env(),
                prompt_log=scope.prompt_log if scope is not None else None,
                ledger=paid_call.task_ledger(config, None, task_id, provenance),
            ),
        )
    except OSError as exc:
        logger.warning(
            "Reviewer did not launch",
            task_id=task_id,
            provenance=provenance,
            error=str(exc),
        )
        raise
    if outcome.timed_out:
        return ReviewCall(text="", stderr="", returncode=-1, cost_usd=None, timed_out=True)
    parsed = outcome.parsed
    assert parsed is not None
    return ReviewCall(
        text=parsed.text,
        stderr=outcome.stderr,
        returncode=outcome.returncode,
        cost_usd=parsed.cost_usd,
        is_error=parsed.is_error,
    )


def _attempt_number(config: ExecutorConfig, task_id: str) -> int | None:
    """The attempt this review belongs to: the one `record_attempt` has not yet
    written (a review runs before its attempt is recorded), numbered within the
    invocation like every call-start (`next_evidence_attempt`, #480). None when
    the state cannot be read -- a number is evidence, not a reason to skip a call."""
    from .state import ExecutorState

    try:
        with _LEDGER_LOCK, ExecutorState(config) as state:
            return int(state.next_evidence_attempt(task_id))
    except Exception:  # noqa: BLE001
        return None


#: Serialises ledger writes from the parallel review pool. Each role opens its
#: own `ExecutorState`, and five of those arriving together made SQLite return
#: "database is locked" *immediately* — `busy_timeout` does not cover a write
#: lock taken during the schema setup every connection runs. The first run of
#: `test_each_parallel_role_gets_its_own_row` lost one role's row to exactly
#: that. A review call takes minutes; serialising a millisecond write is free,
#: and losing an accounting row is the thing this change exists to stop.
_LEDGER_LOCK = paid_call_lock


def _budget_refusal(
    config: ExecutorConfig, task_id: str, provenance: str, pending_cost: float | None = 0.0
):
    """Ask the pre-call guard, or None when no cap is configured.

    Opens no state when there is no budget: a run that set no cap must not
    gain a database read per review call from a feature it never enabled.
    """
    from .budget import UNREADABLE, BudgetRefusal, check_before_call
    from .state import ExecutorState

    if not budget_is_active(config):
        return None
    try:
        with _LEDGER_LOCK, ExecutorState(config) as state:
            return check_before_call(config, state, task_id, provenance, pending_cost)
    except Exception as exc:
        # Fail closed. A guard that cannot read spend is the extreme case of
        # the unprovable remainder it already refuses on, and proceeding here
        # would break the guarantee in exactly the situation where nobody is
        # counting: an unreadable ledger lets every remaining call through
        # (Copilot, PR #217). The first version of this reasoned that
        # refusing on a broken reader stops work "for a reason that may not
        # be true" — but "we do not know" is not a reason to spend.
        logger.error(
            "Budget guard could not read spend — refusing the call",
            task_id=task_id,
            provenance=provenance,
            error=str(exc),
        )
        return BudgetRefusal(
            UNREADABLE,
            f"the budget guard could not read recorded spend ({exc}), so the remaining "
            f"budget cannot be proven — not starting the {provenance} call",
        )


def _fix_touches_the_harness(config: ExecutorConfig, before: dict[str, str] | None) -> bool:
    """Whether a reviewer's fixes must stay uncommitted (#64, `strict` only).

    `post_done_hook` refuses the attempt and restores the harness files; a
    commit made here first would put the refused oracle edit into the branch,
    where the next task's baseline — under ``create_git_branch: false`` —
    would read it as legitimate.
    """
    if config.harness_guard != "strict" or not harness_violations(config, before):
        return False
    logger.warning("Review fixes touch the harness — not committed")
    return True


def run_code_review(
    task: Task,
    config: ExecutorConfig,
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
    pending_cost: float | None = 0.0,
) -> tuple[ReviewVerdict, str | None, str | None]:
    """Run code review on completed task.

    Args:
        task: Task that was completed
        config: Executor configuration
        test_output: Test run output to include in review context
        lint_output: Lint check output to include in review context
        previous_error: Error from previous attempt (retry context)

    Returns:
        Tuple of (verdict, error_message, review_output).
    """
    log_progress("🔍 Starting code review", task.id)
    harness_before = snapshot_harness(config)

    # Use review-specific command/model if configured, then persona, then main settings
    review_cmd = config.review_command or config.claude_command
    review_model = config.review_model or config.get_model_for_role("reviewer")
    review_template = _resolve_review_template(config, review_cmd)

    # Build prompt with CLI-specific template
    prompt = build_review_prompt(
        task,
        config,
        cli_name=review_cmd,
        test_output=test_output,
        lint_output=lint_output,
        previous_error=previous_error,
    )

    # The prompt as sent, through the writer every paid stage shares (#282
    # follow-up). This path already logged its prompt; what it did not do was
    # use the same format, the same bound, or the same provenance as the RED
    # pass — and the per-role path below logged nothing at all, so a parallel
    # review left five paid calls and no record of what any of them was asked.
    prompt_log = log_prompt(config, task.id, REVIEW_PROVENANCE, prompt)

    try:
        log_progress(
            f"🔍 Review using: {review_cmd}" + (f" ({review_model})" if review_model else ""),
            task.id,
        )

        # A reviewer that never launches raises, and the `except Exception`
        # below catches it — the runner survives and the verdict becomes
        # `error`, so an artefact left holding the prompt alone is the one
        # shape the invariant reserves for a runner that died (Copilot, PR
        # #298). Measured: with the binary missing, that file was open.
        try:
            with paid_call_scope(
                task_id=task.id, provenance=REVIEW_PROVENANCE, prompt_log=prompt_log
            ):
                call = _run_reviewer(
                    config,
                    task.id,
                    REVIEW_PROVENANCE,
                    prompt,
                    review_cmd,
                    review_model,
                    review_template,
                    pending_cost,
                )
        except OSError as exc:
            append_not_started(prompt_log, f"the reviewer did not launch: {exc}")
            raise
        if call.budget_refusal:
            # NOT_RUN, never SKIPPED: `skipped` is what a *policy* decision
            # looks like, and under `review_policy: advisory` the absence of a
            # review must not read as a review that went fine. Nothing was
            # asked, so nothing is known about this code.
            #
            # The artefact says so too (#296). The prompt is already on disk —
            # it is written before the guard runs — and a file holding a
            # complete review prompt and nothing else is byte-shape identical
            # to one from a call that launched and died.
            append_not_started(prompt_log, call.budget_refusal)
            return ReviewVerdict.NOT_RUN, call.budget_refusal, None

        output = call.text
        stderr = call.stderr
        combined_output = output + "\n" + stderr

        # Save the answer beside the prompt it answered — including the two
        # facts that make it readable later: what the process returned, and
        # what the call cost (`unknown` when the CLI reported none, never 0.0).
        #
        # Above the timeout check, so a timed-out call leaves a record too: it
        # ran, and it was billed for as long as it ran. This path used to
        # return first and write nothing, while the per-role path below wrote
        # the block — the same two-paths-disagree shape as #270, in the
        # artefact instead of the verdict.
        append_output(
            prompt_log,
            output,
            stderr,
            returncode=call.returncode,
            cost_usd=call.cost_usd,
            note=(f"timed out after {config.review_timeout_minutes}m" if call.timed_out else None),
        )

        if call.timed_out:
            log_progress(
                "⏰ Code review produced no verdict: timed out after "
                f"{config.review_timeout_minutes}m",
                task.id,
            )
            return ReviewVerdict.NOT_RUN, "Review timed out", None

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"💥 Code review error: {error_pattern}", task.id)
            return ReviewVerdict.ERROR, f"API error: {error_pattern}", output

        # Whether this output may be read for a verdict at all is one shared
        # decision (#241), not this module's opinion: a non-zero exit means the
        # reviewer did not finish, so whatever it managed to print is not a
        # considered verdict — including a marker (Copilot, PR #156). The
        # output is still returned: discarding the verdict must not discard
        # what was said.
        answer = classify_agent_answer(
            CliResult(output, None, None, call.cost_usd, is_error=call.is_error),
            call.returncode,
        )
        if answer is AgentAnswer.CRASHED:
            reason = (
                f"process exited with code {call.returncode}"
                if call.returncode != 0
                else "the CLI reported an error"
            )
            log_progress(f"💥 Code review error: {reason}", task.id)
            if stderr.strip():
                log_progress(f"   stderr: {stderr.strip()[:200]}", task.id)
            return ReviewVerdict.ERROR, f"Review {reason}", output or None

        if answer is AgentAnswer.EMPTY:
            log_progress("⚠️ Code review produced no verdict: empty response", task.id)
            return ReviewVerdict.NOT_RUN, "Review returned empty response", None

        # One reading for both paths (#270), and markers are lines rather than
        # substrings — a reviewer writing "this is not a REVIEW_FAILED
        # situation" used to be recorded as having failed the review.
        signal = read_review(combined_output)
        if signal.conflicting:
            stated = ", ".join(signal.stated)
            log_progress(f"⚠️ Code review stated conflicting verdicts: {stated}", task.id)
            return (
                ReviewVerdict.ERROR,
                f"Review stated conflicting verdicts ({stated}); a person has to read it",
                output,
            )
        if signal.only == "REVIEW_PASSED":
            log_progress("✅ Code review passed", task.id)
            return ReviewVerdict.PASSED, None, output
        elif signal.only == "REVIEW_FIXED":
            log_progress("✅ Code review: issues fixed", task.id)
            format_failure = _review_fix_format_failure(config)
            if format_failure is not None:
                # The tree still contains reviewer mutations. Preserve FIXED
                # so post_done repeats every deterministic gate before any
                # general task commit can sweep them up.
                return ReviewVerdict.FIXED, format_failure, output
            if _fix_touches_the_harness(config, harness_before):
                return ReviewVerdict.FIXED, None, output
            # Commit the fixes — runtime state stays out of the commit (#62)
            if stage_all_except_runtime(config):
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"{task.id}: code review fixes"],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
                if commit_result.returncode != 0:
                    logger.warning(
                        "Review fix commit failed",
                        stderr=commit_result.stderr.strip()[:200],
                    )
            return ReviewVerdict.FIXED, None, output
        elif signal.only == "REVIEW_FAILED":
            log_progress("❌ Code review found unresolved issues", task.id)
            preview = output.strip()[-300:]
            log_progress(f"   Review output (last 300 chars): {preview}", task.id)
            return ReviewVerdict.FAILED, "Review found issues", output
        else:
            # #138: this used to return PASSED — "the agent said nothing I
            # recognize" was recorded, and displayed with a tick, as a clean
            # review. Silence is not approval: the reviewer may have produced
            # prose, hit its context limit, or misunderstood the protocol, and
            # none of those is evidence about the code.
            preview = output.strip()[-200:] if output.strip() else "(empty)"
            log_progress("⚠️ Code review produced no verdict: no status marker", task.id)
            log_progress(f"   Review output (last 200 chars): {preview}", task.id)
            return ReviewVerdict.NOT_RUN, "Review produced no verdict marker", output

    # No `except subprocess.TimeoutExpired` here: the subprocess is owned by
    # `_run_reviewer`, which turns a timeout into a recorded call and a
    # `timed_out` result. Catching it here again would mean a path that spent
    # money and wrote no ledger row.
    except Exception as e:
        log_progress(f"💥 Code review error: {e}", task.id)
        return ReviewVerdict.ERROR, str(e), None


def _run_single_role_review(
    role: str,
    role_prompt: str,
    base_prompt: str,
    review_cmd: str,
    review_model: str,
    review_template: str,
    config: ExecutorConfig,
    task_id: str,
    pending_cost: float | None = 0.0,
) -> tuple[str, ReviewVerdict, str]:
    """Run a single role-specific review. Returns (role, verdict, output).

    Its cost is recorded under its own provenance, `review:<role>`. One
    aggregate row for the whole parallel pass would hide which role was
    expensive and which one was never measured at all.
    """
    full_prompt = f"{role_prompt}\n\n{base_prompt}"
    # Per role, under its own provenance: `review:security` is a different
    # question from `review:performance`, and one aggregate log would hide
    # which role was asked what — the same reasoning that gave each role its
    # own ledger row.
    prompt_log = log_prompt(config, task_id, role_provenance(role), full_prompt)
    try:
        try:
            with paid_call_scope(
                task_id=task_id, provenance=role_provenance(role), prompt_log=prompt_log
            ):
                call = _run_reviewer(
                    config,
                    task_id,
                    role_provenance(role),
                    full_prompt,
                    review_cmd,
                    review_model,
                    review_template,
                    pending_cost,
                )
        except OSError as exc:
            append_not_started(prompt_log, f"the reviewer did not launch: {exc}")
            raise
        if call.budget_refusal:
            # No call was made, so there is no answer to record — and a log
            # claiming one would say money was spent that was not. What the
            # artefact does record is that refusal itself (#296): silence left
            # it indistinguishable from a call that died mid-flight.
            append_not_started(prompt_log, call.budget_refusal)
            return role, ReviewVerdict.NOT_RUN, call.budget_refusal
        # Beside the prompt it answered, exactly as the single path does
        # (Copilot, PR #287): a role log holding the question and not the
        # answer is the half of a postmortem nobody needs.
        append_output(
            prompt_log,
            call.text,
            call.stderr,
            returncode=call.returncode,
            cost_usd=call.cost_usd,
            note=(f"timed out after {config.review_timeout_minutes}m" if call.timed_out else None),
        )
        if call.timed_out:
            return role, ReviewVerdict.NOT_RUN, f"Review timeout ({role})"
        output = call.text + "\n" + call.stderr
        if call.is_error or call.returncode != 0:
            # Same rule as the single path: a role that crashed — or whose CLI
            # said it failed while exiting 0 — has no verdict.
            return role, ReviewVerdict.ERROR, output
        # The same reading as the single path — one classifier, so the two
        # cannot disagree again (#270). This path used to try FAILED first and
        # the other PASSED first, which made a self-contradicting reviewer's
        # verdict depend on whether a budget was active.
        signal = read_review(output)
        if signal.conflicting:
            stated = ", ".join(signal.stated)
            # The explanation is **prefixed**, not substituted (Copilot, PR
            # #278). This branch exists so that a person reads what the
            # reviewer said; returning the message alone would delete exactly
            # that, and the function's contract is `(role, verdict, output)` —
            # the aggregate report quotes this text under the role's heading.
            return (
                role,
                ReviewVerdict.ERROR,
                f"Review stated conflicting verdicts ({stated}); a person has to read it.\n\n"
                f"{output}",
            )
        if signal.only == "REVIEW_FAILED":
            return role, ReviewVerdict.FAILED, output
        elif signal.only == "REVIEW_FIXED":
            return role, ReviewVerdict.FIXED, output
        elif signal.only == "REVIEW_PASSED":
            return role, ReviewVerdict.PASSED, output
        # Same rule as the single-review path (#138): no marker means this role
        # produced no verdict, not that it approved.
        return role, ReviewVerdict.NOT_RUN, output
    except Exception as e:
        return role, ReviewVerdict.ERROR, str(e)


def run_parallel_review(
    task: Task,
    config: ExecutorConfig,
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
    pending_cost: float | None = 0.0,
) -> tuple[ReviewVerdict, str | None, str | None]:
    """Run multiple review agents in parallel, one per role.

    Each role gets a specialized focus prompt prepended to the base review prompt.
    Verdicts are aggregated: any FAILED → overall FAILED.
    """
    log_progress(f"🔍 Starting parallel review ({len(config.review_roles)} roles)", task.id)
    harness_before = snapshot_harness(config)

    review_cmd = config.review_command or config.claude_command
    review_model = config.review_model or config.get_model_for_role("reviewer")
    review_template = _resolve_review_template(config, review_cmd)

    base_prompt = build_review_prompt(
        task,
        config,
        cli_name=review_cmd,
        test_output=test_output,
        lint_output=lint_output,
        previous_error=previous_error,
    )

    # Get role prompts for configured roles
    roles_to_run = [
        (role, REVIEW_ROLES[role]) for role in config.review_roles if role in REVIEW_ROLES
    ]

    if not roles_to_run:
        log_progress("⚠️ No valid review roles configured, falling back to single review", task.id)
        return run_code_review(task, config, test_output, lint_output, previous_error)

    def _run(role: str, role_prompt: str) -> tuple[str, ReviewVerdict, str]:
        return _run_single_role_review(
            role,
            role_prompt,
            base_prompt,
            review_cmd,
            review_model,
            review_template,
            config,
            task.id,
            pending_cost,
        )

    results: list[tuple[str, ReviewVerdict, str]] = []
    if budget_is_active(config):
        # #213: serialised while a cap is set, and the guarantee is the reason.
        # "No new paid call starts once the limit is reached, so the overshoot
        # is bounded by one call" is simply false for five roles launched
        # together: all five pass the check before any of them reports a cost.
        # Sequential is slower and is what makes the sentence true.
        log_progress("💰 Budget active — reviewing one role at a time", task.id)
        for role, role_prompt in roles_to_run:
            results.append(_run(role, role_prompt))
    else:
        with ThreadPoolExecutor(max_workers=len(roles_to_run)) as pool:
            futures = [pool.submit(_run, role, role_prompt) for role, role_prompt in roles_to_run]
            for future in futures:
                results.append(future.result())

    # Aggregate verdicts. Precedence (#138): concrete findings first, then a
    # broken reviewer, then one that produced nothing — a role that never
    # answered must never be averaged away into an overall "passed".
    all_outputs: list[str] = []
    has_failed = has_error = has_not_run = has_fixed = False
    for role, verdict, output in results:
        log_progress(f"  📋 {role}: {verdict.value}", task.id)
        all_outputs.append(f"=== {role.upper()} REVIEW ===\n{output[:2000]}")
        if verdict == ReviewVerdict.FAILED:
            has_failed = True
        elif verdict == ReviewVerdict.ERROR:
            has_error = True
        elif verdict == ReviewVerdict.NOT_RUN:
            has_not_run = True
        elif verdict == ReviewVerdict.FIXED:
            has_fixed = True

    # Precedence: findings first, then fixes, then a reviewer that broke or
    # never answered. FIXED outranks ERROR/NOT_RUN deliberately (Copilot,
    # PR #156): it is the verdict the pipeline acts on — `post_done_hook`
    # re-runs tests and lint on FIXED — and a role that edited the tree must
    # get those gates regardless of what the *other* roles managed to return.
    # Ranking a silent role above it left the fixes to be swept up by the
    # general auto-commit later, ungated. The silent roles are not lost: each
    # is logged above and named in the returned reason.
    silent = [
        f"{role}: {verdict.value}"
        for role, verdict, _ in results
        if verdict in (ReviewVerdict.NOT_RUN, ReviewVerdict.ERROR)
    ]
    if has_failed:
        overall_verdict = ReviewVerdict.FAILED
    elif has_fixed:
        overall_verdict = ReviewVerdict.FIXED
    elif has_error:
        overall_verdict = ReviewVerdict.ERROR
    elif has_not_run:
        overall_verdict = ReviewVerdict.NOT_RUN
    else:
        overall_verdict = ReviewVerdict.PASSED

    format_failure_reason: str | None = None
    if has_fixed:
        # Committing is driven by "a role changed the tree", not by the overall
        # verdict: leaving the edits uncommitted here hands them to the general
        # auto-commit, which runs no gates.
        format_failure = _review_fix_format_failure(config)
        if format_failure is not None:
            format_failure_reason = format_failure
        elif _fix_touches_the_harness(config, harness_before):
            pass
        else:
            # Commit fixes from any review agent — minus runtime state (#62)
            try:
                staged = stage_all_except_runtime(config)
            except RuntimeError as exc:
                logger.warning("Staging failed after parallel review fixes", error=str(exc))
                staged = False
            if staged:
                subprocess.run(
                    ["git", "commit", "-m", f"{task.id}: parallel review fixes"],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
    combined_output = "\n\n".join(all_outputs)
    log_progress(f"🔍 Parallel review result: {overall_verdict.value}", task.id)

    # The reason keeps every role that produced nothing, whatever the overall
    # verdict turned out to be — otherwise a silent reviewer disappears from
    # the record the moment another role has something to say.
    reasons: list[str] = []
    if overall_verdict == ReviewVerdict.FAILED:
        reasons.append("Review found issues")
    if format_failure_reason:
        reasons.append(format_failure_reason)
    if silent:
        reasons.append("no verdict from " + ", ".join(silent))
    error = "; ".join(reasons) or None
    return overall_verdict, error, combined_output


def format_review_findings(task_id: str, task_name: str, review_output: str) -> str:
    """Format review findings for HITL display."""
    separator = "=" * 50
    return (
        f"\n{separator}\nReview: {task_id} — {task_name}\n{separator}\n\n{review_output[:3000]}\n"
    )


def prompt_hitl_verdict() -> str:
    """Prompt user for HITL review verdict.

    Returns:
        One of: 'approve', 'reject', 'fix', 'skip'.
    """
    print("\n  [a]pprove  [r]eject  [f]ix-and-retry  [s]kip")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        elif choice in ("r", "reject"):
            return "reject"
        elif choice in ("f", "fix"):
            return "fix"
        elif choice in ("s", "skip"):
            return "skip"
        print("  Invalid choice. Use: a, r, f, or s")
