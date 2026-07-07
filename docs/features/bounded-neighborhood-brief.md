# The Bounded Neighborhood — chosen inner circle + soft ceiling (design brief)

> **Design center (MJ, verbatim, this is the whole brief):**
> *"i like the ceiling on genuine relationship. this is the way based on science. and inner
> circle as well. maybe warp gates are for inner circles and a scrollable list for everyone
> else. this isn't about making the most friends. it's about sharing sparks and ideas and
> lessons."*
>
> This is a **design brief**, not an implementation. It extends the shipped Neighborhood
> (`docs/features/friends-neighborhood-design.md`, `CONTINUITY_friends_neighborhood.md`,
> `apps/friends/`) without contradicting a single shipped semantic: the spark trust-gate,
> consent-everything, approve-before-it-leaves, agents-backstage, dark-launch behind
> `friends_enabled`. Where this brief proposes something new, it is **additive** — a new
> per-viewer table, additive payload fields, one iOS function swap. **Zero data migration.**
> Nothing here relitigates the Neighborhood's decided spine.

---

## 1. Vision — bounded by design; depth of exchange over headcount

The Neighborhood already refuses to be a follower machine: every edge is a mutual **wave**, a
neighbor's galaxy only opens once they've **shared a spark**, and a Dunbar-ish ceiling of 150
is already in the code. The Bounded Neighborhood makes that boundedness the *point* rather than
a safety rail. It gives the Neighborhood a **shape**: a small, chosen **inner circle** you fly
to, wrapped in a **wider neighborhood** you browse.

Two tiers, one philosophy:

- **"My sky" — the inner circle (~12).** The people you deliberately keep close. Putting
  someone in your sky is an explicit act — *"add to my sky"* — and it is **capped so the choice
  costs something.** These are the neighbors whose galaxies become **wormhole gates** in your
  3D flight: you steer to them, warp in, come home. Because the sky is small, a gate *means*
  someone.
- **The wider neighborhood (~150).** Everyone else you've waved to. They live in a **scrollable
  warp directory** — search, recents, pinned — not the flight. The spark-gate is unchanged
  everywhere: you can still warp to any neighbor who's shared, straight from the directory.

The app is built to **celebrate exchange, not accumulation.** What lights up is a spark shared,
an idea brought home, a mission you showed up for together — never a headcount. The ceiling
reads as *philosophy* ("your Neighborhood is meant to be people you actually know"), never as a
paywall or a locked feature. The Dunbar layers (an inner ~5, a sympathy group ~15, ~50, ~150)
support the **shape**; the exact numbers are **product choices informed by the science, not
claims the science dictates them.** We say "this is the way we think relationships stay real,"
not "science says 150."

This is a small, high-leverage layer. It reuses the constellation grammar wholesale, adds one
tiny private table, and turns a placeholder (the flight currently shows a recency-picked handful
of gates) into an intentional one (the flight shows *your sky*).

---

## 2. The two tiers & their mechanics

### 2.1 "My sky" — the chosen inner circle

**What it is.** A **private, one-way, personal curation** of accepted neighbors. It is *yours*:
you decide who's in it, it is invisible to the people in it, and it exists only to shape *your*
flight and *your* attention. It is the polar opposite of a follower graph — nobody can see they're
in your sky, and there is no reciprocal "who added me."

**How you add someone.** An explicit **"Add to my sky"** action, available in two places:

- On a **NeighborProfileSheet** (the profile you already open from the directory or a gate).
- Inline in the **warp directory** (a quiet star/pin affordance per row).

Adding is deliberate and instant; the celestial copy carries the weight ("added to your sky").

**The cap (recommend 12, HARD).** The inner circle is a **hard cap of 12.** The friction *is*
the feature: when your sky is full, adding a 13th neighbor requires **removing** one first — a
gentle "your sky is full — make room?" moment, never an upsell. A soft cap here would let the sky
silently become a junk drawer and the gates would lose meaning. Twelve sits deliberately just above
Dunbar's ~5 support clique and ~15 sympathy group — close enough to feel like "the people who
matter," small enough that every gate is a face. (See open question 1 for the exact number.)

**Removal is frictionless and private.** One tap removes a neighbor from your sky. It is **not**
an unfriend — the neighbor stays in your wider neighborhood, you stay neighbors, nothing is
revoked, they are never told. Removing from sky only takes their gate out of your flight.

**What renders in the flight.** The 3D flight renders a wormhole gate **only** for neighbors in
your sky **who have also shared ≥1 spark with you** (the spark-gate, unchanged). So the flight is
`in_my_sky AND spark_count > 0`. This is the exact swap point described in §5.2 — it replaces the
current "top-K = 6 by recency" placeholder. An in-sky neighbor who hasn't shared yet is a **quiet
sky-slot** (they're chosen, but there's nothing to warp to yet) — surfaced in the sky roster in the
directory, not as a dead gate in space (see open question 3 for how to render this).

**Recency may SUGGEST, never auto-place.** The system may *nudge* you toward a candidate — *"Kiho's
been sharing a lot lately — add her to your sky?"* — as a **passive, dismissable hint** in the
directory, sourced from evidence the backend already has (inbound spark recency, recent 1:1 chat,
a shared active Mission). It is never a push notification, never auto-adds, and never says "someone
added you." Backend computes the evidence; the human decides — the same posture as every other
agent suggestion in the product (see open question 4).

### 2.2 The wider neighborhood — the scrollable warp directory

**What it is.** Every accepted neighbor, in a **scrollable directory**: **search** by name/handle,
**recents** (neighbors you've chatted with or who've sparked lately), and **pinned** (a lightweight
manual pin, distinct from the sky). It is the calm index of your whole Neighborhood — the place you
go to find someone, open their profile, add them to your sky, or warp to them directly.

**The spark-gate is unchanged, everywhere.** A directory row shows a **"warp"** affordance exactly
when that neighbor has shared ≥1 spark (`spark_count > 0`) — the same gate that governs the flight
and the profile sheet today. You do **not** need someone in your sky to warp to them; the sky only
governs which gates appear in the *flight*. The directory is the universal door to any neighbor's
shared galaxy.

**The soft ceiling (~150) with honest copy.** The 150 ceiling already ships (`MAX_NEIGHBORS = 150`,
enforced only when a wave/claim would *grow* your network — it never retroactively removes anyone).
This brief keeps that behavior and dresses it as philosophy. Draft copy candidates for the
at-ceiling moment (pick one; all avoid the paywall register):

1. *"Your Neighborhood is full at 150. This isn't a limit we're selling you past — it's about how
   many people anyone can really keep up with. Make room when someone new matters more."*
2. *"150 neighbors. That's about the most any of us can genuinely know. To add someone, let someone
   go — no hard feelings, no notifications."*
3. *"You've reached a full Neighborhood (150). We cap it on purpose: sparks and lessons only travel
   between people who actually know each other. Room for one more? Say goodbye to make it."*

(The shipped message is close to candidate 2 already — this brief refines the register, not the
mechanic. See open question 2 for whether 150 stays a firm-on-growth stop or becomes truly soft.)

---

## 3. Anti-metrics rules — PRODUCT LAW

These are not guidelines; they are invariants that bind every surface, including assistant output.
The Neighborhood competes on *demonstrable trust and depth*, and vanity metrics are the exact
disease it exists to avoid.

**Never displayed, anywhere:**

1. **Friend/neighbor totals as status.** No "247 neighbors," no growth graph, no milestone badge,
   no profile stat. The count exists **only** internally for cap enforcement. The one permitted
   surfacing is **capacity framed as room-to-choose** — "3 spots left in your sky," "your
   Neighborhood is full" — never an achievement.
2. **Follower asymmetry.** There is no one-way follow and no "followers/following." Every wider-tier
   edge is a mutual accepted wave. The inner circle is one-way but **invisible** — so it can never
   render as a follower count, and there is deliberately **no "X people added you to their sky"**
   counter (that would rebuild follower optics and leak a private choice).
3. **Leaderboards / rankings.** No "most sparks shared," no "most warped-to," no most-neighbors,
   no top-contributors, no social ranking of any kind.
4. **Streaks or quotas on social acts.** No "you haven't waved in 5 days," no sharing streak, no
   nudge to grow the number. (Mission consistency is about the *goal*, never about social volume.)
5. **Surveillance/pressure signals.** No public "seen by," no "visited your galaxy N times." A warp
   is a quiet, private read; visiting a neighbor's shared galaxy must never generate a watched
   feeling. (The existing `WormholeVisit` watermark is per-viewer and drives only *your own* "new
   since last visit" glow — it is never shown to the owner.)

**What the moments feed celebrates instead — exchange, the moment an idea moved between people:**

- **A spark arrived.** *"Kenji shared a spark with you."*
- **An idea traveled home.** *"You brought Aya's batch-cook spark into your galaxy."* (an `adopt`)
- **A mission you showed up for.** *"You and Aya are 6/7 on July Steps."*
- **A spark that became a conversation** (a share that led to a 1:1 reply).

The feed's unit is **an exchange**, never a tally. The shipped `neighborhood_home` moments feed
already surfaces *decisions* (incoming waves, share-proposals to approve); this brief extends it
with *exchange* moments (spark shared, spark adopted, mission check-in) and audits every other
surface to strip any headcount-as-status (§6, BN-PR4).

---

## 4. Backend changes

All additive. No change to any shipped table's columns, no data backfill, no change to existing
endpoint response shapes beyond **new optional fields**. Every new public table follows the friends
RLS backstop invariant (relock migration in-PR; FORCE-RLS candidacy per the
`apps/friends/migrations/0008_friends_rls_backstop.py` pattern).

### 4.1 How the inner circle is stored — recommendation: a new per-viewer table, NOT a Circle

**The question:** a per-neighbor tier flag, or reuse the existing `Circle` model as a built-in
system Circle named "My sky"?

**Recommendation: a new tiny per-viewer, directional table — `SkyMembership` — mirroring
`WormholeVisit` exactly.** Not a Circle.

```python
# apps/friends/models.py  (sketch)
class SkyMembership(models.Model):
    """One row per (viewer, friendship): 'I have chosen this neighbor for my sky.'
    PRIVATE + ONE-WAY + INVISIBLE to the other party — the polar opposite of a Circle.
    Read ONLY self-scoped, through apps/friends/access.py (chokepoint)."""
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    viewer_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sky_memberships")
    friendship    = models.ForeignKey(Friendship, on_delete=models.CASCADE, related_name="+")
    added_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "friend_sky_memberships"
        constraints = [models.UniqueConstraint(fields=["viewer_tenant", "friendship"], name="uq_sky_membership")]
        indexes = [models.Index(fields=["viewer_tenant"])]
```

**Why this shape, and why NOT a Circle** (the `Circle` model is `apps/friends/models.py:582`):

| Property | "My sky" needs | `Circle` provides | Verdict |
|---|---|---|---|
| Directionality | **one-way** (my private choice) | **mutual** (a shared group) | Circle is wrong |
| Visibility | **invisible** to members | members **see each other** (roster, group chat) | Circle leaks the choice |
| Group chat | **none** | `FriendThread(kind=circle)` | Circle adds unwanted surface |
| Shared grants | **none** (sky ≠ a sharing audience) | circle-scoped `LessonShareGrant` | Circle would mis-scope sharing |
| Consent | none needed (I curate privately) | membership **is** a consent grant | Circle implies false consent |
| Cap | ~12 | `MAX_CIRCLES_PER_TENANT=8` / `MAX_CIRCLE_MEMBERS=50` | Circle caps collide |
| Absorb/leave machinery | none | circle-scoped absorb + leave-purge fires | Circle fires the wrong plumbing |

Forcing "My sky" into a Circle would create a fake group whose "members" never consented to being
grouped, risk the circle's mutual-visibility and group-chat semantics exposing a private choice, and
trip the circle caps and circle-scoped absorb/leave code. `SkyMembership` mirrors the **already-proven**
`WormholeVisit` shape (`apps/friends/models.py:305`): per-`(viewer, friendship)`, private, lifecycle
tied to the edge via `Friendship` CASCADE (unfriend/block cleans up the sky row for free).

> **Ultra-minimal alternative (note, don't default to it):** add a single `in_sky = BooleanField(default=False)`
> column to the existing `WormholeVisit` table. It's even more migration-light (one additive, defaulted
> column on an already-relocked table — no new relock migration), but it overloads a "visit watermark"
> with "membership," and `WormholeVisit` rows are created lazily on first warp, so an add-to-sky before
> ever warping must still create the row. Recommend the dedicated table for clean semantics + a cheap
> `count()` cap query; fall back to the column only if avoiding a new relock migration is worth the muddiness.

### 4.2 Ranked / paginated neighbors endpoint (the directory)

`list_neighborhood` (`apps/friends/services.py:154`) and the richer `neighborhood_home`
(`services.py:520`) return the full neighbor list unpaginated, sorted by display name. That's fine at
today's scale but is the directory's backing data. Add a **ranked, paginated** directory endpoint:

- `GET /api/v1/friends/directory/?q=<search>&cursor=<opaque>&limit=` — accepted neighbors only,
  each row: `{friendship_id, display_name, handle, avatar_hue, spark_count, in_my_sky, pinned,
  has_unread_thread, last_exchange_at}`. Default order: **recents** (`last_exchange_at` desc — last
  chat or last inbound spark), then alphabetical. `q` filters by display_name/handle. Keyset cursor
  reuses the `build_since_page`/`encode_cursor` pattern already forked into `apps/friends/feed.py`.
- The existing `GET /api/v1/friends/` and `/home/` keep working unchanged (additive `in_my_sky` /
  `pinned` fields on their neighbor rows; older clients ignore them).

### 4.3 Server-side "warpable" — the sky-filtered gate list

The flight should not have to decide membership. Mark it server-side so the client just renders what
it's handed:

- Extend `list_wormholes` (`apps/friends/services.py:888`) / `wormhole_targets`
  (`apps/friends/access.py:444`) to include **`in_my_sky: bool`** on each target, and accept a filter:
  `GET /api/v1/friends/wormholes/?warpable=sky` returns **only** in-sky + ready-spark gates (the flight
  set); no filter returns all spark-sharing neighbors (today's behavior — the directory's warp set).
- Optionally a dedicated `GET /api/v1/friends/sky/` returning the sky roster (including quiet
  in-sky-no-spark slots) for the directory's "your sky" section.

This makes the iOS/web flight swap (§5.2) a **filter, not a computation** — the client renders the
`warpable=sky` list verbatim.

### 4.4 Ceiling enforcement points

- **Inner circle (hard 12):** enforce at the **add action** — `POST /api/v1/friends/<friendship_id>/sky/`
  checks `SkyMembership.objects.filter(viewer_tenant=me).count() < MAX_SKY` (recommend `MAX_SKY = 12`)
  and returns a gentle 409/422 "your sky is full — make room" with the current members so the client can
  offer a swap. `DELETE /api/v1/friends/<friendship_id>/sky/` removes, no checks. This mirrors the circle
  cap check at `apps/friends/circles.py:107`.
- **Wider neighborhood (soft-on-growth 150):** **already enforced** at wave-send (`services.py:229`) and
  invite-claim (`services.py:426`). No new enforcement — only the copy refinement (§2.2).

### 4.5 RLS implications (the friends backstop invariant binds this)

The three friends content tables (`shared_lessons`, `lesson_share_grants`, `friend_messages`) run
FORCE-RLS with tenant-GUC policies `TO app_user` and are confirmed binding in prod
(`apps/friends/migrations/0008_friends_rls_backstop.py`; Django's pool connects as non-superuser
`app_user`). Any new table follows the same backstop:

- **Relock migration in the same PR** (RLS `ENABLE`, **no policy** — the `test_public_schema_lockdown`
  test forbids policies on `public.*` for the anon Data API), depending on both the new `friends.00NN`
  and the latest `tenants.00NN` so it runs last in topo order (the recurring `rls_relock_topo_shift`
  hazard). Pattern: `tenants/00NN_relock_after_sky`.
- **FORCE-RLS candidacy:** `SkyMembership` holds no cross-tenant *content* — only "viewer X chose
  friendship Y." But *whom you keep close is private*, so reads must stay self-scoped through the
  accessor, and it should get a `viewer_tenant_id = app.tenant_id` SELECT policy when the FORCE-RLS set
  is next extended (add it to `FRIENDS_TABLES`). Its blast radius is lower than the content tables
  (leaking a sky row reveals a private preference, not another person's life), so accessor + relock is
  sufficient for MVP with the FORCE policy as the standard hardening follow-up.
- **Accessor routing:** all sky reads/writes go through `apps/friends/access.py` (the AST chokepoint
  test `test_access_chokepoint.py` fails the build otherwise) — `assert_neighbors` already gives the
  party-checked edge before any sky write.

---

## 5. iOS changes

iOS is the strategic surface. The Neighborhood UI shipped as N0–N8 on `feat/neighborhood-n0`; the 3D
flight/wormholes are in flight on `feat/flight-wormholes`. Requires MJ Xcode builds. All additive,
behind the same dark flags.

### 5.1 "Add to my sky" action (profile + directory)

- **NeighborProfileSheet:** add an **"Add to my sky" / "In your sky ✓"** toggle beside the existing
  spark-gated warp affordance (`sparkCount > 0`). Full → present the "make room" swap sheet. Copy is
  celestial and warm; the act feels deliberate.
- **Directory rows:** a quiet inline star/pin affordance to add-to-sky without opening the sheet.
- Calls `POST/DELETE /api/v1/friends/<friendship_id>/sky/`; optimistic with rollback on the 409-full.

### 5.2 The flight-gate swap — ONE function

<!-- IOS_SWAP_POINT: exact file/function slotted from the flight-wormholes exploration below -->
On `feat/flight-wormholes`, the neighbors rendered as wormhole gates in the flight are chosen by a
**single pure function** — currently a **top-K = 6 by recency** placeholder, deliberately isolated as
*the* swap point. The entire iOS behavior change is to swap that function's body:

- **From:** "take all warp targets, sort by recency, keep the first 6."
- **To:** "keep the targets where `inMySky == true`" — or, if the backend ships `warpable=sky`
  (§4.3), the function collapses to "render exactly what the sky endpoint returned." Either way it is a
  **one-function diff**; no change to gate rendering, warp choreography, return-home, or the second
  scene.

Because the placeholder already isolates selection behind one pure function, this swap is the whole
point of the branch's design — the flight goes from "a recency-picked handful" to "your chosen sky"
with no structural change.

### 5.3 Directory UX

- A **scrollable list**: search field, "Your sky" section at top (the ≤12, including quiet no-spark
  slots), then **Recents**, then **Pinned**, then the full alphabetical roster.
- Each row: avatar hue dot, name/@handle, a **warp** affordance iff `spark_count > 0`, an **add-to-sky**
  affordance (or ✓ if in sky), unread-thread dot.
- Backed by `GET /api/v1/friends/directory/` (§4.2), keyset-paginated, poll-free (it's not a live feed).

### 5.4 Empty & ceiling states

- **Empty sky:** *"Your sky is empty. Add the neighbors you want to keep closest — their galaxies
  become gates you can fly to."* (with a jump to the directory).
- **Sky full (adding #13):** a warm swap sheet — *"Your sky holds 12. Who makes room for {name}?"* —
  listing current sky members to remove; never an upsell.
- **In-sky, no sparks yet (quiet slot):** *"{name} is in your sky. When they share a spark, a gate
  appears in your flight."*
- **Neighborhood full (150):** the philosophy copy from §2.2.
- **Flight with an empty sky:** the flight shows your own galaxy with no gates and a soft prompt —
  *"Add neighbors to your sky to see their gates out here."*

---

## 6. Phasing — PR-sized, additive, migration-free from today's placeholder

Every PR is dark-flag-safe (`friends_enabled`), independently verifiable on the MJ + Kiho test pair, and
**purely additive**: today's top-6-recency placeholder keeps working until BN-PR2 swaps it, and every
new backend field defaults to "not in sky," so nothing breaks in between.

- **BN-PR1 — backend foundation (invisible).** `SkyMembership` model + migration + **relock migration**
  (`tenants/00NN_relock_after_sky`); `MAX_SKY = 12` hard cap; `POST/DELETE /friends/<friendship_id>/sky/`;
  `in_my_sky` added to `wormhole_targets` / `list_wormholes` / `neighborhood_home` rows; `?warpable=sky`
  filter; accessor `sky_membership_*` helpers + chokepoint coverage. **No UI.** Verify: add/remove a sky
  edge between MJ↔Kiho; cap rejects the 13th; relock + chokepoint tests green; existing endpoints
  unchanged for old clients. *Additive table only — zero data migration.*
- **BN-PR2 — iOS flight swap (the one function).** Swap the flight-gate selector from top-6-recency to
  `inMySky` (or to the `warpable=sky` payload). Empty-sky flight state. Verify in the Simulator: with a
  sky set, only sky gates render; with an empty sky, none do; warp/return-home unchanged. *One-function
  diff.*
- **BN-PR3 — iOS directory + add-to-sky.** The scrollable directory (search/recents/pinned + "Your sky"
  section), the add-to-sky action on profile + directory rows, all empty/ceiling states, the dismissable
  suggestion hint. Backed by `GET /friends/directory/` (add it in this PR or fold into BN-PR1). Verify:
  browse, search, add/remove sky, hit the full-sky swap sheet.
- **BN-PR4 — moments as exchange + anti-metrics sweep.** Extend `neighborhood_home` moments with
  exchange events (spark shared / spark adopted / mission check-in); audit every Neighborhood surface
  (web + iOS) to strip any headcount-as-status; ship the philosophy ceiling copy. Verify: an adopt and a
  new inbound spark appear as moments; no surface shows a neighbor total as a stat.
- **BN-PR5 — web parity (optional, later).** Mirror the sky filter into the web flight
  (`frontend/lib/constellation-game/galaxy-scene.ts` renders all wormholes today — apply the same
  `warpable=sky` filter) and add a web directory. Lower priority; iOS is the strategic surface.
- **BN-PR6 — sky FORCE-RLS hardening (defense-in-depth, final).** Add `friend_sky_memberships` to the
  FORCE-RLS set with a `viewer_tenant_id = app.tenant_id` SELECT policy, per the PR8 pattern. Infra-light
  (the `app_user` role and GUC wiring already exist).

**Migration-free path from today:** BN-PR1 creates one new empty table and adds defaulted/derived
fields — no existing row is touched, no backfill runs. The placeholder function stays live until BN-PR2
replaces it. If BN-PR2 shipped before BN-PR1's data existed, every sky would simply be empty (flight
shows no gates) — degraded, not broken.

---

## 7. Open questions for MJ (each with a recommended default)

1. **Inner-circle cap — exact number & hardness.** *Recommend **12, HARD*** (adding #13 requires
   removing one). Twelve sits just above Dunbar's ~5/~15 inner layers; the hardness is what makes "add
   to my sky" a real choice. Alternatives if 12 feels off: 10 (tighter, more reverent) or 15 (the
   sympathy-group number exactly). Do not make the inner cap soft — a soft inner circle becomes a junk
   drawer and the gates stop meaning anything.

2. **The 150 ceiling — firm-on-growth or truly soft?** *Recommend **keep firm-on-growth** (the shipped
   behavior: you can't add the 151st until you remove someone), reframed as philosophy* (§2.2 copy).
   MJ likes "the ceiling on genuine relationship" — a truly-soft "warn but allow unlimited" would erode
   the very ceiling he likes. Keep it a real stop, dressed warmly.

3. **Inner-circle placement — reciprocity or one-way private curation?** *Recommend **one-way private
   curation**:* you can add any accepted neighbor to your sky regardless of whether they've sparked you;
   it's your sky, your choice, invisible to them. The spark-gate still governs whether a **gate renders**
   (in-sky + no sparks = a quiet slot, not a dead gate). This keeps "my sky" about *your* attention, not
   a mutual-status handshake. (If MJ wants the sky to require they've sparked you, that's a one-line
   guard on the add action — but it conflates curation with their behavior, so: one-way.)

4. **Acceptable suggestion signals.** *Recommend:* passive, dismissable hints in the directory only,
   sourced from **inbound-spark recency + recent 1:1 chat + a shared active Mission**; capped to ~2–3 at
   a time; **never a push**, never auto-add, never "someone added you." Backend computes evidence, human
   decides. (Explicitly out: "popular in your area," friend-of-friend graph mining, contact-book — the
   last also blocked by the App Store no-Contacts-string constraint.)

5. **Do Circles and My-sky interact?** *Recommend **independent**:* Circles are mutual, visible, shared
   groups (chat + circle-scoped sharing); My-sky is a private personal curation for the flight. Adding a
   Circle does **not** populate your sky, and vice-versa. Optional nicety: a Circle's members may appear
   as *suggested* sky candidates (per Q4), never auto-added. Keeping them independent avoids leaking the
   private sky into a shared group's semantics.

6. **Launch sequencing vs App Store 1.0.4.** *Recommend **ship after 1.0.4**, as an increment on the
   1.0.5 Neighborhood release train.* 1.0.4 is represented to Apple as "1:1 private, no social/UGC," and
   the Neighborhood (N0–N8) already carries the Guideline 1.2 kit + 17+ re-rating requirement. The
   Bounded Neighborhood is purely additive to that build and introduces **no new UGC surface** (sky is
   private curation, not content) — so it rides the same ASC re-representation rather than forcing its
   own. Dark behind `friends_enabled` regardless.

---

## 8. Flags & honest constraints from the SHIPPED Neighborhood

Nothing in this brief contradicts a shipped semantic; these are the seams and frictions to respect.

- **`spark_count > 0` is load-bearing and stays.** The flight's `in_my_sky AND spark_count > 0` builds
  *on top of* the existing gate condition — it does not replace or weaken it. `wormhole_targets` already
  omits neighbors with zero ready grants; the sky filter is an additional narrowing, never a widening.
  (Consequence to design for, not a contradiction: an in-sky neighbor with no shared sparks has no gate —
  hence the "quiet slot" UX. Flagged as open question 3.)
- **`Friendship` is an unordered shared edge; "my sky" must be per-viewer.** You cannot hang a "tier"
  boolean on `Friendship` — one row serves both parties (`pair_key` dedup), so a flag there would be
  shared, but sky membership is directional and private. This is exactly why the recommendation is a
  per-`(viewer, friendship)` row (like `WormholeVisit`), not a column on the edge. Anyone reaching for
  "just add a field to Friendship" will build a two-way public tier by accident.
- **The 150 cap is enforced only on *growth*.** Existing tenants already over an eventual lower number
  are never retroactively trimmed (`accepted_neighbor_count >= MAX_NEIGHBORS` gates the *new* wave/claim
  only). Same principle should hold if the numbers ever change: cap the next add, never force a purge.
- **Circles carry consent + visibility semantics that My-sky must not inherit.** `CircleMembership` *is*
  a consent grant and members see each other; reusing Circles for the sky would silently grant that
  visibility. The brief's core backend recommendation (don't reuse Circle) exists precisely to avoid this
  contradiction.
- **Agents stay backstage — the sky is a human surface.** The inner circle is a *human* curation of
  attention; no agent proposes, populates, or reads another human's sky. Suggestions (Q4) are evidence
  surfaced to *your* human, consistent with the AGENTS.md backstage gate — never an agent action.
- **RLS backstop is real in prod now.** Because `app_user` is non-superuser and the three content tables
  FORCE-RLS bind, any new table genuinely needs its relock migration (and a GUC-scoped policy for the
  FORCE set) — this is no longer a theoretical "someday" like it was pre-PR8. Treated as a hard
  requirement in §4.5 / BN-PR1 / BN-PR6.
- **Web flight renders all wormholes today** (`galaxy-scene.ts` `buildWormholeGates` over the full
  `fetchWormholes()` list — no top-K). The sky filter should reach web too (BN-PR5), but web is lower
  priority and currently correct-but-unbounded; at small scale it's fine until parity lands.

---

*Prepared as a draft brief for MJ's direction-setting. No code changes accompany this document. The
established Neighborhood design doc (`docs/features/friends-neighborhood-design.md`) remains the
implementation source of truth; this brief is the increment that gives the Neighborhood its bounded
shape.*
