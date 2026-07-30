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
  APPLE_TRANSACTION_REFRESH_MS,
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
import { hasPendingAppAuthorize } from "@/lib/app-authorize-stash";

export type { AppleAuthenticationResult } from "@/lib/apple-auth";

interface SharedAppleButtonProps {
  className?: string;
  disabled?: boolean;
  label?: string;
  legalCopy?: ReactNode;
  showDivider?: boolean;
}

interface AuthenticateAppleButtonProps extends SharedAppleButtonProps {
  flow: "authenticate";
  onAuthenticated: (
    result: AppleAuthenticationResult,
  ) => void | Promise<void>;
}

interface LinkAppleButtonProps extends SharedAppleButtonProps {
  flow: "link";
  currentPassword: string;
  onLinked: () => void | Promise<void>;
}

type AppleSignInButtonProps =
  | AuthenticateAppleButtonProps
  | LinkAppleButtonProps;

type PreparationStatus = "preparing" | "ready" | "failed";

const emptySubscribe = () => () => {};
const PREPARATION_RETRY_MS = 5_000;
const RATE_LIMIT_RETRY_MS = 60_000;

function getEligibilitySnapshot(): boolean {
  if (typeof window === "undefined") return false;
  return isAppleSignInEligible({
    clientId: APPLE_CLIENT_ID,
    redirectUri: APPLE_REDIRECT_URI,
    origin: window.location.origin,
    hasPendingHandoff: hasPendingAppAuthorize(),
  });
}

export function AppleSignInButton(props: AppleSignInButtonProps) {
  // Server/static-export snapshot is false. The first client snapshot performs
  // all eligibility checks, so no Apple markup participates in hydration.
  const eligible = useSyncExternalStore(
    emptySubscribe,
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
        const elapsed = Date.now() - prepared.preparationStartedAt;
        refreshTimerRef.current = window.setTimeout(
          prepareFresh,
          Math.max(0, APPLE_TRANSACTION_REFRESH_MS - elapsed),
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
        // The visible, enabled failed-state button can also retry immediately.
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
    [prepareFresh],
  );

  const finishAttempt = useCallback(
    async (
      prepared: PreparedAppleAuthorization,
      signInPromise: ReturnType<typeof activateApplePopup>,
    ) => {
      try {
        const response = await signInPromise;
        if (props.flow === "authenticate") {
          const result = await submitAppleAuthorization({
            flow: "authenticate",
            prepared,
            response,
          });
          await props.onAuthenticated(result);
        } else {
          await submitAppleAuthorization({
            flow: "link",
            prepared,
            response,
            currentPassword: props.currentPassword,
          });
          await props.onLinked();
        }
        attemptInFlightRef.current = false;
        setBusy(false);
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
    if (!prepared) {
      setErrorMessage(
        getAppleAuthErrorMessage({ kind: "popup" }),
      );
      prepareFresh();
      return;
    }
    if (
      Date.now() - prepared.preparationStartedAt >=
      APPLE_TRANSACTION_REFRESH_MS
    ) {
      preparedRef.current = null;
      setErrorMessage(
        getAppleAuthErrorMessage({ kind: "popup" }),
      );
      prepareFresh();
      return;
    }

    attemptInFlightRef.current = true;
    preparedRef.current = null;
    if (refreshTimerRef.current !== null) {
      window.clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = null;
    }
    setBusy(true);

    let signInPromise: ReturnType<typeof activateApplePopup>;
    try {
      // Frozen activation contract: this synchronous call is the first
      // asynchronous boundary after the user gesture. signIn() takes no args.
      signInPromise = activateApplePopup(prepared);
    } catch (error) {
      handleFailure(error);
      return;
    }
    void finishAttempt(prepared, signInPromise);
  }

  if (!eligible) return null;

  const disabled =
    Boolean(props.disabled) || busy || status === "preparing";
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
