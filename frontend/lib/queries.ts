"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isLoggedIn } from "@/lib/auth";
import { getLiveQueryClient } from "@/lib/query-persist";
import {
  AbsorbedItem,
  AuthUser,
  ChatPage,
  ChatThread,
  CircleDetail,
  CircleSummary,
  CronJob,
  Integration,
  MissionSummary,
  Neighbor,
  NeighborhoodData,
  NeighborProfile,
  PendingGoalAction,
  PendingRemindersResponse,
  PendingWave,
  PersonalAccessToken,
  ProvisioningStatus,
  RefreshConfigStatus,
  Tenant,
} from "@/lib/types";
import {
  appendToDocument,
  bulkDeleteCronJobs,
  bulkUpdateForeground,
  cancelPendingReminder,
  fetchPendingReminders,
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
  fetchChatMessagesFirstPage,
  fetchDocument,
  fetchDocuments,
  fetchIntegrations,
  fetchJournalEntries,
  fetchMe,
  fetchPersonas,
  fetchPreferences,
  fetchPushStatus,
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
  confirmInsight,
  createPurpose,
  dismissExtraction,
  refuteInsight,
  updatePurpose,
  completeTask,
  reopenTask,
  fetchHorizons,
  fetchJournalStatus,
  fetchUsageHistory,
  fetchUsageSummary,
  fetchTransparency,
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
  fetchCredits,
  requestCreditCheckout,
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
  fetchWorkoutCount,
  fetchScheduleWindow,
  createWorkout,
  updateWorkout,
  deleteWorkout,
  skipWorkout,
  completeWorkout,
  acquireEditLock,
  fetchFuelVersion,
  releaseEditLock,
  swapWorkouts,
  fetchFuelProgress,
  fetchBodyWeight,
  createBodyWeight,
  deleteBodyWeight,
  updateBodyWeight,
  updateFuelSettings,
  updateCoreSettings,
  fetchCoreProfile,
  updateCoreProfile,
  fetchFuelProfile,
  updateFuelProfile,
  fetchWorkoutTemplates,
  createWorkoutTemplate,
  deleteWorkoutTemplate,
  duplicateWorkout,
  fetchWeeklyVolume,
  fetchPRFeed,
  fetchFuelGoals,
  createFuelGoal,
  deleteFuelGoal,
  fetchRestingHR,
  createRestingHR,
  updateRestingHR,
  deleteRestingHR,
  fetchSleep,
  createSleep,
  updateSleep,
  deleteSleep,
  fetchPATs,
  mintPAT,
  revokePAT,
  fetchSautaiLink,
  connectSautaiLink,
  disconnectSautaiLink,
  fetchByoCredentials,
  connectByoCredential,
  disconnectByoCredential,
  fetchConstellation,
  fetchGalaxy,
  fetchPendingLessons,
  approveLesson,
  dismissLesson,
  deleteLesson,
  fetchNeighborhood,
  sendWave,
  acceptWave,
  declineWave,
  blockWave,
  unfriend,
  fetchNeighborProfile,
  updateNeighborProfile,
  createFriendInvite,
  fetchLessons,
  fetchPendingShares,
  shareLesson,
  shareLessonToCircle,
  approveShare,
  rejectShare,
  revokeShare,
  fetchWormholes,
  fetchAbsorbed,
  purgeAbsorbed,
  fetchThreads,
  openThread,
  fetchThreadMessages,
  sendThreadMessage,
  markThreadRead,
  patchThreadMembership,
  fetchMissions,
  createMission,
  fetchMissionDetail,
  patchMission,
  joinMission,
  leaveMission,
  addMissionUpdate,
  addMissionTask,
  fetchGoalActions,
  approveGoalAction,
  rejectGoalAction,
  fetchCircles,
  createCircle,
  joinCircle,
  fetchCircleDetail,
  addCircleMember,
  leaveCircle,
  removeCircleMember,
  regenerateInviteCode,
} from "@/lib/api";
import { selectGreeting } from "@/lib/welcome-message";

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
    // Poll while a picker change is in flight so the AI provider page can
    // transition the "Switching…" badge to "Active" once the container
    // adopts the change. `applied_model` is stamped only after a successful
    // gateway.reload (apps/orchestrator/tasks.py); when it equals
    // `preferred_model`, we stop polling.
    refetchInterval: (query) => {
      const data = query.state.data as Tenant | undefined;
      if (!data) return false;
      if (!data.preferred_model || data.preferred_model === data.applied_model) {
        return false;
      }
      return 5000;
    },
    refetchIntervalInBackground: false,
  });
}

export function useWelcomeMessageQuery() {
  const { data: tenant } = useTenantQuery();

  return useQuery({
    queryKey: ["welcome-message", tenant?.id],
    queryFn: async () => selectGreeting(await fetchChatMessagesFirstPage()),
    staleTime: 60_000,
    retry: false,
    enabled: isLoggedIn() && tenant?.status === "active",
  });
}

export function useDashboardQuery() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: fetchDashboard,
    staleTime: 60_000,
    enabled: isLoggedIn(),
  });
}

export function useGalaxyQuery() {
  return useQuery({
    queryKey: ["galaxy"],
    queryFn: fetchGalaxy,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useUsageHistoryQuery() {
  return useQuery({
    queryKey: ["usage-history"],
    queryFn: fetchUsageHistory,
    staleTime: 60_000,
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

export function useJournalStatusQuery() {
  return useQuery({
    queryKey: ["journal-status"],
    queryFn: fetchJournalStatus,
    staleTime: 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCompleteTaskMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => completeTask(taskId),
    // CurrentStatusCard shows an inline "couldn't save — retry" on failure, so
    // opt out of the global mutation error toast (avoid a double signal).
    meta: { skipErrorToast: true },
    // Instant feedback (checked + strikethrough) is driven by local state in
    // CurrentStatusCard so the row can animate out; here we only reconcile
    // against the server. `onSettled` (not `onSuccess`) so a failed attempt
    // also re-syncs — the refetch now bypasses the HTTP cache (fetchJournalStatus
    // uses `no-store`), so the completed task drops immediately instead of
    // reappearing from the stale `max-age=10` browser cache.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["journal-status"] });
    },
  });
}

export function useReopenTaskMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskId: string) => reopenTask(taskId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["journal-status"] });
    },
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

export function useCreatePurposeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ statement, pillars }: { statement: string; pillars?: string[] }) =>
      createPurpose(statement, pillars),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["horizons"] });
    },
  });
}

export function useUpdatePurposeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      patch,
    }: {
      id: string;
      patch: { statement?: string; status?: import("@/lib/types").NorthStarStatus; pillars?: string[] };
    }) => updatePurpose(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["horizons"] });
    },
  });
}

export function useConfirmInsightMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, note }: { id: string; note?: string }) => confirmInsight(id, note),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["horizons"] });
    },
  });
}

export function useRefuteInsightMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, note }: { id: string; note?: string }) => refuteInsight(id, note),
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

export function usePreferredModelMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updatePreferredModel,
    onMutate: async (newModelId: string) => {
      await queryClient.cancelQueries({ queryKey: ["tenant"] });
      const previous = queryClient.getQueryData<Record<string, unknown>>(["tenant"]);
      queryClient.setQueryData(["tenant"], (old: Record<string, unknown> | undefined) =>
        old ? { ...old, preferred_model: newModelId } : old,
      );
      return { previous };
    },
    onError: (_err, _newModelId, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["tenant"], context.previous);
      }
    },
    onSettled: () => {
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
    onSuccess: (tenant) => {
      const liveQueryClient = getLiveQueryClient() ?? queryClient;
      liveQueryClient.setQueryData<AuthUser>(["me"], (me) =>
        me ? { ...me, tenant } : me,
      );
      void liveQueryClient.invalidateQueries({ queryKey: ["me"] });
      void liveQueryClient.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function useCheckoutMutation() {
  return useMutation({
    mutationFn: requestStripeCheckout,
  });
}

export function useCreditsQuery() {
  return useQuery({
    queryKey: ["credits"],
    queryFn: fetchCredits,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useCreditCheckoutMutation() {
  return useMutation({
    mutationFn: (packId: string) => requestCreditCheckout(packId),
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
//
// `pairingActive` = the caller is showing a live pairing QR/deep-link and
// needs the snappy 3s cadence to flip to "Connected" the moment the bot
// confirms. Everywhere else 15s is plenty — and once linked there is nothing
// to poll for at all, so the interval shuts off entirely.
export function useTelegramStatusQuery(enabled = true, pairingActive = false) {
  return useQuery({
    queryKey: ["telegram-status"],
    queryFn: fetchTelegramStatus,
    enabled: isLoggedIn() && enabled,
    staleTime: 0,
    refetchOnWindowFocus: "always",
    refetchInterval: enabled
      ? (query) => {
          if (query.state.status === "error") return false;
          if (query.state.data?.linked === true) return false;
          return pairingActive ? 3000 : 15_000;
        }
      : false,
  });
}

export function useGenerateTelegramLinkMutation() {
  return useMutation({
    mutationFn: generateTelegramLink,
    meta: { skipErrorToast: true },
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

// LINE — same polling contract as useTelegramStatusQuery above.
export function useLineStatusQuery(enabled = true, pairingActive = false) {
  return useQuery({
    queryKey: ["line-status"],
    queryFn: fetchLineStatus,
    enabled: isLoggedIn() && enabled,
    staleTime: 0,
    refetchOnWindowFocus: "always",
    refetchInterval: enabled
      ? (query) => {
          if (query.state.status === "error") return false;
          if (query.state.data?.linked === true) return false;
          return pairingActive ? 3000 : 15_000;
        }
      : false,
  });
}

export function useGenerateLineLinkMutation() {
  return useMutation({
    mutationFn: generateLineLink,
    meta: { skipErrorToast: true },
  });
}

export function usePushStatusQuery(enabled = true) {
  return useQuery({
    queryKey: ["push-status"],
    queryFn: fetchPushStatus,
    enabled: isLoggedIn() && enabled,
    staleTime: 0,
    refetchOnWindowFocus: "always",
    refetchInterval: (query) =>
      query.state.data?.registered === false ? 15_000 : false,
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
    // Poll every 15s only while an update is pending so the UI reflects when
    // it's applied. Idle (nothing pending) needs no interval — refetch on
    // window focus covers cron-triggered pending bumps cheaply.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.has_pending_update ? 15_000 : false;
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
    staleTime: 90_000,
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
    staleTime: 60_000,
  });
}

export function useDocumentsQuery(kind?: string) {
  return useQuery({
    queryKey: ["documents", kind ?? "all"],
    queryFn: () => fetchDocuments(kind),
    staleTime: 90_000,
    enabled: isLoggedIn(),
  });
}

export function useSidebarTreeQuery() {
  return useQuery({
    queryKey: ["sidebar-tree"],
    queryFn: fetchSidebarTree,
    staleTime: 120_000,
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
    staleTime: 60_000,
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

// Pending one-off reminders (separate from recurring crons — different
// lifecycle, gateway-only source of truth, auto-deleting). NOT polled:
// this is gateway-backed, so a background interval can cold-start a
// hibernated tenant container every 60s for no user benefit. Refetch on
// window focus + mutation invalidation (cancel) keeps the list honest.
export function usePendingRemindersQuery() {
  return useQuery<PendingRemindersResponse>({
    queryKey: ["pending-reminders"],
    queryFn: fetchPendingReminders,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    enabled: isLoggedIn(),
  });
}

export function useCancelPendingReminderMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => cancelPendingReminder(name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["pending-reminders"] });
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
    staleTime: 5 * 60_000,
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
  const qc = useQueryClient();
  return useQuery({
    queryKey: [
      "fuel-workouts",
      params?.status ?? "",
      params?.category ?? "",
      params?.date_from ?? "",
      params?.date_to ?? "",
      String(params?.limit ?? ""),
    ],
    queryFn: async () => {
      const data = await fetchWorkouts(params);
      // Prime the per-workout detail cache so opening the drawer is instant.
      data.forEach((w) => qc.setQueryData(["fuel-workout", w.id], w));
      return data;
    },
    staleTime: 2 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useWorkoutQuery(id: string | null) {
  return useQuery({
    queryKey: ["fuel-workout", id],
    queryFn: () => fetchWorkout(id!),
    staleTime: 60_000,
    // Always re-validate against the server when the detail drawer mounts.
    // Without this, the persisted-cache + schedule-list-priming path can
    // surface a row id that's been deleted upstream (assistant runtime,
    // plan regeneration, another tab) — the user types into a phantom
    // form and only finds out at save time.
    refetchOnMount: "always",
    enabled: isLoggedIn() && !!id,
    // A 404 here means the workout was deleted by another actor (the
    // assistant runtime, another browser tab, etc.). Retrying won't bring
    // it back; surface the error so the caller renders a recovery UI.
    retry: (failureCount, err) => {
      const status = (err as Error & { status?: number })?.status;
      if (status === 404 || status === 401) return false;
      return failureCount < 1;
    },
  });
}

export function useWorkoutCountQuery(params?: { status?: string; category?: string }) {
  return useQuery({
    queryKey: ["fuel-workout-count", params?.status ?? "", params?.category ?? ""],
    queryFn: () => fetchWorkoutCount(params),
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createWorkout,
    // Fan out to every fuel list — including ["fuel-schedule"]. Hand-rolling
    // the list here previously omitted the schedule window, so a new workout
    // showed on the Calendar instantly but lagged on the Schedule tab until
    // its staleTime expired. Route through the shared helper so create stays
    // in lockstep with delete/skip/complete (which already do).
    onSuccess: () => invalidateFuelLists(qc),
  });
}

export function useUpdateWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<import("@/lib/types").FuelWorkout> }) =>
      updateWorkout(id, data),
    // Was missing ["fuel-schedule"] (+ ["fuel-workout-count"]) on success, so
    // an edit refreshed the Calendar but left the Schedule card showing
    // pre-edit values. invalidateFuelLists covers all six keys.
    onSuccess: () => invalidateFuelLists(qc),
    onError: (err) => {
      if (isNotFound(err)) invalidateFuelLists(qc);
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
      void qc.invalidateQueries({ queryKey: ["fuel-workout-count"] });
      void qc.invalidateQueries({ queryKey: ["fuel-schedule"] });
    },
    onError: (err) => {
      if (isNotFound(err)) invalidateFuelLists(qc);
    },
  });
}

export function useScheduleWindowQuery(window: string = "7d") {
  const qc = useQueryClient();
  return useQuery({
    queryKey: ["fuel-schedule", window],
    queryFn: async () => {
      const data = await fetchScheduleWindow(window);
      // Prime the per-workout detail cache so opening the drawer is instant.
      data.forEach((w) => qc.setQueryData(["fuel-workout", w.id], w));
      return data;
    },
    staleTime: 60_000,
    enabled: isLoggedIn(),
  });
}

function invalidateFuelLists(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: ["fuel-calendar"] });
  void qc.invalidateQueries({ queryKey: ["fuel-workouts"] });
  void qc.invalidateQueries({ queryKey: ["fuel-workout"] });
  void qc.invalidateQueries({ queryKey: ["fuel-workout-count"] });
  void qc.invalidateQueries({ queryKey: ["fuel-schedule"] });
  void qc.invalidateQueries({ queryKey: ["fuel-progress"] });
}

// 404 on any workout write means the row was deleted upstream (assistant
// runtime, plan regen, another tab) since the cache was last populated.
// Drop our stale schedule/list caches so the dead row stops appearing,
// but let the caller decide what UX to show.
function isNotFound(err: unknown): boolean {
  return (err as Error & { status?: number })?.status === 404;
}

export function useSkipWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) => skipWorkout(id, reason),
    onSuccess: () => invalidateFuelLists(qc),
    onError: (err) => {
      if (isNotFound(err)) invalidateFuelLists(qc);
    },
  });
}

export function useCompleteWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data?: { notes?: string; rpe?: number; duration_minutes?: number };
    }) => completeWorkout(id, data),
    onSuccess: () => invalidateFuelLists(qc),
    onError: (err) => {
      if (isNotFound(err)) invalidateFuelLists(qc);
    },
  });
}

export function useSwapWorkoutsMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ a, b }: { a: string; b: string }) => swapWorkouts(a, b),
    onSuccess: () => invalidateFuelLists(qc),
  });
}

export function useFuelVersionQuery(opts?: { refetchInterval?: number; enabled?: boolean }) {
  return useQuery({
    queryKey: ["fuel-version"],
    queryFn: fetchFuelVersion,
    staleTime: 0,
    refetchInterval: opts?.refetchInterval ?? 30_000,
    enabled: opts?.enabled !== false && isLoggedIn(),
  });
}

export function useAcquireEditLockMutation() {
  return useMutation({
    mutationFn: (workoutId: string) => acquireEditLock(workoutId),
  });
}

export function useReleaseEditLockMutation() {
  return useMutation({
    mutationFn: (workoutId: string) => releaseEditLock(workoutId),
  });
}

export function useFuelProgressQuery(category: string) {
  return useQuery({
    queryKey: ["fuel-progress", category],
    queryFn: () => fetchFuelProgress(category),
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useBodyWeightQuery() {
  return useQuery({
    queryKey: ["fuel-body-weight"],
    queryFn: fetchBodyWeight,
    staleTime: 5 * 60_000,
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

export function useDeleteBodyWeightMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteBodyWeight,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-body-weight"] });
    },
  });
}

export function useUpdateBodyWeightMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: { date?: string; weight_kg?: number } }) =>
      updateBodyWeight(id, data),
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
      void queryClient.invalidateQueries({ queryKey: ["fuel-profile"] });
    },
  });
}

// -- Core (Mindfulness) --

export function useUpdateCoreSettingsMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateCoreSettings,
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

export function useCoreProfileQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["core-profile"],
    queryFn: fetchCoreProfile,
    staleTime: 10 * 60_000,
    enabled: isLoggedIn() && !!tenant?.core_enabled,
  });
}

export function useUpdateCoreProfileMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: updateCoreProfile,
    onSuccess: (profile) => {
      qc.setQueryData(["core-profile"], profile);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["core-profile"] });
    },
  });
}

export function useFuelProfileQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["fuel-profile"],
    queryFn: fetchFuelProfile,
    staleTime: 10 * 60_000,
    enabled: isLoggedIn() && !!tenant?.fuel_enabled,
  });
}

export function useUpdateFuelProfileMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: updateFuelProfile,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-profile"] });
    },
  });
}

// Templates
export function useWorkoutTemplatesQuery(category?: string) {
  return useQuery({
    queryKey: ["fuel-templates", category ?? ""],
    queryFn: () => fetchWorkoutTemplates(category),
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateWorkoutTemplateMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createWorkoutTemplate,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-templates"] });
    },
  });
}

export function useDeleteWorkoutTemplateMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteWorkoutTemplate,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-templates"] });
    },
  });
}

export function useDuplicateWorkoutMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: duplicateWorkout,
    // Same fix as create/update: include ["fuel-schedule"] so a duplicated
    // session appears on the Schedule tab immediately, not just the Calendar.
    onSuccess: () => invalidateFuelLists(qc),
  });
}

// Weekly volume
export function useWeeklyVolumeQuery(weekStart?: string) {
  return useQuery({
    queryKey: ["fuel-weekly-volume", weekStart ?? ""],
    queryFn: () => fetchWeeklyVolume(weekStart),
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

// PRs
export function usePRFeedQuery() {
  return useQuery({
    queryKey: ["fuel-prs"],
    queryFn: () => fetchPRFeed(),
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

// Goals
export function useFuelGoalsQuery() {
  return useQuery({
    queryKey: ["fuel-goals"],
    queryFn: fetchFuelGoals,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateFuelGoalMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createFuelGoal,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-goals"] });
    },
  });
}

export function useDeleteFuelGoalMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteFuelGoal,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-goals"] });
    },
  });
}

// Resting heart rate
export function useRestingHRQuery() {
  return useQuery({
    queryKey: ["fuel-resting-hr"],
    queryFn: fetchRestingHR,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateRestingHRMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createRestingHR,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-resting-hr"] });
    },
  });
}

export function useUpdateRestingHRMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: { date?: string; bpm?: number } }) =>
      updateRestingHR(id, data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-resting-hr"] });
    },
  });
}

export function useDeleteRestingHRMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteRestingHR,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-resting-hr"] });
    },
  });
}

// Sleep
export function useSleepQuery() {
  return useQuery({
    queryKey: ["fuel-sleep"],
    queryFn: fetchSleep,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function useCreateSleepMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createSleep,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-sleep"] });
    },
  });
}

export function useUpdateSleepMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: { date?: string; duration_hours?: number; quality?: number | null; notes?: string };
    }) => updateSleep(id, data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-sleep"] });
    },
  });
}

export function useDeleteSleepMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteSleep,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["fuel-sleep"] });
    },
  });
}

// Personal Access Tokens (Connected Apps)

export function usePATsQuery() {
  return useQuery({
    queryKey: ["pats"],
    queryFn: fetchPATs,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useMintPATMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: mintPAT,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pats"] });
    },
  });
}

export function useRevokePATMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: revokePAT,
    onMutate: async (patId: string) => {
      await qc.cancelQueries({ queryKey: ["pats"] });
      const previous = qc.getQueryData<PersonalAccessToken[]>(["pats"]);
      qc.setQueryData<PersonalAccessToken[]>(["pats"], (old) =>
        old ? old.filter((p) => p.id !== patId) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(["pats"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["pats"] });
    },
  });
}

// sautai account link (Connected Apps)

export function useSautaiLinkQuery() {
  return useQuery({
    queryKey: ["sautai-link"],
    queryFn: fetchSautaiLink,
    staleTime: 30_000,
    enabled: isLoggedIn(),
  });
}

export function useConnectSautaiMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (connectKey: string) => connectSautaiLink(connectKey),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sautai-link"] });
    },
  });
}

export function useDisconnectSautaiMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: disconnectSautaiLink,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["sautai-link"] });
    },
  });
}

// BYO subscription credentials

export function useByoCredentialsQuery(byoEnabled: boolean) {
  return useQuery({
    queryKey: ["byo-credentials"],
    queryFn: fetchByoCredentials,
    staleTime: 30_000,
    enabled: isLoggedIn() && byoEnabled,
    // 404 when the feature flag is off — return [] rather than retrying.
    retry: (failureCount, error) => {
      const status = (error as Error & { status?: number }).status;
      if (status === 404) return false;
      return failureCount < 2;
    },
  });
}

export function useConnectByoMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      data,
      signal,
    }: {
      data: Parameters<typeof connectByoCredential>[0];
      signal?: AbortSignal;
    }) => connectByoCredential(data, signal),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["byo-credentials"] });
      void qc.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

export function useDisconnectByoMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, signal }: { id: string; signal?: AbortSignal }) =>
      disconnectByoCredential(id, signal),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["byo-credentials"] });
      void qc.invalidateQueries({ queryKey: ["tenant"] });
    },
  });
}

// Constellation
export function useConstellationQuery() {
  return useQuery({
    queryKey: ["constellation"],
    queryFn: fetchConstellation,
    staleTime: 5 * 60_000,
    enabled: isLoggedIn(),
  });
}

export function usePendingLessonsQuery() {
  return useQuery({
    queryKey: ["pending-lessons"],
    queryFn: fetchPendingLessons,
    staleTime: 60_000,
    enabled: isLoggedIn(),
  });
}

export function useApproveLessonMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: approveLesson,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pending-lessons"] });
      void qc.invalidateQueries({ queryKey: ["constellation"] });
    },
  });
}

export function useDismissLessonMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: dismissLesson,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pending-lessons"] });
    },
  });
}

export function useDeleteLessonMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: deleteLesson,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["constellation"] });
      void qc.invalidateQueries({ queryKey: ["pending-lessons"] });
    },
  });
}

// ── Neighborhood (Friends) ───────────────────────────────────────────────────

export function useNeighborhoodQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["neighborhood"],
    queryFn: fetchNeighborhood,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

export function useNeighborProfileQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["neighbor-profile"],
    queryFn: fetchNeighborProfile,
    staleTime: 10 * 60_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

// Warp targets for the constellation game's rim. Gated on BOTH login and
// friends_enabled. Deliberately NOT persisted (see lib/query-persist.ts): a
// wormhole set changes with grants/revocations, so it stays session-only.
export function useWormholesQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["wormholes"],
    queryFn: fetchWormholes,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

// Fold an accepted incoming wave straight into the neighbors list so the row
// hops sections instantly instead of waiting on the invalidate round-trip.
function acceptWaveOptimistically(old: NeighborhoodData, friendshipId: string): NeighborhoodData {
  const wave = old.pending_incoming.find((w) => w.friendship_id === friendshipId);
  if (!wave) return old;
  const promoted: Neighbor = {
    friendship_id: wave.friendship_id,
    display_name: wave.display_name,
    handle: wave.handle,
    avatar_hue: wave.avatar_hue,
    status: "accepted",
    since: new Date().toISOString(),
  };
  return {
    ...old,
    pending_incoming: old.pending_incoming.filter((w) => w.friendship_id !== friendshipId),
    neighbors: [promoted, ...old.neighbors],
  };
}

export function useAcceptWaveMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (friendshipId: string) => acceptWave(friendshipId),
    onMutate: async (friendshipId: string) => {
      await qc.cancelQueries({ queryKey: ["neighborhood"] });
      const previous = qc.getQueryData<NeighborhoodData>(["neighborhood"]);
      qc.setQueryData<NeighborhoodData>(["neighborhood"], (old) =>
        old ? acceptWaveOptimistically(old, friendshipId) : old,
      );
      return { previous };
    },
    onError: (_err, _friendshipId, context) => {
      if (context?.previous) {
        qc.setQueryData(["neighborhood"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

export function useDeclineWaveMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (friendshipId: string) => declineWave(friendshipId),
    onMutate: async (friendshipId: string) => {
      await qc.cancelQueries({ queryKey: ["neighborhood"] });
      const previous = qc.getQueryData<NeighborhoodData>(["neighborhood"]);
      qc.setQueryData<NeighborhoodData>(["neighborhood"], (old) =>
        old
          ? {
              ...old,
              pending_incoming: old.pending_incoming.filter(
                (w: PendingWave) => w.friendship_id !== friendshipId,
              ),
            }
          : old,
      );
      return { previous };
    },
    onError: (_err, _friendshipId, context) => {
      if (context?.previous) {
        qc.setQueryData(["neighborhood"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

export function useBlockWaveMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (friendshipId: string) => blockWave(friendshipId),
    onMutate: async (friendshipId: string) => {
      await qc.cancelQueries({ queryKey: ["neighborhood"] });
      const previous = qc.getQueryData<NeighborhoodData>(["neighborhood"]);
      qc.setQueryData<NeighborhoodData>(["neighborhood"], (old) =>
        old
          ? {
              ...old,
              pending_incoming: old.pending_incoming.filter((w) => w.friendship_id !== friendshipId),
              pending_outgoing: old.pending_outgoing.filter((w) => w.friendship_id !== friendshipId),
              neighbors: old.neighbors.filter((n) => n.friendship_id !== friendshipId),
            }
          : old,
      );
      return { previous };
    },
    onError: (_err, _friendshipId, context) => {
      if (context?.previous) {
        qc.setQueryData(["neighborhood"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

export function useUnfriendMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (friendshipId: string) => unfriend(friendshipId),
    onMutate: async (friendshipId: string) => {
      await qc.cancelQueries({ queryKey: ["neighborhood"] });
      const previous = qc.getQueryData<NeighborhoodData>(["neighborhood"]);
      qc.setQueryData<NeighborhoodData>(["neighborhood"], (old) =>
        old ? { ...old, neighbors: old.neighbors.filter((n) => n.friendship_id !== friendshipId) } : old,
      );
      return { previous };
    },
    onError: (_err, _friendshipId, context) => {
      if (context?.previous) {
        qc.setQueryData(["neighborhood"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

// No optimistic write here — the resulting friendship_id/status are only
// known once the server resolves the handle (and a re-wave can resolve
// straight to "accepted"), so there's no local state to reconcile against.
// Errors render inline on the form (meta.skipErrorToast), same as profile save.
export function useSendWaveMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { handle: string; note?: string }) => sendWave(data),
    meta: { skipErrorToast: true },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

export function useUpdateNeighborProfileMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Partial<NeighborProfile>) => updateNeighborProfile(data),
    meta: { skipErrorToast: true },
    onMutate: async (newData: Partial<NeighborProfile>) => {
      await qc.cancelQueries({ queryKey: ["neighbor-profile"] });
      const previous = qc.getQueryData<NeighborProfile>(["neighbor-profile"]);
      qc.setQueryData<NeighborProfile>(["neighbor-profile"], (old) =>
        old ? { ...old, ...newData } : old,
      );
      return { previous };
    },
    onError: (_err, _newData, context) => {
      if (context?.previous) {
        qc.setQueryData(["neighbor-profile"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["neighbor-profile"] });
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

// Invites aren't a persisted list in PR1 (no query to invalidate) — this just
// mints a token/url for the caller to display or share.
export function useCreateInviteMutation() {
  return useMutation({
    mutationFn: (data: { max_uses?: number; expires_in_days?: number } = {}) => createFriendInvite(data),
  });
}

// ── Neighborhood shares (PR2) ────────────────────────────────────────────────
// propose → scrub → preview → approve → publish. See lib/api.ts for the
// discriminated 200/202/409 result shapes the preview + approve endpoints
// return — fetchSharePreview is intentionally NOT wrapped in a query hook
// here; the preview modal polls it directly and branches on `.status` itself.

export function usePendingSharesQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["pending-shares"],
    queryFn: fetchPendingShares,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

// Approved lessons available to share — powers the lesson picker on the
// "Share a lesson" card. Gated the same as the rest of Neighborhood.
export function useApprovedLessonsQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["lessons-approved"],
    queryFn: () => fetchLessons("approved"),
    staleTime: 60_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

// PR7: the audience is now EITHER a neighbor or a circle — exactly one of
// friendshipId / circleId is expected per call (the picker below enforces it).
export function useShareLessonMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      lessonId,
      friendshipId,
      circleId,
    }: {
      lessonId: number;
      friendshipId?: string;
      circleId?: string;
    }) =>
      circleId ? shareLessonToCircle(lessonId, circleId) : shareLesson(lessonId, friendshipId as string),
    // 403 (pillar-blocked) / 400 (missing audience) surface via the default
    // global error toast (see app/providers.tsx) — no meta.skipErrorToast,
    // unlike the wave form, since this action has no inline error slot.
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pending-shares"] });
    },
  });
}

export function useApproveShareMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, finalText }: { id: string; finalText?: string }) => approveShare(id, finalText),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["pending-shares"] });
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

export function useRejectShareMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rejectShare(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["pending-shares"] });
    },
  });
}

export function useRevokeShareMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ lessonId, grantId }: { lessonId: number; grantId: string }) =>
      revokeShare(lessonId, grantId),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["pending-shares"] });
      void qc.invalidateQueries({ queryKey: ["neighborhood"] });
    },
  });
}

// ── Absorbed items (PR4) ────────────────────────────────────────────────────
// What a neighbor's shared spark the assistant has pulled into its own
// context — transparency + a manual purge. Gated the same as the rest of
// Neighborhood. Deliberately NOT in query-persist.ts's allowlist: this list
// should always reflect the assistant's live state, never a stale cache.

export function useAbsorbedQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["absorbed"],
    queryFn: fetchAbsorbed,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

export function usePurgeAbsorbedMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => purgeAbsorbed(id),
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: ["absorbed"] });
      const previous = qc.getQueryData<AbsorbedItem[]>(["absorbed"]);
      qc.setQueryData<AbsorbedItem[]>(["absorbed"], (old) =>
        old ? old.filter((item) => item.id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(["absorbed"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["absorbed"] });
    },
  });
}

// ── Friend chat (PR5) ────────────────────────────────────────────────────────
// 1:1 threads between accepted neighbors. Gated the same as the rest of
// Neighborhood. Deliberately NOT in query-persist.ts's allowlist — replaying
// a stale chat feed from localStorage on reload is actively wrong, not just
// stale (see the Absorbed/Wormholes precedent above).

export function useThreadsQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["friend-threads"],
    queryFn: fetchThreads,
    staleTime: 15_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

// Messages for one open thread. `active` = the thread panel is mounted AND
// the tab is visible — same function-form refetchInterval contract as
// useTelegramStatusQuery above, so a backgrounded tab (or a closed panel)
// doesn't keep polling a possibly-hibernated tenant container every 4s.
export function useThreadMessagesQuery(threadId: string | null, opts: { active: boolean }) {
  return useQuery({
    queryKey: ["friend-messages", threadId],
    queryFn: () => fetchThreadMessages(threadId as string),
    enabled: isLoggedIn() && !!threadId,
    staleTime: 0,
    refetchInterval: (query) => {
      if (!threadId) return false;
      if (query.state.status === "error") return false;
      return opts.active ? 4000 : false;
    },
    refetchIntervalInBackground: false,
  });
}

export function useOpenThreadMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ friendshipId }: { friendshipId: string }) => openThread(friendshipId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["friend-threads"] });
    },
  });
}

// Optimistic send: the outgoing message appears immediately under a
// client-generated id, then `onSettled` reconciles against the server
// (success replaces it with the canonical row; failure's `onError` rolls it
// back). `meta.skipErrorToast` — the composer renders the failure inline
// instead of a global toast, same convention as useSendWaveMutation.
export function useSendMessageMutation(threadId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ text, clientMsgId }: { text: string; clientMsgId: string }) =>
      sendThreadMessage(threadId, { client_msg_id: clientMsgId, text }),
    meta: { skipErrorToast: true },
    onMutate: async ({ text, clientMsgId }: { text: string; clientMsgId: string }) => {
      await qc.cancelQueries({ queryKey: ["friend-messages", threadId] });
      const previous = qc.getQueryData<ChatPage>(["friend-messages", threadId]);
      const optimisticMessage = {
        public_id: `pending-${clientMsgId}`,
        seq: Number.MAX_SAFE_INTEGER,
        text,
        mine: true,
        created_at: new Date().toISOString(),
      };
      qc.setQueryData<ChatPage>(["friend-messages", threadId], (old) =>
        old
          ? { ...old, messages: [...old.messages, optimisticMessage] }
          : { messages: [optimisticMessage], next_cursor: null },
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(["friend-messages", threadId], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["friend-messages", threadId] });
      // Refreshes last_message/last_message_at on the thread list row.
      void qc.invalidateQueries({ queryKey: ["friend-threads"] });
    },
  });
}

export function useMarkThreadReadMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (threadId: string) => markThreadRead(threadId),
    onSuccess: (_data, threadId) => {
      qc.setQueryData<ChatThread[]>(["friend-threads"], (old) =>
        old ? old.map((t) => (t.thread_id === threadId ? { ...t, unread: 0 } : t)) : old,
      );
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["friend-threads"] });
    },
  });
}

export function usePatchMembershipMutation(threadId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { muted?: boolean; agent_absorb_enabled?: boolean }) =>
      patchThreadMembership(threadId, data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["friend-threads"] });
    },
  });
}

// ── Missions (PR6) ────────────────────────────────────────────────────────
// Shared goals between accepted neighbors. Gated the same as the rest of
// Neighborhood. Deliberately NOT added to query-persist.ts's allowlist — the
// crew projection should always reflect the live control-plane stream, never
// a stale replay from localStorage (same reasoning as Absorbed/Wormholes).

export function useMissionsQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["missions"],
    queryFn: fetchMissions,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

export function useMissionDetailQuery(id: string | null) {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["mission", id],
    queryFn: () => fetchMissionDetail(id as string),
    staleTime: 15_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled && !!id,
  });
}

export function useGoalActionsQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["goal-actions"],
    queryFn: fetchGoalActions,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

export function useCreateMissionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Parameters<typeof createMission>[0]) => createMission(data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["missions"] });
    },
  });
}

export function useJoinMissionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, commitment }: { id: string; commitment?: string }) => joinMission(id, commitment),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["missions"] });
      void qc.invalidateQueries({ queryKey: ["mission", variables.id] });
    },
  });
}

// Optimistically drops the mission from the list (mirrors useUnfriendMutation)
// — leaving is immediate and the summary endpoint only ever returns active
// memberships, so the row would disappear on the next fetch regardless.
export function useLeaveMissionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => leaveMission(id),
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: ["missions"] });
      const previous = qc.getQueryData<MissionSummary[]>(["missions"]);
      qc.setQueryData<MissionSummary[]>(["missions"], (old) =>
        old ? old.filter((m) => m.mission_id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(["missions"], context.previous);
      }
    },
    onSettled: (_data, _err, id) => {
      void qc.invalidateQueries({ queryKey: ["missions"] });
      void qc.invalidateQueries({ queryKey: ["mission", id] });
    },
  });
}

export function useAddMissionUpdateMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: { kind: "note" | "progress" | "milestone"; text: string };
    }) => addMissionUpdate(id, data),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["mission", variables.id] });
    },
  });
}

export function useAddMissionTaskMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      data,
    }: {
      id: string;
      data: { title: string; description?: string; due_date?: string };
    }) => addMissionTask(id, data),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["mission", variables.id] });
    },
  });
}

export function usePatchMissionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof patchMission>[1] }) =>
      patchMission(id, data),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["missions"] });
      void qc.invalidateQueries({ queryKey: ["mission", variables.id] });
    },
  });
}

// ── Mission task proposals (PR6 / design §2.10) ────────────────────────────
// Agent-proposed next steps on a shared mission. Approve/reject drop the row
// from the inbox immediately — same optimistic-remove convention as
// usePurgeAbsorbedMutation above.

export function useApproveGoalActionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => approveGoalAction(id),
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: ["goal-actions"] });
      const previous = qc.getQueryData<PendingGoalAction[]>(["goal-actions"]);
      qc.setQueryData<PendingGoalAction[]>(["goal-actions"], (old) =>
        old ? old.filter((a) => a.id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(["goal-actions"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["goal-actions"] });
    },
  });
}

export function useRejectGoalActionMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rejectGoalAction(id),
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: ["goal-actions"] });
      const previous = qc.getQueryData<PendingGoalAction[]>(["goal-actions"]);
      qc.setQueryData<PendingGoalAction[]>(["goal-actions"], (old) =>
        old ? old.filter((a) => a.id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(["goal-actions"], context.previous);
      }
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["goal-actions"] });
    },
  });
}

// ── Circles (PR7) ────────────────────────────────────────────────────────
// Groups built on edges (design §2.11). Gated the same as the rest of
// Neighborhood. Deliberately NOT added to query-persist.ts's allowlist —
// membership/invite-code churn should always reflect the live server state,
// same reasoning as Absorbed/Wormholes/Missions above.

export function useCirclesQuery() {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["circles"],
    queryFn: fetchCircles,
    staleTime: 30_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled,
  });
}

export function useCircleDetailQuery(id: string | null) {
  const { data: tenant } = useTenantQuery();
  return useQuery({
    queryKey: ["circle", id],
    queryFn: () => fetchCircleDetail(id as string),
    staleTime: 15_000,
    enabled: isLoggedIn() && !!tenant?.friends_enabled && !!id,
  });
}

export function useCreateCircleMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Parameters<typeof createCircle>[0]) => createCircle(data),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["circles"] });
    },
  });
}

// Inline error on the join form (meta.skipErrorToast), same convention as
// useSendWaveMutation — an invalid/unknown code is an expected input error,
// not a global-toast-worthy failure.
export function useJoinCircleMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (inviteCode: string) => joinCircle(inviteCode),
    meta: { skipErrorToast: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["circles"] });
    },
  });
}

export function useAddCircleMemberMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, handle }: { id: string; handle: string }) => addCircleMember(id, handle),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["circle", variables.id] });
      void qc.invalidateQueries({ queryKey: ["circles"] });
    },
  });
}

// Optimistically drops the circle from the list (mirrors useLeaveMissionMutation)
// — leaving is immediate and the list endpoint only ever returns active
// memberships, so the row would disappear on the next fetch regardless.
export function useLeaveCircleMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, keep }: { id: string; keep?: boolean }) => leaveCircle(id, keep),
    onMutate: async ({ id }: { id: string; keep?: boolean }) => {
      await qc.cancelQueries({ queryKey: ["circles"] });
      const previous = qc.getQueryData<CircleSummary[]>(["circles"]);
      qc.setQueryData<CircleSummary[]>(["circles"], (old) =>
        old ? old.filter((c) => c.circle_id !== id) : old,
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(["circles"], context.previous);
      }
    },
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["circles"] });
      void qc.invalidateQueries({ queryKey: ["circle", variables.id] });
    },
  });
}

export function useRemoveCircleMemberMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, handle }: { id: string; handle: string }) => removeCircleMember(id, handle),
    onSettled: (_data, _err, variables) => {
      void qc.invalidateQueries({ queryKey: ["circle", variables.id] });
      void qc.invalidateQueries({ queryKey: ["circles"] });
    },
  });
}

export function useRegenerateInviteCodeMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => regenerateInviteCode(id),
    onSuccess: (data) => {
      qc.setQueryData<CircleDetail>(["circle", data.circle_id], (old) =>
        old ? { ...old, invite_code: data.invite_code } : old,
      );
      void qc.invalidateQueries({ queryKey: ["circles"] });
    },
  });
}
