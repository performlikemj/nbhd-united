"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isLoggedIn } from "@/lib/auth";
import {
  AuthUser,
  CronJob,
  Integration,
  ProvisioningStatus,
  RefreshConfigStatus,
  Tenant,
  TransparencyData,
  WorkspacesResponse,
} from "@/lib/types";
import {
  appendToDocument,
  bulkDeleteCronJobs,
  bulkUpdateForeground,
  createAutomation,
  createCronJob,
  createDocument,
  createTemplate,
  createJournalEntry,
  createWeeklyReview,
  deleteAutomation,
  deleteCronJob,
  clearDocument,
  deleteDocument,
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
  fetchLineStatus,
  generateLineLink,
  unlinkLine,
  setPreferredChannel,
  approveExtraction,
  dismissExtraction,
  fetchHorizons,
  fetchUsageHistory,
  fetchUsageSummary,
  fetchTransparency,
  updateDonationPreference,
  updatePreferredModel,
  updateTaskModelPreferences,
  fetchWeeklyReviews,
  updateProfile,
  generateTelegramLink,
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
  updateDocument,
  updateJournalEntry,
  updatePreferences,
  refreshConfig,
  retryProvisioning,
  updateTemplate,
  updateWeeklyReview,
  deleteAccount,
  cancelAccountDeletion,
  fetchWorkingHours,
  updateWorkingHours,
  fetchFinanceDashboard,
  fetchArchivedFinanceAccounts,
  deleteFinanceAccount,
  unarchiveFinanceAccount,
  updateFinanceSettings,
  fetchFuelCalendar,
  fetchWorkouts,
  fetchWorkout,
  createWorkout,
  updateWorkout,
  deleteWorkout,
  fetchFuelProgress,
  fetchBodyWeight,
  createBodyWeight,
  updateFuelSettings,
  fetchWorkspaces,
  createWorkspace,
  updateWorkspace,
  deleteWorkspace,
  switchWorkspace,
} from "@/lib/api";

export function useMeQuery() {
  return useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 5 * 60_000,
    retry: false,
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn(),
  });
}

export function useDashboardQuery() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: fetchDashboard,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useUsageHistoryQuery() {
  return useQuery({
    queryKey: ["usage-history"],
    queryFn: fetchUsageHistory,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useHorizonsQuery() {
  return useQuery({
    queryKey: ["horizons"],
    queryFn: fetchHorizons,
    staleTime: 60_000,
    enabled: isLoggedIn(),
  });
}

export function useApproveExtractionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: approveExtraction,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["horizons"] });
    },
  });
}

export function useDismissExtractionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: dismissExtraction,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["horizons"] });
    },
  });
}

export function useUsageSummaryQuery() {
  return useQuery({
    queryKey: ["usage-summary"],
    queryFn: fetchUsageSummary,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useTransparencyQuery() {
  return useQuery({
    queryKey: ["usage-transparency"],
    queryFn: fetchTransparency,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useDonationPreferenceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateDonationPreference,
    onMutate: async (newData: { donation_enabled?: boolean; donation_percentage?: number }) => {
      await queryClient.cancelQueries({ queryKey: ["usage-transparency"] });
      const previous = queryClient.getQueryData<TransparencyData>(["usage-transparency"]);
      queryClient.setQueryData<TransparencyData>(["usage-transparency"], (old) =>
        old ? { ...old, ...newData } : old,
      );
      return { previous };
    },
    onError: (_err, _newData, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["usage-transparency"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["usage-transparency"] });
    },
  });
}

export function usePreferredModelMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updatePreferredModel,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function useTaskModelPreferencesMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateTaskModelPreferences,
    onMutate: async (newPrefs: Record<string, string>) => {
      await queryClient.cancelQueries({ queryKey: ["tenant"] });
      const previous = queryClient.getQueryData<Tenant>(["tenant"]);
      queryClient.setQueryData<Tenant>(["tenant"], (old) =>
        old ? { ...old, task_model_preferences: { ...old.task_model_preferences, ...newPrefs } } : old,
      );
      return { previous };
    },
    onError: (_err, _newPrefs, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["tenant"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function useIntegrationsQuery() {
  return useQuery({
    queryKey: ["integrations"],
    queryFn: fetchIntegrations,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
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
    onMutate: async (integrationId: string) => {
      await queryClient.cancelQueries({ queryKey: ["integrations"] });
      const previous = queryClient.getQueryData<Integration[]>(["integrations"]);
      queryClient.setQueryData<Integration[]>(["integrations"], (old) =>
        old ? old.filter((i) => i.id !== integrationId) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["integrations"], context.previous);
      }
    },
    onSettled: () => {
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
    enabled: isLoggedIn() && enabled,
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
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["telegram-status"] });
      const previous = queryClient.getQueryData<Record<string, unknown>>(["telegram-status"]);
      queryClient.setQueryData(["telegram-status"], (old: Record<string, unknown> | undefined) =>
        old ? { ...old, linked: false, telegram_username: "" } : old,
      );
      return { previous };
    },
    onError: (_err, _input, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["telegram-status"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["telegram-status"] });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

// LINE
export function useLineStatusQuery(enabled = true) {
  return useQuery({
    queryKey: ["line-status"],
    queryFn: fetchLineStatus,
    enabled: isLoggedIn() && enabled,
    refetchInterval: enabled
      ? (query) => (query.state.status === "error" ? false : 3000)
      : false,
  });
}

export function useGenerateLineLinkMutation() {
  return useMutation({
    mutationFn: generateLineLink,
  });
}

export function useUnlinkLineMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: unlinkLine,
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ["line-status"] });
      const previous = queryClient.getQueryData<Record<string, unknown>>(["line-status"]);
      queryClient.setQueryData(["line-status"], (old: Record<string, unknown> | undefined) =>
        old ? { ...old, linked: false, line_display_name: "" } : old,
      );
      return { previous };
    },
    onError: (_err, _input, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["line-status"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["line-status"] });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useSetPreferredChannelMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (channel: "telegram" | "line") => setPreferredChannel(channel),
    onMutate: async (channel: "telegram" | "line") => {
      await queryClient.cancelQueries({ queryKey: ["me"] });
      const previous = queryClient.getQueryData<AuthUser>(["me"]);
      queryClient.setQueryData<AuthUser>(["me"], (old) =>
        old ? { ...old, preferred_channel: channel } : old,
      );
      return { previous };
    },
    onError: (_err, _channel, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["me"], context.previous);
      }
    },
    onSettled: () => {
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
    enabled: isLoggedIn(),
  });
}

export function usePreferencesQuery() {
  return useQuery({
    queryKey: ["preferences"],
    queryFn: fetchPreferences,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn(),
    // Poll every 15s when an update is pending so the UI reflects when it's applied.
    // Fall back to every 60s otherwise (catches cron-triggered pending bumps).
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 15_000;
      return data.has_pending_update ? 15_000 : 60_000;
    },
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
    enabled: isLoggedIn() && enabled,
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
    enabled: isLoggedIn(),
  });
}

export function useAutomationRunsQuery(automationId?: string) {
  return useQuery({
    queryKey: ["automation-runs", automationId ?? "all"],
    queryFn: () =>
      automationId ? fetchAutomationRunsForAutomation(automationId) : fetchAutomationRuns(),
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn(),
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
    enabled: isLoggedIn() && !!kind && !!slug,
    refetchInterval: 30_000,
  });
}

export function useDocumentsQuery(kind?: string) {
  return useQuery({
    queryKey: ["documents", kind ?? "all"],
    queryFn: () => fetchDocuments(kind),
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useSidebarTreeQuery() {
  return useQuery({
    queryKey: ["sidebar-tree"],
    queryFn: fetchSidebarTree,
    staleTime: 30_000,
    refetchInterval: 60_000,
    enabled: isLoggedIn(),
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

export function useDeleteDocumentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, slug }: { kind: string; slug: string }) => deleteDocument(kind, slug),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["documents"] });
      void queryClient.invalidateQueries({ queryKey: ["sidebar-tree"] });
      // Remove cached document entry directly
      queryClient.removeQueries({ queryKey: ["document", variables.kind, variables.slug] });
    },
  });
}

export function useClearDocumentMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ kind, slug }: { kind: string; slug: string }) => clearDocument(kind, slug),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["documents"] });
      void queryClient.invalidateQueries({ queryKey: ["sidebar-tree"] });
      void queryClient.invalidateQueries({ queryKey: ["document", variables.kind, variables.slug] });
    },
  });
}

// Cron Jobs
export function useCronJobsQuery() {
  return useQuery({
    queryKey: ["cron-jobs"],
    queryFn: fetchCronJobs,
    staleTime: 30_000,
    enabled: isLoggedIn(),
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
    onError: () => {
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
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useToggleCronJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ name, jobId, enabled }: { name: string; jobId?: string; enabled: boolean }) =>
      toggleCronJob(jobId ?? name, enabled),
    onMutate: async ({ name, jobId, enabled }) => {
      await queryClient.cancelQueries({ queryKey: ["cron-jobs"] });
      const previous = queryClient.getQueryData<CronJob[]>(["cron-jobs"]);
      queryClient.setQueryData<CronJob[]>(["cron-jobs"], (old) =>
        old?.map((job) =>
          job.jobId === (jobId ?? name) || job.name === name ? { ...job, enabled } : job,
        ),
      );
      return { previous };
    },
    onError: (_err, _input, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["cron-jobs"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useBulkDeleteCronJobsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => bulkDeleteCronJobs(ids),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

export function useBulkUpdateForegroundMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, foreground }: { ids: string[]; foreground: boolean }) =>
      bulkUpdateForeground(ids, foreground),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["cron-jobs"] });
    },
  });
}

// Workspaces
export function useWorkspacesQuery() {
  return useQuery({
    queryKey: ["workspaces"],
    queryFn: fetchWorkspaces,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createWorkspace,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useUpdateWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, data }: { slug: string; data: Parameters<typeof updateWorkspace>[1] }) =>
      updateWorkspace(slug, data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useDeleteWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => deleteWorkspace(slug),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useSwitchWorkspaceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => switchWorkspace(slug),
    onMutate: async (slug: string) => {
      await queryClient.cancelQueries({ queryKey: ["workspaces"] });
      const previous = queryClient.getQueryData<WorkspacesResponse>(["workspaces"]);
      queryClient.setQueryData<WorkspacesResponse>(["workspaces"], (old) =>
        old
          ? {
              ...old,
              workspaces: old.workspaces.map((ws) => ({ ...ws, is_active: ws.slug === slug })),
            }
          : old,
      );
      return { previous };
    },
    onError: (_err, _slug, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["workspaces"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
}

export function useDeleteAccountMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteAccount(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useCancelDeletionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => cancelAccountDeletion(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

// Working Hours
export function useWorkingHoursQuery() {
  return useQuery({
    queryKey: ["working-hours"],
    queryFn: fetchWorkingHours,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useUpdateWorkingHoursMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateWorkingHours,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["working-hours"] });
    },
  });
}

// Finance
export function useFinanceDashboardQuery() {
  return useQuery({
    queryKey: ["finance-dashboard"],
    queryFn: fetchFinanceDashboard,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useArchivedFinanceAccountsQuery(enabled: boolean = false) {
  return useQuery({
    queryKey: ["finance-archived-accounts"],
    queryFn: fetchArchivedFinanceAccounts,
    staleTime: 30_000,
    enabled: isLoggedIn() && enabled,
  });
}

export function useArchiveFinanceAccountMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteFinanceAccount(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["finance-dashboard"] });
      void qc.invalidateQueries({ queryKey: ["finance-archived-accounts"] });
    },
  });
}

export function useUnarchiveFinanceAccountMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => unarchiveFinanceAccount(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["finance-dashboard"] });
      void qc.invalidateQueries({ queryKey: ["finance-archived-accounts"] });
    },
  });
}

export function useUpdateFinanceSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateFinanceSettings,
    onMutate: async (newData) => {
      await queryClient.cancelQueries({ queryKey: ["tenant"] });
      const previous = queryClient.getQueryData<Tenant>(["tenant"]);
      queryClient.setQueryData<Tenant>(["tenant"], (old) =>
        old ? { ...old, ...newData } : old,
      );
      return { previous };
    },
    onError: (_err, _newData, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["tenant"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

// -- Fuel (Workout Tracking) --

export function useFuelCalendarQuery(year: number, month: number) {
  return useQuery({
    queryKey: ["fuel-calendar", year, month],
    queryFn: () => fetchFuelCalendar(year, month),
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useWorkoutsQuery(params?: {
  category?: string;
  status?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
}) {
  return useQuery({
    queryKey: ["fuel-workouts", params],
    queryFn: () => fetchWorkouts(params),
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useWorkoutQuery(id: string | null) {
  return useQuery({
    queryKey: ["fuel-workout", id],
    queryFn: () => fetchWorkout(id!),
    staleTime: 30_000,
    enabled: isLoggedIn() && !!id,
  });
}

export function useCreateWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createWorkout,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
      void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
    },
  });
}

export function useUpdateWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<import("@/lib/types").FuelWorkout> }) =>
      updateWorkout(id, data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
      void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
      void qc.invalidateQueries({ queryKey: ["fuel-workout"] });
      void qc.invalidateQueries({ queryKey: ["fuel-progress"] });
    },
  });
}

export function useDeleteWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteWorkout,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
      void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
      void qc.invalidateQueries({ queryKey: ["fuel-progress"] });
    },
  });
}

export function useFuelProgressQuery(category: string) {
  return useQuery({
    queryKey: ["fuel-progress", category],
    queryFn: () => fetchFuelProgress(category),
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useBodyWeightQuery() {
  return useQuery({
    queryKey: ["fuel-body-weight"],
    queryFn: fetchBodyWeight,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateBodyWeightMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createBodyWeight,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-body-weight"] });
    },
  });
}

export function useUpdateFuelSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateFuelSettings,
    onMutate: async (newData) => {
      await queryClient.cancelQueries({ queryKey: ["tenant"] });
      const previous = queryClient.getQueryData<Tenant>(["tenant"]);
      queryClient.setQueryData<Tenant>(["tenant"], (old) =>
        old ? { ...old, ...newData } : old,
      );
      return { previous };
    },
    onError: (_err, _newData, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["tenant"], context.previous);
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}
