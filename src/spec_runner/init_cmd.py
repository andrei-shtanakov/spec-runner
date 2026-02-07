"""
spec-runner init — install Claude Code skills to project.

Usage:
    spec-runner-init              # Install to .claude/skills in current directory
    spec-runner-init /path/to/project
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def get_skills_source_dir() -> Path:
    """Get the directory containing bundled skills."""
    return Path(__file__).parent / "skills" / "spec-generator-skill"


def install_skills(target_dir: Path, force: bool = False) -> bool:
    """
    Install skills to target directory.

    Args:
        target_dir: Directory where .claude/skills will be created
        force: Overwrite existing skills if True

    Returns:
        True if installation succeeded, False otherwise
    """
    source_dir = get_skills_source_dir()
    if not source_dir.exists():
        print(f"Error: Skills source not found at {source_dir}", file=sys.stderr)
        return False

    skills_dest = target_dir / ".claude" / "skills" / "spec-generator-skill"

    if skills_dest.exists() and not force:
        print(f"Skills already exist at {skills_dest}")
        print("Use --force to overwrite")
        return False

    # Create parent directories
    skills_dest.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing if force
    if skills_dest.exists():
        shutil.rmtree(skills_dest)

    # Copy skills
    shutil.copytree(source_dir, skills_dest)
    print(f"✓ Installed skills to {skills_dest}")
    return True


def main() -> None:
    """CLI entrypoint for spec-runner-init."""
    parser = argparse.ArgumentParser(
        description="Install Claude Code skills for spec-runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    spec-runner-init              # Install to ./.claude/skills
    spec-runner-init --force      # Overwrite existing skills
    spec-runner-init /path/to/project
        """,
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory (default: current directory)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing skills",
    )

    args = parser.parse_args()

    target_dir = Path(args.target).resolve()

    if not target_dir.is_dir():
        print(f"Error: {target_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    success = install_skills(target_dir, force=args.force)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
