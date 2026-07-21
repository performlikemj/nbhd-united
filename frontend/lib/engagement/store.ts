export type EngagementActionId = "showUp" | "move" | "sit" | "reflect";

export interface EngagementReward {
  id: number;
  action: EngagementActionId;
  affirmation: string;
  isPerfectDay: boolean;
}

export interface EngagementState {
  completed: Record<EngagementActionId, boolean>;
  moveRestDay: boolean;
  rhythm: {
    days: number;
    window: number;
    best: number;
  };
  reward: EngagementReward | null;
}

const AFFIRMATIONS: Record<EngagementActionId, string> = {
  showUp: "You showed up today.", // PROTOTYPE: mocked
  move: "You moved today.", // PROTOTYPE: mocked
  sit: "You sat today.", // PROTOTYPE: mocked
  reflect: "You reflected today.", // PROTOTYPE: mocked
};

const listeners = new Set<() => void>();
let rewardId = 0;

// PROTOTYPE: mocked
let state: EngagementState = {
  completed: {
    showUp: true,
    move: false,
    sit: false,
    reflect: false,
  },
  moveRestDay: false,
  rhythm: {
    days: 9,
    window: 14,
    best: 11,
  },
  reward: null,
};

function emit(): void {
  listeners.forEach((listener) => listener());
}

function isPerfectDay(candidate: EngagementState): boolean {
  return (
    candidate.completed.showUp &&
    (candidate.completed.move || candidate.moveRestDay) &&
    candidate.completed.sit &&
    candidate.completed.reflect
  );
}

export function subscribeToEngagementStore(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getEngagementSnapshot(): EngagementState {
  return state;
}

// PROTOTYPE: mocked
export function toggleEngagementAction(action: EngagementActionId): void {
  const nextDone = !state.completed[action];
  state = {
    ...state,
    completed: { ...state.completed, [action]: nextDone },
    moveRestDay: action === "move" && nextDone ? false : state.moveRestDay,
  };
  emit();
}

// PROTOTYPE: mocked
export function fireEngagementReward(action: EngagementActionId): void {
  const wasPerfect = isPerfectDay(state);
  const nextState: EngagementState = {
    ...state,
    completed: { ...state.completed, [action]: true },
    moveRestDay: action === "move" ? false : state.moveRestDay,
    reward: null,
  };

  state = {
    ...nextState,
    reward: {
      id: ++rewardId,
      action,
      affirmation: AFFIRMATIONS[action],
      isPerfectDay: !wasPerfect && isPerfectDay(nextState),
    },
  };
  emit();
}

// PROTOTYPE: mocked
export function toggleMoveRestDay(): void {
  const nextRestDay = !state.moveRestDay;
  state = {
    ...state,
    completed: {
      ...state.completed,
      move: nextRestDay ? false : state.completed.move,
    },
    moveRestDay: nextRestDay,
  };
  emit();
}

// PROTOTYPE: mocked
export function reachPerfectDay(): void {
  state = {
    ...state,
    completed: {
      showUp: true,
      move: true,
      sit: true,
      reflect: true,
    },
    moveRestDay: false,
    reward: {
      id: ++rewardId,
      action: "reflect",
      affirmation: "The whole constellation is lit.", // PROTOTYPE: mocked
      isPerfectDay: true,
    },
  };
  emit();
}

export function dismissEngagementReward(id: number): void {
  if (state.reward?.id !== id) return;
  state = { ...state, reward: null };
  emit();
}
