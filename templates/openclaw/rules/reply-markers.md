# Reply Markers — Platform-Processed Markup

Some kinds of work — rendering a chart, persisting a pattern observation — are best done as **markup in your reply** rather than separate tool calls. The platform parses these markers on the way out, performs the side-effect (render a PNG, write a DB row), and then strips or substitutes the marker tokens before the user sees the message.

Two reasons to prefer markers over tool calls:

1. **No context-window cost.** A tool call shows up in the agent transcript and consumes tokens on subsequent turns. A marker is just text in the reply.
2. **No "did I remember to call the tool?" cognitive load.** The marker is composed naturally where the side-effect belongs. The platform handles the rest.

Markers are only processed when they appear in your **delivered reply** (the text the user actually sees). Markers placed in daily notes, memory writes, or other persisted markdown stay as literal text — they're not processed there. Channel coverage differs: Telegram and LINE render everything, including chart images. The NBHD app processes insight markers (they fill Horizons) and quick-reply markers (they become tappable chips) but does **not** render chart markers inline — so when the user is on the app, describe the numbers in words instead of relying on a `[[chart:...]]` marker. Surfaces that just show raw markdown (e.g. the dashboard) don't process markers at all.

---

## `[[chart:type|params]]` — chart rendering

When you want to show numeric data over time in a user-facing reply, **never draw ASCII or text charts**. Emit a chart marker and the platform will render it as a PNG and attach it to your message. The data is pulled fresh from the source-of-truth tables (Gravity, Fuel, Journal) at render time — you do not need to fetch and embed numbers yourself.

Syntax: `[[chart:type|params]]` where `params` is optional.

Available types:
- `[[chart:payoff_timeline]]` — loan payoff projection from Gravity
- `[[chart:debt_vs_savings]]` — debt and savings balances over time
- `[[chart:momentum_grid|days=14]]` — daily activity grid (Fuel + Journal)
- `[[chart:mood_trend]]` — mood/energy from journal entries

**DO** — drop the marker into your reply where the chart belongs:

> Your avalanche plan is on track. [[chart:payoff_timeline]] AC and AJ are closest to closeout.

> Here's how the last two weeks looked: [[chart:momentum_grid|days=14]]

**DON'T** — draw ASCII bars or tables to visualize numbers:

> ```
> Debt:   ████████░░░░░░ 60% paid
> Savings:▓▓░░░░░░░░░░░░ 12%
> ```

---

## `[[insight:topic_slug]]statement[[/insight]]` — record an observation

When any reply *raises a pattern observation about the user* — anything you wouldn't write in a context-free Q&A reply because it requires knowing this user's specific trajectory — wrap that observation in an insight marker. The platform extracts it, resolves the topic slug, and writes an `AssistantInsight` row with `status='open'`. The statement stays visible to the user; only the marker tokens (`[[insight:...]]` and `[[/insight]]`) are stripped from what they see.

This is the **primary mechanism for filling Horizons' "What I remember" and "Topics I've learned" surfaces** — without insights getting recorded, those panels stay empty.

Syntax: `[[insight:topic_slug]]observation statement[[/insight]]`, or with an explicit pillar: `[[insight:pillar/topic_slug]]observation statement[[/insight]]`.

### Pick the pillar

Insights live under a **pillar** (the area of life the observation is about). Prefix the slug with the pillar and a slash so the observation is filed correctly:

- `gravity` — money: `dining`, `debt`, `savings`, `subscriptions`, `discretionary`, `fixed_expenses`, `income`, `large_purchases` — **finance/Gravity only**: gravity insights are recorded solely when the Gravity module is active for this user; for anyone else a `gravity/` marker is dropped (the statement still shows, nothing is stored)
- `fuel` — training & body: `exercise`, `sleep_quality`, `sleep_quantity`, `hydration`, `meals`, `energy_level`, `alcohol`, `caffeine`
- `core` — mindfulness/practice: e.g. `practice`, `stress`, `focus`
- `journal` — mood, reflection, goals, day-to-day life: e.g. `mood`, `motivation`, `relationships`
- `constellation`, `horizons`, `lessons` — the corresponding surfaces

Examples: `[[insight:gravity/debt]]…[[/insight]]`, `[[insight:fuel/exercise]]…[[/insight]]`, `[[insight:core/practice]]…[[/insight]]`.

**If you omit the `pillar/` prefix, the insight is filed under `journal`** (the neutral, always-on default) unless the surface you're replying from already sets a pillar. When an observation clearly belongs to a specific area, name that pillar so the memory lands where it belongs. Only reach for `gravity` inside an actual Gravity/finance conversation — the platform records gravity insights **only when the Gravity module is active for this user** and silently drops a `gravity/` marker for everyone else, so don't file money observations for a user who isn't using Gravity. If you use a pillar name the platform doesn't recognise, the whole thing is treated as a topic slug under the default pillar and auto-proposed for ops review — nothing is lost, but it's better to name a real pillar.

- `topic_slug` (the part after any `pillar/`) is the topic within that pillar. If you use a novel slug, the platform auto-proposes it for ops review.
- `statement` is what you'd say if asked *"what pattern do you notice about this user on this topic?"* It should be:
  - **About the user**, not generic advice (`you stay in debt for decades` ✅, not `loans take a long time to pay off` ❌)
  - **Falsifiable** — the user can confirm or correct it
  - **Single observation** — one marker per distinct pattern

**DO** — wrap the observation where it appears in your reply:

> Looking at your trajectory, [[insight:debt]]you're carrying balances across 8 lines and staying in debt 20+ years on most of them[[/insight]] — the avalanche fix kicks in around month 8.

> [[insight:dining]]Dining ran 1.8x your baseline last week, driven by takeout[[/insight]] — worth a check-in?

After processing, the user sees:

> Looking at your trajectory, you're carrying balances across 8 lines and staying in debt 20+ years on most of them — the avalanche fix kicks in around month 8.

…and the observation is now part of your memory of this user, viewable from Horizons.

**DON'T** — wrap generic statements or questions:

> [[insight:debt]]Compound interest works against you[[/insight]] ← generic, not about the user

> [[insight:dining]]Do you eat out a lot?[[/insight]] ← a question, not an observation

**DON'T** — wrap things you don't actually believe are patterns yet:

> [[insight:savings]]You haven't saved much this month[[/insight]] ← one-month data is not a pattern; needs more turns of evidence

If you're confident enough to make the statement in your reply at all, wrap it. If you'd hedge ("might be", "could be") — don't wrap; that's not an observation worth recording yet.

### Confirming / refuting later

When the user agrees or corrects in a follow-up turn, call `nbhd_insights_confirm` or `nbhd_insights_refute` directly — confirmation/refutation still flows through tool calls because there's no convenient place in the reply to embed it.

### Fallback

`nbhd_insights_record` is still available as a tool. Use it only when you need to record an insight *without* surfacing the statement in your reply (rare). The marker is preferred because it keeps the user-facing reply, the memory write, and the topic resolution in lockstep.
