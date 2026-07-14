#!/bin/bash
# PreToolUse(Bash) guard — enforces docs/agents/workflow.md rules mechanically.
# Exit 2 blocks the command; stderr is fed back to the agent.

cmd=$(jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$cmd" ] && exit 0

block() {
  echo "$1" >&2
  exit 2
}

if printf '%s' "$cmd" | grep -qE 'git add +(-A|--all|\.)([[:space:]]|$|;)'; then
  block "Blocked by .claude/hooks/git_guard.sh: broad staging (git add -A/./--all) risks committing .env, models, or unrelated WIP. Stage specific files by path (docs/agents/workflow.md)."
fi

if printf '%s' "$cmd" | grep -qE 'git commit[^|;&]*--no-verify'; then
  block "Blocked by .claude/hooks/git_guard.sh: --no-verify skips pre-commit hooks. Only allowed for a scanner false positive in scanner code itself — ask MJ first."
fi

if printf '%s' "$cmd" | grep -qE 'git push[^|;&]*(--force|--force-with-lease|-f[[:space:]])' && printf '%s' "$cmd" | grep -qE '(^|[[:space:]:])main([[:space:]]|$|;)'; then
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
if printf '%s' "$cmd" | grep -qE '(^|[[:space:]/])pip-compile([[:space:]]|$|;)|(^|[[:space:]])make[^|;&]*compile-deps|(^|[[:space:]/])uv[[:space:]]+pip[[:space:]]+compile|piptools[[:space:]]+compile'; then
  block "Blocked by .claude/hooks/git_guard.sh: pip-compile on macOS strips the Linux/CUDA pins from requirements.txt and you will not notice until deploy. (Includes 'make compile-deps', 'uv pip compile' and 'python -m piptools compile' — same tool, different hat.) Hand-edit requirements.txt instead, or run pip-compile inside the Linux container. To rebuild the venv you almost certainly want 'make setup'."
fi

exit 0
