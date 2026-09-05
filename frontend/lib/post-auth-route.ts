export type PostAuthRoute =
  | "app-authorize"
  | "return-to-app"
  | "onboarding"
  | "journal";

export function decidePostAuthRoute(args: {
  hasPendingHandoff: boolean;
  fromApp: boolean;
  created: boolean;
  needsOnboarding: boolean;
}): PostAuthRoute {
  if (args.hasPendingHandoff) return "app-authorize";
  if (args.fromApp) return "return-to-app";
  if (args.created || args.needsOnboarding) return "onboarding";
  return "journal";
}
