import type { WorkoutCategory } from "@/lib/types";

export interface CategoryMeta {
  label: string;
  accent: string;
  hint: string;
  suggest: string[];
}

export const CATEGORIES: Record<WorkoutCategory, CategoryMeta> = {
  strength: {
    label: "Strength",
    accent: "var(--color-violet-400, #a78bfa)",
    hint: "Lifts, sets, reps, weight",
    suggest: [
      "Push day",
      "Pull day",
      "Leg day",
      "Upper body",
      "Lower body",
      "Full body",
      "Olympic lifting",
      "Powerlifting",
    ],
  },
  cardio: {
    label: "Cardio",
    accent: "var(--color-amber-400, #fbbf24)",
    hint: "Running, cycling, rowing, swimming",
    suggest: [
      "Easy run",
      "Zone 2 run",
      "Tempo run",
      "Long run",
      "Interval run",
      "Cycling",
      "Indoor bike",
      "Rowing",
      "Swimming",
      "Hiking",
    ],
  },
  hiit: {
    label: "HIIT",
    accent: "var(--color-pink-400, #f472b6)",
    hint: "Work/rest intervals, conditioning",
    suggest: [
      "Assault bike intervals",
      "Rowing sprints",
      "Kettlebell flow",
      "EMOM",
      "AMRAP",
      "Tabata",
      "Metcon",
      "Circuit training",
    ],
  },
  calisthenics: {
    label: "Calisthenics",
    accent: "var(--color-teal-400, #2dd4bf)",
    hint: "Bodyweight skills, reps or holds",
    suggest: [
      "Pull volume",
      "Push volume",
      "Levers & planche",
      "Handstand practice",
      "Muscle-ups",
      "Ring work",
      "Core skills",
    ],
  },
  mobility: {
    label: "Mobility",
    accent: "var(--color-slate-400, #94a3b8)",
    hint: "Stretching, yoga, flexibility",
    suggest: [
      "Morning flow",
      "Hip + thoracic",
      "Yoga",
      "Foam rolling",
      "Animal flow",
      "Stretching",
    ],
  },
  sport: {
    label: "Sport",
    accent: "var(--color-blue-400, #60a5fa)",
    hint: "Any activity with a ball, board, or opponent",
    suggest: [
      "Bouldering",
      "Climbing",
      "Pickleball",
      "Tennis",
      "Basketball",
      "Soccer",
      "Jiu-jitsu",
      "Boxing",
      "Surfing",
    ],
  },
  other: {
    label: "Other",
    accent: "var(--color-purple-400, #c084fc)",
    hint: "Anything else that moved your body",
    suggest: [
      "Rucking",
      "Manual labor",
      "Dance",
      "Martial arts",
      "Rehab",
      "Cold plunge",
      "Sauna",
    ],
  },
};

export const CATEGORY_IDS: WorkoutCategory[] = [
  "strength",
  "cardio",
  "hiit",
  "calisthenics",
  "mobility",
  "sport",
  "other",
];
