"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactNode, useEffect, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import { GlobalToastHost, emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import { installPersistence, seedQueryClient } from "@/lib/query-persist";

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60_000,
            refetchOnWindowFocus: true,
            retry: 1,
          },
          mutations: {
            // 401 is handled by apiFetch (it redirects to /login); skip the
            // toast there so users don't see a stale error during redirect.
            // Mutations can opt out of this by setting meta.skipErrorToast.
            onError: (err, _vars, _ctx, mutation) => {
              if (mutation?.meta?.skipErrorToast) return;
              const status = (err as Error & { status?: number })?.status;
              if (status === 401) return;
              emitToast(getErrorMessage(err), "error");
            },
          },
        },
      })
  );

  useEffect(() => {
    seedQueryClient(queryClient);
    return installPersistence(queryClient);
  }, [queryClient]);

  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        {children}
        <GlobalToastHost />
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
