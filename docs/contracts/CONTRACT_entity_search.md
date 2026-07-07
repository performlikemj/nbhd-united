# CONTRACT — Entity search (`?q=`) for Siri AppEntity / EntityQuery (BK3 · Directive 3 / S4 backend half)

Consumer DRF list endpoints the iOS `EntityQuery` layer uses to resolve
Task / Goal / Account references by name (Shortcuts pickers + disambiguation).
Each entity below already returned a **stable UUID `id`** plus a **display name**;
this task added an optional case-insensitive `?q=` name search with a bounded
result cap on the search path.

**Auth:** all three require the app's existing authenticated consumer session.
- Journal (Task/Goal): session auth (`IsAuthenticated`, user's own tenant).
- Finance (Account): JWT bearer (`IsAuthenticated`, `request.user.tenant`).

**`?q=` semantics (all three):**
- Case-insensitive `icontains` on the natural name field.
- Tenant-scoped (never crosses tenants).
- Applied **only when `q` is non-empty**; the unfiltered list keeps its full,
  uncapped behavior so existing web/iOS enumeration is not silently truncated.
- Result cap = **20** on the `q` search path (bounded picker). Empty `q` → no cap.
- No `q` match → empty list, HTTP 200 (not 404).
- `q` composes with the endpoint's other existing filters (status/pillar/archived/etc.).

---

## 1. Task — PRESENT

- **Endpoint:** `GET /api/v1/journal/tasks/`
- **View:** `apps/journal/lifecycle_views.py` → `TaskListCreateView.get`
- **Search param:** `?q=` → `title__icontains`, ordered `-updated_at`, capped 20.
- **Other filters (pre-existing):** `status`, `pillar`, `parent_goal_id`,
  `due_before`, `due_after` (ISO date; malformed date/uuid → 400).
- **Response:** JSON array of `TaskSerializer` objects. Entity-relevant fields:
  - `id` — UUID (stable key)
  - `title` — display name
  - plus `description`, `pillar`, `status`, `due_date`, `completed_at`,
    `parent_goal_id`, `related_ref`, `created_at`, `updated_at`.
- **Example:** `GET /api/v1/journal/tasks/?q=dentist`
  → `[{"id": "<uuid>", "title": "Call the Dentist", ...}]`

## 2. Goal — PRESENT

- **Endpoint:** `GET /api/v1/journal/goals/`
- **View:** `apps/journal/lifecycle_views.py` → `GoalListCreateView.get`
- **Search param:** `?q=` → `title__icontains`, ordered `-updated_at`, capped 20.
- **Other filters (pre-existing):** `status`, `pillar`, `parent_goal_id`.
- **Response:** JSON array of `GoalSerializer` objects. Entity-relevant fields:
  - `id` — UUID (stable key)
  - `title` — display name
  - plus `description`, `pillar`, `topic_id`, `target`, `status`,
    `parent_goal_id`, `target_date`, `achieved_at`, `created_at`, `updated_at`.
- **Example:** `GET /api/v1/journal/goals/?q=marathon`
  → `[{"id": "<uuid>", "title": "Run a Marathon", ...}]`

## 3. Account (Finance) — PRESENT

- **Endpoint:** `GET /api/v1/finance/accounts/`
- **View:** `apps/finance/views.py` → `FinanceAccountListView.get`
- **Search param:** `?q=` → `nickname__icontains`, ordered `nickname`, capped 20.
- **Other filters (pre-existing):** `?archived=true` (returns `is_active=False`
  rows; default returns active rows). `q` composes with `archived`.
- **Response:** JSON array of `FinanceAccountSerializer` objects. Entity-relevant fields:
  - `id` — UUID (stable key)
  - `nickname` — display name
  - plus `account_type`, `current_balance`, `original_balance`, `interest_rate`,
    `minimum_payment`, `credit_limit`, `due_day`, `is_active`, `is_debt`,
    `payoff_progress`, `created_at`, `updated_at`.
- **Note:** Finance is behind the platform Gravity kill switch for writes/enable,
  but reads of existing accounts are unaffected by that gate.
- **Example:** `GET /api/v1/finance/accounts/?q=chase`
  → `[{"id": "<uuid>", "nickname": "Chase Sapphire", ...}]`

---

## Entities with NO existing consumer list endpoint

None missing for the S4 scope (Task / Goal / Account). All three had a consumer
list endpoint already; no brand-new resource endpoints were created. (For
reference, several *runtime* list endpoints exist under
`/api/v1/finance/runtime/<tenant_id>/...` and `/api/v1/journal/runtime/...` but
those are internal-key auth for the OpenClaw plugin, not the app's consumer path,
so they were left untouched.)

## Tests

- `apps/journal/test_lifecycle_views.py::TaskGoalListCreateTests` —
  `test_list_tasks_q_*` (case-insensitive match, no-match empty, tenant scope,
  cap 20) and `test_list_goals_q_*` (match, tenant scope, cap 20).
- `apps/finance/tests.py::ConsumerFinanceViewTests` —
  `test_accounts_list_q_*` (case-insensitive match, tenant scope, no-match empty,
  cap 20).
