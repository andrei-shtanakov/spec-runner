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


class TestDuplicateSectionsAreCaught:
    """Twice now an insertion produced a second `### Added` inside
    `[Unreleased]`, splitting one list in half. Invisible while editing a file
    this long — the same reason the link check exists."""

    def _write(self, tmp_path: Path, changelog: str) -> Path:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.25.0"\n')
        (tmp_path / "CHANGELOG.md").write_text(changelog)
        return tmp_path

    def test_two_added_sections_in_one_version_are_a_problem(self, tmp_path):
        body = _changelog().replace(
            "## [2.25.0] - 2026-08-11\n- something",
            "## [2.25.0] - 2026-08-11\n\n### Added\n- one\n\n### Added\n- two",
        )
        problems = check(self._write(tmp_path, body))
        assert any("two '### Added' sections" in p for p in problems)

    def test_the_same_heading_in_different_versions_is_fine(self, tmp_path):
        body = (
            _changelog()
            .replace(
                "## [2.25.0] - 2026-08-11\n- something",
                "## [2.25.0] - 2026-08-11\n\n### Added\n- one",
            )
            .replace(
                "## [2.24.0] - 2026-08-10\n- something older",
                "## [2.24.0] - 2026-08-10\n\n### Added\n- older",
            )
        )
        assert not [p for p in check(self._write(tmp_path, body)) if "sections" in p]

    def test_the_real_changelog_has_no_duplicates(self):
        assert not [p for p in check(Path(__file__).resolve().parent.parent) if "sections" in p]


class TestDuplicateEntriesAreCaught:
    """The failure mode one level down (Copilot, PR #265): not two headings but
    the same *entry* twice.

    Both times it was written by a script whose first run failed after the
    insertion, so the re-run appended a near-identical bullet — which is why
    the check keys on the bolded title rather than the body. Release notes are
    read by people who count the entries."""

    def _write(self, tmp_path: Path, changelog: str) -> Path:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.25.0"\n')
        (tmp_path / "CHANGELOG.md").write_text(changelog)
        return tmp_path

    def test_the_same_entry_twice_in_one_version_is_a_problem(self, tmp_path):
        body = _changelog().replace(
            "## [Unreleased]",
            "## [Unreleased]\n\n### Fixed\n\n- **Claims are released** (#260). one\n\n"
            "- **Claims are released** (#260). one, again with a different word",
            1,
        )
        problems = check(self._write(tmp_path, body))
        assert any("two entries titled 'Claims are released'" in p for p in problems)

    def test_different_entries_are_fine(self, tmp_path):
        body = _changelog().replace(
            "## [Unreleased]",
            "## [Unreleased]\n\n### Fixed\n\n- **Claims are released** (#260). one\n\n"
            "- **Repair reads the replay** (#263). two",
            1,
        )
        assert not [p for p in check(self._write(tmp_path, body)) if "entries titled" in p]

    def test_the_same_title_in_different_versions_is_fine(self, tmp_path):
        """A fix released twice — a backport, a revert-and-reland — is two
        entries in two sections, and neither is a duplicate of the other."""
        body = (
            _changelog()
            .replace("## [2.25.0] - 2026-08-11\n- something", "## [2.25.0]\n\n- **X** (#1). a")
            .replace(
                "## [2.24.0] - 2026-08-10\n- something older", "## [2.24.0]\n\n- **X** (#1). a"
            )
        )
        assert not [p for p in check(self._write(tmp_path, body)) if "entries titled" in p]

    def test_the_real_changelog_has_no_duplicate_entries(self):
        root = Path(__file__).resolve().parent.parent
        assert not [p for p in check(root) if "entries titled" in p]


class TestTheTagTimeCheck:
    """The second enforcement point (#192 follow-up, owner's item 3).

    The PR run gives early feedback; the tag/publish run is the fail-closed
    barrier, because the one moment the links *must* be right is the moment the
    artifact goes out. Deliberately the **same script** — a second validator
    would be a second thing to keep in step with the first.
    """

    def test_a_matching_tag_passes(self, tmp_path):
        assert check(_project(tmp_path, "2.25.0", _changelog()), tag="v2.25.0") == []

    def test_a_tag_that_does_not_match_pyproject_is_caught(self, tmp_path):
        problems = check(_project(tmp_path, "2.25.0", _changelog()), tag="v2.26.0")
        assert any("v2.26.0" in p and "2.25.0" in p for p in problems)

    def test_the_bare_version_form_is_accepted(self, tmp_path):
        """`GITHUB_REF_NAME` carries the `v`; a human running it locally may
        not. Refusing the bare form would only teach people to skip the check."""
        assert check(_project(tmp_path, "2.25.0", _changelog()), tag="2.25.0") == []

    def test_a_tag_with_no_changelog_section_is_caught(self, tmp_path):
        """The section is what the GitHub Release is written from — publishing
        without one means shipping with no notes."""
        body = _changelog().replace("## [2.25.0] - 2026-08-11\n- something\n\n", "")
        problems = check(_project(tmp_path, "2.25.0", body), tag="v2.25.0")
        assert any("no CHANGELOG section" in p or "was not cut" in p for p in problems)

    def test_stale_links_still_fail_at_tag_time(self, tmp_path):
        """The whole point of the second enforcement point: the mistake that
        got through twice must not get through here either."""
        root = _project(tmp_path, "2.25.0", _changelog(unreleased_from="2.24.0"))
        assert check(root, tag="v2.25.0")

    def test_a_junk_tag_is_a_stated_problem_not_a_crash(self, tmp_path):
        problems = check(_project(tmp_path, "2.25.0", _changelog()), tag="release-candidate")
        assert problems and all(isinstance(p, str) for p in problems)

    def test_without_a_tag_nothing_new_is_required(self, tmp_path):
        """The PR run must stay exactly as permissive as it was — a release PR
        legitimately has no tag yet."""
        assert check(_project(tmp_path, "2.25.0", _changelog())) == []

    def test_an_empty_changelog_does_not_hide_the_tag_mismatch(self, tmp_path):
        """Raised in review: the "no sections" branch used to return on its
        own, so a publish run could report only that while the tag disagreed
        with the version — the more actionable of the two."""
        root = _project(tmp_path, "2.25.0", "# Changelog\n\n## [Unreleased]\n")
        problems = check(root, tag="v2.26.0")
        assert any("does not match" in p for p in problems), problems
        assert any("no released version sections" in p for p in problems), problems
