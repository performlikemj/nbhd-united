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
}

export const MODELS: ModelUI[] = [
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

export const DEFAULT_MODEL = "openrouter/minimax/minimax-m2.7";

export function modelSummary(): string {
  const names = ACTIVE_MODELS.filter((m) => !m.requires).map((m) => m.name);
  return `${names.length} AI models included: ${names.join(", ")}`;
}
