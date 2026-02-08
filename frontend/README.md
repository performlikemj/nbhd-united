# Frontend — Next.js Subscriber Console

Separate frontend build for NBHD United subscriber self-service workflows.

## Included Scaffold

- App Router + TypeScript + Tailwind baseline
- React Query provider + typed API client
- Initial routes:
  - `/` home snapshot and quick actions
  - `/onboarding` onboarding status checklist
  - `/integrations` OAuth integration management surface
  - `/usage` token/message usage and budget view
  - `/billing` Stripe portal and subscription controls

## Stack

- Next.js 14+ (App Router)
- Tailwind CSS
- React Query
- TypeScript

## Setup

```bash
cd frontend
cp .env.example .env.local
npm install
npm run dev
```

Default API base URL in `.env.local`:

```bash
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

This frontend remains a separate deployment artifact from the Django backend.
