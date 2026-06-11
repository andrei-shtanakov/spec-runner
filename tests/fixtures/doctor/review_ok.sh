#!/usr/bin/env bash
# Used for BOTH the executor and review calls. Creates the file + prints the
# executor marker, and also prints the review marker so review extraction sees it.
printf 'PONG' > SMOKE.txt
echo "Reviewed the diff, no issues."
echo "cost: \$0.01" >&2
echo "TASK_COMPLETE"
echo "REVIEW_PASSED"
exit 0
