"use client";

import clsx from "clsx";
import { useEffect } from "react";

import type {
  EngagementActionId,
  EngagementReward,
} from "@/lib/engagement/store";

const ACCENT_CLASSES: Record<EngagementActionId, string> = {
  showUp: "text-engagement-show-up",
  move: "text-engagement-move",
  sit: "text-engagement-sit",
  reflect: "text-engagement-reflect",
};

function BurstStar({ action }: { action: EngagementActionId }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={clsx("h-9 w-9", ACCENT_CLASSES[action])}
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="m12 2.8 2.2 5.8 6.2.3-4.8 3.9 1.7 6-5.3-3.4-5.3 3.4 1.7-6-4.8-3.9 6.2-.3L12 2.8Z" />
    </svg>
  );
}

export function RewardMoment({
  reward,
  onDismiss,
}: {
  reward: EngagementReward;
  onDismiss: (id: number) => void;
}) {
  useEffect(() => {
    const timeout = window.setTimeout(
      () => onDismiss(reward.id),
      reward.isPerfectDay ? 2800 : 2200,
    );
    return () => window.clearTimeout(timeout);
  }, [onDismiss, reward.id, reward.isPerfectDay]);

  const rayCount = reward.isPerfectDay ? 8 : 6;

  return (
    <div
      className="absolute inset-0 z-20 flex items-center justify-center rounded-xl bg-bg/95 px-5 text-center backdrop-blur-sm"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <div className="flex flex-col items-center">
        <div
          className={clsx(
            "relative flex items-center justify-center motion-safe:animate-reward-burst",
            reward.isPerfectDay ? "h-28 w-28" : "h-24 w-24",
          )}
          aria-hidden="true"
        >
          {Array.from({ length: rayCount }).map((_, index) => (
            <span
              key={index}
              className="absolute inset-1"
              style={{ transform: `rotate(${(360 / rayCount) * index}deg)` }}
            >
              <span
                className={clsx(
                  "absolute left-1/2 top-0 h-5 w-px origin-bottom -translate-x-1/2 bg-current opacity-60 motion-safe:animate-reward-ray",
                  ACCENT_CLASSES[reward.action],
                )}
                style={{ animationDelay: `${index * 45}ms` }}
              />
            </span>
          ))}
          <span
            className={clsx(
              "absolute rounded-full bg-current opacity-10",
              ACCENT_CLASSES[reward.action],
              reward.isPerfectDay ? "h-20 w-20" : "h-16 w-16",
            )}
          />
          <BurstStar action={reward.action} />
        </div>
        <p
          className={clsx(
            "font-serif text-xl leading-snug text-ink sm:text-2xl",
            reward.isPerfectDay && "max-w-sm",
          )}
        >
          {reward.affirmation}
        </p>
      </div>
    </div>
  );
}
