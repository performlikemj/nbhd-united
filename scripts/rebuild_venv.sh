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
# So: build on CI's Python, install everything EXCEPT the Linux-only set, then replicate
# the two extra steps CI performs (ci-cd.yml installs ruff and the spaCy model separately —
# neither is in requirements.txt, and a venv without them is not CI parity either).

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

CI_PYTHON=3.12  # .github/workflows/ci-cd.yml: python-version
PY_BIN="/opt/homebrew/opt/python@${CI_PYTHON}/bin/python${CI_PYTHON}"

[ -x "$PY_BIN" ] || { echo "Missing CI python. Run: brew install python@${CI_PYTHON}" >&2; exit 1; }

if pgrep -f "manage.py test" >/dev/null 2>&1; then
  echo "REFUSING: another session is mid-test against this venv." >&2
  echo "The venv is shared mutable state across concurrent Claude sessions — swapping it now" >&2
  echo "breaks their run. Wait for it to finish." >&2
  exit 1
fi

TMP_REQ="$(mktemp)"
trap 'rm -f "$TMP_REQ"' EXIT

# Linux-only: no macOS wheels exist. torch itself DOES build on macOS — do not drop it.
grep -vE '^(nvidia-|cuda-|triton==)' requirements.txt > "$TMP_REQ"

echo "==> building a fresh venv on Python ${CI_PYTHON}"
rm -rf .venv.new
"$PY_BIN" -m venv .venv.new
.venv.new/bin/python -m pip install -q --upgrade pip wheel

echo "==> installing requirements.txt (minus the Linux-only set)"
.venv.new/bin/python -m pip install -q -r "$TMP_REQ"

echo "==> replicating CI's extra steps (ci-cd.yml — NOT in requirements.txt)"
.venv.new/bin/python -m pip install -q ruff
.venv.new/bin/python -m spacy download en_core_web_sm

# Rewrite shebangs so the venv survives the move (they hard-code the build path), keeping
# the swap window sub-second instead of a multi-minute reinstall gap.
OLD="$PWD/.venv.new"; NEW="$PWD/.venv"
for f in .venv.new/bin/*; do
  [ -f "$f" ] && head -c2 "$f" 2>/dev/null | grep -q '#!' && sed -i '' "s|$OLD|$NEW|g" "$f"
done
sed -i '' "s|$OLD|$NEW|g" .venv.new/bin/activate* 2>/dev/null || true

rm -rf .venv.old
[ -d .venv ] && mv .venv .venv.old
mv .venv.new .venv

echo "==> parity:"
.venv/bin/python -c "
import sys, django, importlib.metadata as md
print(f'    python {sys.version_info[0]}.{sys.version_info[1]} · django {django.get_version()} · dj-stripe {md.version(\"dj-stripe\")}')"
echo "==> done. Previous venv kept at .venv.old — delete it once you're happy."
