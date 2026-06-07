# Cron leader/worker model — workers draft, the reasoning leader reviews

**Status:** proposed (not built) · **Date:** 2026-06-07

## Context

- Routine crons were moved off the reasoning model (DeepSeek V4 Pro) onto a
  small/fast **worker** model (Gemma 4 31B) — see the cron-model fix — because
  DeepSeek overshot the per-turn cron timeout. DeepSeek stays the reasoning
  **leader** for the interactive chat session, where its latency is paid for by
  a human waiting on a real answer.
- Today every cron is a **single turn**: one model gathers data, decides what's
  worth saying, and speaks — end to end. There is no worker→leader handoff.

This note captures the next step: a two-stage cron for the jobs where the
*speaking* (synthesis/judgment) actually matters.

## The pattern

For synthesis-heavy crons (morning briefing, weekly reflection, heartbeat),
split the turn:

1. **Worker stage** (fast/cheap — Gemma): gather data, call the grounding tools
   (`nbhd_current_status`, etc.), and produce a **structured draft + the
   grounded facts it used**. No final "voice" — just the material.
2. **Leader stage** (reasoning — DeepSeek): **review** the draft against those
   grounded facts — catch a stale/ungrounded claim, decide whether it's even
   worth sending, polish the voice — then send (or stay silent).

## Why

- **Quality where it counts, cheaply.** The strong model does the judgment, but
  only over a compact draft + facts — not the expensive multi-tool gather (which
  the cheap worker handles). You pay leader-latency once, on a small payload.
- **Grounding becomes a check, not just a prompt rule.** The leader review is
  the natural enforcement point for "don't surface a status you didn't verify
  this turn" — it compares the draft to the worker's grounded snapshot.
- **Latency stays off the timeout path.** The worker finishes fast; the leader
  reviews a short draft, well inside a sane ceiling.

## Where it fits the current code

- Crons are built in `apps/orchestrator/config_generator.py` +
  `apps/cron/patterns/`. A two-stage cron is a new payload shape — **or** the
  leader pass can live in the `nbhd-cron-enforcement` plugin's `message_sending`
  hook, which already intercepts a cron's outbound message; a leader-review step
  there could rewrite/cancel before send.
- The worker grounds on the same `CRON_GROUNDING_RULE` / `nbhd_current_status`
  snapshot the leader reviews against — one source of truth, two readers.

## Candidates

- **Heartbeat** — its whole job is a leader judgment ("is anything genuinely
  new?"). It's currently a Gemma worker (per the cron-model fix). If it reads
  thin on Gemma, **promote it to leader-review** rather than just bumping the
  model — this is the canonical first candidate.
- **Morning briefing / weekly reflection** — synthesis-heavy; the voice matters.
- **NOT candidates:** `pure_reminder`, `quote_user_intent` — verbatim echo, no
  judgment; single-stage worker is correct.

## Status / trigger

Deferred. **Build it if** the Gemma worker crons (especially briefings /
heartbeat) read thin on the canary after the cron-model fix. Verify on the
canary first; add the leader stage only for the crons that need it.

## Risks

- **Double cost/latency** — gate strictly to synthesis-heavy crons.
- **The leader re-authoring from memory.** The review prompt must bind the
  leader to the worker's grounded facts — *review and polish*, never re-write
  from the model's own recollection (that would reintroduce the exact
  stale-narrative problem the grounding work fixed).
