"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { AppleSignInButton } from "@/components/apple-sign-in-button";
import { StatusPill } from "@/components/status-pill";

interface AppleLinkCardProps {
  linked: boolean;
}

export function AppleLinkCard({ linked }: AppleLinkCardProps) {
  const queryClient = useQueryClient();
  const [currentPassword, setCurrentPassword] = useState("");
  const [linkedThisSession, setLinkedThisSession] = useState(false);

  const isLinked = linked || linkedThisSession;

  const handleLinked = () => {
    setCurrentPassword("");
    setLinkedThisSession(true);
    void queryClient.invalidateQueries({ queryKey: ["me"] });
  };

  return (
    <div className="min-w-0 overflow-visible rounded-panel border border-border bg-surface-elevated p-4 sm:col-span-2">
      <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-muted">
        Apple ID
      </dt>
      <dd className="mt-1">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="text-sm leading-relaxed text-ink-muted">
            {isLinked
              ? "Connected for secure sign-in."
              : "Confirm your password, then connect an Apple ID to this account."}
          </p>
          {isLinked ? <StatusPill status="active" /> : null}
        </div>

        {!isLinked ? (
          <div className="mt-4 max-w-md space-y-3">
            <label
              htmlFor="apple-link-current-password"
              className="block font-mono text-[10px] uppercase tracking-[0.14em] text-ink-muted"
            >
              Current password
            </label>
            <input
              id="apple-link-current-password"
              type="password"
              required
              maxLength={128}
              autoComplete="current-password"
              aria-describedby="apple-link-password-help"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              className="min-h-[44px] w-full rounded-xl border border-border bg-surface px-4 py-3 text-sm text-ink outline-none transition placeholder:text-ink-faint focus:border-accent focus:ring-1 focus:ring-accent"
              placeholder="Enter your password"
            />
            <p id="apple-link-password-help" className="text-xs leading-relaxed text-ink-faint">
              This password check protects changes to your sign-in methods.
            </p>
            <AppleSignInButton
              flow="link"
              currentPassword={currentPassword}
              disabled={!currentPassword}
              onLinked={handleLinked}
              onTerminalFailure={() => setCurrentPassword("")}
            />
          </div>
        ) : (
          <div
            className="mt-4 flex items-start gap-2 rounded-panel border border-emerald-text/20 bg-emerald-bg p-3 text-sm text-emerald-text"
            role="status"
            aria-live="polite"
          >
            <svg
              className="mt-0.5 h-4 w-4 shrink-0"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
            </svg>
            <span>Apple ID connected</span>
          </div>
        )}
      </dd>
    </div>
  );
}
