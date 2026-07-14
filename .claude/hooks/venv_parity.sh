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
  *) exit 0 ;;  # fast path: no python spawned for ordinary Bash
esac

# Resolve the interpreter this command will actually use. Worktree sessions borrow the
# primary checkout's venv, so fall back to the first `git worktree list` entry.
PY=$(printf '%s\n' "$CMD" | grep -oE '[^ ]*/\.venv[^ /]*/bin/python[0-9.]*' | head -1)
[ -x "$PY" ] || PY="$CLAUDE_PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(git -C "$CLAUDE_PROJECT_DIR" worktree list 2>/dev/null | head -1 | awk '{print $1}')/.venv/bin/python"
[ -x "$PY" ] || exit 0

REQ="$CLAUDE_PROJECT_DIR/requirements.txt"
[ -f "$REQ" ] || REQ="$(git -C "$CLAUDE_PROJECT_DIR" worktree list 2>/dev/null | head -1 | awk '{print $1}')/requirements.txt"
[ -f "$REQ" ] || exit 0

IGNORE="$(dirname "$0")/venv_parity_ignore.txt"

CI_PYTHON="3.12"  # .github/workflows/ci-cd.yml: python-version

"$PY" - "$REQ" "$IGNORE" "$CI_PYTHON" "$CMD" <<'PYEOF'
import json, re, sys
from importlib.metadata import PackageNotFoundError, version

req_path, ignore_path, ci_python, cmd = sys.argv[1:5]


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

if not wrong and not absent and not py_drift:
    sys.exit(0)  # clean — say nothing

# Headline the packages whose version actually changes behaviour, then the count. A generic
# "drift detected" is wallpaper; the specifics are what make it unignorable.
HEADLINE = ("django", "dj-stripe", "djangorestframework", "django-environ", "psycopg")
lead = [f"{n} {have}(local) vs {want}(CI)" for n, want, have in wrong if n in HEADLINE]
bits = []
if py_drift:
    bits.append(f"python {py_now} vs CI {ci_python}")
bits += lead
rest = len(wrong) - len(lead)
if rest > 0:
    bits.append(f"+{rest} more mismatched")
if absent:
    bits.append(f"{len(absent)} pinned packages absent")

what = "migration" if "makemigrations" in cmd or re.search(r"manage\.py migrate", cmd) else "test run"
extra = (
    " A migration generated here carries the LOCAL Django version in its header, and "
    "`makemigrations --check` in CI is the only drift verdict that counts."
    if what == "migration"
    else ""
)

msg = (
    f"VENV DRIFT (hook): this {what} is NOT CI parity — " + "; ".join(bits) + ". "
    "Treat a local green as a LOGIC SMOKE only. Never cite 'it passes locally' as evidence "
    "for anything version-sensitive: migrations, ORM/DRF semantics, tables a dependency "
    "adds, RLS coverage. CI is the arbiter." + extra + " "
    "(2026-07-14: exactly this drift made an agent call an RLS failure 'pre-existing'. It was not.)"
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
PYEOF

exit 0
