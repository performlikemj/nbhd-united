#!/bin/bash
# PreToolUse(Bash) guard — enforces docs/agents/workflow.md rules mechanically.
# Exit 2 blocks the command; stderr is fed back to the agent.

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$cmd" ] && exit 0

block() {
  echo "$1" >&2
  exit 2
}

# SCAN THE COMMAND WITH MESSAGE PAYLOADS REMOVED. A commit message that NAMES a forbidden
# command is not that command — but every rule below matched raw text, so the guard blocked
# people for DESCRIBING it: `git commit -m "block pip-compile"` tripped the pip-compile rule,
# `git commit -m "never git add -A"` tripped the staging rule. This PR's own commit messages
# escaped only by accident (backticks happened not to match the leading-whitespace anchor).
# A guard that punishes you for documenting it teaches you to route around it, and a guard
# people route around is worse than none.
#
# Strips ONLY the argument to -m/--message/-b/--body/-F/--notes, and truncates at a heredoc
# introducer (a heredoc body is data by definition). Deliberately NOT a blanket quote-strip:
# that would let `bash -c "pip-compile requirements.in"` through, which is real execution.
# Chained commands still land — `git commit -m "x" && pip-compile y` keeps the pip-compile.
# `[[:space:]]*=?[[:space:]]*`, not `[[:space:]]+`: `-m"msg"` (no space) and `--message="msg"`
# are ordinary spellings, and requiring a space meant they were NOT payload-stripped and so
# could still false-block. Rare, but it fails in the loud direction, which is the direction
# that erodes the guard.
#
# KNOWN RESIDUAL, stated rather than left to be discovered: the heredoc rule truncates the
# scan at the introducer, so a command chained AFTER a heredoc body in the SAME Bash call is
# never scanned. Exotic, and `compile-deps` has the Makefile recipe's uname check as its real
# wall — the string layer is a backstop, not the barrier. If that ever needs closing, delete
# only the heredoc BODY (introducer through terminator) instead of truncating.
MSGFLAG='(-m|-am|-sm|--message|-b|--body|-F|--notes|-t|--title)'
scan=$(printf '%s\n' "$cmd" \
  | sed -E "s/${MSGFLAG}[[:space:]]*=?[[:space:]]*'[^']*'/\1 /g" \
  | sed -E "s/${MSGFLAG}[[:space:]]*=?[[:space:]]*\"[^\"]*\"/\1 /g" \
  | sed -n '/<</q;p')

if printf '%s' "$scan" | grep -qE 'git add +(-A|--all|\.)([[:space:]]|$|;)'; then
  block "Blocked by .claude/hooks/git_guard.sh: broad staging (git add -A/./--all) risks committing .env, models, or unrelated WIP. Stage specific files by path (docs/agents/workflow.md)."
fi

if printf '%s' "$scan" | grep -qE 'git commit[^|;&]*--no-verify'; then
  block "Blocked by .claude/hooks/git_guard.sh: --no-verify skips pre-commit hooks. Only allowed for a scanner false positive in scanner code itself — ask MJ first."
fi

if printf '%s' "$scan" | grep -qE 'git push[^|;&]*(--force|--force-with-lease|-f[[:space:]])' && printf '%s' "$scan" | grep -qE '(^|[[:space:]:])main([[:space:]]|$|;)'; then
  block "Blocked by .claude/hooks/git_guard.sh: force-push touching main is never allowed."
fi

# requirements.txt is compiled on LINUX. Re-compiling it on macOS silently DROPS the
# Linux-only CUDA/torch pins the PII model needs, and the loss is invisible until a
# deploy. Standing MJ rule, previously enforced only by memory — mechanical now.
#
# EVERY WRAPPER, not just the literal command. A hook only ever sees the command STRING the
# caller typed, never what make expands it to — so blocking bare `pip-compile` left
# `make compile-deps` as an unguarded side door to identical damage. (`make setup` was a
# third door; it is now fixed at the source and delegates to scripts/rebuild_venv.sh.)
#
# `make[^|;&]*compile-deps` rather than `make[[:space:]]+compile-deps`: the strict form is
# defeated by `make -j2 compile-deps`, `make -C . compile-deps`, and `make lint compile-deps`
# — flags and preceding targets are the normal way people invoke make, so the strict form was
# guarding only the one spelling nobody has to use. `uv pip compile` and `python -m piptools
# compile` are the same tool under other names. Any new alias belongs on this line.
#
# The boundary is `[^A-Za-z0-9._-]`, not `[[:space:]/]`: the whitespace anchor never matched a
# QUOTE, so `bash -c "pip-compile requirements.in"` — real execution, real damage — walked
# straight through the guard from the day it was written. Found by a truth table, not by
# reading the regex; a regex you only read is a regex you only hope about.
if printf '%s' "$scan" | grep -qE '(^|[^A-Za-z0-9._-])pip-compile([^A-Za-z0-9._-]|$)|(^|[[:space:]])make[^|;&]*compile-deps|(^|[^A-Za-z0-9._-])uv[[:space:]]+pip[[:space:]]+compile|piptools[[:space:]]+compile'; then
  block "Blocked by .claude/hooks/git_guard.sh: pip-compile on macOS strips the Linux/CUDA pins from requirements.txt and you will not notice until deploy. (Includes 'make compile-deps', 'uv pip compile' and 'python -m piptools compile' — same tool, different hat.) Hand-edit requirements.txt instead, or run pip-compile inside the Linux container. To rebuild the venv you almost certainly want 'make setup'."
fi

exit 0
