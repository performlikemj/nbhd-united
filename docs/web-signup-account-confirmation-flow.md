# DESIGN — Account-aware web→app signup handoff ("you already have an account")

**Status:** Draft for review — no code written yet.
**Scope:** Frontend only (`hoodunited.org` SWA). No backend change, no iOS rebuild.
**Fixes:** New iOS user lands in a *different* (old) account and sees its data (the "Daily Notes show April dates" report).
**Companion to:** `docs/web-signup-handoff-directive.md` (the original PKCE handoff this refines).

---

## 1. The problem (what's broken today)

When a user taps **Create an account** in the iOS app, the app opens the web
handoff inside `ASWebAuthenticationSession` with `prefersEphemeralWebBrowserSession = false`
(`nbhd-ios/.../WebAuthCoordinator.swift:122`) — so it **shares Safari's
cookie + localStorage jar** on the device.

The web authorize page then does:

```
authorize/page.tsx:92   if (isLoggedIn()) { completeHandoff(params); }
lib/auth.ts:24          isLoggedIn() = (localStorage["nbhd_access_token"] != null)
```

`isLoggedIn()` only asks "is there a token in this browser?" — never *whose*.
So if any **leftover token from a prior account** is sitting in that shared jar
(e.g. the device was used to web-log-in to an old account months ago), the page
**ignores `intent=register`** and silently mints a one-time code for that old
account. The app receives tokens for the wrong account and faithfully renders
its real data.

Confirmed against production: a genuinely-new tenant owns **zero** old notes;
the April notes belong to old, mostly-suspended tenants. The backend serving
layer is correctly tenant-scoped — **this is purely the handoff trusting a
leftover browser session.**

### The bug chain
```
old web login leaves token in Safari/SWA localStorage
        │
iOS "Create an account"  ──opens──▶  ASWebAuthenticationSession (shares Safari jar)
        │
        ▼
/app/authorize?intent=register   ──isLoggedIn()? yes──▶  completeHandoff()
        │                                                 (for the OLD account!)
        ▼
POST /api/v1/auth/authorize/ (Bearer = old token) ──▶ one-time code for OLD user
        │
        ▼
iOS exchanges code ─▶ stores OLD account's JWT ─▶ Journal shows OLD account's notes
```

---

## 2. Goals & principles

1. **Account-aware, not silent.** Never act on a leftover session without
   telling the user whose it is. Surface it; let them choose.
2. **Non-destructive by construction.** Nothing in this flow may delete an
   account or lose data. The only state changes allowed are: write/clear a token
   in *this browser* (local logout), create a *new* account (new email), log
   into an *existing* account, or mint a one-time PKCE code. (See §6.)
3. **Recognize returning users.** If the leftover session *is* their account,
   offer a one-tap "Continue as you" — that's the "you already have an account,
   let's just log you in" path.
4. **No iOS rebuild.** The fix lives in the web page that renders *inside* the
   already-shipped auth sheet, so it ships via the frontend deploy and fixes
   builds already in users' hands.
5. **Reuse what exists.** `GET /api/v1/auth/me/` already returns the account's
   email; the PKCE mint/exchange endpoints are unchanged.

---

## 3. Current flow (as-is)

```
                 ┌─────────────────────────── /app/authorize ───────────────────────────┐
 iOS app ──▶ GET │ parse+validate params (from URL on first hop, from stash on bounce)   │
 (shared jar)    │                                                                        │
                 │   isLoggedIn()? ──── yes ──▶ completeHandoff()  ◀── BUG: whose token?  │
                 │        │                                                               │
                 │        └─ no ──▶ router.replace(intent=signin ? /login : /signup)      │
                 └────────────────────────────────────────────────────────────────────┬─┘
                                                                                        │
   /signup or /login  ──success──▶ setTokens(); if pending handoff:                     │
                                   router.replace("/app/authorize")  ── no query ───────┘
                                   (bounce-back: params come from sessionStorage stash)
```

**Key existing fact we rely on:** the first hop from the app carries the PKCE
params *in the URL*; the post-auth bounce-back returns to `/app/authorize` with
**no query string** (`signup/page.tsx:42`, `login/page.tsx:30`). So
"`parseAuthorizeParams(URL) !== null`" reliably means **"first hop from the app"**
vs "**came back after the user just authenticated.**"

---

## 4. Proposed flow (to-be)

The page becomes a tiny **state machine**. The pivotal new idea: only
auto-complete a handoff for a session the user **just established in this flow**.
A session that *pre-existed* the flow (first hop, already logged in) is treated
as "unknown ownership" → we resolve who it is and **ask**.

### 4.1 States
| State | UI | Meaning |
|---|---|---|
| `verifying` | spinner "Connecting…" | parsing/validating params |
| `choose-account` | **interstitial** | "You're already signed in as `X`." → [Continue as X] / [Use a different account] |
| `redirecting` | spinner | bouncing to `/signup` or `/login` |
| `finishing` | spinner "Finishing sign-in…" | mint code + `nbhd://…?code=&state=` |
| (terminal error) | — | `nbhd://auth/callback?error=…` |

### 4.2 Decision logic (pure, unit-testable)
```
params = parseAuthorizeParams(URL) ?? readStash()
if (!params || !valid(params)) → error("invalid_request"); STOP
stash(params)

firstHop = parseAuthorizeParams(URL) !== null     // URL carried the params

if (firstHop):
    if (!isLoggedIn()):
        → redirecting → (intent==="signin" ? /login : /signup)
    else:
        me = probeIdentity()            // raw GET /me, see §4.4 — NOT apiFetch
        if (me === null):               // expired/invalid leftover token
            clearTokens()               // local logout of a dead session
            → redirecting → (intent==="signin" ? /login : /signup)
        else:
            → choose-account(email = me.email)
else:   // bounce-back — the user just authenticated in THIS flow
    if (isLoggedIn()): → finishing → completeHandoff(params)
    else:              → redirecting → (intent==="signin" ? /login : /signup)   // defensive
```

`choose-account` button actions:
- **Continue as `X`** → `finishing` → `completeHandoff(params)`  *(log into the existing account)*
- **Use a different account** → *(see Decision D2)* → `redirecting` → `/signup` (or `/login`)

> Why `completeHandoff` on the bounce-back is always correct: reaching it
> requires passing through `/signup` or `/login`, both of which call
> `setTokens(fresh)` immediately before bouncing back. So the token at
> `finishing` is *always* the one the user just authenticated as — never a
> leftover.

### 4.3 Scenario walk-throughs

**S1 — Brand-new user, no prior session (happy path, unchanged)**
```
app ▶ /app/authorize?intent=register  (firstHop, not logged in)
    ▶ /signup ▶ create (new email) ▶ setTokens ▶ /app/authorize (no query)
    ▶ finishing ▶ code ▶ nbhd:// ▶ app signed in as the NEW account ✓
```

**S2 — Returning user, leftover session = THEIR account ("just log me in")**
```
app ▶ /app/authorize?intent=register  (firstHop, logged in)
    ▶ probe /me → jane@… ▶ choose-account
    ▶ [Continue as jane@…] ▶ finishing ▶ app signed in as Jane's existing account
      (her real history loads — legitimately hers) ✓
```

**S3 — Shared device / wrong leftover session (today's bug — now fixed)**
```
app ▶ /app/authorize?intent=register  (firstHop, logged in as someone-else@)
    ▶ probe /me → someone-else@ ▶ choose-account
    ▶ user sees it's not them ▶ [Use a different account]
    ▶ /signup ▶ create (new email) ▶ bounce ▶ finishing ▶ correct NEW account ✓
   (no more silent wrong-account; the old account is untouched)
```

**S4 — Expired / dead leftover token**
```
app ▶ /app/authorize?intent=register  (firstHop, token present but stale)
    ▶ probe /me → 401 (refresh also fails) → null
    ▶ clearTokens() ▶ /signup ▶ normal flow ✓
```

**S5 — Sign-in intent (if confirmation applies to signin too — Decision D1)**
```
app ▶ /app/authorize?intent=signin  (firstHop, logged in)
    ▶ probe /me → choose-account ▶ [Continue]/[different account → /login] ✓
```

### 4.4 The identity probe (important implementation note)
Do **not** use the shared `fetchMe()` / `apiFetch()` for the probe. On a 401
with a refresh token present, `apiFetch` calls `window.location.href = "/login"`
(`lib/api.ts:148-152`) — that would **hijack the register routing** and dump the
user on the wrong page. Instead use a small, self-contained probe:

```
probeIdentity():
  token = getAccessToken()
  r = raw fetch GET /api/v1/auth/me/ with Bearer token
  if r.ok           → return { email }
  if r.401 & refresh present:
       rr = raw POST /api/v1/auth/refresh/ { refresh }
       if rr.ok → setTokens(rr); retry GET /me; return {email} or null
  return null
```

This keeps the leftover-session check fully under the page's control and still
honors a valid-but-expired access token whose refresh is alive (a real, live
session we *should* offer "Continue as X" for).

---

## 5. What changes / what doesn't

**Changes (frontend only):**
- `frontend/app/app/authorize/page.tsx` — rework into the state machine + the
  `choose-account` interstitial UI (reuse `OnboardingShell`, follow `DESIGN.md`).
- `frontend/lib/app-authorize.ts` — add the pure `decideAuthorizeAction(...)`
  helper + a raw `probeIdentity()` (keeps logic unit-testable; mirrors the iOS
  habit of pure, testable auth helpers).
- *(Maybe)* a small `AccountChoiceCard` component, or inline in the page.
- Tests: unit tests for `decideAuthorizeAction` across S1–S5; a probe test.

**Unchanged (explicitly):**
- Backend: `AuthorizeBeginView`, `ExchangeView`, `MeView` — untouched. No
  migration, no new endpoint.
- iOS: nothing required. (Optional future hardening in §8.)
- PKCE/security model: single-use short-TTL code, `state` nonce, `redirect_uri`
  allowlist, no-oracle exchange — all untouched.

---

## 6. Non-destructiveness guarantees (the safety question)

This flow **cannot delete an account or lose data.** Every state-changing action
it can reach:

| Action | What it actually does | Reversible? |
|---|---|---|
| `clearTokens()` | removes the JWT from **this browser's** localStorage = local logout | Yes — log back in |
| go to `/signup` → create | makes a **new, separate** account under a **new email** | old account untouched; emails are unique so it *cannot* overwrite |
| go to `/login` → sign in | authenticates as an **existing** account | n/a |
| `completeHandoff()` | mints a one-time PKCE **code** | n/a |

There is **no account-deletion call anywhere in this path.** (A
`/api/v1/tenants/delete-account/` endpoint exists in the app, but it is
unrelated, requires an explicit `confirm:"DELETE"`, and we do **not** touch it.)
"Use a different account" at worst performs a local logout — the account and all
its server-side data remain intact.

---

## 7. Edge cases & risks
- **`apiFetch` 401 redirect hijack** — handled by the dedicated raw probe (§4.4).
- **React StrictMode double-invoke** — keep the `ran.current` ref guard so the
  probe fires once.
- **`sessionStorage` unavailable (private mode)** — stash fails; bounce-back has
  no params → existing `invalid_request` error path (unchanged behavior).
- **Auth pages while a stale token is present** — `/signup` and `/login` do not
  redirect-if-logged-in today, so landing there with a leftover token is safe;
  a successful auth overwrites it. (If D2 = clear-on-switch, this is moot.)
- **PII** — showing "signed in as `email`" on the user's own device is the
  standard pattern; the email comes from the authenticated `/me`.

---

## 8. Rollout & verification
- **Ship:** frontend PR → merge → SWA deploy. Fixes already-shipped iOS builds
  immediately. No backend deploy, no migration.
- **Verify (no device needed for logic):** unit tests for `decideAuthorizeAction`
  S1–S5. **Verify (device):** reproduce the bug — web-login as A on a device,
  then iOS "Create an account": before = lands as A; after = sees
  "Continue as A / different account."
- **Optional iOS defense-in-depth (separate, deferred):** set
  `prefersEphemeralWebBrowserSession = true` for the register intent so the
  auth sheet starts with a clean jar. Not required — the web fix fully resolves
  the reported bug — and it needs an iOS build, so keep it out of this change.

---

## 9. Open decisions (for review)
- **D1 — Scope:** apply the "Continue as X / different account" confirmation to
  **both** intents (register + signin), or **register only**?
  *Recommend: both* — sign-in should also never silently land on a leftover
  wrong account. (Register is the reported bug; signin is the same risk class.)
- **D2 — "Use a different account":** local-logout-then-route (`clearTokens()`
  first — cleaner, guarantees a fresh auth page) vs route-without-clearing
  (gentlest — touches nothing until they actively re-auth)?
  *Recommend: local-logout-then-route*, since clearing is a reversible local
  logout, not data loss.
- **D3 — Complementary nudge (separate change):** when a brand-new user types an
  email at `/signup` that **already exists**, turn the rejection into a "that
  email already has an account — sign in instead?" prompt. In or out of scope
  for this work?
- **D4 — Copy:** exact wording of the interstitial + buttons.
```
