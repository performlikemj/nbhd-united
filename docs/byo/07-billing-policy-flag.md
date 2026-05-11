# ⚠️ READ FIRST — billing policy may invalidate the refactor's goal

## The finding

OpenClaw's upstream documentation, captured at `/tmp/openclaw-audit/openclaw-core/docs/help/testing-live.md:202`:

> *"`pnpm test:docker:live-cli-backend:claude-subscription` requires portable Claude Code subscription OAuth through either `~/.claude/.credentials.json` with `claudeAiOauth.subscriptionType` or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`. It first proves direct `claude -p` in Docker, then runs two Gateway CLI-backend turns without preserving Anthropic API-key env vars.* **This subscription lane disables the Claude MCP/tool and image probes by default because Claude currently routes third-party app usage through extra-usage billing instead of normal subscription plan limits.**

The bolded sentence describes Anthropic's billing policy for the Claude Code CLI when invoked as a subprocess from a third-party application (which is exactly what OpenClaw does).

## What this means for the BYO refactor

The user's stated goal was *"each per-tenant OpenClaw container uses Claude via the local claude CLI authenticated with that tenant's own Claude OAuth credentials"*, with the explicit assumption that this would shift billing **from the platform's Anthropic API key to the tenant's Pro/Max subscription chat allowance.**

OpenClaw's upstream maintainers are documenting that **this assumption may be wrong.** Specifically:
- When `claude` is invoked as a subprocess by OpenClaw (which it always is in our setup), Anthropic appears to route the inference through the **extra-usage** billing pool, not the subscription plan limits.
- This is the same pool that `claude setup-token` tokens billed against — the thing we were trying to move away from.
- So switching from `setup-token` → full OAuth credentials may produce **identical billing behavior** (extra-usage, not subscription).

## What's still uncertain

The OpenClaw docs say *"Claude currently routes ..."* — present-tense, implying a current Anthropic policy that could change. Three open questions:

1. **Is this universal, or only for OpenClaw specifically?** Anthropic's third-party-app detection might key on the User-Agent string, the client identifier in the OAuth exchange, or other signals. If it's User-Agent-based, OpenClaw's `claude` invocations carry the Claude Code CLI's User-Agent — which Anthropic may classify as either first-party or third-party.

2. **Is "extra-usage" still cheaper than the platform API key?** Even if billing routes through extra-usage, the *tenant* is paying (not NBHD), which preserves the primary product goal of "tenants bring their own Claude account." The question is whether the tenant gets the *full* Pro/Max value (subscription chat allowance, then extra-usage as overflow) or just the extra-usage portion.

3. **Does it apply equally to `setup-token` and full OAuth credentials?** The OpenClaw doc lists both as "subscription OAuth" credentials in the same sentence. If the routing is policy-based (Anthropic detects "called as subprocess from non-Claude-Code app"), the credential shape may not matter. If the routing is credential-based (Anthropic distinguishes setup-token from full-OAuth), the refactor still helps.

## How to find out

1. **Read the OpenClaw upstream code** at `/tmp/oc-pack/package/dist/` and search for "third-party app" / "extra-usage" / billing-related comments. The doc cites this as a constraint they're working around — there may be a workaround documented in code that we can adopt.

2. **Ask the Anthropic team directly.** Anthropic's developer relations should be able to confirm:
   - Does the Claude Code CLI's Pro/Max OAuth route through extra-usage when invoked as a subprocess by a non-Anthropic app?
   - Is there a User-Agent or app-identifier we can set to be classified as first-party for this routing?
   - Is `claude --print` (the way OpenClaw uses it) categorized differently from interactive `claude` usage?

3. **Empirically test on a willing user.** Have someone connect their Pro/Max account via the new flow, run a few turns, then check their Anthropic billing dashboard. If extra-usage went up and chat-allowance didn't, the doc claim is real for our setup.

## What to do in the meantime

**Don't block the refactor on this finding**, but **do flag it in the connect UI**:

- Update the connect modal copy to be honest about uncertainty. Suggested language:
  > *"Connecting your Claude account will route inference through your subscription credentials. Anthropic's current billing policy for third-party apps may route some or all usage through extra-usage rather than your subscription chat allowance — we're verifying this with Anthropic. Either way: you're paying Anthropic directly, not NBHD."*

- This sets honest expectations without killing the feature.

- Once we have a definitive answer from Anthropic (or empirical evidence), update the copy accordingly.

## Connection to the larger product question

If the answer is *"yes, OpenClaw → claude → Anthropic always bills against extra-usage,"* then the BYO Claude refactor's primary value proposition (cheaper Claude via subscription) is partially defeated. The remaining value:

- **Auth boundary still moves to the tenant.** They're paying Anthropic, not us. Eliminates the shared-key billing-incident class.
- **Per-tenant accountability.** Their usage shows up on their bill, not ours.
- **Rate limit isolation.** One tenant burning their account doesn't affect others.

These are still meaningful wins. But the "use your subscription chat allowance" framing may need to soften.

If the answer is *"no, full OAuth credentials get subscription routing; only setup-token gets extra-usage,"* then the refactor delivers its full value and the existing setup-token flow's main weakness is confirmed.

**This is the single most important question to resolve before shipping the connect modal copy.** Implementation work can proceed in parallel.
