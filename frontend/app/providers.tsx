"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactNode, useEffect, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import { GlobalToastHost, emitToast } from "@/components/toast";
import { WebVitals } from "@/components/web-vitals";
import { getErrorMessage } from "@/lib/errors";
import { installPersistence, seedQueryClient } from "@/lib/query-persist";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(() => {
    const qc = new QueryClient({
      defaultOptions: {
        queries: {
          staleTime: 60_000,
          // Keep detached queries in memory for the session so navigating
          // away and back doesn't drop warm data. Persistence covers
          // cross-session; this covers within-session.
          gcTime: 24 * 60 * 60 * 1000,
          refetchOnWindowFocus: true,
          retry: 1,
        },
        mutations: {
          // 401 is handled by apiFetch (redirects to /login); skip the
          // toast there so users don't see a stale error during redirect.
          // Mutations can opt out of this by setting meta.skipErrorToast
          // (e.g. when they render their own inline error UI).
          onError: (err, _vars, _ctx, mutation) => {
            if (mutation?.meta?.skipErrorToast) return;
            const status = (err as Error & { status?: number })?.status;
            if (status === 401) return;
            emitToast(getErrorMessage(err), "error");
          },
        },
      },
    });
    // Hydrate synchronously before any child mounts, so components mount
    // with cache already populated and observers skip the fetch.
    seedQueryClient(qc);
    return qc;
  });

  useEffect(() => installPersistence(queryClient), [queryClient]);

  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <WebVitals />
        {children}
        <GlobalToastHost />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
