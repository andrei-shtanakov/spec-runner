"""`python -m spec_runner` -- the entry point `mcp_launch.child_entry()` uses
to launch a child in the parent's own interpreter/venv (#485 §2.3, BEH-11),
never a `spec-runner` console script that might resolve to a different venv
or nothing at all on a trimmed `PATH`.
"""

from .executor import main

if __name__ == "__main__":
    main()
