"""RED for TASK-004 (spec-runner#485, DT-04): child-side ready handshake.

`spec_runner_run_task` waits for the real child (`spec-runner run --task
...`, spawned exactly as `mcp_server.spec_runner_run_task` does it in
production) to publish `config.ready_file` before answering `started`
(design §3.1, BEH-15). Nothing in `cli._run_tasks_inner` writes that file
yet, so a real child that completes its task successfully still never
publishes ready -- the parent must see it exit without ever having
answered `started`. This is the "at least one red on a real child
process" the decomposition's red-frame for DT-04 asks for
(workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/30-decomposition.md#DT-04),
not a `Popen` double.
"""

import json
from pathlib import Path
from unittest.mock import patch

import spec_runner.mcp_server as server
from spec_runner.cli import _build_parser
from spec_runner.config import _resolve_config_path, build_config, load_config_from_yaml
from spec_runner.mcp_server import spec_runner_run_task

FAKE_CLI = Path(__file__).parent / "fixtures" / "fake_claude.sh"

CONFIG_YAML = """\
claude_command: "{fake_cli}"
command_template: "{{cmd}} -p {{prompt}}"
skip_permissions: true
mcp_ready_timeout_seconds: 3
max_retries: 1
task_timeout_minutes: 1
hooks:
  pre_start:
    create_git_branch: false
  post_done:
    run_tests: false
    run_lint: false
    auto_commit: false
    run_review: false
"""

TASKS_MD = """\
# Tasks

### TASK-001: Add login page
\U0001f7e0 P1 | ⬜ TODO | Est: 1h

**Checklist:**
- [ ] Create login form
"""


class TestBEH15StartedNotBeforeReadyOnARealChild:
    def test_real_child_that_completes_its_task_never_reports_started(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project_root = tmp_path / "project"
        (project_root / "spec").mkdir(parents=True)
        (project_root / "spec-runner.config.yaml").write_text(
            CONFIG_YAML.format(fake_cli=str(FAKE_CLI))
        )
        (project_root / "spec" / "tasks.md").write_text(TASKS_MD)

        response_file = tmp_path / "response.txt"
        response_file.write_text("TASK_COMPLETE\n")
        monkeypatch.setenv("FAKE_RESPONSE_FILE", str(response_file))
        monkeypatch.setenv("FAKE_EXIT_CODE", "0")
        monkeypatch.delenv("FAKE_DELAY", raising=False)

        # Build the parent's launch config exactly the way the real CLI
        # would (same YAML, same parser) -- not a hand-built ExecutorConfig
        # that might drift from what `spec_runner_run_task` itself resolves.
        args = _build_parser().parse_args(["run", "--project-root", str(project_root)])
        config_path = _resolve_config_path(project_root)
        yaml_config = load_config_from_yaml(config_path)
        config = build_config(yaml_config, args)

        assert not config.ready_file.exists()

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            # The child (a real `spec-runner run --task TASK-001` subprocess,
            # not a double) completes TASK-001 via the fake CLI and exits 0
            # long before the 3s ready-timeout elapses. Once the child-side
            # handshake (DT-04) publishes `config.ready_file` right after
            # `clear_stop_file`, the parent observes it and answers
            # `started` instead of reporting the child's exit.
            assert result["status"] == "started", result

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)
