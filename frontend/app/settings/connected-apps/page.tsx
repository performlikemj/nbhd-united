"use client";

import clsx from "clsx";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ErrorBoundary } from "@/components/error-boundary";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { useMintPATMutation, usePATsQuery, useRevokePATMutation } from "@/lib/queries";
import type { PATCreateResponse, PATScope, PersonalAccessToken } from "@/lib/types";

// ── Helpers ──────────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return "Never";
  const date = new Date(iso);
  const diff = Date.now() - date.getTime();
  const future = diff < 0;
  const seconds = Math.floor(Math.abs(diff) / 1000);
  if (seconds < 60) return future ? "in a moment" : "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return future ? `in ${hours}h` : `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return future ? `in ${days}d` : `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return future ? `in ${months}mo` : `${months}mo ago`;
  const years = Math.floor(days / 365);
  return future ? `in ${years}y` : `${years}y ago`;
}

function isExpired(pat: PersonalAccessToken): boolean {
  return Boolean(pat.expires_at) && new Date(pat.expires_at!).getTime() <= Date.now();
}

function isLikelyLost(pat: PersonalAccessToken): boolean {
  if (pat.last_used_at) return false;
  const ageMs = Date.now() - new Date(pat.created_at).getTime();
  return ageMs > 5 * 60 * 1000;
}

// ── Scope chips ──────────────────────────────────────────────────────────────

function ScopeChip({ scope }: { scope: PATScope }) {
  const tone = scope === "sessions:write" ? "emerald" : "sky";
  const cls =
    tone === "emerald"
      ? "border-emerald-text/30 bg-status-emerald/15 text-emerald-text"
      : "border-status-sky-text/30 bg-status-sky/15 text-status-sky-text";
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.08em]",
        cls,
      )}
    >
      {scope}
    </span>
  );
}

// ── Token row ────────────────────────────────────────────────────────────────

function TokenRow({
  pat,
  onRevoke,
}: {
  pat: PersonalAccessToken;
  onRevoke: (pat: PersonalAccessToken) => void;
}) {
  const expired = isExpired(pat);
  const possiblyLost = isLikelyLost(pat);

  return (
    <article className="rounded-panel border border-border bg-surface-elevated p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-medium text-ink truncate">{pat.name}</h3>
            {expired ? <StatusPill status="expired" /> : null}
          </div>
          <p className="mt-1 font-mono text-xs text-ink-faint">
            pat_{pat.token_prefix}…
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {pat.scopes.map((s) => (
              <ScopeChip key={s} scope={s} />
            ))}
          </div>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:max-w-md">
            <dt className="text-ink-faint">Last used</dt>
            <dd className="text-ink-muted">{relativeTime(pat.last_used_at)}</dd>
            <dt className="text-ink-faint">Expires</dt>
            <dd className="text-ink-muted">
              {pat.expires_at ? relativeTime(pat.expires_at) : "Never"}
            </dd>
            <dt className="text-ink-faint">Created</dt>
            <dd className="text-ink-muted">{relativeTime(pat.created_at)}</dd>
          </dl>
          {possiblyLost ? (
            <p className="mt-3 rounded-lg border border-rose-border/40 bg-rose-bg/50 px-3 py-2 text-xs text-rose-text">
              Not used yet. If you didn&apos;t save the token, revoke this one and create a new one.
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => onRevoke(pat)}
          className="rounded-full border border-rose-border/60 px-3 py-1.5 text-sm text-rose-text transition hover:bg-rose-bg/50 disabled:cursor-not-allowed disabled:opacity-45 min-h-[36px]"
        >
          Revoke
        </button>
      </div>
    </article>
  );
}

// ── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({ onConnect }: { onConnect: () => void }) {
  return (
    <article className="rounded-panel border border-dashed border-border bg-surface/50 p-8 text-center">
      <h3 className="font-headline text-lg text-ink">No connected apps yet.</h3>
      <p className="mx-auto mt-2 max-w-md text-sm text-ink-muted">
        Apps like YardTalk push your work-session context here so your assistant can answer
        questions like &ldquo;what was I working on yesterday?&rdquo; — without being there.
      </p>
      <button
        type="button"
        onClick={onConnect}
        className="glow-purple mt-6 rounded-full bg-accent px-5 py-3 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] min-h-[44px]"
      >
        Connect an app
      </button>
    </article>
  );
}

// ── Modal shell ──────────────────────────────────────────────────────────────

function Modal({
  open,
  onClose,
  closable,
  children,
  labelledBy,
}: {
  open: boolean;
  onClose: () => void;
  closable: boolean;
  children: React.ReactNode;
  labelledBy: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && closable) {
        onClose();
      }
    };
    window.addEventListener("keydown", handler);
    // Focus first focusable element inside the modal
    const first = containerRef.current?.querySelector<HTMLElement>(
      "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
    );
    first?.focus();
    return () => window.removeEventListener("keydown", handler);
  }, [open, closable, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
      onClick={() => closable && onClose()}
    >
      <div className="absolute inset-0 bg-overlay backdrop-blur-md" aria-hidden="true" />
      <div
        ref={containerRef}
        onClick={(e) => e.stopPropagation()}
        className="glass-card-horizons relative w-full max-w-lg rounded-xl p-6 shadow-panel animate-reveal sm:p-8"
      >
        {children}
      </div>
    </div>
  );
}

// ── Connect-an-app modal ─────────────────────────────────────────────────────

type Preset = "yardtalk" | "custom";
type Step = "preset" | "form" | "reveal";

const ALL_SCOPES: PATScope[] = ["sessions:write", "sessions:read"];

const EXPIRY_OPTIONS: { label: string; value: number | null }[] = [
  { label: "Never", value: null },
  { label: "30 days", value: 30 },
  { label: "90 days", value: 90 },
  { label: "1 year", value: 365 },
];

function ConnectAppModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const mint = useMintPATMutation();
  const [step, setStep] = useState<Step>("preset");
  const [preset, setPreset] = useState<Preset>("yardtalk");
  const [name, setName] = useState("YardTalk");
  const [scopes, setScopes] = useState<PATScope[]>(["sessions:write"]);
  const [expiresInDays, setExpiresInDays] = useState<number | null>(null);
  const [minted, setMinted] = useState<PATCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const reset = useCallback(() => {
    setStep("preset");
    setPreset("yardtalk");
    setName("YardTalk");
    setScopes(["sessions:write"]);
    setExpiresInDays(null);
    setMinted(null);
    setCopied(false);
    setErrorMsg(null);
  }, []);

  // Reset state on close
  useEffect(() => {
    if (!open) {
      // Defer reset slightly so close animation can use prior state
      const t = setTimeout(reset, 150);
      return () => clearTimeout(t);
    }
  }, [open, reset]);

  const choosePreset = (p: Preset) => {
    setPreset(p);
    if (p === "yardtalk") {
      setName("YardTalk");
      setScopes(["sessions:write"]);
      setExpiresInDays(null);
    } else {
      setName("");
      setScopes(["sessions:write"]);
      setExpiresInDays(null);
    }
    setStep("form");
  };

  const submitMint = async () => {
    setErrorMsg(null);
    try {
      const payload = {
        name: name.trim() || preset,
        scopes,
        ...(expiresInDays !== null ? { expires_in_days: expiresInDays } : {}),
      };
      const result = await mint.mutateAsync(payload);
      setMinted(result);
      setStep("reveal");
    } catch (err) {
      const status = (err as Error & { status?: number }).status;
      if (status === 429) {
        setErrorMsg(
          "You've created several tokens recently. Please wait a few minutes and try again.",
        );
      } else if (status === 400) {
        setErrorMsg("That request wasn't valid. Check the name and scopes.");
      } else {
        setErrorMsg(
          err instanceof Error ? err.message : "Could not create the token. Please try again.",
        );
      }
    }
  };

  const copyToken = async () => {
    if (!minted) return;
    try {
      await navigator.clipboard.writeText(minted.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      setErrorMsg("Couldn't copy automatically. Select the token and copy manually.");
    }
  };

  const closable = step !== "reveal" || copied;

  return (
    <Modal open={open} onClose={onClose} closable={closable} labelledBy="connect-app-title">
      {step === "preset" ? (
        <>
          <h2 id="connect-app-title" className="font-headline text-xl font-bold text-ink">
            Connect an app
          </h2>
          <p className="mt-1 text-sm text-ink-muted">
            Choose what you&apos;re connecting. We&apos;ll mint a token to paste into it.
          </p>
          <div className="mt-6 grid gap-3">
            <button
              type="button"
              onClick={() => choosePreset("yardtalk")}
              className="flex flex-col items-start rounded-panel border border-border bg-surface-elevated p-4 text-left transition hover:border-accent/40 hover:bg-accent/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <span className="font-headline text-base font-semibold text-ink">YardTalk</span>
              <span className="mt-1 text-xs text-ink-muted">
                macOS app. Pushes work-session summaries with screen-recording context.
              </span>
            </button>
            <button
              type="button"
              onClick={() => choosePreset("custom")}
              className="flex flex-col items-start rounded-panel border border-border bg-surface-elevated p-4 text-left transition hover:border-accent/40 hover:bg-accent/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <span className="font-headline text-base font-semibold text-ink">Custom</span>
              <span className="mt-1 text-xs text-ink-muted">
                Any other app or script. You&apos;ll see a curl example after minting.
              </span>
            </button>
          </div>
          <div className="mt-6 flex justify-end">
            <button
              type="button"
              onClick={onClose}
              className="rounded-full border border-border px-4 py-2 text-sm text-ink-muted transition hover:bg-surface-hover min-h-[36px]"
            >
              Cancel
            </button>
          </div>
        </>
      ) : null}

      {step === "form" ? (
        <>
          <h2 id="connect-app-title" className="font-headline text-xl font-bold text-ink">
            {preset === "yardtalk" ? "Connect YardTalk" : "Connect a custom app"}
          </h2>
          <p className="mt-1 text-sm text-ink-muted">
            Give the token a label and scope. The raw token will be shown once.
          </p>

          <div className="mt-6 space-y-5">
            <div>
              <label
                htmlFor="pat-name"
                className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40"
              >
                Label
              </label>
              <input
                id="pat-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={255}
                className="mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] placeholder:text-white/25 focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] outline-none transition"
                placeholder="e.g. YardTalk on MacBook Pro"
              />
            </div>

            {preset === "custom" ? (
              <fieldset>
                <legend className="font-mono text-[10px] uppercase tracking-[0.14em] text-white/40">
                  Scopes
                </legend>
                <div className="mt-2 space-y-2">
                  {ALL_SCOPES.map((scope) => {
                    const checked = scopes.includes(scope);
                    return (
                      <label
                        key={scope}
                        className="flex cursor-pointer items-center gap-3 rounded-lg border border-border bg-surface-elevated px-3 py-2.5 text-sm text-ink transition hover:border-accent/40"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) => {
                            setScopes((curr) =>
                              e.target.checked
                                ? [...curr, scope]
                                : curr.filter((s) => s !== scope),
                            );
                          }}
                          className="h-4 w-4 rounded border-border bg-transparent accent-accent"
                        />
                        <span className="font-mono text-xs">{scope}</span>
                        <span className="text-xs text-ink-faint">
                          {scope === "sessions:write" ? "Push sessions" : "Read sessions back"}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>
            ) : null}

            <div>
              <label
                htmlFor="pat-expiry"
                className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40"
              >
                Expires
              </label>
              <select
                id="pat-expiry"
                value={expiresInDays ?? ""}
                onChange={(e) =>
                  setExpiresInDays(e.target.value === "" ? null : Number(e.target.value))
                }
                className="mt-1 w-full rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 text-sm text-[#e0e3e8] focus:border-[#5dd9d0]/50 focus:shadow-[0_0_8px_rgba(93,217,208,0.15)] outline-none transition"
              >
                {EXPIRY_OPTIONS.map((opt) => (
                  <option key={opt.label} value={opt.value ?? ""}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            {errorMsg ? (
              <p className="rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
                {errorMsg}
              </p>
            ) : null}
          </div>

          <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-between">
            <button
              type="button"
              onClick={() => setStep("preset")}
              className="rounded-full border border-border px-4 py-2 text-sm text-ink-muted transition hover:bg-surface-hover min-h-[36px]"
            >
              Back
            </button>
            <button
              type="button"
              onClick={submitMint}
              disabled={
                mint.isPending ||
                !name.trim() ||
                (preset === "custom" && scopes.length === 0)
              }
              className="glow-purple rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {mint.isPending ? "Minting…" : "Create token"}
            </button>
          </div>
        </>
      ) : null}

      {step === "reveal" && minted ? (
        <>
          <h2 id="connect-app-title" className="font-headline text-xl font-bold text-ink">
            Save your token
          </h2>
          <p className="mt-1 text-sm text-rose-text">
            This is the only time you&apos;ll see it. Copy it before closing this dialog.
          </p>

          <div className="mt-5">
            <label
              htmlFor="pat-token"
              className="block font-mono text-[10px] uppercase tracking-[0.14em] text-white/40"
            >
              Token
            </label>
            <div className="mt-1 flex gap-2">
              <input
                id="pat-token"
                type="text"
                readOnly
                value={minted.token}
                onFocus={(e) => e.currentTarget.select()}
                className="min-w-0 flex-1 rounded-xl border border-white/10 bg-white/[0.05] px-4 py-3 font-mono text-sm text-[#e0e3e8] outline-none"
              />
              <button
                type="button"
                onClick={copyToken}
                className={clsx(
                  "rounded-xl border px-4 py-3 text-sm font-medium transition min-h-[44px] whitespace-nowrap",
                  copied
                    ? "border-emerald-text/40 bg-status-emerald/15 text-emerald-text"
                    : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20",
                )}
              >
                {copied ? "Copied ✓" : "Copy"}
              </button>
            </div>
          </div>

          <SetupInstructions preset={preset} />

          {errorMsg ? (
            <p className="mt-4 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
              {errorMsg}
            </p>
          ) : null}

          <div className="mt-6 flex justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={!copied}
              className="glow-purple rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 min-h-[44px]"
            >
              {copied ? "I've saved it" : "Copy first"}
            </button>
          </div>
        </>
      ) : null}
    </Modal>
  );
}

function SetupInstructions({ preset }: { preset: Preset }) {
  if (preset === "yardtalk") {
    return (
      <div className="mt-5 rounded-panel border border-border bg-surface-elevated p-4 text-sm text-ink-muted">
        <p className="font-medium text-ink">Next: paste it in YardTalk</p>
        <ol className="mt-2 list-inside list-decimal space-y-1 text-xs">
          <li>Open YardTalk → Preferences → NU</li>
          <li>Paste the token into the &ldquo;NU access token&rdquo; field</li>
          <li>Save — the next session you record will push automatically</li>
        </ol>
      </div>
    );
  }
  return (
    <div className="mt-5 rounded-panel border border-border bg-surface-elevated p-4 text-sm text-ink-muted">
      <p className="font-medium text-ink">Push a session</p>
      <pre className="mt-2 overflow-x-auto rounded-lg bg-bg/60 p-3 font-mono text-[11px] text-ink">
{`curl -X POST https://nbhd.example.com/api/v1/sessions/create/ \\
  -H "Authorization: Bearer pat_…" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -H "Content-Type: application/json" \\
  -d '{
    "source": "myapp/0.1.0",
    "project": "side project",
    "project_identity": "https://github.com/me/side-project.git",
    "session_start": "2026-04-28T14:00:00Z",
    "session_end":   "2026-04-28T15:00:00Z",
    "summary": "what got done"
  }'`}
      </pre>
    </div>
  );
}

// ── Revoke confirm modal ─────────────────────────────────────────────────────

function RevokeConfirmModal({
  pat,
  onClose,
}: {
  pat: PersonalAccessToken | null;
  onClose: () => void;
}) {
  const revoke = useRevokePATMutation();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  if (!pat) return null;

  const handleRevoke = async () => {
    setErrorMsg(null);
    try {
      await revoke.mutateAsync(pat.id);
      onClose();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Could not revoke. Please try again.");
    }
  };

  return (
    <Modal open={true} onClose={onClose} closable={!revoke.isPending} labelledBy="revoke-title">
      <h2 id="revoke-title" className="font-headline text-xl font-bold text-ink">
        Revoke this token?
      </h2>
      <p className="mt-2 text-sm text-ink-muted">
        <span className="text-ink">{pat.name}</span> will stop working immediately. Any app using
        it will need a new token.
      </p>

      {errorMsg ? (
        <p className="mt-4 rounded-xl border border-rose-border bg-rose-bg px-4 py-2.5 text-sm text-rose-text">
          {errorMsg}
        </p>
      ) : null}

      <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button
          type="button"
          onClick={onClose}
          disabled={revoke.isPending}
          className="rounded-full border border-border px-4 py-2 text-sm text-ink-muted transition hover:bg-surface-hover disabled:opacity-50 min-h-[36px]"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleRevoke}
          disabled={revoke.isPending}
          className="rounded-full bg-rose-text/90 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-rose-text disabled:opacity-50 min-h-[44px]"
        >
          {revoke.isPending ? "Revoking…" : "Revoke"}
        </button>
      </div>
    </Modal>
  );
}

// ── Page content ─────────────────────────────────────────────────────────────

function ConnectedAppsContent() {
  const { data: pats, isLoading, error } = usePATsQuery();
  const [connectOpen, setConnectOpen] = useState(false);
  const [revoking, setRevoking] = useState<PersonalAccessToken | null>(null);

  const sortedPATs = useMemo(() => {
    if (!pats) return [];
    return [...pats].sort((a, b) => {
      // Most-recently-created first
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [pats]);

  if (isLoading) return <SectionCardSkeleton lines={4} />;

  return (
    <SectionCard
      title="Connected Apps"
      subtitle="Apps that push work context to your assistant. Each app gets its own revocable token."
    >
      {error ? (
        <p className="mb-4 rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
          Could not fetch tokens. Please refresh and try again.
        </p>
      ) : null}

      {sortedPATs.length === 0 ? (
        <EmptyState onConnect={() => setConnectOpen(true)} />
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm text-ink-muted">
              {sortedPATs.length} {sortedPATs.length === 1 ? "token" : "tokens"}
            </p>
            <button
              type="button"
              onClick={() => setConnectOpen(true)}
              className="glow-purple rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white transition-all hover:brightness-110 active:scale-[0.98] min-h-[36px]"
            >
              Connect an app
            </button>
          </div>
          {sortedPATs.map((pat) => (
            <TokenRow key={pat.id} pat={pat} onRevoke={setRevoking} />
          ))}
        </div>
      )}

      <ConnectAppModal open={connectOpen} onClose={() => setConnectOpen(false)} />
      <RevokeConfirmModal
        key={revoking?.id ?? "closed"}
        pat={revoking}
        onClose={() => setRevoking(null)}
      />
    </SectionCard>
  );
}

export default function ConnectedAppsPage() {
  return (
    <div className="space-y-4">
      <Suspense fallback={<SectionCardSkeleton lines={4} />}>
        <ErrorBoundary
          fallback={
            <p className="rounded-panel border border-rose-border bg-rose-bg p-3 text-sm text-rose-text">
              Could not load Connected Apps. Please refresh and try again.
            </p>
          }
        >
          <ConnectedAppsContent />
        </ErrorBoundary>
      </Suspense>
    </div>
  );
}
