export type TenantStatus =
  | "pending"
  | "provisioning"
  | "active"
  | "suspended"
  | "deprovisioning"
  | "deleted";

export type TenantTier = "starter";

export interface TenantUser {
  id: string;
  username: string;
  email: string;
  display_name: string;
  language: string;
  telegram_chat_id: number | null;
  telegram_username: string;
}

export interface Tenant {
  id: string;
  user: TenantUser;
  status: TenantStatus;
  model_tier: TenantTier;
  has_active_subscription: boolean;
  is_trial: boolean;
  trial_ends_at: string | null;
  trial_days_remaining: number | null;
  container_id: string;
  container_fqdn: string;
  messages_today: number;
  messages_this_month: number;
  tokens_this_month: number;
  estimated_cost_this_month: string;
  monthly_token_budget: number;
  monthly_cost_budget: string;
  preferred_model: string;
  task_model_preferences: Record<string, string>;
  last_message_at: string | null;
  provisioned_at: string | null;
  created_at: string;
  pending_deletion: boolean;
  deletion_scheduled_at: string | null;
  platform_budget_exceeded: boolean;
  finance_enabled: boolean;
}

export interface RefreshConfigStatus {
  can_refresh: boolean;
  last_refreshed: string | null;
  cooldown_seconds: number;
  status: string;
  has_pending_update: boolean;
  container_image_tag: string | null;
  latest_image_tag: string | null;
  image_outdated: boolean;
}

export interface ProvisioningStatus {
  tenant_id: string;
  user_id: string;
  status: TenantStatus;
  container_id: string;
  container_fqdn: string;
  has_container_id: boolean;
  has_container_fqdn: boolean;
  provisioned_at: string | null;
  created_at: string;
  updated_at: string;
  ready: boolean;
}

export interface Integration {
  id: string;
  provider: "google" | "reddit" | "sautai";
  status: "active" | "expired" | "revoked" | "error";
  provider_email: string;
  scopes: string[];
  connected_at: string;
  updated_at: string;
}

export interface AuthUser {
  id: string;
  email: string;
  username: string;
  display_name: string;
  language: string;
  timezone: string;
  location_city: string;
  location_lat: number | null;
  location_lon: number | null;
  telegram_chat_id: number | null;
  telegram_username: string;
  line_user_id: string | null;
  line_display_name: string;
  preferred_channel: "telegram" | "line";
  tenant: Tenant | null;
}

export interface DashboardData {
  tenant: {
    id: string;
    status: string;
    model_tier: string;
    provisioned_at: string | null;
  };
  usage: {
    messages_today: number;
    messages_this_month: number;
    tokens_this_month: number;
    estimated_cost_this_month: string;
    monthly_token_budget: number;
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost: string;
  };
  connections: Array<{
    provider: string;
    provider_email: string;
    connected_at: string;
  }>;
  health: Record<string, unknown>;
}

export interface UsageRecord {
  id: string;
  event_type: string;
  input_tokens: number;
  output_tokens: number;
  model_used: string;
  cost_estimate: string;
  created_at: string;
}

export interface UsageModelBreakdown {
  model: string;
  display_name: string;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  count: number;
}

export interface UsageBudgetSummary {
  tenant_tokens_used: number;
  tenant_token_budget: number;
  tenant_estimated_cost: number;
  tenant_cost_used: number;
  tenant_cost_budget: number;
  budget_percentage: number;
  global_spent: number;
  global_remaining: number | null;
}

export interface UsageSummary {
  period: {
    start: string;
    end: string;
  };
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  total_cost: number;
  message_count: number;
  by_model: UsageModelBreakdown[];
  budget: UsageBudgetSummary;
}

export interface TransparencyData {
  period: {
    start: string;
    end: string;
  };
  subscription_price: number;
  your_actual_cost: number;
  platform_infra: number;
  surplus: number;
  donation_amount: number;
  donation_enabled: boolean;
  donation_percentage: number;
  message_count: number;
  model_rates: Array<{
    model: string;
    display_name: string;
    input_per_million: number;
    output_per_million: number;
  }>;
  infra_breakdown: {
    container: number;
    database_share: number;
    storage_share: number;
    total: number;
    source?: string;
  };
  explanation: string;
}

// Journal (legacy structured entries)
/** @deprecated Use DailyNote types instead. */
export type JournalEntryEnergy = "low" | "medium" | "high";

/** @deprecated Use DailyNote types instead. */
export interface JournalEntry {
  id: string;
  date: string;
  mood: string;
  energy: JournalEntryEnergy;
  wins: string[];
  challenges: string[];
  reflection: string;
  created_at: string;
  updated_at: string;
}

export interface NoteTemplateSection {
  slug: string;
  title: string;
  content: string;
  source?: string;
}

export interface NoteTemplate {
  id: string;
  slug: string;
  name: string;
  sections: NoteTemplateSection[];
  is_default: boolean;
  source: string;
  created_at: string;
  updated_at: string;
}

// Weekly Reviews
export type WeekRating = "thumbs-up" | "thumbs-down" | "meh";

export interface WeeklyReview {
  id: string;
  week_start: string;
  week_end: string;
  mood_summary: string;
  top_wins: string[];
  top_challenges: string[];
  lessons: string[];
  week_rating: WeekRating;
  intentions_next_week: string[];
  created_at: string;
  updated_at: string;
}

// Working Hours (heartbeat window)
export interface WorkingHoursConfig {
  enabled: boolean;
  start_hour: number;
  window_hours: number;
  feature_tips: boolean;
}

// Cron Jobs (OpenClaw Gateway scheduled tasks)
export interface CronJobSchedule {
  kind: string;
  expr: string;
  tz: string;
}

export interface CronJobPayload {
  kind: string;
  message: string;
}

export interface CronJobDelivery {
  mode: string;
  channel?: string;
  to?: string;
}

export interface CronJob {
  jobId?: string;
  name: string;
  schedule: CronJobSchedule;
  // Always "isolated" under the universal isolation model. Kept on the type
  // for back-compat with the gateway response shape (legacy jobs may still
  // report "main" until they are recreated).
  sessionTarget: string;
  // Deprecated alongside sessionTarget. May still appear on legacy jobs.
  wakeMode?: string;
  payload: CronJobPayload;
  delivery: CronJobDelivery;
  enabled: boolean;
  // Whether this task pushes a Phase 2 sync into the main session after it
  // runs (only fires if the run actually sent the user a message). Default
  // is true. Derived server-side from the message body's Phase 2 marker.
  foreground?: boolean;
}

// Workspaces — separate conversation contexts per topic domain
export interface Workspace {
  id: string;
  name: string;
  slug: string;
  description: string;
  is_default: boolean;
  is_active: boolean;
  created_at: string | null;
  last_used_at: string | null;
}

export interface WorkspacesResponse {
  tenant_id: string;
  workspaces: Workspace[];
  active_workspace_id: string | null;
  limit: number;
}

export interface CreateWorkspaceResponse {
  tenant_id: string;
  workspace: Workspace;
  default_workspace_created: boolean;
}

export interface UpdateWorkspaceResponse {
  tenant_id: string;
  workspace: Workspace;
  updated: string[];
}

export interface DeleteWorkspaceResponse {
  tenant_id: string;
  deleted_id: string;
  fell_back_to_default: boolean;
}

export interface SwitchWorkspaceResponse {
  tenant_id: string;
  workspace: Workspace;
  previous_workspace_id: string | null;
}

export type AutomationKind = "daily_brief" | "weekly_review";
export type AutomationStatus = "active" | "paused";
export type AutomationScheduleType = "daily" | "weekly";

export interface Automation {
  id: string;
  kind: AutomationKind;
  status: AutomationStatus;
  timezone: string;
  schedule_type: AutomationScheduleType;
  schedule_time: string;
  schedule_days: number[];
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  last_run_at: string | null;
  next_run_at: string;
  created_at: string;
  updated_at: string;
}

// Journal v2 Documents
export type DocumentKind = "daily" | "weekly" | "monthly" | "goal" | "project" | "tasks" | "ideas" | "memory";

export interface DocumentResponse {
  id: string;
  kind: DocumentKind;
  slug: string;
  title: string;
  markdown: string;
  created_at: string;
  updated_at: string;
}

export interface DocumentListItem {
  id: string;
  kind: DocumentKind;
  slug: string;
  title: string;
  updated_at: string;
}

export interface SidebarSection {
  kind: string;
  label: string;
  items: Array<{
    slug: string;
    title: string;
    updated_at: string | null;
  }>;
}

export type AutomationRunStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";
export type AutomationTriggerSource = "manual" | "schedule";

export interface AutomationRun {
  id: string;
  automation: string;
  tenant: string;
  status: AutomationRunStatus;
  trigger_source: AutomationTriggerSource;
  scheduled_for: string;
  started_at: string | null;
  finished_at: string | null;
  idempotency_key: string;
  input_payload: Record<string, unknown>;
  result_payload: Record<string, unknown>;
  error_message: string;
  created_at: string;
  updated_at: string;
}


export interface Lesson {
  id: number;
  text: string;
  context: string;
  tags: string[];
  cluster_id: number | null;
  cluster_label: string;
  source_type: string;
  source_ref: string;
  status: "pending" | "approved" | "dismissed";
  suggested_at: string;
  approved_at: string | null;
  created_at: string;
}

export interface ConstellationNode {
  id: number;
  text: string;
  context?: string;
  tags: string[];
  cluster_id: number | null;
  cluster_label: string;
  source_type?: string;
  source_ref?: string;
  x: number | null;
  y: number | null;
  created_at: string;
}

export interface ConstellationEdge {
  source: number;
  target: number;
  similarity: number;
  connection_type: string;
}

export interface ConstellationData {
  nodes: ConstellationNode[];
  edges: ConstellationEdge[];
  affinity_edges: ConstellationEdge[];
  clusters: { id: number; label: string; count: number; tags: string[] }[];
}

// Horizons
export interface HorizonsGoal {
  id: string;
  title: string;
  slug: string;
  preview: string;
  created_at: string;
  updated_at: string;
}

export interface HorizonsPendingExtraction {
  id: string;
  kind: "goal" | "task";
  text: string;
  confidence: string;
  source_date: string | null;
  created_at: string;
}

export interface HorizonsWeeklyPulse {
  week_start: string;
  week_end: string;
  week_rating: WeekRating;
  top_win: string | null;
}

export interface HorizonsMomentumDay {
  date: string;
  message_count: number;
  has_journal: boolean;
}

export interface HorizonsWeeklyDocument {
  id: string;
  title: string;
  slug: string;
  preview: string;
  updated_at: string;
}

export interface HorizonsData {
  goals: HorizonsGoal[];
  pending_extractions: HorizonsPendingExtraction[];
  weekly_pulse: HorizonsWeeklyPulse[];
  weekly_documents: HorizonsWeeklyDocument[];
  mood_trend: { date: string; mood: string; energy: string }[];
  momentum: HorizonsMomentumDay[];
  current_streak: number;
}

// ── Finance ──────────────────────────────────────────────────────────
export interface FinanceAccount {
  id: string;
  account_type: string;
  nickname: string;
  current_balance: string;
  original_balance: string | null;
  interest_rate: string | null;
  minimum_payment: string | null;
  credit_limit: string | null;
  due_day: number | null;
  is_active: boolean;
  is_debt: boolean;
  payoff_progress: number | null;
  created_at: string;
  updated_at: string;
}

export interface FinanceTransaction {
  id: string;
  account: string;
  account_nickname: string;
  transaction_type: string;
  amount: string;
  description: string;
  date: string;
  created_at: string;
}

export interface PayoffPlanScheduleEntry {
  month: number;
  accounts: { nickname: string; balance: string; payment: string }[];
  total_remaining: string;
}

export interface PayoffPlan {
  id: string;
  strategy: string;
  monthly_budget: string;
  total_debt: string;
  total_interest: string;
  payoff_months: number;
  payoff_date: string;
  schedule_json: PayoffPlanScheduleEntry[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface FinanceSnapshot {
  id: string;
  date: string;
  total_debt: string;
  total_savings: string;
  total_payments_this_month: string;
  accounts_json: { nickname: string; type: string; balance: string }[];
  created_at: string;
}

export interface FinanceDashboardData {
  total_debt: string;
  total_savings: string;
  total_minimum_payments: string;
  debt_account_count: number;
  savings_account_count: number;
  accounts: FinanceAccount[];
  active_plan: PayoffPlan | null;
  snapshots: FinanceSnapshot[];
  recent_transactions: FinanceTransaction[];
}
