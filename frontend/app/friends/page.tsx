"use client";

import { CSSProperties, FormEvent, useEffect, useRef, useState } from "react";

import { ConfirmDialog } from "@/components/journal/confirm-dialog";
import { IconMore } from "@/components/icons/constellation";
import { SectionCard } from "@/components/section-card";
import { SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { emitToast } from "@/components/toast";
import { getErrorMessage } from "@/lib/errors";
import {
  useAcceptWaveMutation,
  useBlockWaveMutation,
  useDeclineWaveMutation,
  useNeighborhoodQuery,
  useNeighborProfileQuery,
  useSendWaveMutation,
  useUnfriendMutation,
  useUpdateNeighborProfileMutation,
} from "@/lib/queries";
import type { Neighbor, NeighborProfile } from "@/lib/types";

// The one place a raw dynamic color is allowed — the avatar hue is a
// user-chosen 0-359 value, not a design-system token.
function avatarStyle(hue: number): CSSProperties {
  return { backgroundColor: `hsl(${hue} 70% 55%)` };
}

export default function FriendsPage() {
  const { data, isLoading } = useNeighborhoodQuery();
  const acceptMutation = useAcceptWaveMutation();
  const declineMutation = useDeclineWaveMutation();
  const blockMutation = useBlockWaveMutation();
  const unfriendMutation = useUnfriendMutation();

  const [confirmTarget, setConfirmTarget] = useState<Neighbor | null>(null);

  const neighbors = data?.neighbors ?? [];
  const pendingIncoming = data?.pending_incoming ?? [];
  const pendingOutgoing = data?.pending_outgoing ?? [];
  const hasRequests = pendingIncoming.length > 0 || pendingOutgoing.length > 0;

  const requestUnfriend = (neighbor: Neighbor) => setConfirmTarget(neighbor);
  const confirmUnfriend = () => {
    if (confirmTarget) unfriendMutation.mutate(confirmTarget.friendship_id);
    setConfirmTarget(null);
  };

  return (
    <div className="mx-auto pb-24">
      {/* ── Hero ── */}
      <header className="mb-8 sm:mb-10">
        <span className="mb-2 block text-[10px] font-bold uppercase tracking-[0.24em] text-signal sm:text-xs">
          Neighborhood
        </span>
        <h1 className="font-display text-4xl italic leading-[1.05] text-ink md:text-5xl">
          The people
          <br />
          <span className="text-ink-muted">you&rsquo;ve let in.</span>
        </h1>
        <p className="mt-4 max-w-[560px] text-sm leading-relaxed text-ink-muted">
          Wave to a neighbor by @handle, accept who waves back, and keep your own corner tidy.
          Nothing here is public &mdash; only people you&rsquo;ve both agreed to know each other.
        </p>
      </header>

      <div className="space-y-6">
        {isLoading ? (
          <>
            <SectionCardSkeleton lines={2} />
            <SectionCardSkeleton lines={3} />
          </>
        ) : (
          <>
            {hasRequests && (
              <SectionCard title="Requests" subtitle="Waves waiting on a reply.">
                {pendingIncoming.length > 0 && (
                  <div className="space-y-3">
                    {pendingIncoming.map((wave) => {
                      const accepting =
                        acceptMutation.isPending && acceptMutation.variables === wave.friendship_id;
                      const declining =
                        declineMutation.isPending && declineMutation.variables === wave.friendship_id;
                      return (
                        <div
                          key={wave.friendship_id}
                          className="flex items-start gap-3 rounded-xl border border-border bg-surface/60 p-3"
                        >
                          <span
                            className="mt-0.5 h-9 w-9 shrink-0 rounded-full"
                            style={avatarStyle(wave.avatar_hue)}
                            aria-hidden
                          />
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm font-medium text-ink">{wave.display_name}</p>
                            <p className="truncate text-xs text-ink-faint">@{wave.handle}</p>
                            {wave.note && (
                              <p className="mt-1 text-xs italic text-ink-muted">&ldquo;{wave.note}&rdquo;</p>
                            )}
                          </div>
                          <div className="flex shrink-0 items-center gap-2">
                            <button
                              type="button"
                              onClick={() => declineMutation.mutate(wave.friendship_id)}
                              disabled={declining || accepting}
                              className="min-h-[44px] rounded-lg border border-border px-3 text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {declining ? "Declining…" : "Decline"}
                            </button>
                            <button
                              type="button"
                              onClick={() => acceptMutation.mutate(wave.friendship_id)}
                              disabled={accepting || declining}
                              className="glow-purple min-h-[44px] rounded-lg bg-accent px-3 text-xs font-semibold text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {accepting ? "Accepting…" : "Accept"}
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}

                {pendingOutgoing.length > 0 && (
                  <div className={pendingIncoming.length > 0 ? "mt-5 border-t border-border pt-5" : ""}>
                    <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                      Waiting on them
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {pendingOutgoing.map((wave) => (
                        <span
                          key={wave.friendship_id}
                          className="inline-flex items-center gap-2 rounded-full border border-border bg-surface/40 px-3 py-1.5 text-xs text-ink-muted"
                        >
                          <span
                            className="h-2 w-2 rounded-full"
                            style={avatarStyle(wave.avatar_hue)}
                            aria-hidden
                          />
                          @{wave.handle}
                          <StatusPill status="pending" size="sm" />
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </SectionCard>
            )}

            <SectionCard
              title="Neighbors"
              subtitle={`${neighbors.length} ${neighbors.length === 1 ? "neighbor" : "neighbors"}`}
              delay={hasRequests ? 80 : 0}
            >
              {neighbors.length === 0 ? (
                <p className="rounded-panel border border-dashed border-border bg-surface/40 p-8 text-center text-sm text-ink-muted">
                  No neighbors yet &mdash; wave to someone by @handle below to get started.
                </p>
              ) : (
                <div className="space-y-2">
                  {neighbors.map((neighbor) => (
                    <NeighborRow
                      key={neighbor.friendship_id}
                      neighbor={neighbor}
                      onRequestUnfriend={() => requestUnfriend(neighbor)}
                      onBlock={() => blockMutation.mutate(neighbor.friendship_id)}
                      blocking={
                        blockMutation.isPending && blockMutation.variables === neighbor.friendship_id
                      }
                    />
                  ))}
                </div>
              )}
            </SectionCard>

            <WaveForm />
            <ProfileEditor />
          </>
        )}
      </div>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="Unfriend this neighbor?"
        message={
          confirmTarget
            ? `You and ${confirmTarget.display_name} will no longer be neighbors. They won’t be notified.`
            : ""
        }
        confirmLabel="Unfriend"
        variant="danger"
        onConfirm={confirmUnfriend}
        onCancel={() => setConfirmTarget(null)}
      />
    </div>
  );
}

function NeighborRow({
  neighbor,
  onRequestUnfriend,
  onBlock,
  blocking,
}: {
  neighbor: Neighbor;
  onRequestUnfriend: () => void;
  onBlock: () => void;
  blocking: boolean;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-surface/60 p-3">
      <span className="h-9 w-9 shrink-0 rounded-full" style={avatarStyle(neighbor.avatar_hue)} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-ink">{neighbor.display_name}</p>
        <p className="truncate text-xs text-ink-faint">@{neighbor.handle}</p>
      </div>
      <NeighborMenu
        label={`Actions for ${neighbor.display_name}`}
        onUnfriend={onRequestUnfriend}
        onBlock={onBlock}
        blocking={blocking}
      />
    </div>
  );
}

function NeighborMenu({
  label,
  onUnfriend,
  onBlock,
  blocking,
}: {
  label: string;
  onUnfriend: () => void;
  onBlock: () => void;
  blocking: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="relative shrink-0" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={label}
        aria-expanded={open}
        className="flex h-11 w-11 items-center justify-center rounded-full text-ink-faint transition hover:bg-surface-hover hover:text-ink"
      >
        <IconMore className="h-4 w-4" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-40 rounded-xl border border-border bg-card shadow-panel p-1">
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onUnfriend();
            }}
            className="flex min-h-[44px] w-full items-center rounded-lg px-3 py-2.5 text-left text-xs text-ink-muted transition hover:bg-surface-hover hover:text-ink"
          >
            Unfriend
          </button>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onBlock();
            }}
            disabled={blocking}
            className="flex min-h-[44px] w-full items-center rounded-lg px-3 py-2.5 text-left text-xs text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50"
          >
            Block
          </button>
        </div>
      )}
    </div>
  );
}

function WaveForm() {
  const sendWaveMutation = useSendWaveMutation();
  const [handle, setHandle] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = handle.trim().replace(/^@/, "");
    if (!trimmed) return;
    setError(null);
    try {
      const result = await sendWaveMutation.mutateAsync({
        handle: trimmed,
        note: note.trim() || undefined,
      });
      setHandle("");
      setNote("");
      emitToast(
        result.status === "accepted"
          ? `You and ${result.display_name} are neighbors now.`
          : `Wave sent to ${result.display_name}.`,
        "success",
      );
    } catch (err) {
      setError(getErrorMessage(err));
    }
  };

  return (
    <SectionCard
      title="Wave to a neighbor"
      subtitle="Send a wave by @handle — they’ll see it in their requests."
      delay={160}
    >
      <form onSubmit={submit} className="space-y-3">
        <div className="flex flex-col gap-3 sm:flex-row">
          <label className="flex-1 min-w-0">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">Handle</span>
            <div className="mt-1 flex items-center rounded-xl border border-border bg-surface/60 px-4 transition focus-within:border-accent/50 min-h-[44px]">
              <span className="text-ink-faint">@</span>
              <input
                type="text"
                value={handle}
                onChange={(e) => setHandle(e.target.value)}
                placeholder="handle"
                maxLength={30}
                autoCapitalize="none"
                autoCorrect="off"
                className="w-full bg-transparent px-1 py-2.5 text-sm text-ink outline-none placeholder:text-ink-faint"
              />
            </div>
          </label>
          <label className="flex-[2] min-w-0">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              Note (optional)
            </span>
            <input
              type="text"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Say hi…"
              maxLength={280}
              className="mt-1 w-full rounded-xl border border-border bg-surface/60 px-4 py-2.5 text-sm text-ink outline-none placeholder:text-ink-faint transition focus:border-accent/50 min-h-[44px]"
            />
          </label>
        </div>

        {error && (
          <p role="alert" className="rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={!handle.trim() || sendWaveMutation.isPending}
          className="glow-purple min-h-[44px] rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {sendWaveMutation.isPending ? "Waving…" : "Wave"}
        </button>
      </form>
    </SectionCard>
  );
}

function ProfileEditor() {
  const { data: profile, isLoading } = useNeighborProfileQuery();
  const mutation = useUpdateNeighborProfileMutation();

  const [handle, setHandle] = useState("");
  const [bio, setBio] = useState("");
  const [hue, setHue] = useState(210);
  const [savedHandle, setSavedHandle] = useState("");
  const [savedBio, setSavedBio] = useState("");
  const [savedHue, setSavedHue] = useState(210);
  const [seededFrom, setSeededFrom] = useState<string | null>(null);
  const [handleError, setHandleError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);

  // Seed local drafts from the server during render (not an effect) so a
  // background refetch that returns the *same* values never clobbers active
  // typing — mirrors components/core/context-panel.tsx.
  const incomingKey = profile ? `${profile.handle} ${profile.bio} ${profile.avatar_hue}` : null;
  if (incomingKey !== null && incomingKey !== seededFrom && profile) {
    setSeededFrom(incomingKey);
    setHandle(profile.handle);
    setBio(profile.bio);
    setHue(profile.avatar_hue);
    setSavedHandle(profile.handle);
    setSavedBio(profile.bio);
    setSavedHue(profile.avatar_hue);
  }

  const dirty = handle.trim() !== savedHandle || bio !== savedBio || hue !== savedHue;

  const save = async () => {
    setHandleError(null);
    setSaveError(null);
    try {
      const patch: Partial<NeighborProfile> = { bio, avatar_hue: hue };
      if (handle.trim() !== savedHandle) patch.handle = handle.trim().toLowerCase();
      const updated = await mutation.mutateAsync(patch);
      setSeededFrom(`${updated.handle} ${updated.bio} ${updated.avatar_hue}`);
      setHandle(updated.handle);
      setBio(updated.bio);
      setHue(updated.avatar_hue);
      setSavedHandle(updated.handle);
      setSavedBio(updated.bio);
      setSavedHue(updated.avatar_hue);
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 2600);
    } catch (err) {
      const msg = getErrorMessage(err);
      if (msg.toLowerCase().includes("handle")) {
        setHandleError(msg);
      } else {
        setSaveError(msg);
      }
    }
  };

  if (isLoading || !profile) {
    return <SectionCardSkeleton lines={3} />;
  }

  return (
    <SectionCard title="Your profile" subtitle="How neighbors see you." delay={240}>
      <div className="space-y-4">
        <div className="flex items-center gap-4">
          <span
            className="h-12 w-12 shrink-0 rounded-full border-2 border-border"
            style={avatarStyle(hue)}
            aria-hidden
          />
          <label className="flex-1 min-w-0">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              Avatar hue
            </span>
            <input
              type="range"
              min={0}
              max={359}
              value={hue}
              onChange={(e) => setHue(Number(e.target.value))}
              className="mt-2 w-full accent-accent"
              aria-label="Avatar hue"
            />
          </label>
        </div>

        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">Handle</span>
          <div className="mt-1 flex items-center rounded-xl border border-border bg-surface/60 px-4 transition focus-within:border-accent/50 min-h-[44px]">
            <span className="text-ink-faint">@</span>
            <input
              type="text"
              value={handle}
              onChange={(e) => setHandle(e.target.value.toLowerCase())}
              maxLength={30}
              autoCapitalize="none"
              autoCorrect="off"
              className="w-full bg-transparent px-1 py-2.5 text-sm text-ink outline-none"
            />
          </div>
          {handleError && (
            <p role="alert" className="mt-1.5 text-xs text-rose-text">
              {handleError}
            </p>
          )}
        </label>

        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
            Bio <span className="text-ink-faint">({bio.length}/280)</span>
          </span>
          <textarea
            value={bio}
            onChange={(e) => setBio(e.target.value.slice(0, 280))}
            rows={3}
            maxLength={280}
            placeholder="A line or two about you…"
            className="mt-1 w-full resize-none rounded-xl border border-border bg-surface/60 px-4 py-2.5 text-sm text-ink outline-none placeholder:text-ink-faint transition focus:border-accent/50"
          />
        </label>

        {saveError && (
          <p role="alert" className="rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text">
            {saveError}
          </p>
        )}

        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-ink-faint" role="status" aria-live="polite">
            {justSaved ? "Saved." : " "}
          </span>
          <button
            type="button"
            onClick={() => void save()}
            disabled={!dirty || !handle.trim() || mutation.isPending}
            className="glow-purple min-h-[44px] rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </SectionCard>
  );
}
