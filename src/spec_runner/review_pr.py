"""Read-only review-bot loop — phase M1 of `spec-runner review-pr` (#102).

Collects inline review comments left by allowed bot identities on a GitHub
PR, verifies each against the actual codebase with an agent call, persists
per-comment verdicts (durable cursor: a stored comment is never
re-processed), and prints a text or ``--json`` report.

M1 is strictly read-only: no fixes, no replies, no pushes. Design:
docs/superpowers/specs/2026-08-06-review-pr-loop-design.md.

Exit-code contract (stable for external callers):
    0 — all collected comments verified as valid or refuted
    1 — fail-closed condition (draft/closed PR, gh/API failure, bad ref)
    2 — NEEDS_HUMAN: at least one comment is uncertain or unverified
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
        self._conn.commit()

    def known_ids(self, repo: str, pr_number: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT comment_id FROM pr_review_comments WHERE repo = ? AND pr_number = ?",
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
            "SELECT comment_id, author, path, line, url, verdict, evidence "
            "FROM pr_review_comments WHERE repo = ? AND pr_number = ? ORDER BY comment_id",
            (repo, pr_number),
        )
        cols = ["comment_id", "author", "path", "line", "url", "verdict", "evidence"]
        return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]

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
    decides), never a guessed verdict.
    """
    matches = re.findall(r"VERDICT:\s*(VALID|REFUTED|UNCERTAIN)", output, re.IGNORECASE)
    if not matches:
        tail = output.strip()[-200:] if output.strip() else "(empty output)"
        return VERDICT_UNCERTAIN, f"No VERDICT marker in verifier output; tail: {tail}"
    verdict = matches[-1].lower()
    m = re.search(r"EVIDENCE:\s*(.+)", output, re.IGNORECASE | re.DOTALL)
    evidence = m.group(1).strip()[:2000] if m else ""
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


def _worktree_fingerprint(config: ExecutorConfig) -> str:
    """Cheap fingerprint of uncommitted changes (read-only guard)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    return result.stdout


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
        f"uncertain: {counts['uncertain']}  unverified: {counts['unverified']}"
    )
    if counts["uncertain"] or counts["unverified"]:
        print("  ⚠️  NEEDS_HUMAN: uncertain/unverified comments require an operator")
    print("  (M1 is read-only: no fixes, no replies — see the design doc)")


def _verdict_counts(rows: list[dict]) -> dict:
    counts = {"valid": 0, "refuted": 0, "uncertain": 0, "unverified": 0}
    for row in rows:
        key = row["verdict"] if row["verdict"] in counts else "unverified"
        counts[key] += 1
    return counts


def cmd_review_pr(args, config: ExecutorConfig) -> int:
    """`spec-runner review-pr <url-or-number>` — M1 read-only loop."""
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

        with ReviewPrState(config) as state:
            known = state.known_ids(repo, pr_number)
            new = [c for c in comments if c.comment_id not in known]
            logger.info(
                "review-pr collected",
                repo=repo,
                pr=pr_number,
                total=len(comments),
                new=len(new),
                allowed_bots=allowed,
            )

            baseline = _worktree_fingerprint(config)
            for comment in new:
                state.record(repo, pr_number, meta["head_sha"], comment)
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

            rows = state.rows(repo, pr_number)

        counts = _verdict_counts(rows)
        needs_human = bool(counts["uncertain"] or counts["unverified"])
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
