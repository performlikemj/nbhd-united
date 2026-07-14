#!/bin/bash
# PostToolUse(Bash) — poison the false citation "it passes locally".
#
# WHY THIS EXISTS (2026-07-14). An agent ran a test locally, saw green, and declared a CI
# failure "pre-existing, not ours". It was ours. The local venv ran Django 5.1.15 and
# dj-stripe 2.10.3 while CI/prod run 6.0.7 and 2.11.0 — and dj-stripe 2.10.3 has no
# `0003_2_11` migration, so `djstripe_accountv2` and `djstripe_productfeature`, the two
# tables the failing RLS test was ABOUT, could not exist locally at all. The local green
# was not weak evidence. It was structurally incapable of failing, and nothing said so.
#
# Prose could not have prevented it. The global CLAUDE.md already says "verify the symptom,
# not a proxy", and both the agent AND its reviewer walked straight past it — because the
# miss happens at CLAIM time, not at rule-reading time. So this fires at claim time: an
# agent about to quote a local green finds the contradiction sitting in the very tool
# result it would quote from.
#
# ADVISORY, never blocking. Local runs are genuinely useful as logic smokes; real bugs were
# caught with them. The failure was never RUNNING one. It was CITING one as parity.
#
# WHAT SILENCE MEANS — and it is NOT "you are safe". It means: the venv matched origin/main
# AS OF THE LAST FETCH. This hook makes no network call (a hook must not), so a dependency
# bump merged after your last fetch is invisible to it. That residual cannot be closed
# locally, so it is made VISIBLE instead: the baseline's age is printed whenever the hook
# speaks, and a baseline older than BASELINE_STALE_DAYS speaks up on its own — because the
# dangerous state would otherwise produce total silence, and silence is the one output that
# cannot carry a caveat.
#
# Fires ONLY on test/migration commands. A hook that fires on everything is wallpaper by
# week two, and then it erodes trust in the hooks that matter.

CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

case "$CMD" in
  *manage.py\ test*|*manage.py\ makemigrations*|*manage.py\ migrate*|*pytest*) ;;
  # The repo's OWN front door: CLAUDE.md's Key commands lead with `make test`. A hook only
  # ever sees the command STRING, never what make expands it to — so "make test" matched
  # nothing and sailed straight past. That is the shape a human MJ session is most likely
  # to use, and the one with no reviewer watching.
  #
  # `make integrate-gate` is what /integrate runs to stamp a branch combination as safe to
  # land: it mirrors CI (ruff check + format --check, makemigrations --check, the backend
  # suite, frontend npm ci + lint + build). "The integrate gate was green" is therefore the
  # most load-bearing citation in this repo, and the one this hook most needs to poison —
  # a gate run on a drifted venv stamps a lie. (There is no `make integrate` target;
  # assuming there was, and writing a comment saying so, was itself this PR's own error.)
  *make\ test*|*make\ migrate*|*make\ integrate-gate*) ;;
  *) exit 0 ;;  # fast path: no python spawned for ordinary Bash
esac

# Resolve the interpreter this command will actually use. Worktree sessions borrow the
# primary checkout's venv, so fall back to the first `git worktree list` entry.
PY=$(printf '%s\n' "$CMD" | grep -oE '[^ ]*/\.venv[^ /]*/bin/python[0-9.]*' | head -1)
[ -x "$PY" ] || PY="$CLAUDE_PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(git -C "$CLAUDE_PROJECT_DIR" worktree list 2>/dev/null | head -1 | awk '{print $1}')/.venv/bin/python"
[ -x "$PY" ] || exit 0

REPO="$CLAUDE_PROJECT_DIR"
[ -f "$REPO/requirements.txt" ] || REPO="$(git -C "$CLAUDE_PROJECT_DIR" worktree list 2>/dev/null | head -1 | awk '{print $1}')"
[ -f "$REPO/requirements.txt" ] || exit 0

IGNORE="$(dirname "$0")/venv_parity_ignore.txt"
CI_PYTHON="3.12"        # .github/workflows/ci-cd.yml: python-version
BASELINE_STALE_DAYS=2   # beyond this, an unfetched origin/main is itself worth saying

# MEASURE THE VENV AGAINST origin/main, NOT THE requirements.txt ON DISK.
#
# There is ONE .venv and every worktree borrows it. A single shared interpreter cannot track
# a per-branch file — so "does the venv match THIS checkout's requirements.txt?" was never a
# coherent question, and answering it produced a lie the moment the checkout was stale. A
# freshly-rebuilt, perfectly-correct venv got reported as "NOT CI parity" against old pins.
# origin/main is the only stable target a shared venv can have.
#
# No network call: uses the last-fetched ref, falling back to the local file only if
# origin/main is unknown (a fresh clone that has never fetched). That fallback silently
# reproduces the original bug, so it is named in the banner when it happens.
REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
BASELINE="origin/main"
if ! git -C "$REPO" show origin/main:requirements.txt > "$REQ" 2>/dev/null; then
  cp "$REPO/requirements.txt" "$REQ"
  BASELINE="local-file"
fi

# TWO INDEPENDENT QUESTIONS, both answered from the MERGE-BASE — never from a behind-count.
#
# The behind-count version of this shipped a banner that asserted the REVERSE OF REALITY. On
# a branch behind main it said "this run is exercising old dependencies AND old code". False:
# the venv is SHARED and tracks main, so such a run exercises main's NEW dependencies against
# the branch's OLD code — precisely the opposite. And the arm carrying the load-bearing fact
# ("CI installs the BRANCH's pins, not what your venv has") was gated on behind==0, a state
# that at this repo's merge velocity lasts hours. Three real states, three identical false
# banners; the true arm never fired once. A warning that lies is how a warning earns the
# right to be ignored — and this one's whole job is to be believed.
#
# So ask both questions separately. Either, neither, or BOTH may be true.
MB=$(git -C "$REPO" merge-base HEAD origin/main 2>/dev/null || true)
BRANCH_CHANGED=0   # did THIS branch touch requirements.txt since it was cut?
MAIN_CHANGED=0     # did MAIN touch requirements.txt since this branch was cut?
if [ -n "$MB" ]; then
  git -C "$REPO" diff --quiet "$MB" -- requirements.txt 2>/dev/null || BRANCH_CHANGED=1
  git -C "$REPO" diff --quiet "$MB" origin/main -- requirements.txt 2>/dev/null || MAIN_CHANGED=1
fi

# How old is the baseline? See "WHAT SILENCE MEANS" above.
FH=$(git -C "$REPO" rev-parse --git-path FETCH_HEAD 2>/dev/null || true)
case "$FH" in /*) ;; *) FH="$REPO/$FH" ;; esac
FETCH_AGE=""
if [ -f "$FH" ]; then
  NOW=$(date +%s)
  MT=$(stat -f %m "$FH" 2>/dev/null || stat -c %Y "$FH" 2>/dev/null || echo "$NOW")
  FETCH_AGE=$(( (NOW - MT) / 86400 ))
fi

"$PY" - "$REQ" "$IGNORE" "$CI_PYTHON" "$CMD" "$BRANCH_CHANGED" "$MAIN_CHANGED" \
        "$FETCH_AGE" "$BASELINE_STALE_DAYS" "$BASELINE" <<'PYEOF'
import json, re, sys
from importlib.metadata import PackageNotFoundError, version

req_path, ignore_path, ci_python, cmd = sys.argv[1:5]
branch_changed = sys.argv[5] == "1"
main_changed = sys.argv[6] == "1"
fetch_age = sys.argv[7]                 # whole days, or "" if unknown
stale_days = int(sys.argv[8])
baseline = sys.argv[9]                  # "origin/main" | "local-file"


def norm(n):
    return re.sub(r"\[.*\]", "", n).strip().lower().replace("_", "-")


ignore = set()
try:
    with open(ignore_path) as fh:
        ignore = {norm(line) for line in fh if line.strip() and not line.startswith("#")}
except OSError:
    pass

# The name class MUST allow ',' — extras carry them (`cuda-toolkit[cublas,cudart,...]==...`,
# 1 of 173 pins today). A pin this regex cannot parse is SILENTLY UNWATCHED: a false clean
# BY CONSTRUCTION, which is the exact failure this hook exists to prevent, one level down.
# So anything with '==' that still fails to parse is REPORTED, never dropped.
PIN = re.compile(r"^([A-Za-z0-9._,\[\]-]+)==([0-9][^\s;#]*)")

wrong, absent, unparsed = [], [], []
with open(req_path) as fh:
    for line in fh:
        s = line.strip()
        if not s or s.startswith(("#", "-")):
            continue
        m = PIN.match(s)
        if not m:
            if "==" in s:
                unparsed.append(s.split("==")[0][:40])
            continue
        name, want = norm(m.group(1)), m.group(2)
        if name in ignore:
            continue
        try:
            have = version(name)
        except PackageNotFoundError:
            absent.append(name)
            continue
        if have != want:
            wrong.append((name, want, have))

py_now = f"{sys.version_info[0]}.{sys.version_info[1]}"
py_drift = py_now != ci_python
baseline_stale = bool(fetch_age) and int(fetch_age) >= stale_days

if not (wrong or absent or py_drift or unparsed or branch_changed or main_changed
        or baseline_stale or baseline == "local-file"):
    sys.exit(0)  # clean — say nothing

# Headline the packages whose version actually changes behaviour, then NAME the rest before
# falling back to a count. A generic "drift detected" is wallpaper, and a banner that says
# only "+3 more mismatched" forced a reviewer to re-derive the names by hand — the moment a
# warning starts getting skipped. Labelled "(main)", not "(CI)": on a branch that changes
# requirements.txt, CI installs the BRANCH's file, not main's.
HEADLINE = ("django", "dj-stripe", "djangorestframework", "django-environ", "psycopg")
lead = [f"{n} {have}(local) vs {want}(main)" for n, want, have in wrong if n in HEADLINE]
tail = [f"{n} {have} vs {want}" for n, want, have in wrong if n not in HEADLINE]

bits = []
if py_drift:
    bits.append(f"python {py_now} vs CI {ci_python}")
bits += lead
NAMED = 4
bits += tail[:NAMED]
if len(tail) > NAMED:
    bits.append(f"+{len(tail) - NAMED} more mismatched")
if absent:
    bits.append(f"{len(absent)} pinned absent ({', '.join(absent[:3])})")

what = "migration" if "makemigrations" in cmd or re.search(r"manage\.py migrate", cmd) else "test run"

parts = []
if bits:
    parts.append(
        f"VENV DRIFT — the venv does not match origin/main: {'; '.join(bits)}. "
        "Fix: `make setup` (delegates to scripts/rebuild_venv.sh)."
    )
if unparsed:
    parts.append(
        f"UNWATCHED PINS — {len(unparsed)} requirements line(s) this hook could not parse "
        f"({', '.join(unparsed[:3])}), so their versions are NOT being checked at all. Fix "
        "the PIN regex; an unparseable pin is a false clean by construction."
    )
if branch_changed:
    parts.append(
        "THIS BRANCH CHANGES requirements.txt. CI installs the BRANCH's pins; the venv is "
        "shared across worktrees and tracks main — so this local run is NOT exercising the "
        "dependency set CI will use for this branch, and any drift named above is expected."
    )
if main_changed:
    parts.append(
        "MAIN'S PINS MOVED since this branch was cut. The shared venv tracks main, so this "
        "run puts main's NEWER dependencies against this branch's OLDER code — while CI will "
        "install the branch's OLDER file. Local differs from CI in BOTH directions. Rebase or "
        "pull to converge."
    )
if baseline == "local-file":
    parts.append(
        "NO origin/main REF — fell back to comparing against this checkout's own "
        "requirements.txt, which is the original bug this hook was written to catch. "
        "`git fetch origin main` and re-run; until then this check proves nothing."
    )
if baseline_stale:
    parts.append(
        f"BASELINE IS {fetch_age}d OLD — origin/main was last fetched {fetch_age} days ago, so "
        "a dependency bump merged since then is invisible here and would show as CLEAN. "
        "`git fetch origin main`."
    )

age_note = f" (baseline: origin/main as last fetched {fetch_age}d ago.)" if fetch_age and not baseline_stale else ""
extra = (
    " A migration generated here carries the LOCAL Django version in its header, and "
    "`makemigrations --check` in CI is the only drift verdict that counts."
    if what == "migration"
    else ""
)

msg = (
    f"VENV PARITY (hook) on this {what}. "
    + " ".join(parts)
    + " Treat a local green as a LOGIC SMOKE only. Never cite 'it passes locally' as "
    "evidence for anything version-sensitive: migrations, ORM/DRF semantics, tables a "
    "dependency adds, RLS coverage. CI is the arbiter." + extra + age_note + " "
    "(2026-07-14: exactly this made an agent call a real RLS failure 'pre-existing'. It was "
    "not — dj-stripe 2.10.3 lacks the migration creating the very tables it failed on, so "
    "the local run COULD NOT have failed.)"
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
PYEOF

exit 0
