"""The durable review-bot loop — `spec-runner review-pr` (#102, M1+M2).

Collects inline review comments left by allowed bot identities on a GitHub
PR, verifies each against the actual codebase with an agent call (verdict
``valid``/``refuted``/``uncertain``, fail-closed), then — in the default
full-loop mode — fixes the valid ones (TDD agent, per-comment commits with
provenance trailers), re-runs the project gates, pushes, and replies in
each thread (fix SHA, or refutation evidence). ``uncertain`` is always a
human's call: no fix, no auto-pushback. Durable cursor + resolution state
in the executor DB make re-invocations resume, never repeat.

Read-only modes: ``--verify-only`` stops after verdicts (the M1 behavior),
``--no-verify`` stops after collection. Design:
docs/superpowers/specs/2026-08-06-review-pr-loop-design.md.

Exit-code contract (stable for external callers, e.g. a Maestro hook):
    0 — complete: every comment fixed-and-replied or refuted-and-replied
        (in read-only modes: every comment verified valid/refuted)
    1 — fail-closed (draft/closed PR, API failure, dirty tree, head-SHA
        mismatch, force-push detected, push failure)
    2 — NEEDS_HUMAN: uncertain/unverified/limit-stopped/deleted comments
"""

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime

from .config import ExecutorConfig
from .logging import get_logger

logger = get_logger("review_pr")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NEEDS_HUMAN = 2

VERDICT_VALID = "valid"
VERDICT_REFUTED = "refuted"
VERDICT_UNCERTAIN = "uncertain"


class ReviewPrError(Exception):
    """Fail-closed condition — the loop must stop, not guess."""


@dataclass(frozen=True)
class BotComment:
    """One inline review comment from an allowed bot identity."""

    comment_id: int
    author: str
    path: str
    line: int | None
    body: str
    diff_hunk: str
    url: str


def _gh(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess:
    """Run a gh CLI command in the project root. Never raises on rc != 0."""
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
    except FileNotFoundError as exc:
        raise ReviewPrError("gh CLI not found — review-pr requires the GitHub CLI") from exc


def parse_pr_ref(ref: str, config: ExecutorConfig) -> tuple[str, int]:
    """Resolve a PR URL or bare number to (owner/repo, pr_number)."""
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/pull/(\d+)", ref)
    if m:
        return m.group(1), int(m.group(2))
    if ref.isdigit():
        result = _gh(config, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if result.returncode != 0 or not result.stdout.strip():
            raise ReviewPrError(
                "Cannot resolve the repository for a bare PR number: "
                f"{result.stderr.strip()[:200] or 'gh repo view returned nothing'}"
            )
        return result.stdout.strip(), int(ref)
    raise ReviewPrError(f"Not a PR URL or number: {ref!r}")


def fetch_pr_meta(config: ExecutorConfig, repo: str, pr_number: int) -> dict:
    """Fetch PR state/draft/head SHA. Fail-closed on any API error."""
    result = _gh(config, "api", f"repos/{repo}/pulls/{pr_number}")
    if result.returncode != 0:
        raise ReviewPrError(f"Cannot fetch PR {repo}#{pr_number}: {result.stderr.strip()[:200]}")
    try:
        data = json.loads(result.stdout)
        return {
            "state": data["state"],
            "draft": bool(data.get("draft", False)),
            "head_sha": data["head"]["sha"],
            "head_ref": data["head"]["ref"],
        }
    except (json.JSONDecodeError, KeyError) as exc:
        raise ReviewPrError(f"Unparseable PR payload for {repo}#{pr_number}: {exc}") from exc


def fetch_bot_comments(
    config: ExecutorConfig, repo: str, pr_number: int, allowed_bots: list[str]
) -> list[BotComment]:
    """Fetch inline review comments authored by allowed bot identities.

    Comments from any other author (including humans) are ignored — the
    loop never processes, and later never answers, anyone it was not
    explicitly allowed to.
    """
    result = _gh(config, "api", f"repos/{repo}/pulls/{pr_number}/comments", "--paginate")
    if result.returncode != 0:
        raise ReviewPrError(
            f"Cannot fetch review comments for {repo}#{pr_number}: {result.stderr.strip()[:200]}"
        )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise ReviewPrError(f"Unparseable comments payload: {exc}") from exc

    comments = []
    for item in payload:
        author = (item.get("user") or {}).get("login", "")
        if author not in allowed_bots:
            continue
        comments.append(
            BotComment(
                comment_id=item["id"],
                author=author,
                path=item.get("path", ""),
                line=item.get("line") or item.get("original_line"),
                body=item.get("body", ""),
                diff_hunk=item.get("diff_hunk", ""),
                url=item.get("html_url", ""),
            )
        )
    return comments


class ReviewPrState:
    """Durable per-comment state in the executor SQLite DB (own table).

    Keyed by (repo, pr_number, comment_id): a recorded comment is never
    re-processed, so a crashed or re-invoked run resumes instead of
    repeating work. Schema creation is idempotent; adding this table is a
    non-breaking DB change per docs/state-schema.md.
    """

    def __init__(self, config: ExecutorConfig):
        config.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(config.state_file))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pr_review_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                comment_id INTEGER NOT NULL,
                head_sha TEXT,
                author TEXT,
                path TEXT,
                line INTEGER,
                body TEXT,
                url TEXT,
                verdict TEXT,
                evidence TEXT,
                collected_at TEXT,
                verified_at TEXT,
                UNIQUE(repo, pr_number, comment_id)
            )
        """)
        # M2: fix/reply bookkeeping (idempotent migration)
        import contextlib

        for col in ("resolution", "fix_sha", "replied_at"):
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE pr_review_comments ADD COLUMN {col} TEXT")
        # M2: bounded rounds — one row per (pr, head_sha)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pr_review_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                started_at TEXT,
                UNIQUE(repo, pr_number, head_sha)
            )
        """)
        self._conn.commit()

    def known_ids(self, repo: str, pr_number: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT comment_id FROM pr_review_comments WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchall()
        return {r[0] for r in rows}

    def unverified_ids(self, repo: str, pr_number: int) -> set[int]:
        """Collected comments with no verdict yet (e.g. from a --no-verify
        run) — a later run must pick these up, not strand them."""
        rows = self._conn.execute(
            "SELECT comment_id FROM pr_review_comments "
            "WHERE repo = ? AND pr_number = ? AND verdict IS NULL",
            (repo, pr_number),
        ).fetchall()
        return {r[0] for r in rows}

    def record(self, repo: str, pr_number: int, head_sha: str, comment: BotComment) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO pr_review_comments "
                "(repo, pr_number, comment_id, head_sha, author, path, line, "
                "body, url, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo,
                    pr_number,
                    comment.comment_id,
                    head_sha,
                    comment.author,
                    comment.path,
                    comment.line,
                    comment.body,
                    comment.url,
                    datetime.now().isoformat(),
                ),
            )

    def set_verdict(
        self, repo: str, pr_number: int, comment_id: int, verdict: str, evidence: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE pr_review_comments SET verdict = ?, evidence = ?, verified_at = ? "
                "WHERE repo = ? AND pr_number = ? AND comment_id = ?",
                (
                    verdict,
                    evidence[:2000],
                    datetime.now().isoformat(),
                    repo,
                    pr_number,
                    comment_id,
                ),
            )

    def rows(self, repo: str, pr_number: int) -> list[dict]:
        cursor = self._conn.execute(
            "SELECT comment_id, author, path, line, url, verdict, evidence, "
            "resolution, fix_sha, replied_at "
            "FROM pr_review_comments WHERE repo = ? AND pr_number = ? ORDER BY comment_id",
            (repo, pr_number),
        )
        cols = [
            "comment_id",
            "author",
            "path",
            "line",
            "url",
            "verdict",
            "evidence",
            "resolution",
            "fix_sha",
            "replied_at",
        ]
        return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]

    def set_resolution(
        self,
        repo: str,
        pr_number: int,
        comment_id: int,
        resolution: str,
        fix_sha: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE pr_review_comments SET resolution = ?, fix_sha = ? "
                "WHERE repo = ? AND pr_number = ? AND comment_id = ?",
                (resolution, fix_sha, repo, pr_number, comment_id),
            )

    def mark_replied(self, repo: str, pr_number: int, comment_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE pr_review_comments SET replied_at = ? "
                "WHERE repo = ? AND pr_number = ? AND comment_id = ?",
                (datetime.now().isoformat(), repo, pr_number, comment_id),
            )

    def start_round(self, repo: str, pr_number: int, head_sha: str) -> int:
        """Register the round for this head SHA (idempotent); return the
        total round count for the PR. A new head SHA = a new bounded round."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO pr_review_rounds "
                "(repo, pr_number, head_sha, started_at) VALUES (?, ?, ?, ?)",
                (repo, pr_number, head_sha, datetime.now().isoformat()),
            )
        row = self._conn.execute(
            "SELECT COUNT(*) FROM pr_review_rounds WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchone()
        return int(row[0])

    def previous_round_sha(self, repo: str, pr_number: int, current_sha: str) -> str | None:
        """Most recent round SHA other than the current one (force-push check)."""
        row = self._conn.execute(
            "SELECT head_sha FROM pr_review_rounds "
            "WHERE repo = ? AND pr_number = ? AND head_sha != ? ORDER BY id DESC LIMIT 1",
            (repo, pr_number, current_sha),
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReviewPrState":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


VERIFY_PROMPT = """\
You are verifying a code-review bot comment against the actual codebase.

PR: {repo}#{pr_number}
File: {path} (line {line})
Diff hunk:
{diff_hunk}

Bot comment:
{body}

Read the relevant code in this repository and decide whether the comment is
factually correct and actionable.

STRICT RULES:
- Do NOT modify, create, or delete any files. This is read-only verification.
- Base the verdict only on evidence you actually checked (code you read,
  read-only commands you ran).

Reply with exactly these two lines at the end:
VERDICT: VALID | REFUTED | UNCERTAIN
EVIDENCE: <2-4 sentences of verifiable evidence — file:line references or
command output that a human can re-check>

Use VALID when the comment is correct and worth fixing, REFUTED when it is
demonstrably wrong (the evidence must disprove it), UNCERTAIN when the code
alone does not settle it."""


def parse_verdict(output: str) -> tuple[str, str]:
    """Extract (verdict, evidence) from verifier output.

    Fail-closed: no marker or an unknown one → ``uncertain`` (a human
    decides), never a guessed verdict. The LAST verdict marker wins (an
    agent may revise mid-answer), so the evidence is taken from the last
    ``EVIDENCE:`` block too — pairing a final verdict with an earlier
    draft's evidence would be misleading.
    """
    matches = re.findall(r"VERDICT:\s*(VALID|REFUTED|UNCERTAIN)", output, re.IGNORECASE)
    if not matches:
        tail = output.strip()[-200:] if output.strip() else "(empty output)"
        return VERDICT_UNCERTAIN, f"No VERDICT marker in verifier output; tail: {tail}"
    verdict = matches[-1].lower()
    evidence_blocks = re.findall(
        r"EVIDENCE:\s*(.+?)(?=\nVERDICT:|\Z)", output, re.IGNORECASE | re.DOTALL
    )
    evidence = evidence_blocks[-1].strip()[:2000] if evidence_blocks else ""
    return verdict, evidence


def verify_comment(
    comment: BotComment, repo: str, pr_number: int, config: ExecutorConfig
) -> tuple[str, str]:
    """Run one verification agent call. Fail-closed to ``uncertain``."""
    from .review import _resolve_review_template
    from .runner import build_cli_command

    cmd = config.review_command or config.claude_command
    template = _resolve_review_template(config, cmd)
    model = config.review_model or config.get_model_for_role("reviewer")
    prompt = VERIFY_PROMPT.format(
        repo=repo,
        pr_number=pr_number,
        path=comment.path or "(no file)",
        line=comment.line if comment.line is not None else "?",
        diff_hunk=comment.diff_hunk[:3000] or "(none)",
        body=comment.body[:4000],
    )
    argv = build_cli_command(
        cmd=cmd,
        prompt=prompt,
        model=model,
        template=template,
        skip_permissions=config.skip_permissions,
    )
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=config.review_timeout_minutes * 60,
            cwd=config.project_root,
        )
    except subprocess.TimeoutExpired:
        return VERDICT_UNCERTAIN, f"Verifier timed out after {config.review_timeout_minutes}m"
    if result.returncode != 0 and not result.stdout.strip():
        return (
            VERDICT_UNCERTAIN,
            f"Verifier exited {result.returncode}: {result.stderr.strip()[:200]}",
        )
    return parse_verdict(result.stdout)


def _dirty_paths(config: ExecutorConfig) -> list[str]:
    """`git status --porcelain` lines, minus executor runtime state.

    The loop's own state DB (and its WAL/SHM sidecars, logs, locks) lives
    in the worktree and is written by this very command — counting it as
    dirt would make review-pr fail-closed on itself in repos where the
    runtime gitignore has not been written yet (#62 covers runs, not this
    command).
    """
    from .git_ops import runtime_state_paths

    rels: set[str] = set()
    for p in runtime_state_paths(config):
        try:
            rels.add(str(p.relative_to(config.project_root)))
        except ValueError:
            continue
    out = []
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    for line in result.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if any(path == r or path.startswith(r + "/") or path.startswith(r + "-") for r in rels):
            continue  # runtime state (incl. -wal/-shm sidecars)
        if path == "spec/.gitignore":
            continue  # harness-owned (#96)
        out.append(line)
    return out


def _worktree_fingerprint(config: ExecutorConfig) -> str:
    """Cheap fingerprint of uncommitted changes (read-only guard)."""
    return "\n".join(_dirty_paths(config))


# --- M2: fix + reply -------------------------------------------------------


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=config.project_root)


FIX_PROMPT = """\
You are fixing the codebase in response to a VERIFIED code-review comment.

PR: {repo}#{pr_number}
File: {path} (line {line})
Diff hunk:
{diff_hunk}

Review comment (verified as valid):
{body}

Verification evidence:
{evidence}

STRICT RULES:
- Work TDD where the comment concerns behavior: first add or adjust a test
  that captures the problem, then make it pass. Doc/comment-only fixes need
  no test.
- Change ONLY what this comment requires. No drive-by refactoring.
- Do NOT commit, push, or touch git in any way — the harness commits.
- Do NOT modify test commands, CI configs, or other verification harness.

When finished, print exactly one line:
FIX_COMPLETE: <one-sentence summary of the change>
or, if the fix cannot be made safely:
FIX_FAILED: <reason>"""


def run_fix_agent(
    comment: BotComment, evidence: str, repo: str, pr_number: int, config: ExecutorConfig
) -> tuple[bool, str, float]:
    """Run the fix agent for one valid comment.

    Returns (ok, note, cost_usd). Fail-closed: a FIX_FAILED marker, an
    error exit with no output, or a timeout all report ok=False.
    """
    from .runner import build_cli_invocation, parse_cli_result

    prompt = FIX_PROMPT.format(
        repo=repo,
        pr_number=pr_number,
        path=comment.path or "(no file)",
        line=comment.line if comment.line is not None else "?",
        diff_hunk=comment.diff_hunk[:3000] or "(none)",
        body=comment.body[:4000],
        evidence=evidence[:2000] or "(none recorded)",
    )
    invocation = build_cli_invocation(
        cmd=config.claude_command,
        prompt=prompt,
        model=config.get_model_for_role("implementer"),
        template=config.command_template,
        skip_permissions=config.skip_permissions,
        json_output=True,
    )
    try:
        result = subprocess.run(
            invocation.argv,
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
        )
    except subprocess.TimeoutExpired:
        return False, f"Fix agent timed out after {config.task_timeout_minutes}m", 0.0
    cli_result = parse_cli_result(
        invocation.result_format, result.stdout, result.stderr, result.returncode
    )
    cost = cli_result.cost_usd or 0.0
    output = cli_result.text
    m = re.search(r"FIX_FAILED:\s*(.+)", output)
    if m:
        return False, m.group(1).strip()[:300], cost
    m = re.search(r"FIX_COMPLETE:\s*(.+)", output)
    if m:
        return True, m.group(1).strip()[:300], cost
    if result.returncode != 0:
        return False, f"Fix agent exited {result.returncode} without a marker", cost
    # No marker but clean exit: accept only if something actually changed —
    # the caller checks the tree either way.
    return True, "(no FIX_COMPLETE marker; accepted on clean exit)", cost


def _changed_lines_in_head(config: ExecutorConfig) -> int:
    """Insertions + deletions of the HEAD commit (per-fix diff cap)."""
    result = _git(config, "diff", "--numstat", "HEAD^", "HEAD")
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for p in parts[:2]:
                if p.isdigit():
                    total += int(p)
    return total


def _run_gates(config: ExecutorConfig) -> tuple[bool, str]:
    """Full tests + lint after a mutation (the #65 invariant).

    Honors the project's run_tests_on_done / run_lint_on_done switches —
    a project that disabled its gates made that call for every stage.
    """
    if config.run_tests_on_done and config.test_command:
        result = subprocess.run(
            config.test_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode != 0:
            return False, f"tests failed: {(result.stdout + result.stderr)[-500:]}"
    if config.run_lint_on_done and config.lint_command:
        result = subprocess.run(
            config.lint_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode != 0:
            return False, f"lint failed: {(result.stdout + result.stderr)[-500:]}"
    return True, ""


def _commit_fix(config: ExecutorConfig, comment: BotComment, note: str) -> str | None:
    """Commit the fix with per-comment provenance. Returns the SHA or None."""
    from .git_ops import stage_all_except_runtime

    try:
        if not stage_all_except_runtime(config):
            return None
    except RuntimeError as exc:
        logger.warning("Staging failed during review-pr fix", error=str(exc))
        return None
    loc = f"{comment.path}:{comment.line}" if comment.path else "review comment"
    msg = (
        f"fix: address review comment on {loc}\n\n"
        f"{note}\n\n"
        f"Review-Comment-Id: {comment.comment_id}\n"
        f"Review-Comment-Url: {comment.url}"
    )
    result = _git(config, "commit", "-m", msg)
    if result.returncode != 0:
        logger.warning("Fix commit failed", stderr=result.stderr.strip()[:200])
        return None
    return str(_git(config, "rev-parse", "HEAD").stdout).strip()


def _check_apply_preconditions(
    config: ExecutorConfig, state: ReviewPrState, repo: str, pr_number: int, meta: dict
) -> None:
    """Fail-closed preconditions for the mutating phase."""
    if _git(config, "rev-parse", "--git-dir").returncode != 0:
        raise ReviewPrError("not a git repository — cannot apply fixes")
    dirty = _dirty_paths(config)
    if dirty:
        raise ReviewPrError(
            "working tree is not clean — commit or stash before applying fixes:\n"
            + "\n".join(dirty)[:300]
        )
    local_head = _git(config, "rev-parse", "HEAD").stdout.strip()
    if local_head != meta["head_sha"]:
        raise ReviewPrError(
            f"local HEAD {local_head[:12]} != PR head {meta['head_sha'][:12]} — "
            f"check out the PR branch first (git checkout {meta['head_ref']})"
        )
    prev_sha = state.previous_round_sha(repo, pr_number, meta["head_sha"])
    if prev_sha:
        ancestor = _git(config, "merge-base", "--is-ancestor", prev_sha, meta["head_sha"])
        if ancestor.returncode != 0:
            # Rewritten history (force-push) — or a SHA git cannot resolve.
            # Either way the stored state no longer describes this branch.
            raise ReviewPrError(
                f"previous round SHA {prev_sha[:12]} is not an ancestor of the "
                "current head — history was rewritten (force-push?); resolve manually"
            )


def _reply(config: ExecutorConfig, repo: str, pr_number: int, comment_id: int, body: str) -> bool:
    result = _gh(
        config,
        "api",
        "-X",
        "POST",
        f"repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
        "-f",
        f"body={body}",
    )
    if result.returncode != 0:
        logger.warning(
            "Reply failed — will retry on the next invocation",
            comment_id=comment_id,
            stderr=result.stderr.strip()[:200],
        )
        return False
    return True


def _print_text_report(repo: str, pr_number: int, rows: list[dict], new_count: int) -> None:
    print(f"\n🔍 review-pr {repo}#{pr_number} — {len(rows)} bot comment(s), {new_count} new")
    icons = {VERDICT_VALID: "🟠", VERDICT_REFUTED: "🟢", VERDICT_UNCERTAIN: "❓", None: "⬜"}
    for row in rows:
        icon = icons.get(row["verdict"], "⬜")
        loc = f"{row['path']}:{row['line']}" if row["path"] else "(no file)"
        print(f"  {icon} [{row['verdict'] or 'unverified'}] {loc} — comment {row['comment_id']}")
        if row["evidence"]:
            print(f"     {row['evidence'][:300]}")
    counts = _verdict_counts(rows)
    print(
        f"\n  valid: {counts['valid']}  refuted: {counts['refuted']}  "
        f"uncertain: {counts['uncertain']}  unverified: {counts['unverified']}  "
        f"fixed: {counts['fixed']}  replied: {counts['replied']}"
    )
    if counts["uncertain"] or counts["unverified"]:
        print("  ⚠️  NEEDS_HUMAN: uncertain/unverified comments require an operator")


def _verdict_counts(rows: list[dict]) -> dict:
    counts = {"valid": 0, "refuted": 0, "uncertain": 0, "unverified": 0, "fixed": 0, "replied": 0}
    for row in rows:
        key = row["verdict"] if row["verdict"] in counts else "unverified"
        counts[key] += 1
        if row.get("resolution") == "fixed":
            counts["fixed"] += 1
        if row.get("replied_at"):
            counts["replied"] += 1
    return counts


def _row_complete(row: dict) -> bool:
    """A comment is fully handled when it is fixed or refuted AND replied."""
    return row.get("resolution") in ("fixed", "refuted") and bool(row.get("replied_at"))


def _apply_phase(
    args,
    config: ExecutorConfig,
    state: ReviewPrState,
    repo: str,
    pr_number: int,
    meta: dict,
    comment_map: dict[int, BotComment],
    started_at: float,
) -> None:
    """M2: fix valid comments, gate, push, reply. All limits fail to human.

    Mutates only via per-comment fix commits; every failure path records
    resolution='needs_human' and moves on. Replies are published only after
    a successful push (fixes) — refutation replies need no push.
    """
    import time

    rows = state.rows(repo, pr_number)
    todo = [r for r in rows if r["verdict"] and not r["resolution"]]
    if len(todo) > config.review_pr_max_comments:
        print(
            f"⚠️  comment limit exceeded ({len(todo)} > {config.review_pr_max_comments}) "
            "— NEEDS_HUMAN, no fixes applied"
        )
        return

    # Cheap, git-free resolutions first: deleted comments and refutations.
    fixable = []
    for row in todo:
        cid = row["comment_id"]
        if comment_map.get(cid) is None:
            # Stored but no longer on the PR: deleted comment — fail-closed.
            state.set_resolution(repo, pr_number, cid, "deleted")
            logger.warning("Stored comment no longer on the PR", comment_id=cid)
        elif row["verdict"] == VERDICT_REFUTED:
            state.set_resolution(repo, pr_number, cid, "refuted")
        elif row["verdict"] == VERDICT_VALID:
            fixable.append(row)
        # uncertain: human's call — no fix, no auto-pushback

    # Mutating work only below — git preconditions and bounded rounds
    # apply to fixes, not to refutation replies.
    if fixable:
        _check_apply_preconditions(config, state, repo, pr_number, meta)
        round_count = state.start_round(repo, pr_number, meta["head_sha"])
        if round_count > config.review_pr_max_rounds:
            logger.warning(
                "review-pr round limit exceeded",
                rounds=round_count,
                limit=config.review_pr_max_rounds,
            )
            print(
                f"⚠️  round limit exceeded ({round_count} > {config.review_pr_max_rounds}) "
                "— NEEDS_HUMAN, no fixes applied"
            )
            fixable = []

    spent_usd = 0.0
    pushed_shas: list[tuple[int, str]] = []  # (comment_id, fix_sha) awaiting push
    for row in fixable:
        if (time.monotonic() - started_at) / 60 > config.review_pr_max_wall_minutes:
            logger.warning("review-pr wall-clock limit hit — stopping")
            break
        cid = row["comment_id"]
        comment = comment_map[cid]
        if spent_usd > config.review_pr_max_cost_usd:
            logger.warning("review-pr cost limit hit — stopping fixes", spent=spent_usd)
            break
        pre_fix_head = _git(config, "rev-parse", "HEAD").stdout.strip()
        ok, note, cost = run_fix_agent(comment, row["evidence"] or "", repo, pr_number, config)
        spent_usd += cost
        if not ok:
            _git(config, "reset", "--hard", pre_fix_head)
            state.set_resolution(repo, pr_number, cid, "needs_human")
            logger.warning("Fix agent failed", comment_id=cid, note=note)
            continue
        if not _worktree_fingerprint(config).strip():
            state.set_resolution(repo, pr_number, cid, "needs_human")
            logger.warning("Fix agent changed nothing", comment_id=cid)
            continue
        gates_ok, gate_detail = _run_gates(config)
        if not gates_ok:
            _git(config, "reset", "--hard", pre_fix_head)
            state.set_resolution(repo, pr_number, cid, "needs_human")
            logger.warning("Gates failed after fix — reverted", comment_id=cid, detail=gate_detail)
            continue
        sha = _commit_fix(config, comment, note)
        if sha is None:
            _git(config, "reset", "--hard", pre_fix_head)
            state.set_resolution(repo, pr_number, cid, "needs_human")
            continue
        if _changed_lines_in_head(config) > config.review_pr_max_changed_lines:
            _git(config, "reset", "--hard", pre_fix_head)
            state.set_resolution(repo, pr_number, cid, "needs_human")
            logger.warning("Fix exceeds diff-size limit — reverted", comment_id=cid)
            continue
        state.set_resolution(repo, pr_number, cid, "fixed", fix_sha=sha)
        pushed_shas.append((cid, sha))

    # Push once for all fix commits; replies only after a successful push.
    if pushed_shas:
        push = _git(config, "push", "origin", f"HEAD:refs/heads/{meta['head_ref']}")
        if push.returncode != 0:
            raise ReviewPrError(
                f"push failed — no replies published (fixes stay local): "
                f"{push.stderr.strip()[:200]}"
            )

    # Replies: fixed (with the actual SHA) and refuted (with evidence).
    # Idempotent — replied_at guards against double replies across runs.
    for row in state.rows(repo, pr_number):
        if row["replied_at"] or row["resolution"] not in ("fixed", "refuted"):
            continue
        if row["resolution"] == "fixed":
            body = (
                f"Addressed in {row['fix_sha']}.\n\n"
                f"_(automated: spec-runner review-pr; verified before fixing)_"
            )
        else:
            evidence = (row["evidence"] or "").strip() or "see verification log"
            body = (
                f"Not applying — verification refuted this comment.\n\n"
                f"Evidence: {evidence[:800]}\n\n"
                f"_(automated: spec-runner review-pr)_"
            )
        if _reply(config, repo, pr_number, row["comment_id"], body):
            state.mark_replied(repo, pr_number, row["comment_id"])


def needs_human_rows(config: ExecutorConfig) -> list[tuple[str, int, int]]:
    """(repo, pr_number, count) of comments awaiting a human — for `status`."""
    import sqlite3 as _sqlite3

    if not config.state_file.exists():
        return []
    try:
        conn = _sqlite3.connect(f"file:{config.state_file}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT repo, pr_number, COUNT(*) FROM pr_review_comments "
                "WHERE resolution IN ('needs_human', 'deleted') "
                "   OR (verdict = 'uncertain' AND resolution IS NULL) "
                "GROUP BY repo, pr_number"
            ).fetchall()
            return [(r[0], int(r[1]), int(r[2])) for r in rows]
        finally:
            conn.close()
    except _sqlite3.Error:
        return []  # no table yet / unreadable — status must never break


def cmd_review_pr(args, config: ExecutorConfig) -> int:
    """`spec-runner review-pr <url-or-number>` — the durable review-bot loop.

    Default: collect → verify → fix valid → gates → push → reply (M2).
    ``--verify-only`` stops after verdicts (the M1 behavior);
    ``--no-verify`` stops after collection.
    """
    import time

    started_at = time.monotonic()
    try:
        repo, pr_number = parse_pr_ref(args.pr_ref, config)
        meta = fetch_pr_meta(config, repo, pr_number)
        if meta["draft"]:
            print(f"⛔ PR {repo}#{pr_number} is a draft — review-pr is fail-closed on drafts")
            return EXIT_FAIL
        if meta["state"] != "open":
            print(f"⛔ PR {repo}#{pr_number} is {meta['state']} — review-pr only runs on open PRs")
            return EXIT_FAIL

        allowed = config.review_pr_allowed_bots
        comments = fetch_bot_comments(config, repo, pr_number, allowed)
        comment_map = {c.comment_id: c for c in comments}

        with ReviewPrState(config) as state:
            known = state.known_ids(repo, pr_number)
            new = [c for c in comments if c.comment_id not in known]
            # Comments collected earlier without a verdict (a --no-verify
            # run, or a crash mid-verification) are re-queued — the cursor
            # skips re-COLLECTING, never re-VERIFYING what has no verdict.
            pending_ids = state.unverified_ids(repo, pr_number)
            pending = [c for c in comments if c.comment_id in pending_ids]
            logger.info(
                "review-pr collected",
                repo=repo,
                pr=pr_number,
                total=len(comments),
                new=len(new),
                pending_unverified=len(pending),
                allowed_bots=allowed,
            )

            for comment in new:
                state.record(repo, pr_number, meta["head_sha"], comment)

            baseline = _worktree_fingerprint(config)
            for comment in new + pending:
                if getattr(args, "no_verify", False):
                    continue
                verdict, evidence = verify_comment(comment, repo, pr_number, config)
                after = _worktree_fingerprint(config)
                if after != baseline:
                    # Read-only guard: a verifier that touched the tree
                    # forfeits its verdict — a human sorts it out.
                    logger.error(
                        "Verifier modified the working tree — verdict discarded",
                        comment_id=comment.comment_id,
                    )
                    verdict = VERDICT_UNCERTAIN
                    evidence = (
                        "Verifier modified the working tree during read-only "
                        "verification; its verdict was discarded. Inspect "
                        "`git status` before trusting the tree."
                    )
                    baseline = after  # don't cascade the taint to later comments
                state.set_verdict(repo, pr_number, comment.comment_id, verdict, evidence)

            # M2: fix + reply, unless a read-only mode was requested
            read_only = getattr(args, "no_verify", False) or getattr(args, "verify_only", False)
            if not read_only and any(
                r["verdict"] and not _row_complete(r) for r in state.rows(repo, pr_number)
            ):
                _apply_phase(args, config, state, repo, pr_number, meta, comment_map, started_at)

            rows = state.rows(repo, pr_number)

        counts = _verdict_counts(rows)
        if read_only:
            # M1 contract preserved: verified valid/refuted is a clean exit.
            needs_human = bool(counts["uncertain"] or counts["unverified"])
        else:
            needs_human = not all(_row_complete(r) for r in rows) if rows else False
        if getattr(args, "json_output", False):
            print(
                json.dumps(
                    {
                        "repo": repo,
                        "pr_number": pr_number,
                        "head_sha": meta["head_sha"],
                        "new_comments": len(new),
                        "comments": rows,
                        "counts": counts,
                        "needs_human": needs_human,
                    },
                    indent=2,
                )
            )
        else:
            _print_text_report(repo, pr_number, rows, len(new))

        return EXIT_NEEDS_HUMAN if needs_human else EXIT_OK

    except ReviewPrError as exc:
        print(f"⛔ review-pr: {exc}")
        return EXIT_FAIL
