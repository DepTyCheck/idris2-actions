#!/bin/bash
# First run prints the pack git ls-remote failure and exits 1.
# The retry action must run this again; the second run exits 0.
set -euo pipefail

counter="${RUNNER_TEMP:?}/ls-remote-attempts"
n=0
if [ -f "$counter" ]; then
	n=$(cat "$counter")
fi
n=$((n + 1))
printf '%s\n' "$n" >"$counter"
echo "ls-remote attempt $n"

if [ "$n" -eq 1 ]; then
	echo 'Command: "git" "ls-remote" "https://git.sr.ht/~janus/utf8" "master"' >&2
	exit 1
fi
if [ "$n" -ne 2 ]; then
	echo "error: expected attempt 2 after one retry, got $n" >&2
	exit 1
fi
