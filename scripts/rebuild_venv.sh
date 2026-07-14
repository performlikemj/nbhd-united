#!/usr/bin/env bash
# Rebuild .venv at CI parity. Run this when .claude/hooks/venv_parity.sh reports drift.
#
# WHY THIS SCRIPT EXISTS (2026-07-14). The venv had silently drifted a WHOLE DJANGO MAJOR
# VERSION — running 5.1.15 locally while CI/prod ran 6.0.7, plus dj-stripe 2.10.3 vs 2.11.0.
# Every local test run had been measuring a different framework than the one we ship, and
# an agent used a local green to declare a real RLS failure "pre-existing, not ours".
#
# It could not be fixed by any pip command, and that is the whole trap:
#
#   * the venv was on Python 3.11, and DJANGO 6 HAS NO py3.11 WHEELS — the newest Django
#     that exists for 3.11 is 5.2.x. The interpreter itself pinned us a major behind.
#   * `pip install -r requirements.txt` FAILS on macOS: the file is pip-compiled on Linux
#     and carries no platform markers, so the CUDA runtime + triton (torch's GPU deps) have
#     no macOS wheels — and pip resolves ATOMICALLY, so one of them fails and NOTHING
#     installs. That is why it was never repaired; the obvious command doesn't work.
#   * `pip-compile` would "fix" that by silently dropping those Linux pins from
#     requirements.txt, breaking the PII container at deploy. It is hook-blocked.
#
# WHY IT BUILDS FROM origin/main AND NOT THE LOCAL FILE. The first version of this script
# used the checkout's own requirements.txt — and was run from the PRIMARY checkout, which
# this repo deliberately keeps as a stale integration station (66 commits behind at the
# time). It therefore built a "parity" venv that was three packages out of date on the day
# it was born. The script written to fix "trusting a stale local artifact" trusted a stale
# local artifact. The hook it ships alongside caught it, in a reviewer's tool output,
# mid-review. Parity means ORIGIN, not whatever happens to be on disk.

set -euo pipefail

# Resolve our OWN resources relative to this script, BEFORE cd-ing anywhere. The ignore file
# is this script's sibling; if we resolved it from the cwd we'd look for it in whichever
# checkout holds the venv — which is a DIFFERENT checkout when the script is run from a
# worktree (and, until this PR merges, one where the file does not exist at all).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IGNORE_FILE="$HERE/../.claude/hooks/venv_parity_ignore.txt"

# Now cd to the PRIMARY checkout — NOT `git rev-parse --show-toplevel`. There is ONE .venv
# and it lives in the primary; every worktree borrows it. Run from a worktree, show-toplevel
# returns the worktree and we would build a stray venv there while the real one stayed
# rotten. `git worktree list` prints the main worktree first — documented, not luck.
cd "$(git worktree list | head -1 | awk '{print $1}')"

CI_PYTHON=3.12  # .github/workflows/ci-cd.yml: python-version
PY_BIN="/opt/homebrew/opt/python@${CI_PYTHON}/bin/python${CI_PYTHON}"

[ -x "$PY_BIN" ] || { echo "Missing CI python. Run: brew install python@${CI_PYTHON}" >&2; exit 1; }
[ -f "$IGNORE_FILE" ] || { echo "Missing ignore file: $IGNORE_FILE" >&2; exit 1; }

# The venv is SHARED MUTABLE STATE across concurrent Claude sessions. Checked twice: once
# now (fail fast), and again immediately before the swap — the dangerous moment is minutes
# from now, after the installs, and a run that starts inside that window would have its
# interpreter yanked out from under it.
# `pgrep -f` matches full command lines, so a caller whose OWN command line contains the
# pattern (a wrapper loop, a `watch`, this script quoted in a shell one-liner) self-matches
# and sees phantom tests forever. Confirmed live: a polling wrapper "saw" 3-5 test processes
# for nine minutes while the machine was idle. Require the match to be an actual python
# interpreter — that is what a real test run is.
in_use() {
  pgrep -f "manage.py test" 2>/dev/null | while read -r pid; do
    case "$(ps -o comm= -p "$pid" 2>/dev/null)" in *python*) echo x ;; esac
  done | grep -q x
}
refuse() {
  echo "REFUSING: another session is mid-test against this venv. Swapping it now breaks" >&2
  echo "their run. Wait for it to finish and re-run." >&2
  exit 1
}
in_use && refuse

echo "==> fetching origin (parity means ORIGIN, not the local checkout — see header)"
git fetch origin main --quiet

TMP_REQ="$(mktemp)"
trap 'rm -f "$TMP_REQ"' EXIT

# ONE list, ONE truth: the exclusions come from the same file the hook uses to decide what
# counts as legitimately-absent. Two hand-maintained lists encoding the same fact will
# diverge, and the divergence is invisible — a package excluded here but not ignored there
# reads as permanent drift; the reverse hides real drift forever.
EXCLUDE=$(grep -vE '^\s*(#|$)' "$IGNORE_FILE" | paste -sd'|' -)
git show origin/main:requirements.txt | grep -vE "^(${EXCLUDE})" > "$TMP_REQ"

echo "==> building a fresh venv on Python ${CI_PYTHON}"
rm -rf .venv.new
"$PY_BIN" -m venv .venv.new
.venv.new/bin/python -m pip install -q --upgrade pip wheel

echo "==> installing origin/main's requirements.txt (minus the $(grep -cvE '^\s*(#|$)' "$IGNORE_FILE") Linux-only pins)"
.venv.new/bin/python -m pip install -q -r "$TMP_REQ"

echo "==> replicating CI's extra steps (ci-cd.yml — NOT in requirements.txt)"
.venv.new/bin/python -m pip install -q ruff
.venv.new/bin/python -m spacy download en_core_web_sm

# Rewrite shebangs so the venv survives the move (they hard-code the build path). This keeps
# the swap window sub-second instead of a multi-minute reinstall gap.
OLD="$PWD/.venv.new"; NEW="$PWD/.venv"
for f in .venv.new/bin/*; do
  [ -f "$f" ] && head -c2 "$f" 2>/dev/null | grep -q '#!' && sed -i '' "s|$OLD|$NEW|g" "$f"
done
sed -i '' "s|$OLD|$NEW|g" .venv.new/bin/activate* 2>/dev/null || true

# SECOND guard — the one that matters. Minutes have passed since the first check, and a test
# that started during the build would have its interpreter yanked mid-run.
#
# This one WAITS rather than refusing: the venv is already built, so throwing away five
# minutes of work because someone started a test at the wrong moment is a bad trade. Fail
# fast BEFORE the work; be patient AFTER it.
if in_use; then
  echo "==> a test started during the build — waiting for a clear window before swapping"
  for _ in $(seq 1 60); do   # up to ~8 min
    in_use || break
    sleep 8
  done
  in_use && { echo "(the new venv is built and waiting at .venv.new — re-run to swap it in)" >&2; refuse; }
fi

rm -rf .venv.old
# `if`, not `[ -d .venv ] && mv ...`. Under `set -e` that one-liner is exempt only because
# it is not the final command in the script — move it to the end, or append nothing after
# it, and a FIRST-TIME setup (no .venv to move) exits 1 having actually succeeded. `make
# setup` now routes here, so the no-.venv path is the common one, not the exotic one.
if [ -d .venv ]; then mv .venv .venv.old; fi
mv .venv.new .venv
# Accepted residual: this only guards `manage.py test`. A long-lived runserver/shell holding
# the old venv survives the move via open file handles — only NEW spawns land on the new one.

echo "==> parity:"
.venv/bin/python -c "
import sys, django, importlib.metadata as md
print(f'    python {sys.version_info[0]}.{sys.version_info[1]} · django {django.get_version()} · dj-stripe {md.version(\"dj-stripe\")}')"
echo "==> done. Previous venv kept at .venv.old — delete it once you're happy."
