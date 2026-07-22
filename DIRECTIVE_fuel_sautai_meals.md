# DIRECTIVE: Tonight's meal in Fuel — the sautai surface

**Vision (MJ, 2026-07-22).** The onboarding demo's Fuel tab shows a TONIGHT card
("Miso-glazed salmon, rice, spinach — light before tomorrow's intervals") that today is
fixture-only. Make it truthful: a linked sautai account's planned meal for tonight renders
in the real Fuel tab's next-up slot — the slot the week-hero redesign already reserved
(nbhd-ios `DIRECTIVE_fuel_week_hero.md`, decision 4: "tonight's planned dinner if Fuel meal
data exists, else the next planned session"). This completes the integration story the
marketing screenshots are already promising.

**Prerequisite (hard):** united PR #1220 (sautai targeting/regenerate fixes) merges FIRST —
MJ's merge. The sautai-side prerequisite (sautai #184) is already merged.

## Binding design decisions

1. **One endpoint, read-only, console-side.** `GET /api/v1/fuel/meals/today/` on united —
   returns the linked sautai account's planned meals for TODAY in the tenant's timezone
   (`apps/common/tenant_tz.py` is the front door; never server-local time). Response:
   `{ "meals": [{ "slot": "dinner", "name": "...", "note": "...", "date": "YYYY-MM-DD" }] }`
   — empty array when unlinked, no plan, or sautai unreachable. The endpoint never errors
   the tab: degraded = empty.
2. **Identity at the PRODUCT level.** Resolve the sautai account via the existing
   link-identity mechanism (feat/sautai-link-identity line) — never email-exact-match
   (standing rule; relay emails exist). No link ⇒ empty response; the endpoint does not
   distinguish "unlinked" from "no plan" to the client in v1 (no upsell surface yet).
3. **Server-to-server, not through the assistant.** United calls sautai's API directly with
   the linked identity, following the existing `apps/integrations` outbound patterns
   (timeouts, retries bounded, no PII in logs). Short server-side cache (~15 min per
   tenant) — meal plans change rarely intra-day; never let a sautai outage slow Fuel.
4. **iOS: the card prefers the meal, never fights the session.** FuelViewModel fetches
   `/fuel/meals/today/` alongside the overview. Next-up card logic: if tonight's dinner
   exists → TONIGHT card (fork/knife glyph, meal name, sautai note verbatim if present —
   quiet secondary line); the next SESSION then renders as the following next-up item if
   space allows, else the checklist already carries it. No meal ⇒ current behavior exactly.
   v1 card is informational — no navigation. (v2 candidates, NOT now: tap → chat deep-link
   "about tonight's dinner"; quiet link-sautai invitation for unlinked accounts.)
5. **Demo-space parity.** Yuki's fixture TONIGHT card stays — after this ships it is a
   truthful preview of a real surface (resolves the onboarding directive's "keep or
   neutralize sautai" open question as: KEEP).
6. **Voice.** The note line is sautai's own text, verbatim; no synthesized coaching copy on
   the card. Empty note = name only. No calorie/macro numbers in v1 even if sautai serves
   them — the card is a plan reminder, not a nutrition dashboard.

## Rollout (two-stage canary, standing rule)

Endpoint + iOS behind the existing Fuel surface (no new flag needed — empty response = old
behavior). Canary: MJ's tenant first (his sautai link is real), verify the card against his
actual sautai plan incl. tenant-tz day boundary (Asia/Tokyo); then Kiho; then fleet-visible
by default. Backend deploys via normal main auto-deploy; iOS rides the next release train
after 2.1.3.

## Tests

united: endpoint tz-boundary (JST evening = still "today"), unlinked/empty/sautai-down all
⇒ empty 200, cache behavior, RLS/tenant scoping, no-PII-in-logs. iOS: card preference
logic (meal present/absent/with-without note), snapshot decode, fallback intact.

## Out of scope

Meal CRUD from NBHD · nutrition data · breakfast/lunch slots on the card (data may include
them; card shows dinner in v1) · linking UX changes · assistant-side sautai tools (that's
the #1220 line, separate).

## User stories (direction check, 2026-07-22 — build against THESE)

1. **The evening glance.** Linked user opens Fuel after work → tonight's dinner is just
   *there*, next to what's left of the training week. One glance answers "what's tonight."
   The value is the PAIRING (meal × session), not the recipe.
2. **The invisible default.** Unlinked user sees nothing new — no upsell card, no empty
   state. Fuel today = Fuel yesterday.
3. **The resilient tab.** sautai slow or down → Fuel loads at full speed, card simply
   absent. A partner outage must never be visible as OUR outage.
4. **The honest clock.** JST user at 23:30 → "today" is still their today. Day boundary =
   tenant tz, tested at the boundary.
5. **One truth, two mouths.** The card and the assistant's chat answer to "what's for
   dinner?" must come from the SAME source (sautai's plan via the live client) — never a
   cached duplicate that can disagree. Consistency is the trust story; add a test asserting
   the endpoint reads through the client, not a local copy table.
6. **The showroom promise kept.** A new user sees Yuki's TONIGHT card in the demo; when
   they create a space and link sautai, the identical surface fills with THEIR plan.
7. **Stories we are explicitly NOT serving in v1** (so codex doesn't drift): tapping for
   the recipe · editing meals from NBHD · nutrition numbers · breakfast/lunch cards ·
   link-sautai onboarding prompts.

Implementation note from recon: main already has Provider.SAUTAI with the Phase-0.5
account-id link (apps/integrations/models.py) and apps/integrations/sautai_client.py —
build on both; do not invent new link plumbing.
