# Lesson Learned: Filtered Select Not Firing onChange

**Date:** 2026-02-19
**Component:** `frontend/components/timezone-selector.tsx`
**Fix commit:** `3c06d91`

## The Bug

Users could search for a timezone (e.g. type "New"), see `America/New_York` appear as the only option in the dropdown, click Save — and the original timezone was sent instead.

## Root Cause

Classic **controlled `<select>` + filter** bug in React.

When a `<select>` has a `value` prop that doesn't match any of the currently rendered `<option>` elements (because filtering hid it), the browser **visually displays the first available option** — but React's `onChange` never fires because the user didn't actually change the selection. The dropdown just *looks* like it changed.

```
State: value="Asia/Tokyo"
Filter: "New" → options reduced to [America/New_York]
Browser shows: America/New_York (first available)
User thinks: "New York is selected"
User clicks Save
Actual value sent: Asia/Tokyo ← onChange never fired
```

## The Fix

Added a `useEffect` that watches the filtered options list. When the current value is no longer in the filtered list, it auto-calls `onChange` with the first available option — syncing what the user sees with what the form state holds.

```tsx
useEffect(() => {
  if (filteredTimezones.length > 0 && !filteredTimezones.includes(currentValue)) {
    onChange(filteredTimezones[0]);
  }
}, [filteredTimezones, currentValue, onChange]);
```

## Debugging Timeline

1. **Initial assumption:** Backend not persisting `tz` → Added logging → Backend was fine (200 OK, correct tz in response)
2. **Second assumption:** Frontend cache stale → Checked React Query invalidation → Working correctly
3. **Third assumption:** Auth issues (401s in logs) → Red herring, was a momentary token refresh
4. **Breakthrough:** Added gunicorn access logs + detailed tz logging to see `patch_tz=Asia/Tokyo` when user selected `America/New_York` → Problem was clearly frontend form state
5. **Root cause found:** Traced the `TimezoneSelector` component — filtered `<select>` with controlled `value` prop

## Key Takeaways

1. **Controlled `<select>` + dynamic options is a known React footgun.** Whenever you filter a `<select>`'s options, you must handle the case where the current `value` disappears from the list. The browser will show the first option but React won't fire `onChange`.

2. **Add observability early.** We couldn't see successful requests in Azure logs because gunicorn had no access logging. Adding `--access-logfile -` to gunicorn immediately revealed the request flow. This should be on by default.

3. **Log both sides of a proxy.** When Django proxies to the Gateway, logging the request payload AND the response payload made it instantly clear that the wrong value was being sent — not that the backend was dropping it.

4. **Don't trust the UI.** The dropdown looked correct to the user. The form state disagreed. When a user says "it's not saving," always verify what the frontend is actually sending before investigating the backend.

## Applies To

Any filtered/searchable `<select>` or dropdown with a controlled value in React (or any framework with one-way data binding). Same issue can occur with comboboxes, autocompletes, or custom dropdown components where the visible options change dynamically.
