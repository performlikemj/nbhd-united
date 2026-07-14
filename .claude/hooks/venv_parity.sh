#!/bin/bash
# PostToolUse(Bash) — poison the false citation "it passes locally".
#
# WHY THIS EXISTS (2026-07-14). An agent ran a test locally, saw green, and declared a
# CI failure "pre-existing, not ours". It was ours. The local venv was running Django
# 5.1.15 and dj-stripe 2.10.3 while CI/prod run 6.0.7 and 2.11.0 — the tables the failing
# test was about did not even EXIST locally. The local green was not weak evidence; it was
# structurally meaningless, and nothing said so.
#
# Prose could not have prevented it. The global CLAUDE.md already says "verify the symptom,
# not a proxy" and both the agent and its reviewer walked straight past it, because the
# miss happens at CLAIM time, not at rule-reading time. So this fires at claim time: any
# agent about to quote a local green now has the contradiction sitting in the same tool
# result it would quote from.
#
# ADVISORY, never blocking. Local runs remain genuinely useful as logic smokes — real bugs
# were caught with them. The failure was never RUNNING them; it was CITING them as parity.
#
# Fires ONLY on test/migration commands. A hook that fires on everything is wallpaper by
# week two, and then it erodes trust in the hooks that matter.

CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

case "$CMD" in
  *manage.py\ test*|*manage.py\ makemigrations*|*manage.py\ migrate*|*pytest*) ;;
  # The repo's OWN front door: CLAUDE.md's Key commands lead with `make test`. The hook
  # only sees the command STRING, so "make test" matched nothing and sailed straight past
  # — the one shape a human MJ session is most likely to use, and the one with no reviewer
  # watching. `make integrate` runs the CI-mirroring suite, and "the integrate gate was
  # green" is precisely the citation this hook exists to poison.
  *make\ test*|*make\ migrate*|*make\ integrate*) ;;
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
CI_PYTHON="3.12"  # .github/workflows/ci-cd.yml: python-version

# MEASURE AGAINST origin/main, NOT THE requirements.txt ON DISK.
#
# There is ONE .venv and every worktree borrows it. A single shared interpreter cannot track
# a per-branch file — so "does the venv match this checkout's requirements.txt?" was never a
# coherent question, and answering it produces a lie the moment the checkout is stale. This
# repo's primary checkout is a DELIBERATELY stale integration station (66 commits behind when
# this was written), so that is the normal case, not the edge case: a freshly-rebuilt,
# perfectly-correct venv got reported as "NOT CI parity" against those stale pins. Lies in the
# loud direction are exactly how a warning earns the right to be ignored — and this warning's
# whole job is to be believed at the one moment an agent is about to cite a local green.
#
# origin/main is the only stable target a shared venv can have. No network call: uses the
# last-fetched ref, falling back to the local file if origin/main is unknown.
REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
if ! git -C "$REPO" show origin/main:requirements.txt > "$REQ" 2>/dev/null; then
  cp "$REPO/requirements.txt" "$REQ"
fi

# SEPARATE QUESTION: does the code being tested want DIFFERENT dependencies than main? Two
# very different causes, and the commit distance tells them apart — so say which, rather than
# making the reader guess. Neither is venv drift.
REQ_DIFFERS=""
if ! git -C "$REPO" diff --quiet origin/main -- requirements.txt 2>/dev/null; then
  BEHIND=$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  # behind>0 → the checkout is stale (its pins are just old news).
  # behind=0 → this BRANCH bumped a dependency; CI will install the branch's file, so the
  #            shared venv legitimately cannot match both it and main. Expect drift; ignore it.
  [ "$BEHIND" -gt 0 ] 2>/dev/null && REQ_DIFFERS="stale:$BEHIND" || REQ_DIFFERS="branch"
fi

"$PY" - "$REQ" "$IGNORE" "$CI_PYTHON" "$CMD" "$REQ_DIFFERS" <<'PYEOF'
import json, re, sys
from importlib.metadata import PackageNotFoundError, version

req_path, ignore_path, ci_python, cmd = sys.argv[1:5]
req_differs = sys.argv[5] if len(sys.argv) > 5 else ""


def norm(n):
    return re.sub(r"\[.*\]", "", n).strip().lower().replace("_", "-")


ignore = set()
try:
    with open(ignore_path) as fh:
        ignore = {norm(l) for l in fh if l.strip() and not l.startswith("#")}
except OSError:
    pass

wrong, absent = [], []
with open(req_path) as fh:
    for line in fh:
        m = re.match(r"^([A-Za-z0-9._\[\]-]+)==([0-9][^\s;#]*)", line.strip())
        if not m:
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

if not wrong and not absent and not py_drift and not req_differs:
    sys.exit(0)  # clean — say nothing

# Headline the packages whose version actually changes behaviour, then NAME the rest before
# falling back to a count. A generic "drift detected" is wallpaper; and a banner that says
# only "+3 more mismatched" is half a banner — it forced a reviewer to re-derive the names
# by hand, which is the moment a warning starts getting skipped.
HEADLINE = ("django", "dj-stripe", "djangorestframework", "django-environ", "psycopg")
lead = [f"{n} {have}(local) vs {want}(CI)" for n, want, have in wrong if n in HEADLINE]
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
extra = (
    " A migration generated here carries the LOCAL Django version in its header, and "
    "`makemigrations --check` in CI is the only drift verdict that counts."
    if what == "migration"
    else ""
)

parts = []
if bits:
    parts.append(
        f"VENV DRIFT — the venv does not match origin/main: {'; '.join(bits)}. "
        "Fix: scripts/rebuild_venv.sh"
    )
if req_differs.startswith("stale:"):
    parts.append(
        f"STALE CHECKOUT — its requirements.txt is {req_differs.split(':')[1]} commits behind "
        "origin/main, so this run is exercising old dependencies AND old code. `git pull`."
    )
elif req_differs == "branch":
    parts.append(
        "THIS BRANCH CHANGES requirements.txt. CI will install the BRANCH's pins; the venv is "
        "shared across worktrees and tracks main, so any drift named above is EXPECTED here — "
        "but it also means this local run is not testing what CI will."
    )

msg = (
    f"VENV PARITY (hook) on this {what}. "
    + " ".join(parts)
    + " Treat a local green as a LOGIC SMOKE only. Never cite 'it passes locally' as "
    "evidence for anything version-sensitive: migrations, ORM/DRF semantics, tables a "
    "dependency adds, RLS coverage. CI is the arbiter." + extra + " "
    "(2026-07-14: exactly this made an agent call a real RLS failure 'pre-existing'. It was not. "
    "Then the SCRIPT that fixed it built from a stale checkout and shipped a 3-package-behind venv — "
    "caught by this hook, in a reviewer's tool output, mid-review.)"
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
PYEOF

exit 0
