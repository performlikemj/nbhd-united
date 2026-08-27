#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REAL_PYTHON="${REAL_PYTHON:-/Users/michaeljones/Projects/nbhd-united/.venv/bin/python}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nbhd-startup.XXXXXX")"
SHIM_DIR="$TEST_ROOT/shims"
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

mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/python" <<'SHIM'
#!/bin/bash
set -e

if [ "$1" = "manage.py" ]; then
  case "$2" in
    migrate|disable_rls|bump_pending_configs)
      exit 0
      ;;
    poll_telegram)
      role="poller"
      ;;
    *)
      exit 64
      ;;
  esac
elif [ "$1" = "-m" ] && [ "$2" = "apps.pii.shared_server" ]; then
  role="sidecar"
else
  exit 64
fi

exec "$FAKE_REAL_PYTHON" -c '
import os
import signal
import sys
import time

role = sys.argv[1]
directory = os.environ["FAKE_STATE_DIR"]
starts_path = os.path.join(directory, role + ".starts")
pids_path = os.path.join(directory, role + ".pids")
with open(starts_path, "a", encoding="utf-8") as starts:
    starts.write("start\n")
with open(pids_path, "a", encoding="utf-8") as pids:
    pids.write(str(os.getpid()) + "\n")
with open(starts_path, encoding="utf-8") as starts:
    count = sum(1 for _line in starts)

def stop(_signum, _frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
prefix = "FAKE_" + role.upper()
if count == 1 and os.environ.get(prefix + "_FAIL_FIRST") == "1":
    time.sleep(float(os.environ.get(prefix + "_FAIL_AFTER_S", "0.1")))
    raise SystemExit(int(os.environ.get(prefix + "_FAIL_CODE", "23")))
exit_after = os.environ.get(prefix + "_EXIT_AFTER_S")
if exit_after is not None:
    time.sleep(float(exit_after))
    raise SystemExit(int(os.environ.get(prefix + "_EXIT_CODE", "0")))
while True:
    time.sleep(3600)
' "$role"
SHIM

cat > "$SHIM_DIR/gunicorn" <<'SHIM'
#!/bin/bash
set -e
exec "$FAKE_REAL_PYTHON" -c '
import os
import signal
import time

role = "gunicorn"
directory = os.environ["FAKE_STATE_DIR"]
with open(os.path.join(directory, role + ".starts"), "a", encoding="utf-8") as starts:
    starts.write("start\n")
with open(os.path.join(directory, role + ".pids"), "a", encoding="utf-8") as pids:
    pids.write(str(os.getpid()) + "\n")

def stop(_signum, _frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
exit_after = os.environ.get("FAKE_GUNICORN_EXIT_AFTER_S")
if exit_after is not None:
    time.sleep(float(exit_after))
    raise SystemExit(int(os.environ.get("FAKE_GUNICORN_EXIT_CODE", "0")))
while True:
    time.sleep(3600)
'
SHIM
chmod +x "$SHIM_DIR/python" "$SHIM_DIR/gunicorn"

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
  PATH="$SHIM_DIR:$PATH" \
  FAKE_REAL_PYTHON="$REAL_PYTHON" \
  FAKE_STATE_DIR="$restart_dir" \
  FAKE_SIDECAR_FAIL_FIRST=1 \
  FAKE_SIDECAR_FAIL_AFTER_S=0.1 \
  FAKE_GUNICORN_EXIT_AFTER_S=3 \
  FAKE_GUNICORN_EXIT_CODE=17 \
  NBHD_STARTUP_BACKOFF_BASE_S=1 \
  NBHD_STARTUP_BACKOFF_CAP_S=2 \
  PII_DETECTOR_TRANSPORT=shared \
  PII_SHARED_SOCKET="$restart_dir/pii.sock" \
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
  PATH="$SHIM_DIR:$PATH" \
  FAKE_REAL_PYTHON="$REAL_PYTHON" \
  FAKE_STATE_DIR="$signal_dir" \
  NBHD_STARTUP_BACKOFF_BASE_S=1 \
  PII_DETECTOR_TRANSPORT=shared \
  PII_SHARED_SOCKET="$signal_dir/pii.sock" \
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
