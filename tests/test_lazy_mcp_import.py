"""Importing spec_runner must not import the mcp SDK (regression: mcp 2.0
made `import spec_runner` crash, taking spec-runner-init down with it)."""

import subprocess
import sys


def test_import_spec_runner_does_not_import_mcp() -> None:
    code = "import sys; import spec_runner; sys.exit(0 if 'mcp' not in sys.modules else 1)"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_mcp_run_server_attribute_still_resolves() -> None:
    import spec_runner

    assert callable(spec_runner.mcp_run_server)
