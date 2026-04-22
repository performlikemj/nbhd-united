"use client";

import { useFuelProfileQuery } from "@/lib/queries";

export function ProfileCard() {
  const { data: profile } = useFuelProfileQuery();

  if (!profile) return null;

  // Don't show the card if profile is pending — the settings page handles that messaging
  if (profile.onboarding_status === "pending") return null;

  if (profile.onboarding_status === "declined") {
    return (
      <div className="rounded-panel border border-border bg-surface-elevated p-4 mb-6">
        <p className="text-sm text-ink-muted">
          Using general workouts. Chat with your assistant anytime to set up a personalized fitness profile.
        </p>
      </div>
    );
  }

  if (profile.onboarding_status === "in_progress") {
    return (
      <div className="rounded-panel border border-accent/25 bg-accent/5 p-4 mb-6">
        <p className="text-sm text-ink-muted">
          Your fitness profile is being set up. Continue chatting with your assistant to complete it.
        </p>
      </div>
    );
  }

  // completed
  return (
    <div className="rounded-panel border border-border bg-surface-elevated p-4 mb-6">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-accent text-[10px] font-bold uppercase tracking-[0.2em]">Profile</span>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        {profile.fitness_level && (
          <div>
            <span className="text-ink-muted">Level</span>{" "}
            <span className="font-medium capitalize">{profile.fitness_level}</span>
          </div>
        )}
        {profile.days_per_week && (
          <div>
            <span className="text-ink-muted">Days/wk</span>{" "}
            <span className="font-medium">{profile.days_per_week}</span>
          </div>
        )}
        {profile.goals.length > 0 && (
          <div>
            <span className="text-ink-muted">Goals</span>{" "}
            <span className="font-medium">{profile.goals.map(g => g.replace(/_/g, " ")).join(", ")}</span>
          </div>
        )}
        {profile.equipment.length > 0 && (
          <div>
            <span className="text-ink-muted">Equipment</span>{" "}
            <span className="font-medium">{profile.equipment.map(e => e.replace(/_/g, " ")).join(", ")}</span>
          </div>
        )}
      </div>
      {profile.limitations.length > 0 && (
        <div className="mt-2 text-sm">
          <span className="text-ink-muted">Limitations</span>{" "}
          <span className="font-medium">{profile.limitations.join(", ")}</span>
        </div>
      )}
    </div>
  );
}
