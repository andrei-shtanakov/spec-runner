#!/usr/bin/env bash
# Standalone pi-driven test-hardening step (the "+ pi test generator" stage).
#
# Runs the pi coding agent with the pi-tester skill to add edge-case / error-path /
# regression tests for a target module, then run them. This is the RECOMMENDED way to use
# pi-tester: a plugin post_done hook is bounded by spec-runner's 60s hook timeout, which is
# too short for a real authoring pass — run this manually or from a Makefile instead.
#
# Usage:
#   scripts/pi-author-tests.sh <module-or-task>      # e.g. slugify.py  or  TASK-001
#
# Honors SR_* env when invoked from a plugin hook (SR_TASK_ID, SR_PROJECT_ROOT).
set -euo pipefail

# Resolve the demo root (this script lives in <root>/scripts).
ROOT="${SR_PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

TARGET="${1:-${SR_TASK_ID:-}}"
if [[ -z "$TARGET" ]]; then
  echo "usage: $0 <module-or-task>" >&2
  exit 2
fi

MODEL="${PI_MODEL:-openai-codex/gpt-5.4}"

exec pi -p --model "$MODEL" \
  --tools read,write,edit,bash,grep,find,ls \
  --skill .pi/skills/pi-tester \
  "Harden the tests for: ${TARGET}. Add missing edge-case, error-path and regression tests, then run the suite with 'python -m pytest -q' and report the result."
