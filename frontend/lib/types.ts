export type TenantStatus =
  | "pending"
  | "provisioning"
  | "active"
  | "suspended"
  | "deprovisioning"
  | "deleted";

export type TenantTier = "basic" | "plus";

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
  container_id: string;
  container_fqdn: string;
  messages_today: number;
  messages_this_month: number;
  tokens_this_month: number;
  estimated_cost_this_month: string;
  monthly_token_budget: number;
  last_message_at: string | null;
  provisioned_at: string | null;
  created_at: string;
}

export interface Integration {
  id: string;
  provider: "gmail" | "google-calendar" | "sautai";
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
  telegram_chat_id: number | null;
  telegram_username: string;
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
