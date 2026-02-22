"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ProvisioningStatus, RefreshConfigStatus } from "@/lib/types";
import {
  appendToDocument,
  createAutomation,
  createCronJob,
  createDocument,
  createTemplate,
  createJournalEntry,
  createWeeklyReview,
  deleteAutomation,
  deleteCronJob,
  deleteTemplate,
  deleteJournalEntry,
  deleteWeeklyReview,
  disconnectIntegration,
  fetchAutomationRuns,
  fetchAutomationRunsForAutomation,
  fetchAutomations,
  fetchCronJobs,
  fetchDashboard,
  fetchDocument,
  fetchDocuments,
  fetchIntegrations,
  fetchJournalEntries,
  fetchMe,
  fetchPersonas,
  fetchPreferences,
  fetchProvisioningStatus,
  fetchRefreshConfigStatus,
  fetchSidebarTree,
  fetchTenant,
  fetchTemplates,
  fetchTelegramStatus,
  fetchUsageHistory,
  fetchUsageSummary,
  fetchTransparency,
  fetchWeeklyReviews,
  updateProfile,
  generateTelegramLink,
  getLLMConfig,
  getOAuthAuthorizeUrl,
  onboardTenant,
  pauseAutomation,
  resumeAutomation,
  runAutomationNow,
  requestStripeCheckout,
  requestStripePortal,
  toggleCronJob,
  unlinkTelegram,
  updateAutomation,
  updateCronJob,
  updateLLMConfig,
  updateDocument,
  updateJournalEntry,
  updatePreferences,
  refreshConfig,
  retryProvisioning,
  updateTemplate,
  updateWeeklyReview,
} from "@/lib/api";

export function useMeQuery() {
  return useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useUpdateProfileMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: updateProfile,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useTenantQuery() {
  return useQuery({
    queryKey: ["tenant"],
    queryFn: fetchTenant,
    staleTime: 5 * 60_000,
  });
}

export function useDashboardQuery() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: fetchDashboard,
    staleTime: 30_000,
  });
}

export function useUsageHistoryQuery() {
  return useQuery({
    queryKey: ["usage-history"],
    queryFn: fetchUsageHistory,
    staleTime: 30_000,
  });
}

export function useUsageSummaryQuery() {
  return useQuery({
    queryKey: ["usage-summary"],
    queryFn: fetchUsageSummary,
    staleTime: 30_000,
  });
}

export function useTransparencyQuery() {
  return useQuery({
    queryKey: ["usage-transparency"],
    queryFn: fetchTransparency,
    staleTime: 5 * 60_000,
  });
}

export function useIntegrationsQuery() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: fetchIntegrations,
    staleTime: 5 * 60_000,
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
    enabled,
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
    staleTime: Infinity,
  });
}

export function usePreferencesQuery() {
  return useQuery({
    queryKey: ["preferences"],
    queryFn: fetchPreferences,
    staleTime: 5 * 60_000,
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

export function useRefreshConfigStatusQuery() {
  return useQuery<RefreshConfigStatus>({
    queryKey: ["refresh-config-status"],
    queryFn: fetchRefreshConfigStatus,
  });
}

export function useRefreshConfigMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: refreshConfig,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["refresh-config-status"] });
    },
  });
}

export function useProvisioningStatusQuery(enabled = true) {
  return useQuery<ProvisioningStatus>({
    queryKey: ["provisioning-status"],
    queryFn: fetchProvisioningStatus,
    enabled,
    refetchInterval: (query) => (query.state.data?.ready ? false : 5000),
  });
}

export function useRetryProvisioningMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: retryProvisioning,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["provisioning-status"] });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
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

// Journal (legacy)
/** @deprecated */
export function useJournalEntriesQuery() {
  return useQuery({
    queryKey: ["journal-entries"],
    queryFn: () => fetchJournalEntries(),
    staleTime: 30_000,
  });
}

/** @deprecated */
export function useCreateJournalEntryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createJournalEntry,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal-entries"] });
    },
  });
}

/** @deprecated */
export function useUpdateJournalEntryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof updateJournalEntry>[1] }) =>
      updateJournalEntry(id, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal-entries"] });
    },
  });
}

/** @deprecated */
export function useDeleteJournalEntryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteJournalEntry,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["journal-entries"] });
    },
  });
}

// Templates
export function useNoteTemplatesQuery() {
  return useQuery({
    queryKey: ["templates"],
    queryFn: fetchTemplates,
    staleTime: Infinity,
  });
}

export function useCreateNoteTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createTemplate,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["templates"] });
    },
  });
}

export function useUpdateNoteTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof updateTemplate>[1] }) =>
      updateTemplate(id, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["templates"] });
    },
  });
}

export function useDeleteNoteTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteTemplate,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["templates"] });
    },
  });
}

// Weekly Reviews
export function useWeeklyReviewsQuery() {
  return useQuery({
    queryKey: ["weekly-reviews"],
    queryFn: fetchWeeklyReviews,
  });
}

export function useCreateWeeklyReviewMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createWeeklyReview,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["weekly-reviews"] });
    },
  });
}

export function useUpdateWeeklyReviewMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof updateWeeklyReview>[1] }) =>
      updateWeeklyReview(id, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["weekly-reviews"] });
      void queryClient.invalidateQueries({ queryKey: ["weekly-review"] });
    },
  });
}

export function useDeleteWeeklyReviewMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteWeeklyReview,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["weekly-reviews"] });
    },
  });
}

// ── Journal v2 Documents ──────────────────────────────────────────────

export function useDocumentQuery(kind: string, slug: string) {
  return useQuery({
    queryKey: ["document", kind, slug],
    queryFn: () => fetchDocument(kind, slug),
    enabled: !!kind && !!slug,
    refetchInterval: 30_000,
  });
}

export function useDocumentsQuery(kind?: string) {
  return useQuery({
    queryKey: ["documents", kind ?? "all"],
    queryFn: () => fetchDocuments(kind),
    staleTime: 30_000,
  });
}

export function useSidebarTreeQuery() {
  return useQuery({
    queryKey: ["sidebar-tree"],
    queryFn: fetchSidebarTree,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

export function useUpdateDocumentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, slug, data }: { kind: string; slug: string; data: { markdown?: string; title?: string } }) =>
      updateDocument(kind, slug, data),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["document", variables.kind, variables.slug] });
      void queryClient.invalidateQueries({ queryKey: ["sidebar-tree"] });
    },
  });
}

export function useAppendDocumentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, slug, content }: { kind: string; slug: string; content: string }) =>
      appendToDocument(kind, slug, content),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["document", variables.kind, variables.slug] });
      void queryClient.invalidateQueries({ queryKey: ["sidebar-tree"] });
    },
  });
}

export function useCreateDocumentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createDocument,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["documents"] });
      void queryClient.invalidateQueries({ queryKey: ["sidebar-tree"] });
    },
  });
}

export function useLLMConfigQuery() {
  return useQuery({
    queryKey: ["llm-config"],
    queryFn: getLLMConfig,
    staleTime: 5 * 60_000,
  });
}

export function useUpdateLLMConfigMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateLLMConfig,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["llm-config"] });
    },
  });
}

// Cron Jobs
export function useCronJobsQuery() {
  return useQuery({
    queryKey: ["cron-jobs"],
    queryFn: fetchCronJobs,
    staleTime: 30_000,
  });
}

export function useCreateCronJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createCronJob,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useUpdateCronJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, jobId, data }: { name: string; jobId?: string; data: Parameters<typeof updateCronJob>[1] }) =>
      updateCronJob(jobId ?? name, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useDeleteCronJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, jobId }: { name: string; jobId?: string }) =>
      deleteCronJob(jobId ?? name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useToggleCronJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, jobId, enabled }: { name: string; jobId?: string; enabled: boolean }) =>
      toggleCronJob(jobId ?? name, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}
