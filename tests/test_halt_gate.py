"""DarkFactory halt check (opt-in, contracts/halt-admission/v1 vendored)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from spec_runner import cli, halt_gate

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "halt-admission" / "v1"
DATA = json.loads((CONTRACT / "vectors.json").read_text())


@pytest.mark.parametrize("v", DATA["vectors"], ids=[v["name"] for v in DATA["vectors"]])
def test_every_vector(v: dict) -> None:
    assert halt_gate.decide(v["listing"], v["detail"])[:2] == (v["admit"], v["code"])


@pytest.mark.parametrize(
    "v", DATA["origin_vectors"], ids=[v["origin"] or "<empty>" for v in DATA["origin_vectors"]]
)
def test_every_origin_vector(v: dict) -> None:
    assert halt_gate.is_github_origin(v["origin"]) is v["github"]


def test_the_vendored_copy_is_the_pinned_one() -> None:
    lines = (CONTRACT / "PINNED.txt").read_text().splitlines()
    pinned = {ln.split("  ")[1]: ln.split("  ")[0] for ln in lines if len(ln.split("  ")) == 2}
    for name in ("README.md", "vectors.json"):
        assert pinned[name] == hashlib.sha256((CONTRACT / name).read_bytes()).hexdigest()


def test_off_without_the_flag() -> None:
    assert halt_gate.enabled({}) is False
    assert halt_gate.enabled({"DARKFACTORY_HALT_CHECK": "1"}) is True


@pytest.mark.parametrize(
    ("origin", "slug"),
    [
        ("git@github.com:o/r.git\n", "o/r"),
        ("https://github.com/o/r", "o/r"),
        ("https://github.com/o/r.git\n", "o/r"),  # live: get-url's newline
        ("deploy@github.com:o/r.git", "o/r"),  # any ssh user (review #631)
        ("https://x:t@github.com/o/r.git", "o/r"),
    ],
)
def test_slug_parsing(origin: str, slug: str) -> None:
    assert halt_gate._slug(origin) == slug


class Fake:
    """Stands in for `_run_rc`: git config answers per *origin* (None = key
    absent, rc 1; "!" = git failed, rc 128); gh per listing/detail."""

    def __init__(self, origin: str | None, listing: str | None, detail: str | None):
        self.origin, self.listing, self.detail = origin, listing, detail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[0] == "git":
            if self.origin is None:
                return 1, ""
            return (128, "") if self.origin == "!" else (0, self.origin + "\n")
        out = self.listing if "--jq" in argv else self.detail
        return (0, out) if out is not None else (1, "")


@pytest.mark.parametrize(
    ("origin", "listing", "detail", "expected"),
    [
        (None, None, None, (True, "admit_not_github")),
        ("git@gitlab.com:o/r.git", None, None, (True, "admit_not_github")),
        ("git@github.com:o/r.git", "", None, (True, "admit_missing")),
        (
            "git@github.com:o/r.git",
            '[7,"darkfactory-halt"]',
            '{"enforcement":"active"}',
            (False, "refuse_on"),
        ),
        (
            "git@github.com:o/r.git",
            '[7,"darkfactory-halt"]',
            '{"enforcement":"disabled"}',
            (True, "admit_off"),
        ),
        ("git@github.com:o/r.git", None, None, (False, "refuse_unknown")),
        # Review #631: an UNREAD origin is unknown, never "not GitHub".
        ("!", None, None, (False, "refuse_unknown")),
        ("deploy@github.com:o/r.git", "", None, (True, "admit_missing")),
    ],
)
def test_check(monkeypatch, tmp_path, origin, listing, detail, expected) -> None:
    fake = Fake(origin, listing, detail)
    monkeypatch.setattr(halt_gate, "_run_rc", fake)
    assert halt_gate.check(tmp_path)[:2] == expected
    if expected[1] == "admit_not_github":
        assert all(c[0] == "git" for c in fake.calls)  # never asks GitHub


class _Config:
    def __init__(self, root: Path) -> None:
        self.project_root = root


def test_the_run_guard_is_inert_without_the_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DARKFACTORY_HALT_CHECK", raising=False)
    monkeypatch.setattr(halt_gate, "check", lambda root: pytest.fail("must not check"))
    cli._enforce_halt(_Config(tmp_path))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("decision", "code"),
    [((False, "refuse_on", "on"), 6), ((False, "refuse_unknown", "x"), 2)],
)
def test_the_run_guard_refuses(monkeypatch, tmp_path, capsys, decision, code) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    monkeypatch.setattr(halt_gate, "check", lambda root: decision)
    with pytest.raises(SystemExit) as exc:
        cli._enforce_halt(_Config(tmp_path))  # type: ignore[arg-type]
    assert exc.value.code == code
    assert "DarkFactory halt" in capsys.readouterr().err


def test_every_run_entry_asks(monkeypatch) -> None:
    """run, retry and watch start NEW work — each asks before governance."""
    import inspect

    src = inspect.getsource(cli)
    assert src.count("    _enforce_halt(config)\n    _enforce_spec_governance(config)") == 3


def test_a_real_checkout_without_origin_admits(monkeypatch, tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert halt_gate.check(tmp_path)[:2] == (True, "admit_not_github")


@pytest.mark.parametrize(
    ("origin", "github"),
    [
        ("deploy@github.com:o/r.git", True),
        ("github.com:o/r.git", True),
        ("git@github.com", False),
        ("C:/path/repo", False),
    ],
)
def test_scp_forms(origin: str, github: bool) -> None:
    assert halt_gate.is_github_origin(origin) is github
    halt_gate._slug(origin)  # never raises (review #631)


def test_the_watch_daemon_asks_per_task(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DARKFACTORY_HALT_CHECK", "1")
    answers = iter([(False, "refuse_on", "on"), (True, "admit_off", "off")])
    monkeypatch.setattr(halt_gate, "check", lambda root: next(answers))
    assert cli._halt_admits(_Config(tmp_path)) is False  # type: ignore[arg-type]
    assert cli._halt_admits(_Config(tmp_path)) is True  # type: ignore[arg-type]
    import inspect

    assert "_halt_admits(config)" in inspect.getsource(cli.cmd_watch)
