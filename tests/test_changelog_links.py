"""The release-hardening check: CHANGELOG compare links must match the version.

Twice now a release PR shipped with stale links — #100 the version link
definitions, #180 `[Unreleased]` still comparing from the previous tag. Both
are the same oversight: the link block lives ~1700 lines from the section being
cut, so the edit looks complete. A check is cheaper than a third catch.
"""

import importlib.util
from pathlib import Path

import pytest

# Loaded by path rather than via `sys.path.insert`: `scripts/` is not a package
# and mutating the import path at collection time leaks into every test that
# runs afterwards.
_spec = importlib.util.spec_from_file_location(
    "check_changelog_links",
    Path(__file__).resolve().parent.parent / "scripts" / "check_changelog_links.py",
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
check = _module.check

REPO = "https://github.com/andrei-shtanakov/spec-runner"


def _project(tmp_path: Path, version: str, changelog: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def _changelog(current="2.25.0", previous="2.24.0", unreleased_from=None, released_from=None):
    unreleased_from = unreleased_from or current
    released_from = released_from or previous
    return f"""# Changelog

## [Unreleased]

## [{current}] - 2026-08-11
- something

## [{previous}] - 2026-08-10
- something older

[Unreleased]: {REPO}/compare/v{unreleased_from}...HEAD
[{current}]: {REPO}/compare/v{released_from}...v{current}
[{previous}]: {REPO}/compare/v2.23.0...v{previous}
"""


class TestTheRealRepoPasses:
    def test_this_repository_is_consistent(self):
        assert check(Path(__file__).resolve().parent.parent) == []


class TestTheTwoMistakesThatActuallyHappened:
    def test_unreleased_left_at_the_previous_tag_is_caught(self, tmp_path):
        """PR #180: the cut moved the entries and left the link behind, so
        [Unreleased] claimed the whole release."""
        root = _project(tmp_path, "2.25.0", _changelog(unreleased_from="2.24.0"))
        [problem] = check(root)
        assert "[Unreleased]" in problem and "already released" in problem

    def test_a_released_link_with_the_wrong_base_is_caught(self, tmp_path):
        """PR #100: the version link definitions at the bottom went stale."""
        root = _project(tmp_path, "2.25.0", _changelog(released_from="2.23.0"))
        [problem] = check(root)
        assert "[2.25.0] should end with" in problem


class TestTheOtherWaysToGetItWrong:
    def test_a_bumped_version_with_no_section_is_caught(self, tmp_path):
        """The version was bumped and the section never cut."""
        root = _project(tmp_path, "2.26.0", _changelog())
        [problem] = check(root)
        assert "the release section was not cut" in problem

    def test_a_missing_unreleased_link_is_caught(self, tmp_path):
        body = _changelog().replace(f"[Unreleased]: {REPO}/compare/v2.25.0...HEAD\n", "")
        root = _project(tmp_path, "2.25.0", body)
        assert any("no [Unreleased] link" in p for p in check(root))

    def test_a_missing_released_link_is_caught(self, tmp_path):
        body = _changelog().replace(f"[2.25.0]: {REPO}/compare/v2.24.0...v2.25.0\n", "")
        root = _project(tmp_path, "2.25.0", body)
        assert any("no [2.25.0] link" in p for p in check(root))

    def test_a_correct_cut_passes(self, tmp_path):
        assert check(_project(tmp_path, "2.25.0", _changelog())) == []

    @pytest.mark.parametrize("version", ["2.25.0", "2.24.0"])
    def test_it_holds_between_releases_too(self, tmp_path, version):
        """Not only at a release: the invariant is true of every commit, which
        is what makes it a guard rather than a checklist item."""
        changelog = _changelog(current=version, previous="2.23.0")
        assert check(_project(tmp_path, version, changelog)) == []


class TestTheGuardFailsLegibly:
    """Its own failure mode matters: a guard that ends in a traceback teaches
    people to ignore what it prints."""

    def test_a_missing_pyproject_is_a_problem_not_a_crash(self, tmp_path):
        [problem] = check(tmp_path)
        assert "pyproject.toml" in problem and "repository root" in problem

    def test_a_missing_changelog_is_a_problem_not_a_crash(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.25.0"\n')
        [problem] = check(tmp_path)
        assert "CHANGELOG.md" in problem

    def test_the_files_are_read_as_utf8(self, tmp_path):
        """The CHANGELOG is full of em-dashes; a runner's locale is not ours to
        assume."""
        root = _project(tmp_path, "2.25.0", _changelog().replace("something", "— dashed —"))
        assert check(root) == []
