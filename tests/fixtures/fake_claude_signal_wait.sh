#!/usr/bin/env bash
# Specialized double for BEH-16/BEH-19 (spec-runner#485): a fake CLI that
# signals it has actually started, then blocks until released -- so a test
# can deterministically call `stop()` while the child's one task is known to
# be in flight, instead of racing it. `fake_claude.sh` itself is not touched
# (design §ready-file, DT-04 red frame): this is the "specialized double
# nearby" the decomposition allows when the three FAKE_* modes are not
# enough.
#
# Env vars:
#   SIGNAL_FILE             -- touched the instant this script runs
#   RELEASE_FILE            -- polled for; once it exists, the script proceeds
#   RELEASE_TIMEOUT_SECONDS -- safety bound so a broken test fails fast (default 30)
#   FAKE_RESPONSE_FILE      -- path to a file with the response text
#   FAKE_EXIT_CODE          -- exit code (default 0)

set -e

touch "$SIGNAL_FILE"

deadline=$(($(date +%s) + "${RELEASE_TIMEOUT_SECONDS:-30}"))
while [ ! -f "$RELEASE_FILE" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "fake_claude_signal_wait: timed out waiting for RELEASE_FILE" >&2
        exit 1
    fi
    sleep 0.05
done

if [ -n "$FAKE_RESPONSE_FILE" ] && [ -f "$FAKE_RESPONSE_FILE" ]; then
    cat "$FAKE_RESPONSE_FILE"
else
    echo "No response configured"
fi

exit "${FAKE_EXIT_CODE:-0}"
