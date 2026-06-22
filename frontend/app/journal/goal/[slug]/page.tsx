import GoalDetailClient from "./goal-detail-client";

// Static export requires every dynamic segment to be enumerated at build time.
// Goal slugs are per-tenant and dynamic (typed:<uuid> or a legacy Document
// slug), so we emit a single placeholder param to generate the route shell.
// At runtime the real slug is read from the pathname and the page is fully
// client-fetched — Azure Static Web Apps' navigationFallback (→ /index.html)
// serves this shell for any /journal/goal/<slug> path. See FA-0341.
//
// generateStaticParams must live in this SERVER component; the interactive
// logic lives in the sibling "use client" GoalDetailClient (a page cannot be
// both "use client" and export generateStaticParams).
export function generateStaticParams() {
  return [{ slug: "_" }];
}

export default function GoalDetailPage() {
  return <GoalDetailClient />;
}
