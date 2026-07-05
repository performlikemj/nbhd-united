"use client";

import clsx from "clsx";
import { CSSProperties, FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { ConfirmDialog } from "@/components/journal/confirm-dialog";
import { IconMore } from "@/components/icons/constellation";
import { SectionCard } from "@/components/section-card";
import { Skeleton, SectionCardSkeleton } from "@/components/skeleton";
import { StatusPill } from "@/components/status-pill";
import { emitToast } from "@/components/toast";
import { fetchSharePreview } from "@/lib/api";
import { getErrorMessage } from "@/lib/errors";
import {
  useAbsorbedQuery,
  useAcceptWaveMutation,
  useApprovedLessonsQuery,
  useApproveShareMutation,
  useBlockWaveMutation,
  useDeclineWaveMutation,
  useMarkThreadReadMutation,
  useNeighborhoodQuery,
  useNeighborProfileQuery,
  useOpenThreadMutation,
  usePatchMembershipMutation,
  usePendingSharesQuery,
  usePurgeAbsorbedMutation,
  useRejectShareMutation,
  useSendMessageMutation,
  useSendWaveMutation,
  useShareLessonMutation,
  useThreadMessagesQuery,
  useThreadsQuery,
  useUnfriendMutation,
  useUpdateNeighborProfileMutation,
} from "@/lib/queries";
import type { ChatThread, Neighbor, NeighborProfile, PendingShare } from "@/lib/types";

// True while the tab is in the foreground — gates the ~4s message poll so a
// backgrounded tab (or the panel being closed, which unmounts ThreadView
// entirely) never keeps hitting a possibly-hibernated tenant container.
function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState === "visible",
  );
  useEffect(() => {
    const handler = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, []);
  return visible;
}

// The one place a raw dynamic color is allowed — the avatar hue is a
// user-chosen 0-359 value, not a design-system token.
function avatarStyle(hue: number): CSSProperties {
  return { backgroundColor: `hsl(${hue} 70% 55%)` };
}

export default function FriendsPage() {
  const { data, isLoading } = useNeighborhoodQuery();
  const { data: pendingShares = [] } = usePendingSharesQuery();
  const { data: threads = [], isLoading: threadsLoading } = useThreadsQuery();
  const acceptMutation = useAcceptWaveMutation();
  const declineMutation = useDeclineWaveMutation();
  const blockMutation = useBlockWaveMutation();
  const unfriendMutation = useUnfriendMutation();
  const openThreadMutation = useOpenThreadMutation();

  const [confirmTarget, setConfirmTarget] = useState<Neighbor | null>(null);
  const [reviewingShare, setReviewingShare] = useState<PendingShare | null>(null);
  const [openThread, setOpenThread] = useState<ChatThread | null>(null);

  const neighbors = data?.neighbors ?? [];
  const pendingIncoming = data?.pending_incoming ?? [];
  const pendingOutgoing = data?.pending_outgoing ?? [];
  const hasRequests = pendingIncoming.length > 0 || pendingOutgoing.length > 0;
  const hasApprovals = pendingShares.length > 0;

  const requestUnfriend = (neighbor: Neighbor) => setConfirmTarget(neighbor);
  const confirmUnfriend = () => {
    if (confirmTarget) unfriendMutation.mutate(confirmTarget.friendship_id);
    setConfirmTarget(null);
  };

  // Reuse the thread's own row when one already exists for this neighbor
  // (accurate muted/agent_absorb_enabled/unread), otherwise open a fresh one
  // seeded from the neighbor's own profile fields.
  const handleMessageNeighbor = async (neighbor: Neighbor) => {
    const existing = threads.find((t) => t.friendship_id === neighbor.friendship_id);
    if (existing) {
      setOpenThread(existing);
      return;
    }
    try {
      const result = await openThreadMutation.mutateAsync({ friendshipId: neighbor.friendship_id });
      setOpenThread({
        thread_id: result.thread_id,
        friendship_id: neighbor.friendship_id,
        display_name: neighbor.display_name,
        handle: neighbor.handle,
        avatar_hue: neighbor.avatar_hue,
        unread: 0,
        last_message: "",
        last_message_at: null,
        muted: false,
        agent_absorb_enabled: false,
      });
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
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
            {hasApprovals && (
              <SectionCard title="Approvals" subtitle="Nothing goes out until you say so.">
                <div className="space-y-2">
                  {pendingShares.map((share) => (
                    <div
                      key={share.id}
                      className="flex items-start gap-3 rounded-xl border border-border bg-surface/60 p-3"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="line-clamp-2 text-sm text-ink">{share.lesson_preview}</p>
                        <p className="mt-1 truncate text-xs text-ink-faint">To {share.audience}</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => setReviewingShare(share)}
                        className="min-h-[44px] shrink-0 rounded-full border border-accent/40 bg-accent/10 px-4 text-xs font-semibold text-accent transition hover:bg-accent/20"
                      >
                        Review &amp; approve
                      </button>
                    </div>
                  ))}
                </div>
              </SectionCard>
            )}

            {hasRequests && (
              <SectionCard
                title="Requests"
                subtitle="Waves waiting on a reply."
                delay={hasApprovals ? 80 : 0}
              >
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
              title="Messages"
              subtitle={
                threads.length > 0
                  ? `${threads.length} ${threads.length === 1 ? "conversation" : "conversations"}`
                  : undefined
              }
              delay={(hasApprovals ? 80 : 0) + (hasRequests ? 80 : 0)}
            >
              {threadsLoading ? (
                <div className="space-y-2">
                  <Skeleton className="h-16 w-full rounded-xl" />
                  <Skeleton className="h-16 w-full rounded-xl" />
                </div>
              ) : threads.length === 0 ? (
                <p className="rounded-panel border border-dashed border-border bg-surface/40 p-8 text-center text-sm text-ink-muted">
                  No conversations yet.
                </p>
              ) : (
                <div className="space-y-2">
                  {threads.map((thread) => (
                    <ThreadRow key={thread.thread_id} thread={thread} onOpen={() => setOpenThread(thread)} />
                  ))}
                </div>
              )}
            </SectionCard>

            <SectionCard
              title="Neighbors"
              subtitle={`${neighbors.length} ${neighbors.length === 1 ? "neighbor" : "neighbors"}`}
              delay={(hasApprovals ? 80 : 0) + (hasRequests ? 80 : 0) + 80}
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
                      onMessage={() => void handleMessageNeighbor(neighbor)}
                    />
                  ))}
                </div>
              )}
            </SectionCard>

            <ShareLessonCard />
            <WaveForm />
            <ProfileEditor />
            <AbsorbedCard />
          </>
        )}
      </div>

      {reviewingShare && (
        <SharePreviewModal share={reviewingShare} onClose={() => setReviewingShare(null)} />
      )}

      {openThread && <ThreadView thread={openThread} onClose={() => setOpenThread(null)} />}

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
  onMessage,
}: {
  neighbor: Neighbor;
  onRequestUnfriend: () => void;
  onBlock: () => void;
  blocking: boolean;
  onMessage: () => void;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border bg-surface/60 p-3">
      <span className="h-9 w-9 shrink-0 rounded-full" style={avatarStyle(neighbor.avatar_hue)} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-ink">{neighbor.display_name}</p>
        <p className="truncate text-xs text-ink-faint">@{neighbor.handle}</p>
      </div>
      <button
        type="button"
        onClick={onMessage}
        className="min-h-[44px] shrink-0 rounded-full border border-accent/40 bg-accent/10 px-4 text-xs font-semibold text-accent transition hover:bg-accent/20"
      >
        Message
      </button>
      <NeighborMenu
        label={`Actions for ${neighbor.display_name}`}
        onUnfriend={onRequestUnfriend}
        onBlock={onBlock}
        blocking={blocking}
      />
    </div>
  );
}

// Row in the "Messages" section's thread list — mirrors NeighborRow's
// avatar/name/handle layout but adds a last-message preview + unread pill,
// and the whole row is the tap target (opens the thread) rather than a
// dedicated action button.
function ThreadRow({ thread, onOpen }: { thread: ChatThread; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex min-h-[44px] w-full items-center gap-3 rounded-xl border border-border bg-surface/60 p-3 text-left transition hover:bg-surface-hover"
    >
      <span className="h-9 w-9 shrink-0 rounded-full" style={avatarStyle(thread.avatar_hue)} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <p className="truncate text-sm font-medium text-ink">{thread.display_name}</p>
          {thread.handle && <p className="shrink-0 truncate text-xs text-ink-faint">@{thread.handle}</p>}
        </div>
        <p className="truncate text-xs text-ink-muted">{thread.last_message || "Say hello…"}</p>
      </div>
      {thread.unread > 0 && (
        <span
          className="flex h-5 min-w-[20px] shrink-0 items-center justify-center rounded-full bg-accent px-1.5 text-[10px] font-semibold text-white"
          aria-label={`${thread.unread} unread`}
        >
          {thread.unread}
        </span>
      )}
    </button>
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

// 1:1 chat with one neighbor. A bottom-sheet/dialog overlay — same
// backdrop+sticky-footer language as SharePreviewModal — rather than a
// bespoke inline panel, so it reads as part of the same design system.
// Polls messages every ~4s while mounted and the tab is visible; marks the
// thread read once per open.
function ThreadView({ thread, onClose }: { thread: ChatThread; onClose: () => void }) {
  const visible = useDocumentVisible();
  const { data: threads = [] } = useThreadsQuery();
  // Prefer the live row from the thread list (accurate muted/unread) once
  // it's loaded; fall back to the snapshot passed in when opening.
  const liveThread = threads.find((t) => t.thread_id === thread.thread_id) ?? thread;

  const { data, isLoading } = useThreadMessagesQuery(thread.thread_id, { active: visible });
  const sendMutation = useSendMessageMutation(thread.thread_id);
  const markReadMutation = useMarkThreadReadMutation();
  const patchMembershipMutation = usePatchMembershipMutation(thread.thread_id);

  const [draft, setDraft] = useState("");
  const [sendError, setSendError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const markedReadRef = useRef(false);

  const messages = data?.messages ?? [];

  // Mark read once per opened thread, not on every poll/refetch.
  useEffect(() => {
    if (markedReadRef.current) return;
    markedReadRef.current = true;
    markReadMutation.mutate(thread.thread_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread.thread_id]);

  // Keep the newest message in view as the list grows.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  // Esc-to-close — never trapped, matches SharePreviewModal.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || sendMutation.isPending) return;
    setSendError(null);
    setDraft("");
    try {
      await sendMutation.mutateAsync({ text, clientMsgId: crypto.randomUUID() });
    } catch (err) {
      setSendError(getErrorMessage(err));
    }
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="thread-view-title"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-overlay backdrop-blur-md" aria-hidden="true" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative flex h-[88vh] w-full max-h-[92vh] flex-col overflow-hidden rounded-t-2xl border border-border bg-card shadow-panel animate-reveal sm:h-[600px] sm:max-h-[85vh] sm:max-w-lg sm:rounded-2xl"
      >
        <div className="flex justify-center pt-2.5 pb-1 sm:hidden">
          <span className="h-1 w-9 rounded-full bg-border" aria-hidden="true" />
        </div>

        <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3 sm:px-6">
          <span className="h-9 w-9 shrink-0 rounded-full" style={avatarStyle(liveThread.avatar_hue)} aria-hidden />
          <div className="min-w-0 flex-1">
            <h2 id="thread-view-title" className="truncate font-headline text-base font-bold text-ink">
              {liveThread.display_name}
            </h2>
            {liveThread.handle && <p className="truncate text-xs text-ink-faint">@{liveThread.handle}</p>}
          </div>
          <button
            type="button"
            onClick={() => patchMembershipMutation.mutate({ muted: !liveThread.muted })}
            aria-pressed={liveThread.muted}
            className={clsx(
              "min-h-[44px] shrink-0 rounded-full border px-3 text-xs font-medium transition",
              liveThread.muted
                ? "border-accent/40 bg-accent/10 text-accent"
                : "border-border text-ink-muted hover:bg-surface-hover hover:text-ink",
            )}
          >
            {liveThread.muted ? "Muted" : "Mute"}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-full text-ink-faint transition hover:bg-surface-hover hover:text-ink"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <div ref={scrollRef} className="flex-1 space-y-2 overflow-y-auto px-4 py-4 sm:px-6">
          {isLoading && messages.length === 0 ? (
            <p className="pt-8 text-center text-sm text-ink-muted">Loading&hellip;</p>
          ) : messages.length === 0 ? (
            <p className="pt-8 text-center text-sm text-ink-muted">
              Say hello &mdash; this is the start of your conversation.
            </p>
          ) : (
            messages.map((msg) => (
              <div key={msg.public_id} className={clsx("flex", msg.mine ? "justify-end" : "justify-start")}>
                <div
                  className={clsx(
                    "max-w-[80%] rounded-2xl px-4 py-2 text-sm leading-relaxed break-words",
                    msg.mine ? "bg-accent text-white" : "border border-border bg-surface/60 text-ink",
                  )}
                >
                  {msg.text}
                </div>
              </div>
            ))
          )}
        </div>

        {sendError && (
          <p
            role="alert"
            className="mx-4 mb-2 shrink-0 rounded-xl border border-rose-border bg-rose-bg px-3 py-2 text-xs text-rose-text sm:mx-6"
          >
            {sendError}
          </p>
        )}

        <div className="flex shrink-0 items-end gap-2 border-t border-border bg-surface/95 px-4 py-3 backdrop-blur-md sm:px-6">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit();
              }
            }}
            rows={1}
            placeholder="Message…"
            aria-label="Message"
            className="max-h-32 min-h-[44px] flex-1 resize-none rounded-xl border border-border bg-surface/60 px-4 py-2.5 text-sm text-ink outline-none placeholder:text-ink-faint transition focus:border-accent/50"
          />
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!draft.trim() || sendMutation.isPending}
            className="glow-purple min-h-[44px] shrink-0 rounded-full bg-accent px-5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

// Transparency surface (PR4): sparks a neighbor shared that the assistant has
// pulled into its own context via agent tooling. Hidden entirely when there's
// nothing to show — an empty "nothing absorbed" card would just be noise on
// a page that's already mostly empty for most tenants.
function AbsorbedCard() {
  const { data: items = [], isLoading } = useAbsorbedQuery();
  const purgeMutation = usePurgeAbsorbedMutation();

  if (isLoading) {
    return <SectionCardSkeleton lines={2} />;
  }

  if (items.length === 0) {
    return null;
  }

  const handlePurge = (id: string) => {
    purgeMutation.mutate(id, {
      onSuccess: () => emitToast("Purged — your assistant will stop using it.", "success"),
    });
  };

  return (
    <SectionCard
      title="What your assistant absorbed"
      subtitle="Sparks your neighbors shared that your assistant is holding. Purge anything you don't want it to use."
      delay={280}
    >
      <div className="space-y-2">
        {items.map((item) => {
          const purging = purgeMutation.isPending && purgeMutation.variables === item.id;
          return (
            <div
              key={item.id}
              className="flex items-start gap-3 rounded-xl border border-border bg-surface/60 p-3"
            >
              <div className="min-w-0 flex-1">
                <p className="line-clamp-2 text-sm text-ink">{item.label || "a shared spark"}</p>
                {item.from_handle && (
                  <p className="mt-1 truncate text-xs text-ink-faint">from @{item.from_handle}</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => handlePurge(item.id)}
                disabled={purging}
                className="min-h-[44px] shrink-0 rounded-full border border-rose-border bg-rose-bg/40 px-4 text-xs font-semibold text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50"
              >
                {purging ? "Purging…" : "Purge"}
              </button>
            </div>
          );
        })}
      </div>
    </SectionCard>
  );
}

// Chosen share affordance: the constellation lesson graph (app/constellation/page.tsx)
// is a bespoke hardcoded-hex SVG canvas with its own Inspector panel — not the
// design-token surface the rest of the app uses — so threading a neighbor
// picker through it would mean either breaking the token-only rule or bolting
// on a visually inconsistent overlay. Sharing from here instead keeps the
// whole trust flow (propose → Approvals → preview → approve) on one page,
// built entirely from existing tokens/components.
function ShareLessonCard() {
  const { data: lessons = [], isLoading: lessonsLoading } = useApprovedLessonsQuery();
  const { data: neighborhood, isLoading: neighborhoodLoading } = useNeighborhoodQuery();
  const neighbors = neighborhood?.neighbors ?? [];
  const shareLessonMutation = useShareLessonMutation();

  const [lessonId, setLessonId] = useState("");
  const [friendshipId, setFriendshipId] = useState("");

  const canShare = !!lessonId && !!friendshipId && !shareLessonMutation.isPending;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!lessonId || !friendshipId) return;
    try {
      await shareLessonMutation.mutateAsync({ lessonId: Number(lessonId), friendshipId });
      emitToast("Shared to your approval queue — review & approve it.", "success");
      setLessonId("");
      setFriendshipId("");
    } catch {
      // Pillar-blocked (403) / missing-field (400) failures already surface
      // via the default global error toast — see useShareLessonMutation.
    }
  };

  if (lessonsLoading || neighborhoodLoading) {
    return <SectionCardSkeleton lines={2} />;
  }

  return (
    <SectionCard
      title="Share a lesson"
      subtitle="Pick something you've learned and send it to a neighbor — they only see it once you approve the preview."
      delay={200}
    >
      {lessons.length === 0 ? (
        <p className="rounded-panel border border-dashed border-border bg-surface/40 p-6 text-center text-sm text-ink-muted">
          No approved lessons yet &mdash; approve one in your Constellation first.
        </p>
      ) : neighbors.length === 0 ? (
        <p className="rounded-panel border border-dashed border-border bg-surface/40 p-6 text-center text-sm text-ink-muted">
          Wave to a neighbor first &mdash; you&rsquo;ll be able to share with them once they accept.
        </p>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">Lesson</span>
            <select
              value={lessonId}
              onChange={(e) => setLessonId(e.target.value)}
              className="mt-1 min-h-[44px] w-full rounded-xl border border-border bg-surface/60 px-4 py-2.5 text-sm text-ink outline-none transition focus:border-accent/50"
            >
              <option value="">Choose a lesson&hellip;</option>
              {lessons.map((lesson) => (
                <option key={lesson.id} value={lesson.id}>
                  {lesson.text.length > 90 ? `${lesson.text.slice(0, 87)}…` : lesson.text}
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">Neighbor</span>
            <select
              value={friendshipId}
              onChange={(e) => setFriendshipId(e.target.value)}
              className="mt-1 min-h-[44px] w-full rounded-xl border border-border bg-surface/60 px-4 py-2.5 text-sm text-ink outline-none transition focus:border-accent/50"
            >
              <option value="">Choose a neighbor&hellip;</option>
              {neighbors.map((n) => (
                <option key={n.friendship_id} value={n.friendship_id}>
                  {n.display_name} (@{n.handle})
                </option>
              ))}
            </select>
          </label>

          <button
            type="submit"
            disabled={!canShare}
            className="glow-purple min-h-[44px] rounded-full bg-accent px-5 py-2.5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {shareLessonMutation.isPending ? "Sending…" : "Share"}
          </button>
        </form>
      )}
    </SectionCard>
  );
}

// The trust surface: what the neighbor will actually see, verbatim, before
// anything goes out. Mirrors the bottom-sheet/dialog pattern used by
// ConfirmDialog / ConnectAnthropicModal (backdrop + sticky footer), so it
// reads as part of the same design language rather than a bespoke overlay.
function SharePreviewModal({ share, onClose }: { share: PendingShare; onClose: () => void }) {
  const approveMutation = useApproveShareMutation();
  const rejectMutation = useRejectShareMutation();

  const [phase, setPhase] = useState<"loading" | "ready" | "failed">("loading");
  const [preview, setPreview] = useState<import("@/lib/types").SharePreview | null>(null);
  const [failureDetail, setFailureDetail] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const mountedRef = useRef(true);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Indirection so the 202 branch can schedule the next poll without the
  // callback below referencing its own `const` binding (a direct
  // self-reference there trips the react-hooks self-recursion lint rule).
  const loadPreviewRef = useRef<() => void>(() => {});

  const loadPreview = useCallback(async () => {
    try {
      const result = await fetchSharePreview(share.lesson_id, share.friendship_id);
      if (!mountedRef.current) return;
      if (result.status === 200) {
        setPreview(result.data);
        setDraft(result.data.redacted_text);
        setPhase("ready");
      } else if (result.status === 202) {
        pollRef.current = setTimeout(() => loadPreviewRef.current(), 2000);
      } else {
        setFailureDetail(result.detail);
        setPhase("failed");
      }
    } catch (err) {
      if (!mountedRef.current) return;
      setFailureDetail(getErrorMessage(err));
      setPhase("failed");
    }
  }, [share.lesson_id, share.friendship_id]);

  useEffect(() => {
    loadPreviewRef.current = () => void loadPreview();
  }, [loadPreview]);

  // Mount-only kickoff — the modal always fully remounts per share (the
  // parent toggles it through `null` between reviews via onClose), so the
  // `useState` initial values above already cover the "start loading, no
  // preview yet" state; this effect only needs to start the fetch chain.
  useEffect(() => {
    mountedRef.current = true;
    (async () => {
      await loadPreview();
    })();
    return () => {
      mountedRef.current = false;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [loadPreview]);

  // Esc-to-close — never trapped, matches ConnectAnthropicModal.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const busy = approveMutation.isPending || rejectMutation.isPending;

  const handleApprove = async () => {
    try {
      const result = await approveMutation.mutateAsync({ id: share.id, finalText: undefined });
      if (result.status === 200) {
        emitToast("Shared with your neighbor.", "success");
        onClose();
      } else if (result.status === 202) {
        // An edit elsewhere (or a re-run) triggered a re-scrub — go back to
        // polling so the human re-previews before it can publish.
        setEditing(false);
        setPhase("loading");
        void loadPreview();
      } else {
        setFailureDetail(result.detail);
        setPhase("failed");
      }
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  const handleEditSubmit = async () => {
    try {
      const result = await approveMutation.mutateAsync({ id: share.id, finalText: draft });
      if (result.status === 200) {
        emitToast("Shared with your neighbor.", "success");
        onClose();
      } else if (result.status === 202) {
        setEditing(false);
        setPhase("loading");
        void loadPreview();
      } else {
        setFailureDetail(result.detail);
        setPhase("failed");
      }
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  const handleReject = async () => {
    try {
      await rejectMutation.mutateAsync(share.id);
      emitToast("Declined — it won't be sent.", "success");
      onClose();
    } catch {
      // Unexpected failures surface via the default global error toast.
    }
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="share-preview-title"
      onClick={onClose}
    >
      <div className="absolute inset-0 bg-overlay backdrop-blur-md" aria-hidden="true" />
      <div
        onClick={(e) => e.stopPropagation()}
        className="relative flex max-h-[92vh] w-full flex-col overflow-y-auto rounded-t-2xl border border-border bg-card shadow-panel animate-reveal sm:max-h-[85vh] sm:max-w-lg sm:rounded-2xl"
      >
        <div className="flex justify-center pt-2.5 pb-1 sm:hidden">
          <span className="h-1 w-9 rounded-full bg-border" aria-hidden="true" />
        </div>

        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute right-3 top-3 z-10 flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full bg-surface/80 text-ink-muted backdrop-blur-md transition hover:bg-surface-hover hover:text-ink sm:right-4 sm:top-4"
        >
          <span aria-hidden="true">✕</span>
        </button>

        <div className="px-6 pb-2 pt-4 sm:p-8 sm:pb-2">
          <div className="pr-12 sm:pr-14">
            <h2 id="share-preview-title" className="font-headline text-xl font-bold text-ink sm:text-2xl">
              Review before it goes out
            </h2>
            <p className="mt-1 text-sm text-ink-muted">{share.lesson_preview}</p>
          </div>
        </div>

        <div className="flex-1 space-y-5 px-6 pb-6 sm:p-8 sm:pt-4">
          {phase === "loading" && (
            <div className="flex flex-col items-center gap-3 py-10 text-center">
              <span
                className="h-8 w-8 animate-spin rounded-full border-2 border-accent/30 border-t-accent"
                aria-hidden="true"
              />
              <p className="text-sm text-ink-muted">Preparing your preview safely&hellip;</p>
            </div>
          )}

          {phase === "failed" && (
            <p role="alert" className="rounded-xl border border-rose-border bg-rose-bg px-4 py-3 text-sm text-rose-text">
              {failureDetail}
            </p>
          )}

          {phase === "ready" && preview && !editing && (
            <>
              <blockquote className="rounded-xl border border-border bg-surface/60 px-4 py-3 text-sm italic leading-relaxed text-ink">
                &ldquo;{preview.redacted_text}&rdquo;
              </blockquote>
              <p className="text-xs text-ink-muted">
                This goes to <span className="font-medium text-ink">{preview.audience}</span>.
              </p>
              <p className="rounded-xl border border-amber-border/50 bg-amber-bg/40 px-4 py-2.5 text-xs leading-relaxed text-amber-text">
                {preview.residuals_banner}
              </p>
            </>
          )}

          {phase === "ready" && preview && editing && (
            <div className="space-y-2">
              <label
                htmlFor="share-edit-text"
                className="block font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint"
              >
                Edit before sending
              </label>
              <textarea
                id="share-edit-text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={5}
                className="w-full resize-y rounded-xl border border-border bg-surface/60 px-4 py-3 text-sm text-ink outline-none transition focus:border-accent/50"
              />
              <p className="text-xs text-ink-faint">
                Editing re-checks your text for anything we hide before it can be approved.
              </p>
            </div>
          )}
        </div>

        <div className="sticky bottom-0 border-t border-border bg-surface/95 px-6 py-4 backdrop-blur-md sm:px-8">
          {phase === "failed" && (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onClose}
                className="min-h-[44px] rounded-full border border-border px-5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover"
              >
                Close
              </button>
            </div>
          )}

          {phase === "ready" && !editing && (
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-between sm:gap-3">
              <button
                type="button"
                onClick={() => void handleReject()}
                disabled={busy}
                className="min-h-[44px] rounded-full border border-rose-border px-5 text-sm font-medium text-rose-text transition hover:bg-rose-bg disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rejectMutation.isPending ? "Declining…" : "Reject"}
              </button>
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:gap-3">
                <button
                  type="button"
                  onClick={() => setEditing(true)}
                  disabled={busy}
                  className="min-h-[44px] rounded-full border border-border px-5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Edit
                </button>
                <button
                  type="button"
                  onClick={() => void handleApprove()}
                  disabled={busy}
                  className="glow-purple min-h-[44px] rounded-full bg-accent px-5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {approveMutation.isPending ? "Approving…" : "Approve"}
                </button>
              </div>
            </div>
          )}

          {phase === "ready" && editing && (
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end sm:gap-3">
              <button
                type="button"
                onClick={() => {
                  setEditing(false);
                  setDraft(preview?.redacted_text ?? "");
                }}
                disabled={approveMutation.isPending}
                className="min-h-[44px] rounded-full border border-border px-5 text-sm font-medium text-ink-muted transition hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleEditSubmit()}
                disabled={approveMutation.isPending || !draft.trim()}
                className="glow-purple min-h-[44px] rounded-full bg-accent px-5 text-sm font-semibold text-white transition hover:brightness-110 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {approveMutation.isPending ? "Checking…" : "Save & re-check"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
