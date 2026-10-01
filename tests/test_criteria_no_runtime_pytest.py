"""#603: spec-runner must not need pytest at runtime; the probe is shipped as text."""

import subprocess
import sys

# B2b appends criteria_measure and cli as they land.
MODULES = [
    "criteria_contract",
    "criteria_process",
    "criteria_protocol",
    "criteria_config",
    "criteria_workspace",
    "criteria_inventory",
    "criteria_select",
    "criteria_aggregate",
    "criteria_run",
]


def test_orchestrator_modules_import_without_pytest():
    code = (
        "import sys; sys.modules['pytest'] = None; sys.modules['_pytest'] = None\n"
        + "".join(f"import spec_runner.{m}\n" for m in MODULES)
        + "import pathlib, tempfile\n"
        + "from spec_runner.criteria_inventory import deploy_probe\n"
        + "d = deploy_probe(pathlib.Path(tempfile.mkdtemp()))\n"
        + "assert (d / '_spec_runner_criteria_probe.py').read_text().startswith('\"\"\"')\n"
        + "assert 'spec_runner.criteria_probe' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
