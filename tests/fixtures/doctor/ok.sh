#!/usr/bin/env bash
# Happy path: creates the file, prints the marker and a cost line.
printf 'PONG' > SMOKE.txt
echo "Created SMOKE.txt"
echo "TASK_COMPLETE"
echo "input_tokens: 120  output_tokens: 8  cost: \$0.01" >&2
exit 0
