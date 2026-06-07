export interface ModelUI {
  model_id: string;
  name: string;
  tagline: string;
  intelligence: number;
  input_rate: number;
  output_rate: number;
  comingSoon?: boolean;
  // When set, the model is only selectable if the tenant has connected
  // the corresponding BYO subscription. The picker still shows the card
  // (as a "Connect to enable" affordance) when the requirement isn't met.
  requires?: "byo-anthropic" | "byo-openai";
  // $0 on our side (no token cost to the tenant's budget).
  free?: boolean;
  // A limited-time promotional model. Only rendered / selectable while the
  // server reports the offer active (tenant.free_model_offer.active). Excluded
  // from the standard "N models included" summary.
  limitedTimeOffer?: boolean;
}

export const MODELS: ModelUI[] = [
  {
    // Limited-time free offer. Selectable only while tenant.free_model_offer.active
    // (the server health cron flips it). Routed via OpenRouter's $0 free variant.
    model_id: "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    name: "Nemotron 3 Ultra (Free)",
    tagline: "Limited-time free · 1M-context frontier model",
    intelligence: 9,
    input_rate: 0,
    output_rate: 0,
    free: true,
    limitedTimeOffer: true,
  },
  { model_id: "openrouter/minimax/minimax-m2.7", name: "MiniMax M2.7", tagline: "Fast and efficient", intelligence: 6, input_rate: 0.28, output_rate: 1.20 },
  { model_id: "openrouter/deepseek/deepseek-v4-pro", name: "DeepSeek V4 Pro", tagline: "Reasoning + 1M context", intelligence: 8, input_rate: 0.435, output_rate: 0.87 },
  { model_id: "openrouter/google/gemma-4-31b-it", name: "Gemma 4 31B", tagline: "Lightweight and affordable", intelligence: 6, input_rate: 0.12, output_rate: 0.37 },
  {
    // BYO Anthropic models use the canonical `anthropic/<model>` prefix.
    // CLI routing (so the tenant's Pro/Max subscription is billed instead
    // of the platform's API key) is activated by the `anthropic:claude-cli`
    // auth profile, which `runtime/openclaw/entrypoint.sh` registers at
    // container boot via `openclaw models auth login --provider anthropic
    // --method cli`. The prefix `anthropic-cli/...` shipped briefly in
    // PR #419 is invalid in OpenClaw 2026.4.25's model registry.
    model_id: "anthropic/claude-opus-4-7",
    name: "Claude Opus 4.7",
    tagline: "Bring your own subscription",
    intelligence: 10,
    input_rate: 0,
    output_rate: 0,
    requires: "byo-anthropic",
  },
  {
    model_id: "anthropic/claude-sonnet-4-6",
    name: "Claude Sonnet 4.6",
    tagline: "Bring your own subscription",
    intelligence: 9,
    input_rate: 0,
    output_rate: 0,
    requires: "byo-anthropic",
  },
];

export const ACTIVE_MODELS = MODELS.filter((m) => !m.comingSoon);

export const DEFAULT_MODEL = "openrouter/deepseek/deepseek-v4-pro";

export function modelSummary(): string {
  const names = ACTIVE_MODELS.filter((m) => !m.requires && !m.limitedTimeOffer).map((m) => m.name);
  return `${names.length} AI models included: ${names.join(", ")}`;
}
