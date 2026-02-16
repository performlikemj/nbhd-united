"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  appendToDocument,
  createAutomation,
  createDailyNoteEntry,
  createDocument,
  createTemplate,
  createJournalEntry,
  createWeeklyReview,
  deleteAutomation,
  deleteDailyNoteEntry,
  deleteTemplate,
  deleteJournalEntry,
  deleteWeeklyReview,
  disconnectIntegration,
  fetchAutomationRuns,
  fetchAutomationRunsForAutomation,
  fetchAutomations,
  fetchDailyNote,
  fetchDashboard,
  fetchDocument,
  fetchDocuments,
  fetchIntegrations,
  fetchJournalEntries,
  fetchMe,
  fetchMemory,
  fetchPersonas,
  fetchPreferences,
  fetchSidebarTree,
  fetchTenant,
  fetchTemplates,
  fetchTelegramStatus,
  fetchUsageHistory,
  fetchUsageSummary,
  fetchWeeklyReviews,
  generateTelegramLink,
  getLLMConfig,
  getOAuthAuthorizeUrl,
  onboardTenant,
  pauseAutomation,
  resumeAutomation,
  runAutomationNow,
  requestStripeCheckout,
  requestStripePortal,
  unlinkTelegram,
  updateAutomation,
  updateDailyNoteSection,
  updateDailyNoteTemplate,
  updateDailyNoteEntry,
  updateLLMConfig,
  updateDocument,
  updateJournalEntry,
  updateMemory,
  updatePreferences,
  updateTemplate,
  updateWeeklyReview,
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

// Journal (legacy)
/** @deprecated */
export function useJournalEntriesQuery() {
  return useQuery({
    queryKey: ["journal-entries"],
    queryFn: () => fetchJournalEntries(),
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

// Daily Notes
export function useDailyNoteQuery(date: string) {
  return useQuery({
    queryKey: ["daily-note", date],
    queryFn: () => fetchDailyNote(date),
    enabled: !!date,
    refetchInterval: 30_000,
  });
}

export function useCreateDailyNoteEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: Parameters<typeof createDailyNoteEntry>[1]) =>
      createDailyNoteEntry(date, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useUpdateDailyNoteEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ index, data }: { index: number; data: Parameters<typeof updateDailyNoteEntry>[2] }) =>
      updateDailyNoteEntry(date, index, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useDeleteDailyNoteEntryMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (index: number) => deleteDailyNoteEntry(date, index),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useUpdateDailyNoteTemplateMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (
      data: Parameters<typeof updateDailyNoteTemplate>[1],
    ) => updateDailyNoteTemplate(date, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

export function useUpdateDailyNoteSectionMutation(date: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, content }: { slug: string; content: string }) =>
      updateDailyNoteSection(date, slug, content),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["daily-note", date] });
    },
  });
}

// User Memory
export function useMemoryQuery() {
  return useQuery({
    queryKey: ["memory"],
    queryFn: fetchMemory,
  });
}

export function useUpdateMemoryMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateMemory,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory"] });
    },
  });
}

// Templates
export function useNoteTemplatesQuery() {
  return useQuery({
    queryKey: ["templates"],
    queryFn: fetchTemplates,
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
