export interface ModelUI {
  model_id: string;
  name: string;
  tagline: string;
  intelligence: number;
  input_rate: number;
  output_rate: number;
  comingSoon?: boolean;
}

export const MODELS: ModelUI[] = [
  { model_id: "openrouter/minimax/minimax-m2.7", name: "MiniMax M2.7", tagline: "Fast and efficient", intelligence: 6, input_rate: 0.3, output_rate: 1.2 },
  { model_id: "openrouter/moonshotai/kimi-k2.6", name: "Kimi K2.6", tagline: "Balanced capability and cost", intelligence: 7, input_rate: 0.60, output_rate: 2.80 },
  { model_id: "openrouter/google/gemma-4-31b-it", name: "Gemma 4 31B", tagline: "Lightweight and affordable", intelligence: 6, input_rate: 0.14, output_rate: 0.40 },
];

export const ACTIVE_MODELS = MODELS.filter((m) => !m.comingSoon);

export const DEFAULT_MODEL = "openrouter/minimax/minimax-m2.7";

export function modelSummary(): string {
  const names = ACTIVE_MODELS.map((m) => m.name);
  return `${names.length} AI models included: ${names.join(", ")}`;
}
