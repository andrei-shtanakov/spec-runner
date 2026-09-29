"""Executor write boundary (#600): an OS sandbox around every agent call.

An agent runs with `skip_permissions` and may write anywhere by absolute path;
devtools#445 is a planted PASS in a sibling repo's run directory. Design and
per-preset measurements: docs/superpowers/specs/2026-09-29-executor-write-boundary-design.md.
"""

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from spec_runner import sandbox
from spec_runner.config import ConfigError, ExecutorConfig, build_config, load_config_from_yaml
from spec_runner.runner import CliInvocation
from spec_runner.sandbox import (
    SandboxUnavailable,
    require_backend,
    sandboxed,
    seatbelt_profile,
    writable_paths,
)

SRC = Path(sandbox.__file__).parent
HAS_SEATBELT = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    overrides.setdefault("test_command", "pytest")
    return ExecutorConfig(project_root=root, **overrides)


def _inv(*argv: str) -> CliInvocation:
    return CliInvocation(list(argv), "text")


def _fake(tmp_path: Path, name: str, body: str = "exit 0") -> str:
    """An executable named like an agent CLI, so identity and `which` hold
    without the real binary on the machine."""
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


class TestConfig:
    def test_default_is_off(self, tmp_path):
        assert _cfg(tmp_path).executor_sandbox == "off"

    def test_an_unknown_mode_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="executor_sandbox"):
            _cfg(tmp_path, executor_sandbox="strict")

    def test_an_empty_allow_entry_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="sandbox_allow has an empty entry"):
            _cfg(tmp_path, sandbox_allow=[""])

    @pytest.mark.parametrize("word,mode", [("on", "on"), ("off", "off"), ("required", "required")])
    def test_a_bare_yaml_word_is_taken_as_written(self, tmp_path, word, mode):
        """YAML 1.1 reads a bare `on`/`off` as a boolean."""
        (tmp_path / "c.yaml").write_text(f"executor_sandbox: {word}\n")
        raw = load_config_from_yaml(tmp_path / "c.yaml")["executor_sandbox"]
        assert _cfg(tmp_path, executor_sandbox=raw).executor_sandbox == mode

    def test_yaml_keys_reach_the_config(self, tmp_path, monkeypatch):
        from spec_runner.cli import _build_parser

        (tmp_path / "spec-runner.config.yaml").write_text(
            "executor_sandbox: on\nsandbox_allow: [~/.npm]\n"
        )
        monkeypatch.chdir(tmp_path)
        args = _build_parser().parse_args(["run"])
        config = build_config(
            load_config_from_yaml(tmp_path / "spec-runner.config.yaml"), args, detect_subdir=False
        )
        assert config.executor_sandbox == "on" and config.sandbox_allow == ["~/.npm"]


class TestOff:
    def test_argv_and_env_pass_through(self, tmp_path):
        env = {"A": "1"}
        call = sandboxed(_cfg(tmp_path), _inv("claude", "-p", "x"), env)
        assert call.argv == ["claude", "-p", "x"] and call.env is env and call.tmpdir is None
        call.cleanup()


class TestNoBackend:
    def test_on_runs_unwrapped_and_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sandbox, "backend", lambda: None)
        monkeypatch.setattr(sandbox, "_warned", set())
        call = sandboxed(_cfg(tmp_path, executor_sandbox="on"), _inv("claude", "-p", "x"))
        assert call.argv == ["claude", "-p", "x"]
        assert "no backend" in capsys.readouterr().err

    def test_required_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sandbox, "backend", lambda: None)
        config = _cfg(tmp_path, executor_sandbox="required")
        with pytest.raises(SandboxUnavailable):
            require_backend(config)
        with pytest.raises(SandboxUnavailable):
            sandboxed(config, _inv("claude", "-p", "x"))

    def test_the_cli_refuses_at_startup_with_exit_2(self, tmp_path, monkeypatch, capsys):
        """Before any paid call — an instrument error, like a gate that
        cannot answer."""
        from spec_runner import cli

        (tmp_path / "spec-runner.config.yaml").write_text("executor_sandbox: required\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sandbox, "backend", lambda: None)
        monkeypatch.setattr("sys.argv", ["spec-runner", "run"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
        assert "executor_sandbox is 'required'" in capsys.readouterr().err


@pytest.mark.skipif(not HAS_SEATBELT, reason="needs macOS sandbox-exec")
class TestTheWrappedCall:
    def test_argv_env_and_temp_dir(self, tmp_path):
        claude = _fake(tmp_path, "claude")
        call = sandboxed(_cfg(tmp_path, executor_sandbox="on"), _inv(claude, "-p", "x"), {})
        try:
            assert call.argv[:2] == ["sandbox-exec", "-p"]
            assert call.argv[3:] == [claude, "-p", "x"]
            assert call.tmpdir is not None and call.tmpdir.is_dir()
            assert call.env is not None
            assert call.env["TMPDIR"] == call.env["CLAUDE_CODE_TMPDIR"] == str(call.tmpdir)
        finally:
            call.cleanup()
        assert not call.tmpdir.exists()

    def test_codex_gets_its_external_sandbox_mode(self, tmp_path):
        """codex's own Seatbelt cannot nest inside ours (measured)."""
        codex = _fake(tmp_path, "codex")
        call = sandboxed(_cfg(tmp_path, executor_sandbox="on"), _inv(codex, "exec", "x"))
        try:
            assert call.argv[3:] == [
                codex,
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "x",
            ]
        finally:
            call.cleanup()


class TestTheWritableSet:
    def test_base_state_allow_and_uv_cache(self, tmp_path):
        tmpdir = tmp_path / "t"
        config = _cfg(
            tmp_path / "p",
            sandbox_allow=["~/.npm"],
            test_command="uv run pytest",
        )
        (tmp_path / "p").mkdir()
        paths = writable_paths(config, "codex", tmpdir)
        assert (tmp_path / "p").resolve() in paths and tmpdir in paths
        assert Path("~/.codex").expanduser().resolve() in paths
        assert Path("~/.npm").expanduser().resolve() in paths
        if shutil.which("uv"):
            uv_cache = subprocess.run(["uv", "cache", "dir"], capture_output=True, text=True)
            assert Path(uv_cache.stdout.strip()).resolve() in paths

    def test_no_uv_cache_without_uv(self, tmp_path):
        paths = writable_paths(_cfg(tmp_path), "claude", tmp_path / "t")
        assert paths == [tmp_path.resolve(), tmp_path / "t"]

    def test_the_git_dir_of_a_subdir_project(self, tmp_path):
        """An agent's `git commit` in a subdir project writes the parent's .git."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "app").mkdir()
        paths = writable_paths(_cfg(tmp_path / "app"), "claude", tmp_path / "t")
        assert (tmp_path / ".git").resolve() in paths

    def test_a_quote_in_a_path_cannot_break_the_profile(self):
        profile = seatbelt_profile([Path('/a"b')])
        assert '(subpath "/a\\"b")' in profile


@pytest.mark.skipif(not HAS_SEATBELT, reason="needs macOS sandbox-exec")
class TestTheBoundaryHolds:
    """The proof, with a fake CLI and no agent: inside is written, outside is
    refused by the OS."""

    def test_inside_written_outside_refused(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        fake = tmp_path / "fake-cli.sh"
        fake.write_text(
            f'#!/bin/sh\necho in > inside.txt || exit 3\necho out > "{outside}" || exit 4\n'
        )
        fake.chmod(0o755)
        call = sandboxed(
            ExecutorConfig(project_root=project, executor_sandbox="on"), _inv(str(fake))
        )
        try:
            result = subprocess.run(
                call.argv, cwd=project, env=call.env, capture_output=True, text=True
            )
        finally:
            call.cleanup()
        assert (project / "inside.txt").read_text() == "in\n"
        assert not outside.exists()
        assert result.returncode == 4, result.stderr

    def test_the_per_call_temp_dir_is_writable(self, tmp_path):
        fake = tmp_path / "fake-cli.sh"
        fake.write_text('#!/bin/sh\necho t > "$TMPDIR/x" && cat "$TMPDIR/x"\n')
        fake.chmod(0o755)
        call = sandboxed(
            ExecutorConfig(project_root=tmp_path, executor_sandbox="on"), _inv(str(fake))
        )
        try:
            result = subprocess.run(call.argv, env=call.env, capture_output=True, text=True)
        finally:
            call.cleanup()
        assert result.returncode == 0 and result.stdout == "t\n"


class TestEveryLaunchSiteGoesThroughTheSeam:
    """Structural: a function that builds an agent argv also calls
    `sandboxed` — the drift this repo has paid for before (#270, #241)."""

    BUILDERS = {"build_cli_invocation", "build_cli_command"}

    @staticmethod
    def _calls(node: ast.AST) -> set[str]:
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if isinstance(func, ast.Name):
                    names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    names.add(func.attr)
        return names

    @staticmethod
    def _builds_claude_argv(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.List) and sub.elts:
                first = sub.elts[0]
                if isinstance(first, ast.Attribute) and first.attr == "claude_command":
                    return True
        return False

    def _functions(self):
        for path in sorted(SRC.glob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield path, fn

    def test_no_unsandboxed_launch(self):
        """A builder counts as covered when it calls `sandboxed`, or hands the
        invocation to a function that does (`_execute_task` →
        `_run_agent_process`)."""
        seamed = {fn.name for _, fn in self._functions() if "sandboxed" in self._calls(fn)}
        offenders = []
        for path, fn in self._functions():
            if path.name in {"runner.py", "sandbox.py"}:
                continue  # the builders themselves; `run_claude_async` is library API
            calls = self._calls(fn)
            builds = calls & self.BUILDERS or self._builds_claude_argv(fn)
            if builds and not ({"sandboxed"} | seamed) & calls:
                offenders.append(f"{path.name}:{fn.name}")
        assert offenders == [], offenders

    def test_the_seam_is_found_at_every_known_site(self):
        """The scan must see all eight sites, or it proves nothing."""
        sites: dict[str, int] = {}
        for path in sorted(SRC.glob("*.py")):
            for sub in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "sandboxed"
                ):
                    sites[path.name] = sites.get(path.name, 0) + 1
        assert sites == {
            "cli_plan.py": 3,
            "execution.py": 1,
            "review.py": 1,
            "review_pr.py": 2,
            "tdd.py": 1,
        }


def test_env_is_not_mutated(tmp_path):
    """The caller's env dict is copied, never edited in place."""
    if not HAS_SEATBELT:
        pytest.skip("needs macOS sandbox-exec")
    env = dict(os.environ)
    before = dict(env)
    call = sandboxed(
        ExecutorConfig(project_root=tmp_path, executor_sandbox="on"),
        _inv(_fake(tmp_path, "x")),
        env,
    )
    call.cleanup()
    assert env == before


@pytest.mark.skipif(not HAS_SEATBELT, reason="needs macOS sandbox-exec")
class TestLocalReviewFindings:
    def test_a_missing_binary_still_fails_to_launch(self, tmp_path):
        """Under the wrapper argv[0] is `sandbox-exec`, which exists; a typo in
        the agent's name must still be a launch failure, not a recorded call."""
        with pytest.raises(FileNotFoundError):
            sandboxed(_cfg(tmp_path, executor_sandbox="on"), _inv("claud-typo", "-p", "x"))

    def test_codex_inside_a_template_is_named_and_warned(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sandbox, "_warned", set())
        call = sandboxed(
            _cfg(tmp_path, executor_sandbox="on"), _inv("/bin/sh", "-c", "codex exec 'x'")
        )
        try:
            assert call.argv[3:] == ["/bin/sh", "-c", "codex exec 'x'"]  # not rewritten
            assert Path("~/.codex").expanduser().resolve().as_posix() in call.argv[2]
        finally:
            call.cleanup()
        assert "--dangerously-bypass-approvals-and-sandbox" in capsys.readouterr().err

    def test_an_unknown_cli_is_warned_as_unmeasured(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sandbox, "_warned", set())
        call = sandboxed(_cfg(tmp_path, executor_sandbox="on"), _inv(_fake(tmp_path, "gemini")))
        call.cleanup()
        assert "gemini: writable state not measured" in capsys.readouterr().err


class TestIdentity:
    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["codex", "exec", "x"], ("codex", True)),
            (["/opt/bin/claude", "-p", "x"], ("claude", True)),
            (["bash", "-lc", "codex exec 'x'"], ("codex", False)),
            (["curl", "-s", "http://x"], ("curl", True)),
        ],
    )
    def test_the_cli_is_found_past_a_wrapper(self, argv, expected):
        assert sandbox.cli_identity(argv) == expected


def test_read_only_commands_are_not_refused(tmp_path, monkeypatch):
    """`required` without a backend refuses only commands that start an agent:
    `status` must keep working (local review of this change)."""
    from spec_runner import cli

    (tmp_path / "spec-runner.config.yaml").write_text("executor_sandbox: required\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sandbox, "backend", lambda: None)
    seen = []
    monkeypatch.setattr(cli, "cmd_status", lambda args, config: seen.append(True))
    monkeypatch.setattr("sys.argv", ["spec-runner", "status"])
    cli.main()
    assert seen == [True]


def test_every_preset_but_llama_cli_is_measured():
    """The table is the measurement (design §3); llama-cli was not installed."""
    from spec_runner.preset_cmd import list_presets

    assert set(list_presets()) - set(sandbox.STATE_DIRS) == {"llama-cli"}
    assert {"llama-cli"} == sandbox.UNMEASURED
    assert sandbox.STATE_DIRS["copilot"] == ("~/.copilot",)
