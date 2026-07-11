"""Wave D — model-behavior eval suite (docs/evals-directive.md §Suite 2).

Drives YAML scenario fixtures (persona + multi-turn script) against a SYNTHETIC
behavior tenant's real container, checks DETERMINISTIC hard assertions against
OBSERVABLE evidence (DB side-effect rows + reply-text patterns) in Python, and
scores subjective soft dimensions with a pinned, spend-capped LLM judge.

Hard-assertion failures gate the run pass/fail; judge scores are recorded but
ADVISORY (non-gating) — the "advisory gate first" NAMED design decision in the
directive. Backend computes evidence; the LLM only judges (standing invariant).

INVARIANT #1: scenario content is SYNTHETIC (never a real user), so the reply
text may flow to the hard assertions and the judge — but it is NEVER written to
``EvalResult.details`` or any log line. Only counts, codes, scores, and labels
are persisted.

This package lands INERT: no schedule, and the behavior tenant is not provisioned
yet. Fire-verification follows provisioning.
"""
