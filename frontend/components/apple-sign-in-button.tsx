"use client";

import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import {
  activateApplePopup,
  APPLE_CLIENT_ID,
  APPLE_REDIRECT_URI,
  AppleAuthFlowError,
  type AppleAuthenticationResult,
  initializeAppleAuthorization,
  normalizeAppleFailure,
  prepareAppleAuthorization,
  type PreparedAppleAuthorization,
  submitAppleAuthorization,
} from "@/lib/apple-auth";
import {
  getAppleAuthErrorMessage,
  isAppleSignInEligible,
} from "@/lib/apple-auth-decision";
import {
  AUTHORIZE_STASH_STORAGE_KEY,
  getAuthorizeStashExpiryMs,
  hasPendingAppAuthorize,
} from "@/lib/app-authorize-stash";
import {
  getAccessToken,
  getAuthenticationEpoch,
} from "@/lib/auth";

export type { AppleAuthenticationResult } from "@/lib/apple-auth";

interface SharedAppleButtonProps {
  className?: string;
  disabled?: boolean;
  label?: string;
  legalCopy?: ReactNode;
  onBusyChange?: (busy: boolean) => void;
  showDivider?: boolean;
}

interface AuthenticateAppleButtonProps extends SharedAppleButtonProps {
  flow: "authenticate";
  onAuthenticated: (
    result: AppleAuthenticationResult,
    authenticationEpoch: number,
  ) => void | Promise<void>;
}

interface LinkAppleButtonProps extends SharedAppleButtonProps {
  flow: "link";
  currentPassword: string;
  onLinked: () => void | Promise<void>;
  onTerminalFailure: () => void;
}

type AppleSignInButtonProps =
  | AuthenticateAppleButtonProps
  | LinkAppleButtonProps;

type PreparationStatus = "preparing" | "ready" | "failed";

const PREPARATION_RETRY_MS = 5_000;
const RATE_LIMIT_RETRY_MS = 60_000;

interface AppleAttemptSnapshot {
  authenticationEpoch: number;
  bearer: string | null;
  currentPassword: string;
}

function getEligibilitySnapshot(): boolean {
  if (typeof window === "undefined") return false;
  return isAppleSignInEligible({
    clientId: APPLE_CLIENT_ID,
    redirectUri: APPLE_REDIRECT_URI,
    origin: window.location.origin,
    hasPendingHandoff: hasPendingAppAuthorize(),
  });
}

function subscribeToEligibility(onStoreChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};

  let expiryTimer: number | null = null;

  const scheduleExpiryCheck = () => {
    if (expiryTimer !== null) window.clearTimeout(expiryTimer);
    expiryTimer = null;

    const expiresAt = getAuthorizeStashExpiryMs();
    if (expiresAt === null) return;
    // The stash is valid at exactly 15 minutes and expires one millisecond
    // later under authorize-stash-decision's strict greater-than check.
    expiryTimer = window.setTimeout(() => {
      expiryTimer = null;
      onStoreChange();
      scheduleExpiryCheck();
    }, Math.max(0, expiresAt - Date.now() + 1));
  };

  const handleStorage = (event: StorageEvent) => {
    if (event.key !== AUTHORIZE_STASH_STORAGE_KEY) return;
    scheduleExpiryCheck();
    onStoreChange();
  };

  window.addEventListener("storage", handleStorage);
  scheduleExpiryCheck();

  return () => {
    window.removeEventListener("storage", handleStorage);
    if (expiryTimer !== null) window.clearTimeout(expiryTimer);
  };
}

export function AppleSignInButton(props: AppleSignInButtonProps) {
  // Server/static-export snapshot is false. The first client snapshot performs
  // all eligibility checks, so no Apple markup participates in hydration.
  const eligible = useSyncExternalStore(
    subscribeToEligibility,
    getEligibilitySnapshot,
    () => false,
  );
  const preparedRef = useRef<PreparedAppleAuthorization | null>(null);
  const refreshTimerRef = useRef<number | null>(null);
  const generationRef = useRef(0);
  const attemptInFlightRef = useRef(false);
  const [status, setStatus] = useState<PreparationStatus>("preparing");
  const [busy, setBusy] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");

  const prepareFresh = useCallback(function prepareFresh() {
    const generation = ++generationRef.current;
    preparedRef.current = null;
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    setStatus("preparing");

    void prepareAppleAuthorization()
      .then((prepared) => {
        if (generation !== generationRef.current) return;
        initializeAppleAuthorization(prepared);
        preparedRef.current = prepared;
        setStatus("ready");
        refreshTimerRef.current = window.setTimeout(
          prepareFresh,
          Math.max(0, prepared.refreshAt - Date.now()),
        );
      })
      .catch((error: unknown) => {
        if (generation !== generationRef.current) return;
        const failure = normalizeAppleFailure(error);
        setStatus("failed");
        setErrorMessage(
          getAppleAuthErrorMessage({
            kind: failure.kind,
            status: failure.status,
            errorCode: failure.errorCode,
          }),
        );
        // Preparation itself failed, so schedule a fresh SDK+begin prefetch.
        // The failed-state control remains disabled until that retry succeeds.
        refreshTimerRef.current = window.setTimeout(
          prepareFresh,
          failure.status === 429
            ? RATE_LIMIT_RETRY_MS
            : PREPARATION_RETRY_MS,
        );
      });
  }, []);

  useEffect(() => {
    if (!eligible) return;
    // Schedule outside the effect body to satisfy the compiler-aware hooks
    // rule while still prefetching both resources immediately after mount.
    const activationTimer = window.setTimeout(prepareFresh, 0);
    return () => {
      window.clearTimeout(activationTimer);
      generationRef.current += 1;
      preparedRef.current = null;
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    };
  }, [eligible, prepareFresh]);

  const handleFailure = useCallback(
    (error: unknown) => {
      const failure = normalizeAppleFailure(error);
      attemptInFlightRef.current = false;
      setBusy(false);
      props.onBusyChange?.(false);
      if (props.flow === "link") props.onTerminalFailure();
      setErrorMessage(
        getAppleAuthErrorMessage({
          kind: failure.kind,
          status: failure.status,
          errorCode: failure.errorCode,
        }),
      );
      // The attempted transaction is never reused, including popup cancel.
      prepareFresh();
    },
    [prepareFresh, props],
  );

  const finishAttempt = useCallback(
    async (
      prepared: PreparedAppleAuthorization,
      signInPromise: ReturnType<typeof activateApplePopup>,
      attempt: AppleAttemptSnapshot,
    ) => {
      try {
        const response = await signInPromise;
        assertAttemptCurrent(attempt.authenticationEpoch);
        if (props.flow === "authenticate") {
          const result = await submitAppleAuthorization({
            flow: "authenticate",
            prepared,
            response,
          });
          assertAttemptCurrent(attempt.authenticationEpoch);
          await props.onAuthenticated(
            result,
            attempt.authenticationEpoch,
          );
        } else {
          await submitAppleAuthorization({
            flow: "link",
            prepared,
            response,
            currentPassword: attempt.currentPassword,
            bearer: attempt.bearer,
            authenticationEpoch: attempt.authenticationEpoch,
          });
          assertAttemptCurrent(attempt.authenticationEpoch);
          await props.onLinked();
        }
        attemptInFlightRef.current = false;
        setBusy(false);
        props.onBusyChange?.(false);
      } catch (error) {
        handleFailure(error);
      }
    },
    [handleFailure, props],
  );

  function handleClick() {
    if (attemptInFlightRef.current || props.disabled) return;
    setErrorMessage("");

    const prepared = preparedRef.current;
    if (!prepared || Date.now() >= prepared.refreshAt) return;

    const attempt: AppleAttemptSnapshot = {
      authenticationEpoch: getAuthenticationEpoch(),
      bearer: props.flow === "link" ? getAccessToken() : null,
      currentPassword:
        props.flow === "link" ? props.currentPassword : "",
    };

    attemptInFlightRef.current = true;
    preparedRef.current = null;
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    setBusy(true);
    props.onBusyChange?.(true);

    let signInPromise: ReturnType<typeof activateApplePopup>;
    try {
      // Frozen activation contract: this synchronous call is the first
      // asynchronous boundary after the user gesture. signIn() takes no args.
      signInPromise = activateApplePopup(prepared);
    } catch (error) {
      handleFailure(error);
      return;
    }
    void finishAttempt(prepared, signInPromise, attempt);
  }

  if (!eligible) return null;

  const prepared = preparedRef.current;
  const disabled =
    Boolean(props.disabled) ||
    busy ||
    status !== "ready" ||
    !prepared ||
    Date.now() >= prepared.refreshAt;
  const label =
    props.label ??
    (props.flow === "link" ? "Connect Apple ID" : "Continue with Apple");

  return (
    <div className={props.className}>
      {props.showDivider ? (
        <div className="my-5 flex items-center gap-3" aria-hidden="true">
          <span className="h-px flex-1 bg-white/[0.08]" />
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/30">
            or
          </span>
          <span className="h-px flex-1 bg-white/[0.08]" />
        </div>
      ) : null}

      <button
        type="button"
        onClick={handleClick}
        disabled={disabled}
        className="flex min-h-[44px] w-full items-center justify-center gap-2 rounded-lg border border-black bg-white px-4 py-3 text-sm font-semibold text-black transition hover:bg-white/90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <svg
          viewBox="0 0 24 24"
          className="h-5 w-5"
          fill="currentColor"
          aria-hidden="true"
        >
          <path d="M16.7 12.9c0-2.1 1.7-3.1 1.8-3.2a3.9 3.9 0 0 0-3.1-1.7c-1.3-.1-2.6.8-3.2.8-.7 0-1.7-.8-2.8-.8a4.2 4.2 0 0 0-3.5 2.1c-1.5 2.6-.4 6.5 1 8.6.7 1 1.6 2.2 2.7 2.1 1.1 0 1.5-.7 2.9-.7 1.3 0 1.7.7 2.9.7s1.9-1 2.6-2.1a9.4 9.4 0 0 0 1.2-2.4 3.7 3.7 0 0 1-2.5-3.4ZM14.5 6.6a3.7 3.7 0 0 0 .9-2.7 3.8 3.8 0 0 0-2.5 1.3 3.5 3.5 0 0 0-.9 2.6 3.1 3.1 0 0 0 2.5-1.2Z" />
        </svg>
        <span>{busy ? "Connecting…" : label}</span>
      </button>

      {errorMessage ? (
        <p
          role="alert"
          className="mt-3 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text"
        >
          {errorMessage}
        </p>
      ) : null}

      {props.legalCopy}
    </div>
  );
}

function assertAttemptCurrent(expectedEpoch: number): void {
  if (getAuthenticationEpoch() !== expectedEpoch) {
    throw new AppleAuthFlowError({ kind: "popup" });
  }
}
