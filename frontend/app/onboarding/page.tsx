"use client";

import { OnboardingShell } from "@/components/onboarding/onboarding-shell";
import { PersonaScene } from "@/components/onboarding/persona-scene";
import { MessagingScene } from "@/components/onboarding/messaging-scene";
import { LaunchSequence } from "@/components/onboarding/launch-sequence";
import { SectionCardSkeleton } from "@/components/skeleton";
import {
  useMeQuery,
  useTelegramStatusQuery,
  useLineStatusQuery,
} from "@/lib/queries";

export default function OnboardingPage() {
  const { data: me, isLoading } = useMeQuery();
  const tenant = me?.tenant;
  const hasTenant = Boolean(tenant);

  const { data: telegramStatus } = useTelegramStatusQuery(hasTenant);
  const { data: lineStatus } = useLineStatusQuery(hasTenant);

  const isTelegramLinked = Boolean(tenant?.user.telegram_chat_id) || Boolean(telegramStatus?.linked);
  const isLineLinked = Boolean(lineStatus?.linked);
  const messagingLinked = isTelegramLinked || isLineLinked;

  if (isLoading) {
    return (
      <OnboardingShell>
        <div className="w-full max-w-[580px]">
          <SectionCardSkeleton lines={6} />
        </div>
      </OnboardingShell>
    );
  }

  // Determine which scene to show
  let scene: "persona" | "messaging" | "launch";
  if (!hasTenant) {
    scene = "persona";
  } else if (!messagingLinked) {
    scene = "messaging";
  } else {
    scene = "launch";
  }

  return (
    <OnboardingShell>
      {scene === "persona" && <PersonaScene />}
      {scene === "messaging" && <MessagingScene />}
      {scene === "launch" && <LaunchSequence />}
    </OnboardingShell>
  );
}
