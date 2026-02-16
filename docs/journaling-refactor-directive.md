# Developer Directive: Journaling Refactor — Frontend + Backend Alignment

## Background: How This System Actually Works

This platform gives each subscriber a personal AI agent. The agent and the user collaborate through a shared journal — think of it like a shared notebook where both parties write throughout the day.

Here's a real example of what a day looks like in practice:

```markdown
# 2026-02-15

## 09:30 — MJ
Started working on the demo video edit. Feeling good about the footage from Friday.
Energy: 7 | Mood: 😊

## 10:15 — Agent
Checked your Gmail — nothing urgent. You have a meeting at 14:00 with the design team (Google Calendar).

## 12:30 — MJ
Lunch break. Tried that new ramen place near Namba. Solid 8/10.

## 14:45 — Agent
Meeting ended. Key takeaway from your calendar notes: design team needs the mockups by Wednesday.

## 18:00 — MJ
Got the timezone branch merged. Productive afternoon.

## 22:00 — Evening Check-in (Agent)
### What happened today
- Demo video editing started (footage from Friday)
- Design meeting — mockups due Wednesday
- Timezone feature merged to main
- New ramen spot discovered near Namba (8/10)

### Decisions
- Prioritize mockups before Thursday standup

### Energy/Mood
- Morning: 7/😊
- Evening: (not recorded)

### Tomorrow
- Mockup drafts for design team
- Continue demo video edit
```

**Key principles:**
1. **One document per day, per user.** Not one row per entry. One growing markdown document.
2. **Both human and agent write to the same document.** The agent appends entries (research findings, email summaries, meeting notes, evening check-ins). The user appends their own thoughts, reflections, moods.
3. **Markdown is the storage format.** Agents process markdown natively and efficiently. The database stores raw markdown.
4. **The user never sees raw markdown.** The frontend parses it into a friendly timeline UI.
5. **Long-term memory is a separate single document per user.** The agent periodically reviews daily notes and promotes important insights, preferences, and decisions into a curated "memory" document (like a personal knowledge base the agent maintains).

---

## Current State

### Branch: `feature/journaling-refactor`

The backend is mostly done. The branch has:

**New models** (`apps/journal/models.py`):
- `DailyNote` — one row per tenant + date, with a `markdown` TextField
- `UserMemory` — one row per tenant (OneToOne), with a `markdown` TextField
- Old `JournalEntry` and `WeeklyReview` models are untouched (legacy, will deprecate later)

**Markdown parser** (`apps/journal/md_utils.py`):
- `parse_daily_note(markdown) -> list[dict]` — parses `## HH:MM — Author` headers into structured entries
- `serialise_daily_note(date_str, entries) -> str` — entries back to markdown
- `append_entry_markdown(existing_md, ...) -> str` — appends a new entry to existing doc
- Extracts `Energy: N | Mood: X` metadata from entry body
- Handles subsections (`### heading` within an entry, used for evening check-ins)

**User-facing API** (`apps/journal/views.py`, under `/api/v1/journal/`):
- `GET /daily/<date>/` — returns the day's markdown parsed into structured entries
- `POST /daily/<date>/entries/` — appends an entry (text + optional mood/energy), returns updated entries
- `PATCH /daily/<date>/entries/<index>/` — edit entry by position index
- `DELETE /daily/<date>/entries/<index>/` — remove entry by position index
- `GET /memory/` — returns user's long-term memory
- `PUT /memory/` — update user's long-term memory

**Runtime/Agent API** (`apps/integrations/runtime_views.py`, under `/api/v1/integrations/runtime/<tenant_id>/`):
- `GET daily-note/?date=YYYY-MM-DD` — returns raw markdown
- `POST daily-note/append/` — appends raw markdown with timestamp + author=agent
- `GET long-term-memory/` — returns raw markdown
- `PUT long-term-memory/` — replaces full memory doc
- `GET journal-context/` — returns last 7 days of daily notes + memory (raw md, for agent session init)

### Branch: `main` (current frontend)

The frontend (`frontend/app/page.tsx`) is a **form-based journal entry creator**. It has:
- A form with: date picker, mood text input, energy dropdown (low/medium/high), wins list, challenges list, reflection textarea
- A list of past entries shown as cards
- Edit/delete per entry

**This frontend needs to be completely replaced.** It does not match the collaborative markdown-first model at all.

### Existing Frontend Stack
- Next.js 14 (App Router)
- React 18
- TanStack Query v5 (for data fetching/mutations)
- Tailwind CSS 3
- Existing components: `SectionCard`, `SectionCardSkeleton`, `StatusPill`, `AppShell`
- API layer: `frontend/lib/api.ts` (fetch wrappers), `frontend/lib/queries.ts` (TanStack hooks), `frontend/lib/types.ts`

---

## What Needs to Be Built

### 1. Frontend: Journal Timeline Page (`frontend/app/page.tsx`)

Replace the current form-based page with a **timeline view**. This is the main page of the app.

**Layout:**
```
┌──────────────────────────────────────────┐
│  ◀  February 15, 2026  ▶    [Today]     │  ← Date navigator
├──────────────────────────────────────────┤
│                                          │
│  ┌─ 9:30 AM ──────────────────────────┐  │
│  │ 👤 MJ                              │  │  ← Human entry (left-aligned or neutral)
│  │ Started working on the demo video  │  │
│  │ edit. Feeling good about the       │  │
│  │ footage from Friday.               │  │
│  │ 😊 Energy: ███████░░░ 7/10        │  │  ← Mood/energy rendered as visual
│  │                          [Edit] ✏️  │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌─ 10:15 AM ─────────────────────────┐  │
│  │ 🤖 Agent                           │  │  ← Agent entry (visually distinct)
│  │ Checked your Gmail — nothing       │  │
│  │ urgent. Meeting at 14:00 with      │  │
│  │ design team.                       │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌─ 10:00 PM ─────────────────────────┐  │
│  │ 🌙 Evening Check-in               │  │  ← Special styling for check-ins
│  │                                    │  │
│  │ What happened today                │  │
│  │ • Demo video editing started       │  │
│  │ • Design meeting — mockups Wed     │  │
│  │ • Timezone feature merged          │  │
│  │                                    │  │
│  │ Decisions                          │  │
│  │ • Prioritize mockups before Thu    │  │
│  │                                    │  │
│  │ Tomorrow                           │  │
│  │ • Mockup drafts for design team    │  │
│  └────────────────────────────────────┘  │
│                                          │
├──────────────────────────────────────────┤
│  ┌────────────────────────────────────┐  │
│  │ What's on your mind?          [+]  │  │  ← Simple input (NOT a form)
│  └────────────────────────────────────┘  │
│  [😊 Mood] [⚡ Energy]                   │  ← Optional toggles, collapsed by default
└──────────────────────────────────────────┘
```

**Behavior:**
- **Date navigation:** Left/right arrows to browse days. "Today" button to jump back. Default to today.
- **Entry cards:** Each entry is a card in the timeline. Show time, author icon (👤 for human, 🤖 for agent), and content.
- **Human vs agent styling:** Agent entries should have a slightly different background color (e.g., light blue/gray tint) so users can visually distinguish who wrote what.
- **Evening check-in:** Entries with `section: "evening-check-in"` get special styling — a moon icon, subsections rendered as labeled lists.
- **Mood/energy display:** If an entry has mood/energy, render it visually (emoji + progress bar or pill, not raw text like "Energy: 7").
- **Edit entry:** Human entries get an edit button. Clicking it opens inline editing (just a textarea replacing the content, with Save/Cancel). Agent entries are read-only for the user.
- **Add entry:** Simple text input at the bottom. NOT a multi-field form. Just a text box and a send button. Optionally expand to show mood emoji picker and energy slider (collapsed by default, toggled via small buttons). Submitting calls `POST /api/v1/journal/daily/<date>/entries/` with `{ text, mood?, energy? }`.
- **Empty state:** If no entries for selected date, show a friendly prompt like "Nothing here yet. How's your day going?"
- **Auto-scroll:** When the page loads, scroll to the most recent entry.

### 2. Frontend: Memory Page (`frontend/app/memory/page.tsx`)

New page accessible from the nav. Shows the user's long-term memory document.

**What is long-term memory?**
It's a curated document the agent maintains. The agent periodically reviews daily notes and pulls out significant things — decisions made, preferences learned ("user prefers morning meetings"), goals, lessons. Think of it as "what the agent knows about you."

**Layout:**
```
┌──────────────────────────────────────────┐
│  🧠 What Your Agent Knows About You      │
├──────────────────────────────────────────┤
│                                          │
│  Your agent reviews your journal and     │
│  builds this knowledge base over time.   │
│  You can edit it to correct or add       │
│  anything.                               │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │ ## Preferences                     │  │  ← Rendered from markdown sections
│  │ • Prefers morning meetings         │  │
│  │ • Likes detailed code reviews      │  │
│  │ • Energy peaks around 10am         │  │
│  │                                    │  │
│  │ ## Goals                           │  │
│  │ • Launch chef platform by March    │  │
│  │ • Exercise 3x per week             │  │
│  │                                    │  │
│  │ ## Decisions                       │  │
│  │ • Using PostgreSQL for all DBs     │  │
│  │ • No Stripe until chef goes live   │  │
│  └────────────────────────────────────┘  │
│                                          │
│  [Edit] ← Opens a rich text/textarea     │
│           editor for the full document   │
└──────────────────────────────────────────┘
```

**Behavior:**
- Fetch from `GET /api/v1/journal/memory/`
- Render the markdown content as formatted HTML (use a markdown renderer like `react-markdown` or similar)
- Edit button switches to a textarea with the raw content (or a simple rich text editor). Save calls `PUT /api/v1/journal/memory/` with `{ markdown: "..." }`
- If empty, show: "Your agent hasn't built any memories yet. As you journal and chat, it will learn what matters to you."

### 3. Frontend: Navigation Update

Update `frontend/components/app-shell.tsx` nav items:

```typescript
const navItems = [
  { href: "/", label: "Journal" },        // Timeline (already exists as route)
  { href: "/memory", label: "Memory" },    // New
  { href: "/settings", label: "Settings" },
];
```

Remove any references to "Reviews" in the UI. `WeeklyReview` is a backend model that will be deprecated — don't expose it in the frontend.

### 4. Frontend: Types Update (`frontend/lib/types.ts`)

Add new types:

```typescript
// Parsed daily note entry (returned by GET /daily/<date>/)
export interface DailyNoteEntry {
  time: string | null;
  author: "human" | "agent";
  content: string;
  mood: string | null;
  energy: number | null;          // 1-10
  section: string | null;         // e.g. "evening-check-in"
  subsections: Record<string, string> | null;  // e.g. {"what-happened-today": "- item\n- item"}
}

// Response from GET /api/v1/journal/daily/<date>/
export interface DailyNoteResponse {
  date: string;                   // "2026-02-15"
  entries: DailyNoteEntry[];
  markdown: string;               // raw markdown (for debugging, not displayed)
}

// Response from GET /api/v1/journal/memory/
export interface UserMemoryResponse {
  markdown: string;
  updated_at: string;
}
```

### 5. Frontend: API Layer Update (`frontend/lib/api.ts`)

Add functions:

```typescript
export async function fetchDailyNote(date: string): Promise<DailyNoteResponse> {
  return apiFetch(`/journal/daily/${date}/`);
}

export async function appendDailyNoteEntry(
  date: string,
  data: { text: string; mood?: string; energy?: number }
): Promise<DailyNoteResponse> {
  return apiFetch(`/journal/daily/${date}/entries/`, { method: "POST", body: data });
}

export async function updateDailyNoteEntry(
  date: string,
  index: number,
  data: { content?: string; mood?: string; energy?: number }
): Promise<DailyNoteResponse> {
  return apiFetch(`/journal/daily/${date}/entries/${index}/`, { method: "PATCH", body: data });
}

export async function deleteDailyNoteEntry(
  date: string,
  index: number
): Promise<void> {
  return apiFetch(`/journal/daily/${date}/entries/${index}/`, { method: "DELETE" });
}

export async function fetchUserMemory(): Promise<UserMemoryResponse> {
  return apiFetch("/journal/memory/");
}

export async function updateUserMemory(markdown: string): Promise<UserMemoryResponse> {
  return apiFetch("/journal/memory/", { method: "PUT", body: { markdown } });
}
```

### 6. Frontend: Query Hooks (`frontend/lib/queries.ts`)

Add hooks:

```typescript
export function useDailyNoteQuery(date: string) {
  return useQuery({
    queryKey: ["daily-note", date],
    queryFn: () => fetchDailyNote(date),
  });
}

export function useAppendEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { text: string; mood?: string; energy?: number }) =>
      appendDailyNoteEntry(date, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useUpdateEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ index, data }: { index: number; data: { content?: string; mood?: string; energy?: number } }) =>
      updateDailyNoteEntry(date, index, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useDeleteEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (index: number) => deleteDailyNoteEntry(date, index),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useUserMemoryQuery() {
  return useQuery({
    queryKey: ["user-memory"],
    queryFn: fetchUserMemory,
  });
}

export function useUpdateMemoryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (markdown: string) => updateUserMemory(markdown),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["user-memory"] });
    },
  });
}
```

### 7. Backend: Verify and Fix API Responses

Make sure the user-facing `GET /api/v1/journal/daily/<date>/` returns this shape:

```json
{
  "date": "2026-02-15",
  "entries": [
    {
      "time": "09:30",
      "author": "human",
      "content": "Started working on the demo video edit. Feeling good about the footage.",
      "mood": "😊",
      "energy": 7,
      "section": null,
      "subsections": null
    },
    {
      "time": "22:00",
      "author": "agent",
      "content": "",
      "mood": null,
      "energy": null,
      "section": "evening-check-in",
      "subsections": {
        "what-happened-today": "- Demo video editing started\n- Design meeting — mockups Wed",
        "decisions": "- Prioritize mockups before Thu",
        "tomorrow": "- Mockup drafts for design team"
      }
    }
  ]
}
```

And `POST /api/v1/journal/daily/<date>/entries/` accepts:
```json
{
  "text": "Had a great lunch at the new ramen place near Namba.",
  "mood": "😊",
  "energy": 8
}
```

The backend appends this to the markdown with the current time and `author=human`, then returns the updated entries list.

### 8. Backend: Remove Legacy Journal Frontend Code

On the `feature/journaling-refactor` branch:
- The old `JournalEntry` form-based API endpoints should remain functional (don't delete the model or migration) but the frontend should NOT use them
- Remove old journal-related query hooks and API functions from the frontend once the new ones are in place
- Keep the old queries/types around if other pages reference them, but the main Journal page should only use the new daily-note endpoints

### 9. Tests

**Frontend (if test framework exists):**
- Timeline renders entries in chronological order
- Date navigation changes the displayed date
- Add entry input submits and refreshes
- Agent entries are read-only (no edit button)
- Empty state shows when no entries

**Backend (extend existing tests in `apps/journal/tests.py`):**
- Roundtrip: POST entry via user API → GET via runtime API → verify markdown format
- Roundtrip: POST via runtime append → GET via user API → verify structured entries
- Verify `GET /journal-context/` returns last 7 days + memory
- Tenant isolation on all new endpoints

---

## Files to Create/Modify

### Create:
- `frontend/app/memory/page.tsx` — Memory page
- `frontend/components/journal/timeline.tsx` — Timeline component
- `frontend/components/journal/entry-card.tsx` — Single entry card
- `frontend/components/journal/entry-input.tsx` — Add entry input
- `frontend/components/journal/date-nav.tsx` — Date navigator
- `frontend/components/journal/evening-checkin.tsx` — Evening check-in card variant
- `frontend/components/journal/mood-energy.tsx` — Mood/energy display + input components

### Modify:
- `frontend/app/page.tsx` — Replace entirely with timeline view
- `frontend/components/app-shell.tsx` — Add Memory nav item
- `frontend/lib/types.ts` — Add new types
- `frontend/lib/api.ts` — Add new API functions
- `frontend/lib/queries.ts` — Add new hooks
- `apps/journal/views.py` — Verify response shapes match spec above
- `apps/journal/serializers.py` — Verify/fix serializer output

### Do NOT modify:
- `apps/journal/models.py` — Models are correct (DailyNote + UserMemory)
- `apps/journal/md_utils.py` — Parser is correct
- `apps/integrations/runtime_views.py` — Runtime API is correct
- Any migration files
- `apps/journal/models.py` old models (JournalEntry, WeeklyReview) — leave them

---

## Design Guidelines

- Use existing Tailwind classes and component patterns from the codebase
- Match the existing design language (see `SectionCard`, `StatusPill` components)
- Mobile-first — this will primarily be used on phones
- Keep it clean and minimal — no unnecessary decoration
- Agent entries: use a subtle background tint (e.g., `bg-blue-50` or `bg-slate-50`) to distinguish from user entries
- Evening check-in: use `bg-indigo-50` or similar with a 🌙 icon
- The add-entry input should feel like a chat input, not a form. One line, expandable.
- Energy: render as a horizontal bar or pill (1-10 scale), not a dropdown
- Mood: render the emoji directly, don't convert to text

---

## Git Instructions

- All work on branch `feature/journaling-refactor`
- Commit with clear messages (e.g., `feat(journal): replace form UI with timeline view`)
- Do NOT merge to main
- Do NOT modify files outside the scope listed above
