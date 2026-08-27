#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/Users/michaeljones/Projects/nbhd-united/.venv/bin/python}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nbhd-startup.XXXXXX")"
SUPERVISOR_PID=""

cleanup() {
  if [ -n "$SUPERVISOR_PID" ] && kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    kill -TERM "$SUPERVISOR_PID" 2>/dev/null || true
    wait "$SUPERVISOR_PID" 2>/dev/null || true
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT INT TERM

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

wait_for_lines() {
  file="$1"
  expected="$2"
  attempts=0
  while [ "$attempts" -lt 100 ]; do
    if [ -f "$file" ]; then
      count="$(wc -l < "$file" | tr -d ' ')"
      if [ "$count" -ge "$expected" ]; then
        return
      fi
    fi
    attempts=$((attempts + 1))
    sleep 0.1
  done
  fail "timed out waiting for $expected line(s) in $file"
}

wait_for_exit() {
  pid="$1"
  attempts=0
  while kill -0 "$pid" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 100 ]; then
      fail "process $pid did not exit"
    fi
    sleep 0.1
  done
}

assert_children_gone() {
  directory="$1"
  for role in sidecar poller gunicorn; do
    file="$directory/$role.pids"
    [ -f "$file" ] || fail "missing PID record for $role"
    while IFS= read -r pid; do
      if kill -0 "$pid" 2>/dev/null; then
        fail "$role child $pid is still alive"
      fi
    done < "$file"
  done
}

restart_dir="$TEST_ROOT/restart"
mkdir -p "$restart_dir"
env \
  NBHD_STARTUP_FAKE=1 \
  NBHD_STARTUP_FAKE_DIR="$restart_dir" \
  NBHD_STARTUP_BACKOFF_BASE_S=1 \
  NBHD_STARTUP_BACKOFF_CAP_S=2 \
  NBHD_FAKE_SIDECAR_FAIL_FIRST=1 \
  NBHD_FAKE_SIDECAR_FAIL_AFTER_S=0.1 \
  NBHD_FAKE_GUNICORN_EXIT_AFTER_S=3 \
  NBHD_FAKE_GUNICORN_EXIT_CODE=17 \
  PII_DETECTOR_TRANSPORT=shared \
  PII_SHARED_SOCKET="$restart_dir/pii.sock" \
  PYTHON_BIN="$PYTHON_BIN" \
  /bin/bash "$PROJECT_DIR/startup.sh" > "$restart_dir/startup.log" 2>&1 &
SUPERVISOR_PID=$!

wait_for_lines "$restart_dir/sidecar.starts" 2
echo "PASS: sidecar death triggers supervised restart"
wait_for_exit "$SUPERVISOR_PID"
restart_rc=0
wait "$SUPERVISOR_PID" || restart_rc=$?
SUPERVISOR_PID=""
[ "$restart_rc" -eq 17 ] || fail "gunicorn exit code was $restart_rc, expected 17"
echo "PASS: gunicorn exit code 17 propagated"
assert_children_gone "$restart_dir"
echo "PASS: gunicorn shutdown leaves no fake children"

signal_dir="$TEST_ROOT/signal"
mkdir -p "$signal_dir"
env \
  NBHD_STARTUP_FAKE=1 \
  NBHD_STARTUP_FAKE_DIR="$signal_dir" \
  NBHD_STARTUP_BACKOFF_BASE_S=1 \
  PII_DETECTOR_TRANSPORT=shared \
  PII_SHARED_SOCKET="$signal_dir/pii.sock" \
  PYTHON_BIN="$PYTHON_BIN" \
  /bin/bash "$PROJECT_DIR/startup.sh" > "$signal_dir/startup.log" 2>&1 &
SUPERVISOR_PID=$!

wait_for_lines "$signal_dir/sidecar.starts" 1
wait_for_lines "$signal_dir/poller.starts" 1
wait_for_lines "$signal_dir/gunicorn.starts" 1
kill -TERM "$SUPERVISOR_PID"
signal_rc=0
wait "$SUPERVISOR_PID" || signal_rc=$?
SUPERVISOR_PID=""
[ "$signal_rc" -eq 143 ] || fail "SIGTERM exit code was $signal_rc, expected 143"
assert_children_gone "$signal_dir"
echo "PASS: SIGTERM stops sidecar, poller, and gunicorn without orphans"

if grep -q 'supervisor wait mode=poll' "$signal_dir/startup.log"; then
  echo "PASS: macOS Bash 3.2 polling fallback exercised"
else
  echo "PASS: wait -n supervisor path exercised"
fi
