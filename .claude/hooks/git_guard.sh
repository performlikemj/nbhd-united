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
# `make compile-deps` MUST be listed separately. A hook only ever sees the command STRING
# the caller typed, never what make expands it to — so blocking the literal `pip-compile`
# leaves the Makefile target as an unguarded side door straight to the same damage. (Found
# by reading the Makefile instead of assuming: `make integrate` doesn't exist either — the
# target is `integrate-gate`.) Any new wrapper around pip-compile belongs on this line.
if printf '%s' "$cmd" | grep -qE '(^|[[:space:]/])pip-compile([[:space:]]|$|;)|(^|[[:space:]])make[[:space:]]+compile-deps([[:space:]]|$|;)'; then
  block "Blocked by .claude/hooks/git_guard.sh: pip-compile on macOS strips the Linux/CUDA pins from requirements.txt and you will not notice until deploy. (This includes 'make compile-deps', which is just pip-compile wearing a hat.) Hand-edit requirements.txt instead, or run pip-compile inside the Linux container."
fi

exit 0
