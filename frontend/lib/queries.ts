"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createAutomation,
  deleteAutomation,
  disconnectIntegration,
  fetchAutomationRuns,
  fetchAutomationRunsForAutomation,
  fetchAutomations,
  fetchDashboard,
  fetchIntegrations,
  fetchMe,
  fetchPersonas,
  fetchPreferences,
  fetchTenant,
  fetchTelegramStatus,
  fetchUsageHistory,
  fetchUsageSummary,
  generateTelegramLink,
  getOAuthAuthorizeUrl,
  onboardTenant,
  pauseAutomation,
  resumeAutomation,
  runAutomationNow,
  requestStripeCheckout,
  requestStripePortal,
  unlinkTelegram,
  updateAutomation,
  updatePreferences,
} from "@/lib/api";

export function useMeQuery() {
  return useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    retry: false,
  });
}

export function useTenantQuery() {
  return useQuery({
    queryKey: ["tenant"],
    queryFn: fetchTenant,
  });
}

export function useDashboardQuery() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: fetchDashboard,
  });
}

export function useUsageHistoryQuery() {
  return useQuery({
    queryKey: ["usage-history"],
    queryFn: fetchUsageHistory,
  });
}

export function useUsageSummaryQuery() {
  return useQuery({
    queryKey: ["usage-summary"],
    queryFn: fetchUsageSummary,
  });
}

export function useIntegrationsQuery() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: fetchIntegrations,
  });
}

export function useOnboardMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: onboardTenant,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function useCheckoutMutation() {
  return useMutation({
    mutationFn: requestStripeCheckout,
  });
}

export function useOAuthAuthorizeMutation() {
  return useMutation({
    mutationFn: getOAuthAuthorizeUrl,
  });
}

export function useDisconnectIntegrationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: disconnectIntegration,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["integrations"] });
    },
  });
}

export function useStripePortalMutation() {
  return useMutation({
    mutationFn: requestStripePortal,
  });
}

// Telegram
export function useTelegramStatusQuery(enabled = true) {
  return useQuery({
    queryKey: ["telegram-status"],
    queryFn: fetchTelegramStatus,
    refetchInterval: enabled
      ? (query) => (query.state.status === "error" ? false : 3000)
      : false,
  });
}

export function useGenerateTelegramLinkMutation() {
  return useMutation({
    mutationFn: generateTelegramLink,
  });
}

export function useUnlinkTelegramMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: unlinkTelegram,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["telegram-status"] });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

// Personas & Preferences
export function usePersonasQuery() {
  return useQuery({
    queryKey: ["personas"],
    queryFn: fetchPersonas,
  });
}

export function usePreferencesQuery() {
  return useQuery({
    queryKey: ["preferences"],
    queryFn: fetchPreferences,
  });
}

export function useUpdatePreferencesMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updatePreferences,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["preferences"] });
    },
  });
}

// Automations
export function useAutomationsQuery() {
  return useQuery({
    queryKey: ["automations"],
    queryFn: fetchAutomations,
  });
}

export function useAutomationRunsQuery(automationId?: string) {
  return useQuery({
    queryKey: ["automation-runs", automationId ?? "all"],
    queryFn: () =>
      automationId ? fetchAutomationRunsForAutomation(automationId) : fetchAutomationRuns(),
  });
}

export function useCreateAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createAutomation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      void queryClient.invalidateQueries({ queryKey: ["automation-runs"] });
    },
  });
}

export function useUpdateAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof updateAutomation>[1] }) =>
      updateAutomation(id, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      void queryClient.invalidateQueries({ queryKey: ["automation-runs"] });
    },
  });
}

export function useDeleteAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteAutomation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      void queryClient.invalidateQueries({ queryKey: ["automation-runs"] });
    },
  });
}

export function usePauseAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: pauseAutomation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
    },
  });
}

export function useResumeAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: resumeAutomation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
    },
  });
}

export function useRunAutomationMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: runAutomationNow,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["automations"] });
      void queryClient.invalidateQueries({ queryKey: ["automation-runs"] });
    },
  });
}
