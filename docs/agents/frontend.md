# Frontend (Next.js console) gotchas

Design system: **`DESIGN.md`** at repo root (tokens, typography, components, do's/don'ts) — read it before any visual work. Source of truth for tokens is `frontend/app/globals.css` with Tailwind mappings in `frontend/tailwind.config.ts`; keep DESIGN.md aligned when those change. It supersedes `frontend/BRAND_GUIDE.md` (pre-Constellation, out of date).

## Build model

- Static export (`npm run build` → `out/`), served by Azure Static Web Apps. **No SSR** — nothing server-only will run.
- **No test runner is wired** (no jest/vitest, no `test` script). CI's frontend gate is `npm run lint` (eslint) + `next build`. Verify logic with `npx tsc --noEmit` + eslint + careful review; anything genuinely testable belongs in Django tests.

## Lint rules that WILL fail CI (react-hooks v6, compiler-aware)

- `react-hooks/refs` — no computed writes to `ref.current` during render (object literals). The plain "latest value" idiom (`const xRef = useRef(x); xRef.current = x;`) is tolerated; composites sync in a `useEffect`.
- `react-hooks/set-state-in-effect` — no `setState()` directly in an effect body. For "derive state from localStorage once on mount": do it during render behind a state flag (`if (data && !initialized) { setInitialized(true); ... }`), not in an effect.

## State & data

- Auth is a Bearer token in localStorage (`nbhd_access_token`).
- React Query cache **persists to localStorage** (`nbhd_qc_v3`, `lib/query-persist.ts`) and rehydrates synchronously before mount — "stale data after deploy" reports are usually this cache, not cookies or the API.

## Layout traps

- `.content-fade-up` (any transformed ancestor) becomes the containing block for `position: fixed` — new full-screen overlays must size with `vw`/`dvh`, not `inset: 0`.
- `flex-1` children inside `flex-col` need `min-h-0` on the parent chain to actually shrink.

## Dependencies

- TipTap: pin `@tiptap/core ^3.25.0`; don't full-regen `package-lock.json` to fix an ERESOLVE — targeted pin only.

## Standards

Mobile-first; WCAG 2.1 AA (4.5:1 contrast, 44×44px touch targets); respect `prefers-reduced-motion`; tokens/CSS variables only, never hardcoded hex.
