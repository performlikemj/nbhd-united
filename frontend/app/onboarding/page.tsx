"use client";

import { useState } from "react";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { PersonaScene } from "@/components/onboarding/persona-scene";
import { ChannelScene } from "@/components/onboarding/channel-scene";
import type { ChannelOutcome } from "@/components/onboarding/channel-outcome";
import { LaunchSequence } from "@/components/onboarding/launch-sequence";
import { SectionCardSkeleton } from "@/components/skeleton";
import { useMeQuery } from "@/lib/queries";

export default function OnboardingPage() {
  const { data: me, isLoading } = useMeQuery();
  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);
  // Local-only outcome: linking is never an onboarding or authentication gate.
  // Reloading simply presents the channel step again with live server statuses.
  const [channelOutcome, setChannelOutcome] = useState<ChannelOutcome | null>(null);

  if (isLoading) {
    return (
      <OnboardingShell>
        <div className="w-full max-w-[580px]">
          <SectionCardSkeleton lines={6} />
        </div>
      </OnboardingShell>
    );
  }

  // Determine which scene to show: persona (no tenant) → channel → launch.
  let scene: "persona" | "channel" | "launch";
  if (!hasTenant) {
    scene = "persona";
  } else if (!channelOutcome) {
    scene = "channel";
  } else {
    scene = "launch";
  }

  return (
    <OnboardingShell>
      {scene === "persona" && <PersonaScene />}
      {scene === "channel" && <ChannelScene onContinue={setChannelOutcome} />}
      {scene === "launch" && channelOutcome ? (
        <LaunchSequence outcome={channelOutcome} />
      ) : null}
    </OnboardingShell>
  );
}
