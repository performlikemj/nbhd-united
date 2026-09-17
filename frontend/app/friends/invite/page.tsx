"use client";

import Link from "next/link";
import { useSyncExternalStore } from "react";

import { AppStoreBadge } from "@/components/app-store-badge";
import { isLoggedIn } from "@/lib/auth";
import { getErrorMessage, isNotFoundError } from "@/lib/errors";
import { useClaimFriendInviteMutation, useFriendInviteQuery } from "@/lib/queries";

import s from "./page.module.css";

function subscribe(callback: () => void) {
  window.addEventListener("popstate", callback);
  window.addEventListener("storage", callback);
  window.addEventListener("focus", callback);
  return () => {
    window.removeEventListener("popstate", callback);
    window.removeEventListener("storage", callback);
    window.removeEventListener("focus", callback);
  };
}

// SWA serves this one exported page for /friends/invite/*, preserving the URL.
// There is no dynamic Next route, so useParams cannot supply the token. Read
// the browser path after hydration to keep the exported loading shell stable.
function browserPath() { return window.location.pathname; }
function serverPath() { return ""; }
function serverLoggedIn() { return false; }

export default function FriendInvitePage() {
  const pathname = useSyncExternalStore(subscribe, browserPath, serverPath);
  const loggedIn = useSyncExternalStore(subscribe, isLoggedIn, serverLoggedIn);
  const token = pathname.match(/^\/friends\/invite\/([A-Za-z0-9_-]+)\/?$/)?.[1] ?? null;
  return <Invite key={token} token={token} ready={!!pathname} loggedIn={loggedIn} />;
}

function Invite({ token, ready, loggedIn }: { token: string | null; ready: boolean; loggedIn: boolean }) {
  const invite = useFriendInviteQuery(token);
  const claim = useClaimFriendInviteMutation(token ?? "");
  const data = invite.data;
  const invalid = (ready && !token) || data?.valid === false || isNotFoundError(invite.error);
  const loading = !ready || (!!token && invite.isPending);
  const name = data?.inviter_display_name || data?.inviter_handle || "Your neighbor";
  const hue = Number.isFinite(data?.inviter_hue) ? data!.inviter_hue : 145;

  return (
    <section className={s.screen} aria-labelledby="invite-heading">
      <header className={s.header}>
        <Link href="/" className={s.wordmark}>NBHD</Link>
        <span className={s.label}>A neighborly invitation</span>
      </header>
      <div className={s.frame}>
        <aside className={s.margin} aria-hidden="true">
          <span className={s.label}>Room for connection</span>
          <span className={s.mark}>↗</span>
        </aside>
        <div className={s.content} aria-live="polite" aria-busy={loading}>
          {claim.isSuccess ? (
            <>
              <p className={s.label}>Connected</p>
              <h1 id="invite-heading">You&apos;re neighbors with {name}.</h1>
              <Link href="/friends" className={s.primary}>Go to your neighborhood</Link>
            </>
          ) : loading ? (
            <>
              <p className={s.label}>One moment</p>
              <h1 id="invite-heading">Opening your invitation.</h1>
              <p role="status">Loading invite…</p>
            </>
          ) : invalid ? (
            <>
              <p className={s.label}>Invitation unavailable</p>
              <h1 id="invite-heading">This invite has expired or already been used.</h1>
              <Link href="/" className={s.secondary}>Go to NBHD home</Link>
            </>
          ) : invite.isError ? (
            <>
              <h1 id="invite-heading">We couldn&apos;t open this invitation.</h1>
              <p role="alert">{getErrorMessage(invite.error)}</p>
              <button className={s.primary} onClick={() => void invite.refetch()} disabled={invite.isFetching}>
                {invite.isFetching ? "Trying again…" : "Try again"}
              </button>
            </>
          ) : data ? (
            <>
              <span className={s.avatar} style={{ backgroundColor: `hsl(${hue} 35% 78%)` }} aria-hidden="true">
                {name.slice(0, 1).toUpperCase()}
              </span>
              <h1 id="invite-heading">
                <strong>{name}</strong> <span className={s.handle}>(@{data.inviter_handle})</span> wants to be your neighbor
              </h1>
              {loggedIn ? (
                <div className={s.actions}>
                  <button className={s.primary} onClick={() => claim.mutate()} disabled={claim.isPending}>
                    {claim.isPending ? "Accepting…" : "Accept"}
                  </button>
                  {claim.isError && <p role="alert">{getErrorMessage(claim.error)}</p>}
                </div>
              ) : (
                <div className={s.actions}>
                  <p>Get NBHD to find your neighborhood.</p>
                  <div>
                    <p className={s.label}>Get NBHD</p>
                    <AppStoreBadge height={48} className={s.badge} />
                  </div>
                  <Link className={s.secondary} href={`/login?next=${encodeURIComponent(`/friends/invite/${token}`)}`}>
                    Sign in
                  </Link>
                </div>
              )}
            </>
          ) : null}
          <div className={s.rule} aria-hidden="true"><i /><i /><i /><i /><i /></div>
        </div>
      </div>
    </section>
  );
}
