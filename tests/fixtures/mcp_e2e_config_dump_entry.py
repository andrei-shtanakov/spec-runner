"""Test entry point for BEH-09 (spec-runner#485): a `child_entry()` seam
replacement that resolves an `ExecutorConfig` exactly the way the real
`spec_runner run` invocation would (`_build_parser()` + `build_config`
against the YAML found by `--project-root`), dumps it as JSON to
`MCP_E2E_CONFIG_DUMP_FILE`, and exits -- instead of running any task.

Invoked as ``<sys.executable> <this file> run --task <id> ...`` by a test
that monkeypatches `spec_runner.mcp_launch.child_entry`, so `argv[1:]` is the
exact serialized argv `spec_runner_run_task` built.
"""

import dataclasses
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from spec_runner.cli import _build_parser  # noqa: E402
from spec_runner.config import (  # noqa: E402
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)
from spec_runner.mcp_launch import PARENT_ONLY_FIELDS, _jsonable  # noqa: E402


def main() -> None:
    argv = sys.argv[1:]
    args = _build_parser().parse_args(argv)
    config_path = _resolve_config_path(Path(args.project_root) if args.project_root else None)
    yaml_config = load_config_from_yaml(config_path)
    config = build_config(yaml_config, args)

    dump = {
        f.name: _jsonable(getattr(config, f.name))
        for f in dataclasses.fields(config)
        if f.name not in PARENT_ONLY_FIELDS
    }
    dump_file = Path(os.environ["MCP_E2E_CONFIG_DUMP_FILE"])
    dump_file.write_text(json.dumps(dump))


if __name__ == "__main__":
    main()
