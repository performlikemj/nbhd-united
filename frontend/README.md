# Frontend — Next.js

Self-service dashboard for NBHD United subscribers.

## Features (planned)

- Onboarding wizard (connect Telegram, subscribe via Stripe)
- OAuth connections (Gmail, Google Calendar)
- Usage dashboard (messages, tokens, cost)
- Subscription management (Stripe Customer Portal)

## Stack

- Next.js 14+ (App Router)
- Tailwind CSS
- React Query
- Stripe Checkout

## Setup

```bash
npx create-next-app@latest . --typescript --tailwind --app
npm install
npm run dev
```

This is a separate build from the Django backend.
