# The Neighborhood — NBHD United's Friends Layer (FINAL design)

> **Design center (non-negotiable, from the 2026-02 blueprint `docs/features/shared-intelligence.md`):**
> *isolation + consent + human approval, agents strictly backstage.* From the outside it looks
> like a group of unusually thoughtful, well-informed friends. The agents do the remembering,
> curating, and connecting — invisibly. Humans approve, and humans get the credit.
>
> This document is the single source of truth for implementation. It is the synthesis of three
> rival designs (Guardian's trust spine, Shipwright's plumbing/sequencing, Neighbor's warmth),
> resolved by a three-judge panel. Where a value or mechanism is stated here, it is decided.
>
> **Repo facts that bind every code sketch below (verified):** the repo is **Django 6.0.6**
> (`requirements.txt`; `models.CheckConstraint` takes **`condition=`**, verified at
> `apps/cron/models.py:176-183`). Scheduling is **QStash, never Celery**. The DB is one
> PostgreSQL 16 shared across all tenants; **Django connects as a BYPASSRLS superuser**, so RLS
> is *not* a tenant backstop today — cross-tenant isolation is 100% Python queryset filters until
> the final hardening PR. That single fact is why the whole design funnels every cross-tenant
> read through one audited accessor guarded by a CI chokepoint test.

---

## 1. Executive summary & product narrative

NBHD United already gives every subscriber a private assistant and a personal **Constellation** —
a galaxy of "stars," each an approved lesson learned. **The Neighborhood** is the final pillar: it
lets those private galaxies *touch* — always by invitation, always human-gated, with the
assistants strictly backstage — without a single byte of a user's raw life ever reaching anyone
they didn't personally invite and personally approve.

We ship it under a warm name. In-product the section is **"Neighborhood,"** the people in it are
**"Neighbors,"** connecting is **"waving"** (*"Kenji waved — wave back?"*), a shared lesson is a
**"spark,"** a group is a **"Circle,"** a shared goal is a **"Mission,"** and the game keeps the
celestial language: **wormholes**, **warp**, **return home**. The code flag and app stay
`friends_*` (`friends_enabled`, `apps/friends/`) for consistency with the existing
`finance_enabled` / `fuel_enabled` / `core_enabled` / `site_publishing_enabled` block at
`apps/tenants/models.py`. "Neighborhood" puts the brand promise in the literal name and reads
warmer than "Friends"; the celestial vocabulary sits naturally beside Gravity / Fuel / Core /
Horizons.

### The moments that make someone text a friend "you have to get on this"

Each beat is grounded in a real seam and gated by the human's choice.

1. **"A wormhole appeared."** You're flying your own galaxy
   (`frontend/lib/constellation-game/galaxy-scene.ts`). Out near the rim, a shimmering gate
   materializes with a soft chime and a name-tag: *"Kenji — 3 sparks shared."* A neighbor you
   waved to has shared their first sparks. It wasn't there yesterday. (Built exactly like
   `buildEncounters`/`startEncounter`, `galaxy-scene.ts:1880/1899`.)

2. **The warp.** You steer into the gate. The camera does the `startEncounter` choreography —
   stop-follow, pan, `tweenZoom` — then a white bloom (the `WarpIn` accent glow already shipped at
   `play/page.tsx:96-97`) and you drop into **Kenji's galaxy**, in his hue, showing only the sparks
   he chose to share. A read-only visit to someone else's mind. You **return home** and you're
   instantly back on your own street — because it was a scene switch, not a teardown.

3. **"I want to try this."** You land on Kenji's spark: *"Batch-cook Sundays so weeknight-me never
   negotiates."* You can **bring the spark home** — a souvenir that becomes a *pending* lesson in
   your own galaxy, routed through your normal approve gate. And later, when you ask your assistant
   about weeknight dinners, it already **absorbed** Kenji's spark, backstage, and surfaces it: *"that
   batch-cook idea Kenji shared might help."* You never filed it. Your assistant did, and it waited
   until it mattered.

4. **The huddle.** You and two neighbors set a **Mission** — "walk 10k steps every day in July."
   Nobody is the project manager. The control plane keeps a **status projection**, and once a week
   each of you gets a warm, non-shaming digest: *"🌱 July Steps — you and Aya both showed up 6/7
   days; Kenji's had a quieter week — a wave might help."* Under the hood, each person's own agent
   quietly nudges **its own human** to show up. The crew feels unusually consistent — because
   everyone has a private coach, and no agent ever posts in the group.

5. **"Want to share this?"** Your agent notices you raved about a ramen place and that your
   neighbors were just talking about lunch. It privately asks (never in the group): *"Want to share
   this with your Neighborhood?"* You tap, and you see **exactly** what they'll see — the literal,
   already-scrubbed text, names gone — plus a banner: *"We hide names — but not amounts, dates, or
   company names."* You approve *that specific text*, and it appears **as you**. The human gets the
   credit; the agent stays invisible. That's the whole trick.

### The invite loop (how the neighborhood grows)

Waving is the growth engine. You send a **wave** by `@handle`, or a **wave link / QR** (reusing the
Telegram-linking pattern). If your friend is already a subscriber, it's a one-tap accept. If they're
**not**, the link routes through signup (`ensure_tenant_provisioned`, `apps/tenants/services.py:21`)
and auto-creates the pending wave, so their very first Neighborhood state is *"Kenji is already
waiting to be your neighbor."* Every wave to a non-subscriber is a soft invitation to the whole
product, with a friend on the other side.

### Why this wins

Every rival social product (Nextdoor, Facebook Groups, LINE groups) drowns signal in noise and
treats your data as inventory. The Neighborhood's differentiator is not features — it is
*demonstrable* trust. The user can see exactly what they share (preview-before-share), inspect
everything their assistant absorbed (the transparency ledger), revoke instantly (read-through, no
residue), and leave cleanly. **Visible trustworthiness is the feature.** We ship it dark behind
`friends_enabled`, roll it out per-tenant like every other pillar, and polish the trust surfaces
hardest.

---

## 2. Data model

New Django app: **`apps/friends/`** — `models.py`, `access.py` (the audited accessor), `services.py`,
`envelope.py`, `tasks.py` (QStash scrub + digest), `scrub.py`, `feed.py`, `projection.py`, `views.py`,
`serializers.py`, `urls.py`, `notifications.py`, `apps.py` (whose `ready()` imports `envelope`), and
`test_access_chokepoint.py`. Every table below is a **`public.*`** table and therefore ships with an
**RLS-relock migration** in the same PR (see §2.13). All PKs are `UUIDField` unless a monotonic keyset
is required (`FriendMessage`).

Feature flag: **`Tenant.friends_enabled`** (`BooleanField(default=False)`) added to the existing flag
block at `apps/tenants/models.py` beside `site_publishing_enabled`. There is **no** change to
`journal.Task` — Mission contributions link via the existing `Task.related_ref` JSON field (§2.9).

The data model has four decoupled concerns, each its own model so none is overloaded:
- **Consent** — `Friendship` (the 1:1 edge) + `NeighborProfile` (identity) + `FriendInvite`.
- **The scrub artifact** — `SharedLesson` (one frozen, friend-agnostic, PII-neutralized snapshot per
  source lesson).
- **Visibility** — `LessonShareGrant` (one row per audience edge; friendship XOR circle).
- **Proposal / approval** — `PendingShare` (agent proposes / human approves).

### 2.1 NeighborProfile — who you are to neighbors

```python
# apps/friends/models.py
class NeighborProfile(models.Model):
    """OneToOne on Tenant so it gates on friends_enabled and never bloats the auth User row."""
    tenant       = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="neighbor_profile")
    handle       = models.CharField(max_length=30, unique=True)   # lowercased [a-z0-9_]; the @handle you wave to
    display_name = models.CharField(max_length=80)                 # defaults from User.display_name; overridable
    bio          = models.CharField(max_length=280, blank=True)
    avatar_hue   = models.IntegerField(default=210)                # 0-359; seeds friend-galaxy tint + wormhole-gate color
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "neighbor_profiles"
        indexes = [models.Index(fields=["handle"])]
```

### 2.2 Friendship — the consent atom

```python
class Friendship(models.Model):
    """One row per unordered pair (pair_key). Direction preserved for 'who waved whom' and for the
    asymmetric block state. `accepted` unlocks mutual visibility; `blocked` freezes it and forbids
    re-invite."""
    class Status(models.TextChoices):
        PENDING  = "pending",  "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        REVOKED  = "revoked",  "Revoked"
        BLOCKED  = "blocked",  "Blocked"

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requester     = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="waves_sent")
    addressee     = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="waves_received")
    requested_by  = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")  # audit
    # DB-enforced single edge: f"{min(a,b)}:{max(a,b)}" of the two tenant UUIDs (36+1+36 = 73 chars).
    # Computed on save. This is the ONLY dedup — never service-layer-only (that races to dup edges).
    pair_key      = models.CharField(max_length=73, unique=True, editable=False)
    status        = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    blocked_by    = models.ForeignKey(Tenant, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    requested_via = models.CharField(max_length=16, default="handle")   # handle | link | qr | referral
    invite        = models.ForeignKey("FriendInvite", on_delete=models.SET_NULL, null=True, blank=True)
    invite_note   = models.CharField(max_length=280, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)
    responded_at  = models.DateTimeField(null=True, blank=True)
    revoked_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friendships"
        constraints = [
            models.UniqueConstraint(fields=["pair_key"], name="uq_friendship_pair"),
            models.CheckConstraint(condition=~models.Q(requester=models.F("addressee")),
                                   name="friendship_no_self"),
        ]
        indexes = [
            models.Index(fields=["addressee", "status"]),
            models.Index(fields=["requester", "status"]),
        ]
```

`pair_key` (DB `UniqueConstraint`) makes `A→B` and `B→A` a single row and kills duplicate/reciprocal
waves **at the database**, not in a service method (a service-layer check races two concurrent waves
into two rows). `friendship_no_self` blocks `A↔A`. `blocked` is directional (`blocked_by`) and
*supersedes* `accepted`. `are_neighbors(a, b)` (in `access.py`) is True iff an `accepted` row exists
for the pair **and** no `blocked` row exists either direction.

### 2.3 FriendInvite — bring a neighbor in (incl. non-subscribers)

```python
class FriendInvite(models.Model):
    """Link/QR wave, including to non-subscribers (the referral loop)."""
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inviter       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="friend_invites")
    token         = models.CharField(max_length=64, unique=True)   # secrets.token_urlsafe(32), high-entropy
    circle        = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True)  # optional: into a Circle (PR7)
    prefill_email = models.EmailField(blank=True)
    max_uses      = models.PositiveIntegerField(default=1)
    uses          = models.PositiveIntegerField(default=0)
    expires_at    = models.DateTimeField()
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "friend_invites"
        indexes = [models.Index(fields=["token"])]
```

High-entropy, single-use by default, expiring. Reuses the Telegram/QR linking UX. A non-subscriber who
claims it signs up → `ensure_tenant_provisioned` (`apps/tenants/services.py:21`) → the pending
friendship resolves to `accepted` on claim. Referral attribution via `inviter`.

### 2.4 SharedLesson — the FROZEN, SCRUBBED, FRIEND-AGNOSTIC snapshot

The load-bearing privacy decision: **cross-tenant lesson content is a frozen, PII-neutralized snapshot,
never a live read of `Lesson.text`.** `Lesson.text` is stored RAW (real names, `apps/lessons/models.py`)
and must never itself cross a boundary. **One scrub serves every audience** — the neutralized text
carries no per-recipient information — so `SharedLesson` is `OneToOne(Lesson)` and *friend-agnostic*.
Visibility is a separate concern (`LessonShareGrant`, §2.5).

```python
class SharedLesson(models.Model):
    """A FROZEN, PII-scrubbed snapshot of one Lesson, safe to show ANY neighbor. NO rehydration map is
    ever attached — the recipient must be structurally unable to un-scrub it."""
    class ScrubStatus(models.TextChoices):
        PENDING = "pending", "Scrub pending"
        READY   = "ready",   "Scrubbed & publishable"
        FAILED  = "failed",  "Blocked (fail-closed)"

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_lesson = models.OneToOneField("lessons.Lesson", on_delete=models.CASCADE, related_name="shared_snapshot")
    owner_tenant  = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="shared_lessons")

    # ── FROZEN, NEUTRALIZED payload (every [PERSON_N] → a generic word; NO map) ──
    redacted_text    = models.TextField(blank=True)
    redacted_context = models.TextField(blank=True)
    tags             = ArrayField(models.CharField(max_length=100), default=list)   # allowlisted safe subset
    cluster_label    = models.CharField(max_length=200, blank=True)                 # scrubbed

    # ── Snapshot galaxy geometry (owner's tenant-local PCA coords, COPIED at freeze; coords are not PII) ──
    position_x = models.FloatField(null=True, blank=True)
    position_y = models.FloatField(null=True, blank=True)
    star_stage = models.CharField(max_length=20, default="proto")

    # ── Fail-closed scrub lifecycle ──
    content_hash       = models.CharField(max_length=64, blank=True)   # sha256(source text+context) → drift → re-scrub
    scrub_status       = models.CharField(max_length=10, choices=ScrubStatus.choices, default=ScrubStatus.PENDING)
    scrub_model_version= models.CharField(max_length=40, blank=True)   # NER model version → re-scrub sweep on upgrade
    scrub_error        = models.TextField(blank=True)

    created_at  = models.DateTimeField(auto_now_add=True)
    scrubbed_at = models.DateTimeField(null=True, blank=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "shared_lessons"
        indexes = [
            models.Index(fields=["owner_tenant", "scrub_status"]),
        ]
```

Invariants baked in:
- The snapshot lives in the **owner's** tenant. Recipients **never** read `Lesson`; they read
  `SharedLesson` (and only `scrub_status="ready"`) **through the accessor** (§4.1). They hold no copy.
- **NO rehydration map is ever attached.** `[PERSON_1]` is neutralized to a generic word; the recipient
  is structurally unable to un-scrub.
- `content_hash` mismatch (owner edited the source lesson) marks the snapshot stale → re-enqueue the
  scrub. All grants transparently see the updated neutralized text.
- `scrub_model_version` lets a model upgrade trigger a re-scrub sweep; a row that can't be re-verified is
  re-quarantined (`failed`), not left stale.
- Geometry is copied at freeze and refreshed coords-only by a debounced reconcile task (§8) when the
  owner re-clusters — no new PII crosses.

> **Deferred (post-launch, kill-list #5):** no `embedding`/`VectorField` on `SharedLesson` and no
> cross-tenant semantic search over neighbors' shares in the MVP. Absorption is pull + envelope ranking,
> not vector recall. Note the seam; do not build it.

### 2.5 LessonShareGrant — per-edge visibility (friendship XOR circle)

```python
class LessonShareGrant(models.Model):
    """WHO may see a SharedLesson. Exactly one audience per row: a friendship XOR a circle. Revocation is
    per-grant — flip status=revoked and access dies instantly with zero residue (read-through model)."""
    class Status(models.TextChoices):
        ACTIVE  = "active",  "Active"
        REVOKED = "revoked", "Revoked"

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_lesson = models.ForeignKey(SharedLesson, on_delete=models.CASCADE, related_name="grants")
    friendship    = models.ForeignKey(Friendship, on_delete=models.CASCADE, null=True, blank=True, related_name="lesson_grants")
    circle        = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True, related_name="lesson_grants")
    granted_by    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    status        = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    created_at    = models.DateTimeField(auto_now_add=True)
    revoked_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "lesson_share_grants"
        constraints = [
            # Exactly one audience — Django 6: condition=, NOT check=.
            models.CheckConstraint(
                condition=models.Q(friendship__isnull=False) ^ models.Q(circle__isnull=False),
                name="grant_exactly_one_audience",
            ),
            # Partial-unique per audience → one grant per (lesson, friendship) and per (lesson, circle).
            # Partial (condition=) is what makes this dedup CORRECTLY where a nullable-scope unique would
            # let NULL rows multiply.
            models.UniqueConstraint(fields=["shared_lesson", "friendship"], name="uq_grant_friendship",
                                    condition=models.Q(friendship__isnull=False)),
            models.UniqueConstraint(fields=["shared_lesson", "circle"], name="uq_grant_circle",
                                    condition=models.Q(circle__isnull=False)),
        ]
        indexes = [
            models.Index(fields=["friendship", "status"]),
            models.Index(fields=["circle", "status"]),
        ]
```

The wormhole/warp query and the agent's absorb pull both resolve to: *`SharedLesson` rows where an
`active` `LessonShareGrant` exists for an accepted friendship (or a circle both parties are members of)
with the viewer, **AND** `scrub_status="ready"`.* This per-edge model replaces any single broadcast
"visible-to-all-neighbors" row — a nullable-scope design fails to dedup NULL-scope rows and can't revoke
one audience without the others.

### 2.6 PendingShare — agent proposes → human approves

Mirrors `apps/journal/PendingExtraction` (`apps/journal/models.py:210`) and reuses the
`send_lesson_approval_buttons` + `handle_lesson_callback` machinery verbatim.

```python
class PendingShare(models.Model):
    class Status(models.TextChoices):
        PENDING  = "pending",  "Pending"
        APPROVED = "approved", "Approved"
        EDITED   = "edited",   "Edited & approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED  = "expired",  "Expired"
        BLOCKED  = "blocked",  "Blocked (scrub failed)"

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant         = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pending_shares")  # author whose HUMAN approves
    source_lesson  = models.ForeignKey("lessons.Lesson", on_delete=models.CASCADE, related_name="pending_shares")  # MVP: EXISTING star only
    proposed_by    = models.CharField(max_length=8, default="agent")   # agent | user
    source_context = models.TextField(blank=True)                      # agent's private "why" — never egressed
    preview_text   = models.TextField(blank=True)                      # convenience mirror of SharedLesson.redacted_text at approve time
    final_text     = models.TextField(blank=True)                      # what the human actually approved (post-edit)
    target_friendship = models.ForeignKey(Friendship, on_delete=models.CASCADE, null=True, blank=True)
    target_circle     = models.ForeignKey("Circle",   on_delete=models.CASCADE, null=True, blank=True)  # PR7

    status         = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # Channel-button idempotency (reuse the lesson-callback pattern)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    line_message_token  = models.CharField(max_length=120, blank=True)
    notified_at    = models.DateTimeField(null=True, blank=True)       # APNs one-push claim
    created_at     = models.DateTimeField(auto_now_add=True)
    resolved_at    = models.DateTimeField(null=True, blank=True)
    expires_at     = models.DateTimeField()                            # +7d, like PendingExtraction

    class Meta:
        db_table = "pending_shares"
        indexes = [models.Index(fields=["tenant", "status"]),
                   models.Index(fields=["target_friendship", "status"])]
```

The agent writes `proposed_by="agent", status="pending"` and **can never flip it to approved**. A human
approve triggers the scrub → `SharedLesson` (re)freeze → `LessonShareGrant(active)`.

> **Deferred (kill-list #6):** the "propose-share-**new**" path (agent proposes a brand-new,
> not-yet-a-Lesson insight via a free `suggested_text`) is **not** in the MVP. `source_lesson` is
> required — the agent proposes an **existing** star only. Keep `final_text` as the seam for a future
> free-text variant; do not wire the endpoint or tool yet.

### 2.7 FriendThread / FriendThreadMembership / FriendMessage — chat

Cross-tenant chat cannot reuse `router.ChatThread`/`AppChatMessage` — their `tenant` FK +
`is_main`-per-tenant partial-unique (`apps/router/models.py:504`) actively forbid a cross-tenant thread.
New control-plane tables:

```python
class FriendThread(models.Model):
    class Kind(models.TextChoices):
        DIRECT = "direct", "1:1"
        CIRCLE = "circle", "Circle"    # PR7

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind          = models.CharField(max_length=8, choices=Kind.choices, default=Kind.DIRECT)
    friendship    = models.ForeignKey(Friendship, on_delete=models.CASCADE, null=True, blank=True, related_name="thread")  # direct
    circle        = models.ForeignKey("Circle", on_delete=models.CASCADE, null=True, blank=True, related_name="threads")   # circle (PR7)
    title         = models.CharField(max_length=160, blank=True)
    created_by    = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="threads_started")
    created_at    = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "friend_threads"
        constraints = [
            models.UniqueConstraint(fields=["friendship"], name="uq_direct_thread",
                                    condition=models.Q(friendship__isnull=False)),  # one direct thread per edge
        ]


class FriendThreadMembership(models.Model):
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread        = models.ForeignKey(FriendThread, on_delete=models.CASCADE, related_name="memberships")
    tenant        = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="thread_memberships")
    user          = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role          = models.CharField(max_length=8, default="member")     # member | admin
    muted         = models.BooleanField(default=False)                   # mute APNs nudges
    agent_absorb_enabled = models.BooleanField(default=True)             # mute MY agent's absorption of THIS thread — without leaving
    last_read_seq    = models.BigIntegerField(default=0)                 # unread counts
    last_absorbed_seq= models.BigIntegerField(default=0)                 # idempotent agent-absorb cursor → never re-absorb
    joined_at     = models.DateTimeField(auto_now_add=True)
    left_at       = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_thread_memberships"
        constraints = [models.UniqueConstraint(fields=["thread", "tenant"], name="uq_thread_member")]
        indexes = [models.Index(fields=["tenant", "left_at"])]


class FriendMessage(models.Model):
    """Plain human-authored text. NOT agent-scrubbed — a human chose these words for other humans
    (consent by typing). It carries no per-tenant [PERSON_N] placeholders. Absorption into each member's
    agent applies THAT tenant's own egress redaction (§4.6)."""
    seq           = models.BigAutoField(primary_key=True)                # monotonic → cheap keyset tiebreaker
    public_id     = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)  # client-facing id
    thread        = models.ForeignKey(FriendThread, on_delete=models.CASCADE, related_name="messages")
    sender_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    sender_user   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    client_msg_id = models.CharField(max_length=64)                      # offline-outbox idempotency
    text          = models.TextField()
    notified_at   = models.DateTimeField(null=True, blank=True)          # coarse one-push claim (isnull→now), 1:1 case
    created_at    = models.DateTimeField(auto_now_add=True, db_index=True)
    edited_at     = models.DateTimeField(null=True, blank=True)
    deleted_at    = models.DateTimeField(null=True, blank=True)          # self-delete / moderation

    class Meta:
        db_table = "friend_messages"
        constraints = [models.UniqueConstraint(fields=["sender_tenant", "client_msg_id"], name="uq_friend_msg_idem")]
        indexes = [models.Index(fields=["thread", "created_at", "seq"])]  # keyset feed
```

Keyset cursor over `(created_at, seq)` — forks `apps/router/chat_history.py:246` `build_since_page`
(same opaque base64 cursor, `encode/decode_cursor`), but selects `FriendMessage` for threads the viewer
is a member of instead of filtering by one tenant. `BigAutoField` gives a monotonic tiebreaker (UUIDv4
is not monotonic).

### 2.8 AbsorbedItem — transparency + purge ledger (NOT a second knowledge store)

Satisfies blueprint non-negotiables #3 (see everything the agent absorbed) and #4 (leave = purge or
keep, user's choice). **Strictly a transparency/purge audit projection over the real corpus** — the
shares and threads *are* the corpus. This is a pointer ledger, not a copy of the knowledge; the agent's
awareness is re-derived from the live accessor-filtered rows each envelope render.

```python
class AbsorbedItem(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant      = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="absorbed_items")  # the absorbER
    source_kind = models.CharField(max_length=16, choices=[("shared_lesson", "Shared spark"), ("friend_message", "Chat")])
    source_id   = models.UUIDField()                # SharedLesson.id or FriendMessage.public_id — the REAL row
    from_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    circle      = models.ForeignKey("Circle", on_delete=models.SET_NULL, null=True, blank=True)
    label       = models.CharField(max_length=200, blank=True)   # display-only denormalized title (NOT the knowledge)
    absorbed_at = models.DateTimeField(auto_now_add=True)
    purged_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_absorbed_items"
        indexes = [models.Index(fields=["tenant", "purged_at"]), models.Index(fields=["tenant", "from_tenant"])]
```

Purging an item sets `purged_at` (a tombstone) → the envelope render excludes it and the agent stops
surfacing it. Because the knowledge itself lives only in the source rows, purge + source-revocation
together give complete, honest control. There is **no** `summary`/knowledge-copy field — that would make
it a parallel store, which is forbidden.

### 2.9 SharedGoal + SharedGoalMembership + SharedGoalUpdate — Missions

Product name **Mission**. A **new cross-tenant** model — *not* a stretched `Goal.parent_goal` (RLS + the
same-tenant `validate()` at `apps/.../lifecycle_serializers.py:73/113` forbid cross-tenant FKs). Each
member's contribution stays as their own **local `journal.Task` rows**, linked by the existing
`Task.related_ref` JSON field — **zero schema change to `journal.Task`.**

```python
class SharedGoal(models.Model):   # product name: Mission
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"; ACHIEVED = "achieved", "Achieved"
        ABANDONED = "abandoned", "Abandoned"; EXPIRED = "expired", "Expired"

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title        = models.CharField(max_length=200)
    description  = models.TextField(blank=True)
    pillar       = models.CharField(max_length=20, blank=True)
    circle       = models.ForeignKey("Circle", on_delete=models.SET_NULL, null=True, blank=True, related_name="missions")
    friendship   = models.ForeignKey(Friendship, on_delete=models.SET_NULL, null=True, blank=True)   # 1:1 mission
    created_by   = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="missions_created")
    target       = models.JSONField(default=dict)   # {metric, unit, cadence:"daily", value:10000}
    target_date  = models.DateField(null=True, blank=True)
    status       = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    # Object-level multi-writer safety = Fuel's optimistic concurrency (Workout.version pattern).
    version         = models.PositiveIntegerField(default=0)
    edit_lock_until = models.DateTimeField(null=True, blank=True)
    edit_lock_owner = models.CharField(max_length=64, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    achieved_at  = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "shared_goals"


class SharedGoalMembership(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_goal  = models.ForeignKey(SharedGoal, on_delete=models.CASCADE, related_name="memberships")
    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="mission_memberships")
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role         = models.CharField(max_length=8, default="member")   # owner | member
    status       = models.CharField(max_length=8, default="active")   # invited | active | left
    commitment   = models.CharField(max_length=200, blank=True)        # "what I'll do"
    joined_at    = models.DateTimeField(auto_now_add=True)
    left_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "shared_goal_memberships"
        constraints = [models.UniqueConstraint(fields=["shared_goal", "tenant"], name="uq_mission_member")]
        indexes = [models.Index(fields=["tenant", "left_at"])]


class SharedGoalUpdate(models.Model):
    """Append-only activity log. THE single stream that feeds the status projection, the digest, AND the
    envelope — so crew progress reads from control-plane data only, never a cross-tenant Task scan in a
    request path."""
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shared_goal  = models.ForeignKey(SharedGoal, on_delete=models.CASCADE, related_name="updates")
    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    user         = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    kind         = models.CharField(max_length=16, choices=[
        ("joined","Joined"), ("task_added","Task added"), ("task_completed","Task completed"),
        ("milestone","Milestone"), ("note","Note"), ("progress","Progress")])
    text         = models.TextField(blank=True)
    payload      = models.JSONField(default=dict)
    created_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "shared_goal_updates"
```

**Local-task linkage (no migration on `journal.Task`):**
`Task.related_ref = {"pillar": "friends", "object_type": "shared_goal", "object_id": "<uuid>"}`. When a
member's agent/human completes a linked Task, the completion appends a `SharedGoalUpdate(task_completed)`.
The projection folds the `SharedGoalUpdate` stream (control-plane) into crew progress; each member's
`related_ref`-linked Task is what their *own* agent and human manage in their own tenant.

### 2.10 Agent-proposed Mission tasks & moderation

```python
class PendingGoalAction(models.Model):
    """Agent proposes a Mission task for ITS OWN human (mirror PendingTaskAction). Human-gated."""
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="pending_goal_actions")
    shared_goal  = models.ForeignKey(SharedGoal, on_delete=models.CASCADE)
    kind         = models.CharField(max_length=16, default="add_task")   # add_task | complete_task | note
    suggested    = models.JSONField(default=dict)                        # {title, description, due_date}
    status       = models.CharField(max_length=10, default="pending")    # pending | approved | rejected | expired
    task         = models.ForeignKey("journal.Task", on_delete=models.SET_NULL, null=True, blank=True)  # minted on approve
    telegram_message_id = models.BigIntegerField(null=True, blank=True)
    line_message_token  = models.CharField(max_length=120, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    resolved_at  = models.DateTimeField(null=True, blank=True)
    expires_at   = models.DateTimeField()

    class Meta:
        db_table = "pending_goal_actions"


class ContentReport(models.Model):
    """MVP moderation: report + block + owner-unshare. No global queue at launch scale."""
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reporter_tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")
    reporter_user   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    target_kind   = models.CharField(max_length=16, choices=[("shared_lesson","Shared spark"), ("friend_message","Chat")])
    shared_lesson = models.ForeignKey(SharedLesson, on_delete=models.CASCADE, null=True, blank=True)
    friend_message= models.ForeignKey(FriendMessage, on_delete=models.CASCADE, null=True, blank=True)
    reason        = models.CharField(max_length=280)
    status        = models.CharField(max_length=12, default="open")   # open | hidden | dismissed
    created_at    = models.DateTimeField(auto_now_add=True)
    resolved_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "content_reports"
```

### 2.11 Circle + CircleMembership (PR7)

```python
class Circle(models.Model):
    """A named set of accepted neighbors (blueprint's Group). Built ON edges — you must be a member's
    friend, or accept an invite, to join."""
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name         = models.CharField(max_length=120)
    description  = models.TextField(blank=True)
    hue          = models.PositiveSmallIntegerField(default=210)   # galaxy tint
    created_by   = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="circles_created")
    invite_code  = models.CharField(max_length=64, unique=True)    # link/QR (reuse linking pattern)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "friend_circles"


class CircleMembership(models.Model):
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    circle       = models.ForeignKey(Circle, on_delete=models.CASCADE, related_name="memberships")
    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="circle_memberships")
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    role         = models.CharField(max_length=8, default="member")      # member | admin
    share_preferences = models.JSONField(default=dict)                   # categories the agent MAY suggest sharing
    agent_absorb_enabled = models.BooleanField(default=True)
    muted        = models.BooleanField(default=False)
    status       = models.CharField(max_length=8, default="active")      # active | left | removed
    joined_at    = models.DateTimeField(auto_now_add=True)
    left_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_circle_memberships"
        constraints = [models.UniqueConstraint(fields=["circle", "tenant"], name="uq_circle_tenant")]
        indexes = [models.Index(fields=["tenant", "status"])]
```

Cap: `MAX_CIRCLES_PER_TENANT = 8` (blueprint's "cap to prevent agent noise"). Membership itself is the
consent grant inside a Circle.

### 2.12 WormholeVisit — the "new since last visit" watermark (NOT a render-row table)

The wormhole is a **derived query** (§8), never a materialized render table. The *only* persisted piece
is a tiny per-viewer watermark so we can show "N new since last visit":

```python
class WormholeVisit(models.Model):
    """One row per (viewer, friendship). Watermark ONLY — placement is deterministic client-side from a
    stable hash of friendship_id; shared-count and 'new' are derived from grant timestamps."""
    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    viewer_tenant   = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="wormhole_visits")
    friendship      = models.ForeignKey(Friendship, on_delete=models.CASCADE, related_name="+")
    last_visited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "friend_wormhole_visits"
        constraints = [models.UniqueConstraint(fields=["viewer_tenant", "friendship"], name="uq_wormhole_visit")]
```

"N new since last visit" = count of `active`+`ready` grants to the viewer for that friendship whose
`created_at > last_visited_at`. Placement seed = a stable hash of `friendship_id`, computed client-side.

### 2.13 Relock migrations (every new public table)

Every table above is `public.*`. Django is BYPASSRLS, so the anon Supabase Data API — not tenant
isolation — is what the relock protects. Each PR that adds tables ends with a relock migration copied
from `apps/tenants/migrations/0083_relock_after_ios_chat.py`: a `RunSQL` `DO $$ … ENABLE ROW LEVEL
SECURITY … WHERE rowsecurity = false … $$` block with **no policy** (the lockdown test
`test_no_policies_on_public_schema` forbids policies on `public.*`), depending on both the
`friends.000X` migration that created the tables **and** the latest `tenants` migration so it runs last
in topo order (the recurring `feedback_rls_relock_topo_shift` hazard). CI gate:
`apps.tenants.test_public_schema_lockdown.test_rls_enabled_on_owned_public_tables`. Relocks land per-PR:
`tenants/00NN_relock_after_friends_edge`, `…_after_shared_lessons`, `…_after_friend_chat`,
`…_after_missions`, `…_after_circles`.

---

## 3. API surface

Two planes, **one rule**: every cross-tenant read routes through the audited accessor `apps/friends/access.py`
(§4.1), and **addressing is by random `friendship_id` / `thread_id` / `circle_id`, never a
client-supplied `tenant_id`** (IDOR dead by construction — §4.5).

- **Console DRF** — mounted at `/api/v1/friends/` via new `apps/friends/urls.py` (added to `config/urls.py`
  beside `chat_urls`). Auth = `IsAuthenticated` (JWT/PAT) → `request.user.tenant`, **plus** an explicit
  accessor call before any cross-tenant read.
- **Runtime (agent-facing)** — extends `apps/integrations/runtime_views.py` + `urls.py`, mounted
  `runtime/<tenant_id>/…`. Auth = `AllowAny` + `_internal_auth_or_401(request, tenant_id)` (per-tenant
  internal key, constant-time, `apps/integrations/internal_auth.py:164-194`), **always scoped to the
  calling tenant**. A foreign `tenant_id` is **never** placed on any endpoint; cross-tenant data is
  *brokered* by Django after the accessor validates the edge, and only frozen scrubbed rows are returned.

### 3.1 Console — neighbors, profile & invites

| Method · Path | Purpose | Auth |
|---|---|---|
| `GET /api/v1/friends/` | Accepted neighbors + pending in/out (display name, handle, hue, shared-spark count). | JWT; self-scoped |
| `POST /api/v1/friends/waves/` | Send a wave (by `@handle` or invite). Rate-limited. | JWT |
| `POST /api/v1/friends/waves/<friendship_id>/accept` · `/decline` · `/block` | Respond to an incoming wave. | JWT + **addressee** check |
| `DELETE /api/v1/friends/<friendship_id>/` | Unfriend (revoke edge; triggers absorbed-item purge offer). | JWT + party check |
| `GET/PATCH /api/v1/friends/profile/` | Your `@handle` / bio / hue. | JWT; own tenant |
| `POST /api/v1/friends/invites/` · `GET /api/v1/friends/invites/<token>/accept/` | Create wave link/QR; accept (routes non-subs to signup → `ensure_tenant_provisioned`). | JWT create; accept `AllowAny`→auth |

### 3.2 Console — sharing & the human gate

| Method · Path | Purpose | Auth |
|---|---|---|
| `POST /api/v1/lessons/<id>/share/` `{friendship_id\|circle_id}` | **Human-initiated** share intent → creates `PendingShare(proposed_by="user")`, ensures `SharedLesson`, enqueues the scrub. **The `LessonShareGrant` is created only at approve-after-preview (§4.2/§4.4— "no preview → no grant" binds every path, including self-initiated).** Sibling to `approve` in `apps/lessons/views.py`, delegating to `apps/friends/services.share_lesson()`. | JWT; lesson owner |
| `GET /api/v1/friends/shares/preview/?lesson_id=&friendship_id=` | **Preview-before-share** — the literal `SharedLesson.redacted_text` the neighbor will see; `202` while scrubbing, `409` if `failed`. | JWT; lesson owner |
| `GET /api/v1/friends/shares/pending/` | My approval queue (agent + user proposals). | JWT; owner |
| `POST /api/v1/friends/shares/<id>/approve` `{final_text?}` · `/reject` | Approve/edit/reject an agent proposal → scrub→freeze→publish (re-scrub on edit, fail-closed). Idempotent. | JWT; owner |
| `DELETE /api/v1/lessons/<id>/share/<grant_id>/` | Revoke one share (grant → revoked; purge downstream `AbsorbedItem`s). | JWT; lesson owner |
| `POST /api/v1/friends/shares/<shared_lesson_id>/adopt/` | **Souvenir** → a PENDING `Lesson` in *my* tenant (see §8). | JWT; must be neighbor of owner |

### 3.3 Console — wormholes / warp (read-only)

| Method · Path | Purpose | Auth |
|---|---|---|
| `GET /api/v1/friends/wormholes/` | Warp targets: one per accepted neighbor with ≥1 `active`+`ready` grant to me `[{friendship_id, display_name, hue, spark_count, new_since_last_visit}]`. | JWT + accessor |
| `GET /api/v1/friends/<friendship_id>/galaxy/` | Neighbor's **shared** constellation as `GalaxyData` from `SharedLesson` snapshots (ready+active only, id-namespaced). **Read-only.** | JWT + `assert_neighbors` |
| `POST /api/v1/friends/<friendship_id>/visited/` | Advance the `WormholeVisit` watermark. | JWT + party check |

### 3.4 Console — chat, Missions & transparency

| Method · Path | Purpose | Auth |
|---|---|---|
| `GET/POST /api/v1/friends/threads/` | List / open a direct thread. | JWT + accessor |
| `GET /api/v1/friends/threads/<id>/messages/?since=<cursor>&limit=` | Keyset feed (forks `build_since_page`). | JWT + participant |
| `POST /api/v1/friends/threads/<id>/messages/` | Send (idempotent `client_msg_id`) → APNs nudge. Gates target lifecycle (§10). | JWT + participant |
| `POST /api/v1/friends/threads/<id>/read/` | Advance `last_read_seq`. | JWT + participant |
| `PATCH /api/v1/friends/threads/<id>/membership/` | Toggle `agent_absorb_enabled` / `muted`. | JWT + participant |
| `GET/POST /api/v1/friends/missions/` · `GET /<id>/` (incl. status projection) · `POST /<id>/join` · `/leave` | Mission CRUD + membership; tasks via existing `journal` Task endpoints w/ `related_ref`. | JWT + member |
| `GET /api/v1/friends/absorbed/` · `POST /api/v1/friends/absorbed/<id>/purge/` | Transparency ledger + purge. | JWT; own tenant |
| `POST /api/v1/friends/report/` | Moderation. | JWT |
| `POST /api/v1/push/register/` | **Reused verbatim** for Friends push (`apps/router/push_views.py:85`). | JWT |

### 3.5 Runtime — agent-facing (per-tenant key; Django brokers all cross-tenant)

All follow `RuntimeLessonCreateView` (`runtime_views.py:1625`): `AllowAny`, `_internal_auth_or_401`
first, then load the calling tenant; any cross-tenant reference is validated against an accepted
`Friendship` for that tenant.

| Method · Path | Purpose |
|---|---|
| `GET runtime/<tid>/neighborhood/context/?since=<cursor>` | The **absorb read side**: accessor-approved scrubbed sparks shared *to me* (one-liners + who), active Missions + status, recent `absorb_enabled` chat highlights. |
| `POST runtime/<tid>/lessons/<id>/propose-share/` `{target_friendship_id\|target_circle_id, source_context}` | Agent proposes sharing an **existing** star → `PendingShare(proposed_by="agent", pending)`. **Never publishes.** |
| `GET runtime/<tid>/missions/` | Status projection for the tid's Missions (to nudge its own human). |
| `POST runtime/<tid>/missions/<id>/propose-task/` `{title, description, due_date}` | Agent proposes a Mission task **for its own human** → `PendingGoalAction`. |
| `POST runtime/<tid>/neighborhood/absorb-ack/` | Advance `last_absorbed_seq` (idempotent). |

**Critical posture** (verified against `internal_auth.py` + Phase 1d): no runtime endpoint accepts a
foreign `tenant_id`. Container A calls only `runtime/<A>/…`; if a response must include data owned by B,
Django computes it server-side after checking the `Friendship`/membership edge, returns only frozen
scrubbed rows, and sets `set_rls_context(service_role=True)` scoped to A. Container A can never
authenticate as tenant B (per-tenant key isolation, Phase 1d). **Deferred (kill-list #5, #6):** no
`propose-share-new` endpoint and no `neighborhood/friends-shared/?q=` semantic-search endpoint in MVP.

---

## 4. Consent & privacy architecture

This is the load-bearing section. Everything cross-tenant funnels through one accessor and one
fail-closed egress pipeline.

### 4.1 The single audited accessor — `apps/friends/access.py`

Every cross-tenant read in the entire feature — console, runtime, wormhole, chat, Missions — routes
through these functions. Nothing hand-rolls a cross-tenant query. A CI architectural test (§4.7) fails
the build if anything does.

```python
# apps/friends/access.py
def are_neighbors(a: Tenant, b: Tenant) -> bool:
    """True iff an accepted Friendship exists for the pair AND no blocked row either direction."""

def assert_neighbors(viewer_tenant, friendship_id) -> Friendship:
    """Return the accepted Friendship the viewer is a party to, or raise PermissionDenied.
    Direct edge ONLY — never transitive (no friend-of-friend). Blocked edges deny."""

def shared_star_qs(viewer_tenant, owner_tenant):
    """SharedLesson rows of owner_tenant visible to viewer_tenant. Visibility = an ACTIVE
    LessonShareGrant exists whose friendship is the accepted edge between the two, OR whose circle
    is one both are members of — AND scrub_status='ready'. Returns EMPTY if no edge — never raises
    into a data leak. This is the ONLY code that omits a tenant= filter."""

def assert_can_write(viewer_tenant, target_tenant, *, allow_hibernated=True):
    """Gate cross-tenant writes (chat delivery, mission nudges). Requires an accepted (non-blocked)
    edge or shared Circle. target SUSPENDED -> raise (store-only, no container touch);
    target HIBERNATED -> allowed, caller decides whether to wake."""
```

Design rules:
- **Addressing is by random `friendship_id` / `thread_id` / `circle_id`, never a raw `tenant_id` from
  the client.** Those ids exist only if the relationship exists — and the accessor *still* re-verifies
  the requester is a party. IDOR defeated by construction.
- The accessor is the *only* code that omits a `tenant=` filter. Everywhere else, dropping `tenant=` is a
  leak; here it is centralized, audited, and tested.
- Reads touch **only** `SharedLesson`/`FriendMessage`/`SharedGoal*` (ready/non-revoked) — never `Lesson`,
  `LessonConnection`, `StarJournalEntry`, `Document`, or `journal`.

### 4.2 The approve → scrub → freeze → publish pipeline

The single **fail-closed egress class** for content leaving a tenant, distinct from the existing
per-request LLM-egress redaction. Concretely the scrub *produces* the preview, so the human's approval
always follows a completed scrub:

```
PROPOSE ─────────────────────────────────────────────────────────────────────
  Agent: runtime/<tid>/lessons/<id>/propose-share/  → PendingShare(pending)
  Human: POST /lessons/<id>/share/                  → (human-initiated == consent)
        └──────────────────────────┬──────────────────────────┘
                                    ▼
SCRUB  (async QStash task `scrub_shared_lesson_task`, on a WARM worker — NEVER inline:
        the 554MB DeBERTa cold-load must not sit in a request; it blew the iOS 60s timeout at 8–114s)
  • fresh RedactionSession over RAW lesson.text using the OWNER's pii_entity_map + seeded pii_denylist
  • copilot._scrub_placeholders → every [PERSON_N] → a generic word ("someone")  → NO map persisted
  • VERIFY the DeBERTa NER path actually ran (see §4.3) — not merely "no exception"
      ├─ NER path did NOT run / model unavailable → scrub_status="failed", scrub_error, notify owner → STOP
      └─ ok → write SharedLesson(redacted_text, positions, content_hash, scrub_model_version,
                                 scrub_status="ready", scrubbed_at=now)
                                    ▼
PREVIEW  GET /friends/shares/preview/ → the LITERAL redacted_text ("here is exactly what Kenji will see")
                                    ▼
APPROVE / EDIT  (human)  — edit → re-scrub the edit, fail-closed
                                    ▼
FREEZE + PUBLISH  create LessonShareGrant(status="active") for the target friendship/circle
                  → the spark appears in that neighbor's wormhole + the agent's absorb pull
```

Reuses `apps/pii/redactor.py:115` `RedactionSession`, `apps/pii/arbiter.py:107` denylist seeding,
`apps/lessons/copilot.py:515` `_scrub_placeholders`, and the redact-then-lock-write structure of
`apps/orchestrator/memory_sync.py:49` — **minus the map persistence**. The shared artifact is frozen text
with no map; the recipient is structurally unable to un-scrub it. All embedding/LLM calls in this path
are `is_system=True` (never eat the user's quota).

### 4.3 Fail-closed rules (non-negotiable) — and the silent-fallback trap

`get_pii_pipeline()` **swallows model-load errors** and the redactor then silently falls back to
**Presidio regex only** (`apps/pii/redactor.py:596-601`). Presidio here has **no PERSON pattern
recognizer**, so real names pass through. Therefore a `try/except` around the scrub is *insufficient* —
the fallback does not raise; it succeeds while leaking. **The scrub must verify the DeBERTa NER path
actually executed** (e.g. assert the loaded pipeline is the NER model and that the entity pass ran), and
treat "fell back to Presidio-only" exactly like a hard failure:

- If the NER path did not run (raised **or** silently degraded), set `scrub_status="failed"`,
  `scrub_error`, notify the owner "couldn't prepare this share safely — try again," and **never** create
  the grant. `failed` snapshots are never visible.
- Scrub runs on a **warm worker via QStash**, never in the approve HTTP request.
- `scrub_model_version` is recorded so a model upgrade can trigger a re-scrub sweep; a previously-`ready`
  row that can't be re-verified is re-quarantined, not left stale.

### 4.4 Preview-before-share (first-class trust surface) + residuals banner

The human **always** sees the frozen, scrubbed text before it publishes — not a description, the literal
bytes (`GET …/preview/`). Because identity-scrub deliberately keeps amounts / dates / employer names /
nicknames (`apps/pii/config.py:77-82` drops AMOUNT/CURRENCY/DATE/COMPANY/JOBTITLE), the preview is the
user's real defense against oversharing those. The card:
- shows the exact `redacted_text`,
- names the audience ("This goes to your 4 Nishi-ku neighbors" or "Kenji"),
- carries the **residuals banner**: *"We hide names — but not amounts, dates, or company names."*,
- offers **edit-then-share** (re-scrubbed, fail-closed).

No preview → no grant.

### 4.5 Threat model — the negative-test matrix

Every row is a required negative test (behavioral or the AST chokepoint). This table *is* the security
test plan.

| Attack | Vector | Mitigation |
|---|---|---|
| **IDOR on friend ids** | Client swaps `friendship_id`/`tenant_id` to read a stranger's galaxy/chat. | Address by random `friendship_id`/`thread_id`; accessor re-verifies party + `accepted` every request. Runtime never accepts a foreign `tenant_id`. |
| **Unshared-star / geometry leakage** | Friend galaxy leaks unshared stars, edges to unshared stars, or coords. | Wormhole endpoint reads **only** `SharedLesson` (ready+granted); edges recomputed over the shared subset only; positions snapshotted per shared row. `Lesson`/`LessonConnection`/`StarJournalEntry` untouched by any friend path. |
| **PII placeholder cross-contamination** | A's `[PERSON_1]` rehydrated as B's `[PERSON_1]`. | Frozen `redacted_text` is **neutralized** (placeholders → generic words) with the owner's session; **no map** attached; recipient never calls `rehydrate_for_tenant`. |
| **Fail-open NER leak** | NER down → Presidio-only silently passes real names cross-tenant. | Scrub **verifies the NER path ran** and fails closed (`scrub_status="failed"`, never visible) — not a bare try/except (§4.3). |
| **Revoked-share residue** | Neighbor still sees a revoked spark. | Read-through model: neighbor holds no copy. Grant → `revoked` → accessor denies immediately. `AbsorbedItem` for that source purged. |
| **Group-leave residue** | Ex-member still absorbs Circle knowledge. | `CircleMembership.status=left` → accessor denies; leave triggers Circle-scoped `AbsorbedItem` purge (default purge; keep = user's choice). |
| **Suspended-tenant write** | Write into a lapsed tenant's container / act as one. | `assert_can_write` blocks SUSPENDED targets (store-only); suspended tenants can't act (no container). |
| **Transitive leak** | Friend-of-friend reads your shares. | Accessor checks **direct** edge only; never joins transitively. |
| **Invite brute-force / spam** | Guess tokens; share/invite bombing. | `secrets.token_urlsafe(32)`, single-use, expiring; rate limits on wave/share; neighbor & Circle caps. |
| **Self-friend / duplicate edge** | Corrupt the edge table. | `friendship_no_self` `CheckConstraint(condition=)` + `pair_key` `UniqueConstraint` (DB-level, not service-level). |
| **BYPASSRLS blast radius** | One missing accessor call = cross-tenant leak with no DB net. | Single accessor chokepoint + architectural CI test (§4.7) + least-privilege `FORCE RLS` role as the final hardening PR (§12, PR8). |
| **Replay of frozen ids** | Namespaced star id replayed against owner-scoped `/lessons/<id>/…`. | Friend-galaxy star ids namespaced `f:<friendship_id>:<shared_lesson_id>`; owner endpoints reject non-PK-shaped ids; accessor re-checks party. |

### 4.6 Chat is raw human text — and that is correct

A chat message is authored by a human *for* a specific human, who consented by opening the thread.
Running the 554MB NER scrub on every turn is infeasible (cold-load rule) and wrong (it would mangle "Hey
Kenji" into "Hey someone"). So `FriendMessage.text` stores **raw** text and renders verbatim human↔human.
The sensitive boundary is not human→human; it is **agent absorption** (§5.4): when a friend's message
reaches *my* assistant, it enters my normal inbound redaction chokepoint and is redacted **fresh in my
session** (my own `pii_entity_map`) before my LLM sees it — no cross-tenant placeholder ever composes,
because chat carries none. This cleanly separates "what a human chose to say to a friend" (raw,
consented) from "what an agent proposes to broadcast" (scrubbed, gated).

### 4.7 Share-never enforcement + the architectural chokepoint test

Three layers of defense-in-depth against sharing what should never be shared:
1. **AGENTS.md gate (agent, §5.2):** the agent is imperatively forbidden to *propose* health, finances/
   amounts, family/personal matters, private-conversation content, or anything not clearly discussed as
   shareable. Because agents can only *propose* and every proposal shows a human preview, this is
   defense-in-depth, not the sole guard.
2. **Backend pillar-block (mechanical, share-never list):** `propose-share` and `POST /lessons/<id>/share/`
   **reject** lessons whose `pillar ∈ {gravity, core}` (finance / mindfulness-health) by default — the
   same higher bar as the deferred Gravity "Surface B." MJ can opt a pillar in per §13. This is the
   mechanically-enforced share-never list, independent of the LLM.
3. **Preview + human approval (§4.4):** the mandatory backstop for the residuals the scrub deliberately
   leaves.

**The architectural CI test — `apps/friends/test_access_chokepoint.py` (non-negotiable, unique to this
design):** an AST/grep guard that **fails the build** if any module under `apps/friends/` or the friends
runtime views issues a `SharedLesson` / `FriendMessage` / `SharedGoal` / `LessonShareGrant` query without
going through `apps/friends/access.py`, or references `Lesson.objects` in a friend endpoint. Plus
behavioral tests for every §4.5 row: non-friend read returns empty, revoked hidden, failed-scrub hidden,
IDOR id-swap denied, transitive denied.

---

## 5. Agent integration

Agents stay **backstage**: they propose privately and absorb quietly; they never post to a neighbor,
chat, Circle, or Mission. All neighbor-visible output comes from the human. Three seams: envelope
injection (awareness/absorb), the AGENTS.md gate (behavior), OpenClaw plugin tools (callbacks).

### 5.1 Envelope sections (`apps/friends/envelope.py`)

Registered exactly like `apps/lessons/envelope.py:18` via `apps/orchestrator/envelope_registry.py`
`register_section`, auto-wired from `apps/friends/apps.py::ready()`, gated on `friends_enabled`, kept
tight for the ~12KB USER.md budget with silent tail truncation:

```python
@register_section(key="neighborhood", heading="## Neighborhood — neighbors & sparks",
    enabled=lambda t: t.friends_enabled,
    refresh_on=(Friendship, LessonShareGrant, FriendMessage, AbsorbedItem, CircleMembership), order=63)
def render_neighborhood(tenant) -> str:
    # TIGHT: accepted neighbors (handles) · NEW sparks shared to you since last render (title only —
    # the absorb hook) · 1-2 recent absorb_enabled chat highlights. Stale sparks rank lower (decay).

@register_section(key="missions", heading="## Missions",
    enabled=lambda t: t.friends_enabled,
    refresh_on=(SharedGoal, SharedGoalMembership, SharedGoalUpdate), order=64)
def render_missions(tenant) -> str:
    # active Missions + this member's commitment/next step + crew progress line (from the projection).
```

Auto-refresh USER.md on the relevant model writes via `envelope_registry.py:117` debounced `push_user_md`.

### 5.2 AGENTS.md behavioral gate (`apps/orchestrator/personas.py::render_workspace_files`)

Appended when `tenant.friends_enabled`, placed **before** the large Gravity block so it never falls in
the ~18000-char silently-truncated tail (site-publish precedent at `personas.py:499`, commit `b5d2cac`).
Written via `reassert_agents_md` on boot + config refresh. Imperative + anti-confabulation:

```
## Neighborhood — you are BACKSTAGE (only if the user has neighbors)

You are INVISIBLE in the Neighborhood. You NEVER post to a neighbor, a chat, a Circle, or a Mission,
and you never appear where neighbors can see you. Everything neighbors see comes from your human, in
their words, with their name on it.

You may do exactly two things:
  1. PROPOSE a share, privately, to your human. When your human's OWN experience would genuinely help a
     specific neighbor or Circle, search the tool catalog for `nbhd_propose_lesson_share` by name and
     CALL it ONCE. It is NOT pre-loaded — find it via toolSearch, then call it. This creates a PROPOSAL
     only; a human must approve before anything is shared. NEVER tell the user something was shared, sent,
     or is visible to a neighbor unless an approval came back THIS turn. No approval → it is still private.
  2. ABSORB quietly. Neighbors' shared sparks and recent neighborhood chat appear in your context. Hold
     them until useful; surface naturally ("that ramen spot Kenji shared is near where you're headed").
     No notifications, no spam. If the user asks what you learned from neighbors, tell them plainly — they
     can inspect and purge all of it.

NEVER propose sharing: health details; money, amounts, or finances; family or personal matters; anything
from a private conversation; anything the user did not clearly discuss as shareable. When unsure, do NOT
propose.

For a Mission, you help YOUR human show up: you may call `nbhd_propose_mission_task` to suggest ONE task
to your human toward the goal. You never act for another person and never message the group.
```

### 5.3 OpenClaw plugin tools (`runtime/openclaw/plugins/nbhd-friends-tools/index.js`)

Gate load in `apps/orchestrator/config_generator.py` next to the site-publishing block (`:1938`) on
`tenant.friends_enabled`, reusing `getRuntimeConfig`/`callRuntime` from `nbhd-journal-tools`
(`X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id`). **All tools are pull-or-propose; there is deliberately no
direct-post tool.** The imperative AGENTS.md gate is what makes the model actually *call* propose under
toolSearch passivity (the site-publish saga, `b5d2cac`).

- `nbhd_propose_lesson_share(lesson_id, why, target)` → `POST runtime/<tid>/lessons/<id>/propose-share/`
- `nbhd_neighborhood_context()` → `GET runtime/<tid>/neighborhood/context/` (absorb read; also injected via envelope)
- `nbhd_mission_context()` → `GET runtime/<tid>/missions/`
- `nbhd_propose_mission_task(mission_id, title)` → `POST runtime/<tid>/missions/<id>/propose-task/`

**Deferred (kill-list #5, #6):** no `nbhd_propose_new_share` and no `nbhd_friends_shared_search`.

### 5.4 Propose → approve → absorb, end to end

- **Share:** agent `nbhd_propose_lesson_share` → `PendingShare(pending)` → approval surfaces on iOS
  in-app queue + APNs nudge, and (transitional fallback) TG/LINE buttons via `send_lesson_approval_buttons`
  + a `handle_share_callback` clone in `apps/router/friends_callbacks.py` with callback-data
  `share:approve:<id>` → human approve → scrub → `SharedLesson` → `LessonShareGrant(active)`.
- **Absorb (backstage, idempotent):** the agent never gets a push per shared item. It pulls
  `neighborhood/context/?since=<last_absorbed_seq>` on its own turns; the envelope surfaces highlights;
  `last_absorbed_seq` + `absorb-ack` make it idempotent so the same spark is never re-absorbed. An
  `AbsorbedItem` row is logged for transparency + purge. **No per-share container nudge to recipients** —
  absorption happens on the recipient's own next turn (notification restraint).
- **Cross-agent collaboration is control-plane brokered only.** Container A cannot reach B (Phase 1d).
  A's agent proposes a Mission task → Django validates the edge → surfaces in B's next envelope → **B's
  human approves** before it becomes B's task. No agent writes another human's task.

---

## 6. Friend chat

**Models:** §2.7. **Store:** control plane (Postgres), never the tenant container — polling a container
would cold-start it (cost/battery).

**Feed:** fork `apps/router/chat_history.py:246` `build_since_page` into `apps/friends/feed.py` — same
`(created_at, seq)` keyset + opaque base64 cursor, but scoped to `FriendMessage` for threads the viewer
is a member of (join `FriendThreadMembership`, plus an `assert_participant(viewer_tenant, thread)`
check), instead of unioning tenant-scoped tables.

**Delivery loop (no new transport):** send → validate participant + `assert_can_write` on each *other*
participant's tenant → insert `FriendMessage` (idempotent on `(sender_tenant, client_msg_id)`) →
atomically claim `notified_at` (isnull→now, one-push) → look up recipients' `DeviceToken`s →
`_push_to_user_devices` (`apps/router/push_views.py:241`, per-environment fan-out + 410 self-heal) with a
per-thread `apns-collapse-id`, dispatched **off the request thread** (`_dispatch_push`). The recipient's
**poll over `?since=` is the source of truth**; APNs is only a wake nudge and **may be a no-op in prod**
until keys land (`apns_configured()`), which is fine. This matches the whole repo's poll+nudge posture
(no streaming primitive exists — `partial_text` = 0 hits).

**Agent absorption (backstage, never a participant):** the agent is **not** a `FriendThreadMembership`.
Messages reach each member's agent via (a) the `neighborhood` envelope section (`agent_absorb_enabled`
respected) and (b) `GET runtime/<tid>/neighborhood/context/` — both read-only, redacted fresh in the
recipient's session. The agent never posts. A future iteration *may* let the agent propose a reply for its
human to send; MVP absorbs silently.

**Surfaces — iOS first, web second:**
- **iOS:** a Neighborhood inbox reusing the `AppChatMessage` poll + APNs pattern against
  `/api/v1/friends/threads/<id>/messages/?since=`; unread badges via `aps.badge`; offline outbox supplies
  `client_msg_id`; poll ~3–5s while open, back off backgrounded.
- **Web:** `frontend/app/friends/` react-query `refetchInterval` (function-form, 3–5s active, off when
  idle/backgrounded, `refetchIntervalInBackground:false`) modeled on `useTelegramStatusQuery`. Chat lists
  are **NOT** added to `PERSISTED_PREFIXES` (`nbhd_qc_v3` in localStorage) — persisting a chat feed replays
  stale messages; keep it session-only.

---

## 7. Missions & PM functionality

**Product name: Missions.** Models §2.9–2.10. **The "project manager" is a control-plane status
projection + a QStash digest cron — not a human, not any single agent.**

**Status projection** (`apps/friends/projection.py::build_mission_status(shared_goal)`, generalizing
`apps/journal/status_projection.py::build_journal_status` — a **pure evidence builder**: backend computes
evidence, the LLM judges): folds the `SharedGoalUpdate` append-only stream into one crew snapshot —
per-member showed-up/streak counts, momentum, next step, overall %. Reading the control-plane
`SharedGoalUpdate` log (not a cross-tenant scan of each member's local Tasks in a request) keeps the
projection cheap and RLS-clean. Served at `GET /api/v1/friends/missions/<id>/` and
`runtime/<tid>/missions/`. Each member's `related_ref`-linked local `Task` is what their own agent/human
manage; completing it appends a `SharedGoalUpdate`.

**Who does what:**
- **Each member's own assistant** nudges **its own human**, backstage and human-gated. No rotating human
  PM (social friction); no shared agent (a container can't act cross-tenant anyway).
- **The digest cron** is the neutral coordinator: a QStash job fans out one nudge per `(mission, member,
  window)` with an `AutomationRun`-style unique `idempotency_key` (`apps/automations/models.py:50`
  pattern) + stale-RUNNING self-heal, so nobody is double-nudged. It rides the existing every-minute
  `run_due_automations` dispatcher (registered in
  `apps/cron/management/commands/register_system_crons.py`); the nudge reaches each member through **their
  own** `send-to-user` seam (`apps/router/cron_delivery.py:95`). It is clearly the **platform** speaking —
  it does NOT post into any chat as if from a human. Default cadence: weekly crew digest; a gentle
  personal daily nudge only if the member opts in (quiet hours + caps respected). Tone: warm,
  non-shaming — *"🌱 July Steps: you + Aya both hit 6/7. Kenji's had a quieter week — a wave might help."*

**Agent collaboration loop (backstage, human-gated):**
1. A's agent reads Mission status (envelope + `nbhd_mission_context`) → "A's next step is X" →
   `nbhd_propose_mission_task(mission, "X")` → `PendingGoalAction`.
2. Django validates A's membership → the proposal surfaces in **A's own** approval queue.
3. A approves → local `Task` (linked via `related_ref`) + `SharedGoalUpdate(task_added)` → projection
   updates → the crew sees progress (never A's raw task text unless A shared it).
4. Cross-member asks land as a **pending suggestion in the target member's queue**; that human approves
   before it's their task. No agent writes another human's task.

**Multi-writer safety:** Mission-level edits (title/target) use Fuel's `Workout.version` monotonic
counter + `edit_lock_until`/`edit_lock_owner` optimistic concurrency (runtime PATCH returns **409** on a
conflicting lock) — no invented locking. Mission cards also surface in `HorizonsView`
(`apps/dashboard/views.py:187`, already dual-reads typed + pending goals; reuse its
`tenant_cache(tag="dashboard")` invalidation).

---

## 8. Wormholes & warp

**Data source:** `GET /api/v1/friends/<friendship_id>/galaxy/` returns the **exact existing `GalaxyData`
shape** (`frontend/lib/constellation-game/encounter-logic.ts:46` — `{stars, edges, clusters}`) so the
Phaser scene needs zero rendering rewrite — but built server-side from `SharedLesson` snapshots via
`shared_star_qs`, pre-filtered to `ready`+active-grant. Stars source frozen fields: `text =
redacted_text`, `x/y = position_x/y`, `star_stage`, `cluster_label`, `tags`. **Edges** are recomputed
over the shared subset only (never an edge to an unshared star), or omitted for MVP and shipped
stars+clusters first. **The client only ever receives shared, scrubbed data** — filtering is server-side,
because the encounter logic runs client-side over whatever arrives, so the payload must already be safe.

**Id namespacing:** star ids in a friend galaxy are prefixed `f:<friendship_id>:<shared_lesson_id>` so
they can never collide with home-galaxy `Lesson` PKs and can never be replayed against owner-scoped
endpoints (`/lessons/<id>/tutor/…`). The scene indexes `StarEntry` by this namespaced id.

**Scene architecture — a second Phaser scene, NOT a prop swap:** register a new **`FriendGalaxyScene`**
alongside the resident `GalaxyScene` (`galaxy-scene.ts:2309`) and use **`scene.switch`** to warp. A React
`galaxy` **prop swap tears down and recreates the whole ~1MB Phaser game and loses home camera/ship
state** (`constellation-game.tsx:26-29`) — forbidden. A second co-resident scene makes **return-home
instant** and preserves home state.

**Placement & warp choreography** (Neighbor's beats, verified citations):
- **Place** one wormhole gate per accepted neighbor with shared sparks, at a deterministic rim position
  from a stable hash of `friendship_id` (never recompute layout), styled in the neighbor's `avatar_hue`,
  with a name-tag ("Kenji — 3 sparks shared") and a soft chime + "new since last visit" glow on first
  appearance (derived from `WormholeVisit`).
- **Arm + trigger** in the `update()` proximity loop (`galaxy-scene.ts:2186`), beside the encounter
  arming logic: armed only after the ship leaves the gate radius, fires on re-approach — built exactly
  like `buildEncounters`/`startEncounter` (`galaxy-scene.ts:1880/1899`).
- **Warp:** run the `startEncounter` camera move (`stopFollow` → pan → `tweenZoom`) + the `WarpIn`
  accent-glow bloom (`play/page.tsx:96-97`) as a jump-to-lightspeed transition, then
  `scene.switch("friend-galaxy", {friendshipId, hue})`.
- **Return home:** a "return home" beacon (always visible; full-bleed page via `fullBleedPages` in
  `app-shell.tsx:299`) → `scene.switch("galaxy")` → `restoreCamera` to the persisted home position
  (`galaxy-scene.ts:2053`) — survives because the home scene was paused, not destroyed.

**Read-only rules (hard):** in `FriendGalaxyScene`, disable `buildEncounters` (the nega-self duel reads
*your* neglect timestamps you don't have for a friend), tutoring, star notes, pin-note, connect, and
mutating co-pilot `reflect` writes. A friend visit is read-only "explore their shared sparks." An
optional warp co-pilot line ("what does this connect to for *you*?") routes through **your** tenant's
`is_system` copilot over the already-frozen text, never the neighbor's lesson ids.

**Souvenirs — "bring a spark home":** landing on a neighbor's shared spark offers **"Bring it home"** →
`POST /api/v1/friends/shares/<shared_lesson_id>/adopt/`, which creates a **PENDING** `Lesson` in *your*
tenant with `text = redacted_text`, `source_type = "shared"`, `source_ref = "shared_lesson:<id>"`, and an
attribution note ("via @kenji"). It enters **your normal pending-lesson approve gate** — nothing
auto-adds. `Lesson.source_type` gains a `("shared", "Adopted from a neighbor")` choice (the only
`lessons`-app schema touch beyond the dormant `shared`/`shared_at` hint on
`apps/lessons/models.py:85-86`).

**Geometry freshness:** a debounced QStash `refresh_shared_positions_task` copies-forward the owner's
current `position_x/y` onto their `SharedLesson`s after the owner re-clusters — coords only, no new PII
crossing. Two coordinate systems are non-invertible; each scene runs its own `layoutStars()` over its own
payload — never compare a neighbor's PCA idea-space coords against your world-space coords.

**Flag discipline:** gated by both `isPlayEnabled()` (existing `nbhd_play_beta` localStorage +
`NEXT_PUBLIC_CONSTELLATION_PLAY`) **and** `friends_enabled`. Dark-safe. All game LLM calls `is_system=True`.

---

## 9. iOS considerations

iOS is the strategic surface (Telegram/LINE removed from UI, backend kept). Current tabs: Chat · Journal ·
Constellation · Horizons · Fuel · Settings. Requires MJ Xcode builds (per working style); backend + web
ship independently behind the flag.

- **New Neighborhood section** (tab or card-driven), honoring the iOS-first nav. Sub-views: Requests ·
  Neighbors · Circles · Missions · Chat.
- **Approvals in-app + APNs (primary):** agent-proposed shares and Mission-task proposals as native
  approval cards with an APNs nudge, reusing `DeviceToken` fan-out + `notified_at` one-push idempotency.
  Because APNs may be unconfigured, **poll is the source of truth**; APNs is a wake nudge. The TG/LINE
  inline-button path is the working fallback that ships value immediately.
- **Chat:** native Neighborhood inbox, `?since=` poll + APNs nudge, unread badges (`aps.badge`), offline
  outbox with `client_msg_id`. Poll the **control plane, never the container** (a poll into a hibernated
  container cold-starts it every interval). Stop polling when backgrounded.
- **Preview-before-share:** the "exactly what they'll see" card + residuals banner must render identically
  on iOS — it is *the* trust surface.
- **Warp:** the Constellation tab's Play is a `WKWebView` to `hoodunited.org/constellation/play`;
  wormholes ship there behind the same dark flags, with a `?friend=<friendship_id>` deep-link to warp
  straight to a neighbor's galaxy. (Native SceneKit warp is a later spike; WKWebView ships first.)

---

## 10. Lifecycle & edge cases

| Event | Behavior |
|---|---|
| **Unfriend** (`DELETE /friends/<id>/`) | `Friendship.status=revoked, revoked_at=now`. Accessor denies immediately — neighbor holds no copy, nothing to purge in their tenant. **Revoke all `LessonShareGrant`s** for that friendship both ways → sparks leave both wormholes; a `SharedLesson` with no remaining active grants is deleted. The direct `FriendThread` is archived (kept for the user's own record; offer delete — user's choice). Offer to purge `AbsorbedItem`s from that neighbor (default purge; **keep = user's choice**). |
| **Revoke one share** (`DELETE /lessons/<id>/share/<grant_id>/`) | Grant → `revoked`. The spark leaves that neighbor's wormhole + absorb pull instantly. Downstream `AbsorbedItem`s for that source purged. **Honest copy:** *"revoking stops new access; your friend may already remember it"* — we cannot claw back what their agent already absorbed. |
| **Leave a Circle** | `CircleMembership.status=left`. Loses Circle chat + Circle-scoped grants. Circle-scoped `AbsorbedItem`s purged by default (keep = choice). **No cross-group leakage:** the agent never suggests Circle A's knowledge in Circle B unless it's the user's own item (scoped absorb feed + AGENTS.md gate). |
| **Block** | Directional `status=blocked, blocked_by`. Supersedes accepted: no reads either direction, no chat delivery, no re-invite, hidden from search. Revoke grants + archive threads. |
| **Suspended friend** (billing lapse, `status=SUSPENDED`) | `assert_can_write` blocks writes into their container (chat/mission nudges deferred or rejected with a friendly notice). Their **frozen shared sparks stay viewable** (control-plane, no container). They can't act (no container). Warp to a suspended friend = read-only frozen still works. |
| **Hibernated friend** (`status=ACTIVE` + `hibernated_at`) | Chat stores + APNs the human; sharing/absorb are control-plane so hibernation is transparent — **do not wake** the container just to absorb (absorption defers to natural wake). Wake via `wake_hibernated_tenant` only if the human explicitly needs a live agent reply. |
| **Invite a non-subscriber** | Wave link/QR → signup → `ensure_tenant_provisioned` → the pending wave auto-resolves to `accepted` on claim. Referral attribution via `FriendInvite.inviter`. |
| **Moderation** | Shares are scoped (1:1 / Circle), human-approved frozen text, not a public broadcast — small blast radius, no algorithmic amplification (the anti-Nextdoor posture). `ContentReport` → hide pending review + reporter-side block/revoke is the MVP; the scrub already removes identities. Repeated reports flag for MJ. No global queue at launch scale. |
| **Deleted tenant** | `CASCADE` removes their `SharedLesson`/edges/memberships/messages; accessor stops resolving; ledger entries retained as tombstones (source marked gone) for the absorber's transparency. |

---

## 11. Decisions record — the 9 contested decisions, final positions

**1. Consent primitive → BOTH, edges first; MVP ships 1:1 `Friendship` edges.** The `Friendship` edge is
the atomic, auditable unit of consent and the sole authorization token every accessor checks. `Circle` is
a convenience scope built *on top* (membership = consent grant). MVP = edges + per-neighbor sharing +
1:1 chat + wormholes; Circles land at PR7. Rationale: the accessor stays trivially auditable ("is A an
accepted neighbor of B?") before group fan-out, the first *wow* (warp to a neighbor) needs the least
machinery, and the blueprint's Group model is preserved as `Circle` without making it the entry
primitive.

**2. Cross-boundary lesson → frozen, scrubbed snapshot (`SharedLesson`), never a live filtered read.**
A live read would either re-run the 554MB scrub in the request path (forbidden) or expose raw text. One
scrub, frozen, friend-agnostic, no rehydration map — previewable, instantly revocable, and (living in the
owner's tenant with read-through access) zero residue on unfriend. Geometry stays "fresh enough" via
coords-only copy-forward; `content_hash` drift re-enqueues a scrub. Positions are never recomputed
cross-tenant.

**3. Friend chat → new cross-tenant `FriendThread`/`FriendMessage`, control-plane store, keyset `?since=`
+ APNs nudge, poll-is-truth.** Human chat text is **raw** (authored human→human, consent by typing); only
agent-originated artifacts are scrubbed. Each agent absorbs **read-only** via envelope + a pull endpoint,
redacted fresh in its own session; the agent is never a participant and never posts. **iOS first, web
second.**

**4. Shared goals (Missions) → new cross-tenant `SharedGoal` + membership; local Tasks via
`related_ref` (zero schema change).** The "PM" is a **control-plane status projection + QStash digest
cron** (idempotent per `(mission, member, window)`), not a human PM and not an agent-participant. Each
agent nudges its **own** human and proposes tasks via `PendingGoalAction`; approvals mint local Tasks and
emit `SharedGoalUpdate` (the single stream feeding projection + digest + envelope). Object-level edits use
Fuel's version/edit-lock (409 on conflict).

**5. Wormholes → derived from `SharedLesson` grants (no render-row table); a second Phaser scene
(`scene.switch`, not a prop swap).** Gates are proximity objects in a rim "social ring," reusing
`buildEncounters`/`startEncounter`/`WarpIn`/`restoreCamera`. Friend galaxy is **fully read-only**
(encounters/tutor/notes/copilot-writes disabled). Ids namespaced `f:<friendship_id>:<shared_lesson_id>`.
Instant **return-home**. "New since last visit" from a tiny `WormholeVisit` watermark, not a materialized
table.

**6. DB backstop → app-layer audited accessor now (mandatory + load-bearing); real RLS-with-role as the
final hardening PR.** The primary guard is a **single audited accessor** every cross-tenant read routes
through, an **architectural CI chokepoint test** that fails the build on any bypass, and a **relock
migration on every new public table** (mandatory for CI / anon-API). Real RLS tenant policies only bite if
the friend path runs through a **non-superuser DB role with `FORCE ROW LEVEL SECURITY`** (Django is
BYPASSRLS) — a meaningful infra change (new role, connection routing) recommended as the **final PR (PR8)
hardening**, not an MVP blocker. Tables are designed so the policy ("visible if `owner_tenant = current`
OR an accepted edge/grant exists") can be added **without a schema change**. Ship the accessor first;
don't stall value.

**7. Approval UX → BOTH, iOS-primary.** In-app pending-approval queue + APNs nudge is the strategic
(and dependable, control-plane-poll) surface. The existing TG/LINE inline-button machinery
(`send_lesson_approval_buttons` + `handle_lesson_callback`) is reused **verbatim** as the transitional
fallback so PR1/PR2 land fast. Web gets an approval-queue page. One backend object (`PendingShare`), three
surfaces; approvals are idempotent control-plane rows.

**8. Lifecycle → purge shared grants on unfriend/block; keep the user's own copies; honest revocation.**
Unfriend/block revoke grants both ways and remove wormholes; the direct thread is archived (delete on
request). Revocation stops future access but can't un-absorb a friend's memory (documented in UI copy).
Suspended → block writes, frozen reads still work; hibernated → no forced wake for absorption.
Non-subscriber invites route through `ensure_tenant_provisioned` (the referral loop). Moderation = report
+ block + revoke, no global queue at launch. Absorbed-item purge defaults on with keep-as-choice.

**9. Naming → "Neighborhood" / "Neighbors" (surface); code stays `friends_*`.** Connection verb = **wave**;
shared lessons may be called **sparks** in copy; groups = **Circles**; shared goals = **Missions**; the
game keeps **wormhole / warp / return home**. Code identifiers stay `friends_enabled` / `apps/friends`.
Tone is warm and quiet: waves are low-stakes knocks, agent proposals are gentle one-line offers, digests
are non-shaming, and the agent *holds sparks until useful.*

---

## 12. Phased PR plan

Every PR is dark-flag-safe (`friends_enabled` default False; game behind `nbhd_play_beta`), independently
verifiable in prod on two test tenants (MJ personal `mj@bywayofmj.com` + a test tenant), no staging. Each
table-adding PR ships its own relock migration and must keep `test_public_schema_lockdown` green. Value
lands early: **first user-visible milestone at PR1, first *wow* (warp) at PR3.** The
`FORCE-RLS-with-non-superuser-role` hardening is deferred to the **final PR (PR8)**.

### PR0 — Foundation (invisible)
- **Scope:** new `apps/friends/` app; `NeighborProfile` + `Friendship` + `FriendInvite` models;
  `Tenant.friends_enabled` flag; the **audited accessor** `apps/friends/access.py` skeleton
  (`are_neighbors`, `assert_neighbors`, `shared_star_qs`, `assert_can_write`); the **architectural CI
  chokepoint test** `test_access_chokepoint.py`; empty `neighborhood` envelope section (renders "" until
  data); `apps/friends/apps.py::ready()`. No UI, no user-facing endpoints.
- **Migrations:** `friends.0001` (NeighborProfile, Friendship, FriendInvite);
  `tenants.00NN_tenant_friends_enabled`; `tenants.00NN_relock_after_friends_edge` (mirror 0083).
- **Flags:** `friends_enabled=False` fleet-wide.
- **Tests:** `pair_key` dedup (DB, concurrent-wave race); `friendship_no_self`; `assert_neighbors` denies
  non-friends / suspended / blocked; **chokepoint test green**; relock green
  (`test_public_schema_lockdown`); envelope renders "" when disabled.
- **Prod verify:** deploy migrates clean; `make health`; confirm flag False on all tenants; CI lockdown +
  chokepoint green.

### PR1 — Neighbors console + wave/accept (first user-visible milestone)
- **Scope:** `NeighborProfile` CRUD (`@handle`/bio/hue); wave/accept/decline/block + invite/claim console
  endpoints; `GET /friends/`; `frontend/app/friends/page.tsx` + nav item (gated) + `friends_enabled` in
  `types.ts` + queries; reuse lesson-approval button machinery for wave-accept on TG/LINE.
- **Migrations:** none new.
- **Flags:** enable `friends_enabled` on MJ + one test tenant only.
- **Tests:** wave/accept/decline/block; duplicate `pair_key` rejected; accessor denies cross-tenant read
  pre-accept; nav hidden when flag off; non-subscriber invite → signup → auto-accept.
- **Prod verify:** from the two flagged tenants, wave one→other, accept, confirm the `accepted` edge and
  both appear in `/friends/`; confirm a third tenant sees nothing; confirm nav absent on an unflagged
  tenant.

### PR2 — Share pipeline: approve → scrub → freeze → publish
- **Scope:** `SharedLesson` + `LessonShareGrant` + `PendingShare`; `Lesson.source_type += "shared"`;
  human-initiated `POST /lessons/<id>/share/` + preview endpoint + `scrub_shared_lesson_task` (QStash,
  **fail-closed with NER-path verification**) + revoke. Reuse `RedactionSession` + denylist seed +
  `_scrub_placeholders` + the `memory_sync` redact-then-lock template. Web approval-queue page +
  preview-before-share UI (with residuals banner).
- **Migrations:** `friends.0002`; `lessons` migration for the `source_type` choice;
  `tenants.00NN_relock_after_shared_lessons`.
- **Flags:** still just the two tenants.
- **Tests:** scrub neutralizes `[PERSON_N]` → generic, no map; **FAIL CLOSED** when the NER path doesn't
  run (silent Presidio fallback treated as failure — no publish); preview == published `redacted_text`;
  revoke hides the grant; `content_hash` drift re-scrubs; per-edge grant dedup (partial-unique);
  `pillar ∈ {gravity, core}` share refused.
- **Prod verify:** share a real lesson between the two tenants; confirm preview == frozen text and **no
  `[PERSON_N]` and no real names** leak; **force the NER load to fail and confirm the share BLOCKS rather
  than leaks**; revoke and confirm it disappears; confirm `is_system` on the embedding call (quota
  untouched).

### PR3 — Wormholes & warp (THE WOW)
- **Scope:** `WormholeVisit` watermark; `GET /friends/<friendship_id>/galaxy/` (`GalaxyData` from
  snapshots, id-namespaced) + `/friends/wormholes/`; `refresh_shared_positions_task`; `FriendGalaxyScene`
  + wormhole gates + warp choreography + return-home + name-tags/new-glow/chime; `adopt` (souvenir)
  endpoint + flow; `fullBleedPages` entry; `?friend=` deep-link. Gated dark (`nbhd_play_beta` +
  `friends_enabled`).
- **Migrations:** `friends.0003` (WormholeVisit) + `tenants.00NN_relock_after_wormhole_visits`.
- **Flags:** game beta flag on the two tenants' devices.
- **Tests (backend):** galaxy endpoint returns only `ready`+active shared sparks, never raw/unshared/
  `StarJournalEntry`; ids namespaced; accessor denies non-friend; `adopt` creates a **pending** Lesson in
  the *viewer's* tenant, not the owner's. (Frontend: manual iOS-Simulator / browser verify per the "verify
  visuals yourself" rule.)
- **Prod verify:** with a shared spark live, open Play, warp through the neighbor's wormhole, explore, hit
  return-home; confirm ship state preserved, no unshared stars appear, and **no write endpoints are hit**
  in the friend scene (network log); confirm a souvenir lands as pending.

### PR4 — Agent integration: propose → approve + absorb
- **Scope:** `runtime/openclaw/plugins/nbhd-friends-tools/` (propose-existing + context tools only) +
  AGENTS.md share gate (before the Gravity block) + runtime `propose-share` + populated `neighborhood`
  envelope + `AbsorbedItem` ledger + purge APIs + `config_generator` gate.
- **Migrations:** `friends.0004` (AbsorbedItem) + `tenants.00NN_relock_after_absorbed_items`.
- **Tests:** `propose-share` creates `PendingShare` (not a live share); approve runs
  scrub→freeze→publish; agent cannot flip grant directly; envelope renders shared-to-me one-liners;
  `propose-share` rejects gravity/core; `absorb-ack` idempotent (no re-absorb).
- **Prod verify:** as a test tenant, have the agent propose a share; approve via the Telegram button;
  confirm the frozen snapshot appears in the neighbor's wormhole; probe the file-share AGENTS.md and
  confirm the gate is present **before** the Gravity block; confirm the agent does **not** auto-share.

### PR5 — Friend chat (1:1)
- **Scope:** `FriendThread`/`FriendThreadMembership`/`FriendMessage`; `?since=` feed (fork
  `build_since_page`) + send/idempotency + `assert_can_write` gating; APNs nudge (reuse
  `_push_to_user_devices`/`notified_at`); iOS chat surface + web poll; agent absorb via envelope +
  `agent_absorb_enabled`/`last_absorbed_seq`.
- **Migrations:** `friends.0005` + `tenants.00NN_relock_after_friend_chat`.
- **Tests:** keyset feed ordering + cursor round-trip; `(sender_tenant, client_msg_id)` idempotency;
  participant-only reads; non-participant denied; `notified_at` one-push; `agent_absorb_enabled` gates
  the envelope; suspended-target store-only; hibernated-target no forced wake; chat lists **not** in
  `PERSISTED_PREFIXES`.
- **Prod verify:** 1:1 chat between the two tenants; confirm poll delivery (APNs nudge if configured, else
  poll works); confirm the agent **absorbs** (surfaces the topic on a later turn) and **never posts**;
  purge an absorbed item.

### PR6 — Missions (shared goals + PM)
- **Scope:** `SharedGoal`/`SharedGoalMembership`/`SharedGoalUpdate` + `PendingGoalAction`; CRUD +
  `build_mission_status` projection + digest cron (`AutomationRun`-idempotent per `(mission, member,
  window)`) + runtime `propose-task` + Fuel-style `version`/`edit_lock`; `missions` envelope section;
  Missions UI; `related_ref` local-task linkage.
- **Migrations:** `friends.0006` + `tenants.00NN_relock_after_missions`. (No `journal.Task` migration —
  `related_ref` already exists.)
- **Tests:** projection folds `SharedGoalUpdate` correctly; digest idempotent per `(mission, member,
  window)` (no double-nudge); `propose-task` human-gated; agent cannot write another human's Task;
  optimistic-concurrency **409** on conflicting edit-lock.
- **Prod verify:** create a Mission between the two tenants; each adds a local task via `related_ref`; run
  the digest cron; confirm each member receives the projection via `send-to-user` once per window; have
  the agent propose a task → human approves → projection updates.

### PR7 — Circles (groups)
- **Scope:** `Circle` + `CircleMembership`; circle chat (`FriendThread(kind=circle)`); circle-scoped
  grants (grant by circle); invite codes / QR; `share_preferences`; `MAX_CIRCLES_PER_TENANT`; circle-scoped
  absorb + leave-purge; `ContentReport` moderation surfacing.
- **Migrations:** `friends.0007` (Circle, CircleMembership, ContentReport) +
  `tenants.00NN_relock_after_circles`.
- **Tests:** join via invite code; circle-scoped grant visibility; cross-group leakage guard (agent won't
  propose Circle A's item into Circle B unless it's the user's own); leave revokes that member's grants +
  purges circle-scoped absorbed items; circle cap enforced.
- **Prod verify:** three tenants form a Circle; a group chat stays human-only; a circle share reaches all
  members; leaving revokes that member's grants.

### PR8 — Hardening (defense-in-depth, final)
- **Scope:** a least-privilege **non-superuser DB role** + real **`FORCE ROW LEVEL SECURITY`** "friend-
  visible" policies on the highest-blast-radius cross-tenant tables (`shared_lessons`,
  `lesson_share_grants`, `friend_messages`), running the friend read path through that role and wiring the
  dormant `app.tenant_id`/friendship-edge GUC (`middleware.py:21`); rate-limit polish; referral-credit
  billing hook (optional).
- **Flags:** role change is infra, coordinated with MJ (connection routing).
- **Tests:** the policy denies a forged foreign read **even if an app-layer filter were dropped**
  (belt-and-suspenders atop the accessor).
- **Prod verify:** attempt a deliberately-bypassing cross-tenant read through the restricted role for a
  non-friend and confirm the **DB** denies it.

---

## 13. Risks & open questions for MJ

1. **Residual PII in shares (highest product-trust decision).** The identity scrubber deliberately drops
   amounts / dates / employer / nicknames (`apps/pii/config.py:77-82`). A perfectly identity-scrubbed
   spark can still surface "$4,200 debt at [employer]." Mitigations shipped: **preview-before-share** +
   residuals banner + the mechanical `gravity/core` pillar-block. **Do you want any finance/health lesson
   shareable at all in v1, or is the pillar-block permanent until a higher-bar "shared-content" scrub
   exists?**
2. **DB backstop cost (PR8).** Ship audited-accessor-only now, or invest in the non-superuser-role +
   `FORCE RLS` hardening before wider rollout? Recommendation: accessor-first (it's the seam the codebase
   uses everywhere); add the DB net before scaling past the launch cohort. The blast radius (another
   user's private data) is what justifies the eventual role change.
3. **APNs still unconfigured?** Friend chat + approvals lean on the poll loop; the nudge is a bonus.
   Landing `APNS_AUTH_KEY/KEY_ID/TEAM_ID/BUNDLE_ID` makes chat feel live. Ship without? (Matches current
   chat behavior.)
4. **Revocation honesty.** A revoked share can't be un-absorbed from a friend's agent memory. Acceptable
   with the UI disclosure ("your friend may already remember it")?
5. **Monetization (blueprint open Q).** Neighborhood in the base subscription, or a Circle/Missions
   add-on? Affects caps and flag defaults.
6. **Caps.** Propose defaults for sign-off: max neighbors (~150), max Circles (8), max Circle size (~50),
   shares/day (~20), absorbed-items-per-turn (bounded for context budget + cost).
7. **Non-subscriber invites + referral loop.** Build referral credit now (billing hook) or ship
   invite→signup→auto-accept first and add credit later? Public wave links are a growth loop *and* a spam
   vector (`max_uses`/`expires_at`/rate-limits bound it) — comfortable with public links, or restrict v1
   to handle-based (existing subscribers)?
8. **Sequencing gate (blueprint).** "Don't build until ≥10 active users and personal agent memory is good
   enough that suggestions are useful." PR0–PR3 (connect + share + warp) are safe behind the flag
   regardless; **PR4 (agent *proposes* shares) is the one that depends on suggestion quality — gate its
   per-tenant enablement on a confidence read of the agent's memory.**
9. **Naming sign-off.** "Neighborhood / Neighbors / wave / sparks / Circles / Missions / wormhole / warp /
   return home," code flag `friends_enabled`. Approve the tone, or prefer a different surface label?

**Biggest risk:** the cross-tenant boundary has **no DB backstop today** (Django is BYPASSRLS; RLS has no
tenant policies — isolation is 100% Python queryset filters). A single missing `friendship`/`tenant`
filter leaks another user's private data with no net. The spine of this design contains that risk to
*one* audited module (`apps/friends/access.py`), guarded by an **architectural CI chokepoint test**, a
**fail-closed scrub** that blocks rather than leaks names when the NER path degrades, a **relock migration
on every new table**, and the **PR8 `FORCE RLS` role** as belt-and-suspenders — and makes the whole thing
*visible* to the user through preview-before-share, the absorbed-items ledger, and instant read-through
revocation. That visible trustworthiness is not overhead; it is the product.
