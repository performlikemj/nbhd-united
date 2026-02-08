# Task Ledger: Frontend Scaffold

Parent: `CONTINUITY.md`
Root: `CONTINUITY.md`
Related: `frontend/README.md`, `README.md`, `frontend/**`
Owner: `codex`

## Goal
- Scaffold the `frontend/` Next.js app as described in README: App Router + TypeScript + Tailwind with initial pages for onboarding, integrations, usage, and billing.

## Constraints / Assumptions
- Frontend is a separate build from Django backend.
- Keep scaffolding minimal but runnable and easy to extend.
- Use explicit API env vars; do not hardcode secrets.

## Key Decisions
- 2026-02-08: Hand-scaffold files instead of running `create-next-app` to avoid interactive/network setup issues.
- 2026-02-08: Use offline-safe local font stacks instead of Google font fetching so build is deterministic in restricted environments.

## State
- Done:
  - Created complete Next.js scaffold in `frontend/` with TypeScript, Tailwind, and React Query.
  - Implemented initial routes: `/`, `/onboarding`, `/integrations`, `/usage`, `/billing`.
  - Added shared app shell/components and typed API/query layer targeting Django endpoints.
  - Updated frontend docs and root structure note.
  - Validation successful:
    - `npm run lint` passes.
    - `npm run build` passes.
- Now:
  - Awaiting user review/next iteration.
- Next:
  - Optional: wire real auth/session handling and Stripe portal endpoint integration once backend API contracts are finalized.

## Links
- Upstream:
  - `CONTINUITY.md`
- Downstream:
  - None.
- Related:
  - `frontend/README.md`

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Preferred auth mechanism for frontend → backend API calls (cookie/session vs JWT) for production.

## Working Set
- Files:
  - `frontend/package.json`
  - `frontend/package-lock.json`
  - `frontend/next.config.mjs`
  - `frontend/tsconfig.json`
  - `frontend/tailwind.config.ts`
  - `frontend/postcss.config.mjs`
  - `frontend/.eslintrc.json`
  - `frontend/.gitignore`
  - `frontend/.env.example`
  - `frontend/app/**`
  - `frontend/components/**`
  - `frontend/lib/**`
  - `frontend/README.md`
  - `README.md`
- Commands:
  - `npm install`
  - `npm run lint`
  - `npm run build`

## Notes
- UI intentionally avoids a default boilerplate look and includes a clear color direction, gradient/grid background, and light reveal animations.
