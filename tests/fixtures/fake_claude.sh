#!/usr/bin/env bash
# Fake Claude CLI for E2E testing.
#
# Env vars:
#   FAKE_RESPONSE_FILE  — path to file with response text
#   FAKE_EXIT_CODE      — exit code (default 0)
#   FAKE_DELAY          — sleep seconds before responding (default 0)
#   FAKE_STDERR         — text to write to stderr
#   FAKE_COUNTER_FILE   — if set, appends attempt index to FAKE_RESPONSE_FILE
#                          e.g. response.0, response.1, response.2

set -e

sleep "${FAKE_DELAY:-0}"

RESPONSE_FILE="$FAKE_RESPONSE_FILE"

# Multi-attempt support: read counter, increment, pick response file
if [ -n "$FAKE_COUNTER_FILE" ]; then
    COUNT=$(cat "$FAKE_COUNTER_FILE" 2>/dev/null || echo 0)
    echo $((COUNT + 1)) > "$FAKE_COUNTER_FILE"
    RESPONSE_FILE="${FAKE_RESPONSE_FILE}.${COUNT}"
fi

if [ -n "$RESPONSE_FILE" ] && [ -f "$RESPONSE_FILE" ]; then
    cat "$RESPONSE_FILE"
else
    echo "No response configured"
fi

if [ -n "$FAKE_STDERR" ]; then
    echo -n "$FAKE_STDERR" >&2
fi

exit "${FAKE_EXIT_CODE:-0}"
