"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { BreathingOrb } from "@/components/core/breathing-orb";
import { CoreAudioPlayer } from "@/components/core/audio-player";
import { CoreStats } from "@/components/core/core-stats";
import { CoreToast } from "@/components/core/core-toast";
import { MeditationLibrary } from "@/components/core/meditation-library";
import { PhaseTimeline } from "@/components/core/phase-timeline";
import { PHASES, computeCoreStats, toMeditation, type Meditation } from "@/lib/core";
import { composeMeditation, fetchMeditation, fetchMeditations } from "@/lib/api";

type Phase = "invite" | "composing" | "ready" | "failed";
const COMPOSE_MSGS = ["Drawing on your week…", "Finding the words…", "Placing the silences…", "Almost there…"];
const POLL_INTERVAL_MS = 3500;
// Stop *watching* after this long — the render's own channel ping still lands,
// so a slow render (rate-limited TTS) becomes "I'll message you" rather than a
// spinner that never ends.
const POLL_TIMEOUT_MS = 4 * 60_000;

export default function CorePage() {
  const [phase, setPhase] = useState<Phase>("invite");
  const [msgIdx, setMsgIdx] = useState(0);
  const [longRunning, setLongRunning] = useState(false);
  const [showToast, setShowToast] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [today, setToday] = useState<Meditation | null>(null);
  const [library, setLibrary] = useState<Meditation[]>([]);
  const [current, setCurrent] = useState<Meditation | null>(null);
  const [playing, setPlaying] = useState(false);

  // Guards an in-flight poll loop so it can be cancelled on unmount / re-compose.
  const pollRef = useRef<{ active: boolean }>({ active: false });

  const loadLibrary = useCallback(async (): Promise<Meditation[]> => {
    try {
      const sessions = await fetchMeditations();
      const meds = sessions.map(toMeditation);
      setLibrary(meds);
      return meds;
    } catch {
      return [];
    }
  }, []);

  // On mount: load the library, and if today's sit already rendered, surface it.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const meds = await loadLibrary();
      if (cancelled) return;
      const todays = meds.find((m) => m.dateLabel === "Today");
      if (todays) {
        setToday(todays);
        setPhase("ready");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadLibrary]);

  // Cancel any poll loop when the page unmounts.
  useEffect(() => {
    const ref = pollRef.current;
    return () => {
      ref.active = false;
    };
  }, []);

  // cycle the composing messages
  useEffect(() => {
    if (phase !== "composing") return;
    const t = setInterval(() => setMsgIdx((i) => (i + 1) % COMPOSE_MSGS.length), 700);
    return () => clearInterval(t);
  }, [phase]);

  const pollUntilDone = useCallback(
    (id: string) => {
      const started = Date.now();
      pollRef.current.active = true;
      const tick = async () => {
        if (!pollRef.current.active) return;
        let session: Awaited<ReturnType<typeof fetchMeditation>> | null = null;
        try {
          session = await fetchMeditation(id);
        } catch {
          // transient (network / refresh) — keep polling until the deadline
        }
        if (!pollRef.current.active) return;
        if (session) {
          if (session.status === "ready" || session.status === "delivered") {
            pollRef.current.active = false;
            const med = toMeditation(session);
            setToday(med);
            setPhase("ready");
            setShowToast(true); // stands in for the channel ping
            void loadLibrary();
            return;
          }
          if (session.status === "failed") {
            pollRef.current.active = false;
            setPhase("failed");
            setError("Your meditation couldn't be composed just now. Please try again in a moment.");
            return;
          }
        }
        if (Date.now() - started > POLL_TIMEOUT_MS) {
          // Give up watching, but it's still rendering — the ping will arrive.
          pollRef.current.active = false;
          setLongRunning(true);
          return;
        }
        window.setTimeout(tick, POLL_INTERVAL_MS);
      };
      void tick();
    },
    [loadLibrary],
  );

  const compose = useCallback(async () => {
    setError(null);
    setLongRunning(false);
    setShowToast(false);
    setPhase("composing");
    setMsgIdx(0);
    try {
      const { meditation_id } = await composeMeditation();
      pollUntilDone(meditation_id);
    } catch (e) {
      setPhase("failed");
      const status = (e as { status?: number } | null)?.status;
      setError(
        status === 403
          ? "Core isn't enabled yet — turn it on in Settings → Integrations, then come back."
          : status === 404
            ? "Your account isn't set up for Core yet. Please refresh, or contact support if this persists."
            : "Couldn't start your meditation. Please try again in a moment.",
      );
    }
  }, [pollUntilDone]);

  const play = (m: Meditation) => {
    if (current?.id === m.id) setPlaying((p) => !p);
    else {
      setCurrent(m);
      setPlaying(true);
    }
  };

  const onOrb = () => {
    if (phase === "invite" || phase === "failed") void compose();
    else if (phase === "ready" && today) play(today);
  };

  // The library already includes today's ready sit after loadLibrary; dedupe in
  // case the refetch hasn't landed yet so it never shows twice.
  const libItems = today && !library.some((m) => m.id === today.id) ? [today, ...library] : library;
  const stats = computeCoreStats(libItems);
  const todayActive = current?.id === today?.id;

  return (
    <div className="mx-auto overflow-x-hidden pb-36 sm:pb-32">
      {/* ── Hero ── */}
      <header className="mb-8 sm:mb-10">
        <span className="mb-2 block text-[10px] font-bold uppercase tracking-[0.24em] text-signal sm:text-xs">
          Core · Mindfulness
        </span>
        <h1 className="font-display text-4xl italic leading-[1.05] text-ink md:text-5xl">
          A quiet ten minutes,
          <br />
          <span className="text-ink-muted">whenever you need it.</span>
        </h1>
        <p className="mt-4 max-w-[560px] text-sm leading-relaxed text-ink-muted">
          Your assistant composes a guided meditation from what it&rsquo;s learned about your week
          &mdash; the words, the pacing, where the silences fall &mdash; then voices it aloud. Nothing
          runs, and nothing&rsquo;s billed, until you press the orb.
        </p>
      </header>

      {/* ── Today / compose ── */}
      <section className="relative mb-6 overflow-hidden rounded-panel border border-border bg-surface/50 p-8 shadow-panel sm:p-12">
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              "radial-gradient(ellipse 60% 50% at 50% 0%, rgba(78,205,196,0.10), transparent 70%), radial-gradient(ellipse 50% 40% at 80% 100%, rgba(124,107,240,0.08), transparent 70%)",
          }}
        />
        <div className="relative flex flex-col items-center text-center">
          <BreathingOrb
            compose={phase === "invite" || phase === "failed"}
            playing={playing && todayActive}
            onClick={onOrb}
          />

          {phase === "invite" && (
            <>
              <p className="mt-8 text-[10px] uppercase tracking-[0.22em] text-ink-faint">Today · on demand</p>
              <h2 className="mt-2 font-display text-2xl italic text-ink sm:text-3xl">Compose today&rsquo;s sit</h2>
              <p className="mx-auto mt-4 max-w-[430px] text-sm leading-relaxed text-ink-muted">
                Press the orb and your assistant writes a fresh ten minutes from your week, then reads it
                to you &mdash; pauses and all.
              </p>
              <p className="mt-5 font-mono text-[11px] text-ink-faint">
                Composed in the background · <span className="text-signal">then yours to replay free</span>, anytime
              </p>
            </>
          )}

          {phase === "composing" && (
            <>
              <p className="mt-8 text-[10px] uppercase tracking-[0.22em] text-signal">Composing</p>
              <h2 className="core-shimmer mt-2 font-display text-2xl italic sm:text-3xl">
                {longRunning ? "Still composing…" : COMPOSE_MSGS[msgIdx]}
              </h2>
              <p className="mt-5 font-mono text-[11px] text-ink-faint">
                {longRunning
                  ? "Taking a little longer than usual — I'll message you the moment it's ready."
                  : "I'll message you when it's ready — feel free to step away"}
              </p>
            </>
          )}

          {phase === "ready" && today && (
            <>
              <p className="mt-8 text-[10px] uppercase tracking-[0.22em] text-ink-faint">Today&rsquo;s sit</p>
              <h2 className="mt-2 font-display text-2xl italic text-ink sm:text-3xl">{today.title}</h2>
              <p className="mt-1.5 text-sm text-ink-muted">{today.durationMin} min · voice-guided</p>
              {today.theme && (
                <p className="mx-auto mt-4 max-w-[420px] text-sm leading-relaxed text-ink-muted">{today.theme}</p>
              )}
              <div className="mt-9 w-full max-w-[540px]">
                <PhaseTimeline phases={PHASES} />
              </div>
            </>
          )}

          {phase === "failed" && (
            <>
              <p className="mt-8 text-[10px] uppercase tracking-[0.22em] text-rose-text">Couldn&rsquo;t compose</p>
              <h2 className="mt-2 font-display text-2xl italic text-ink sm:text-3xl">Let&rsquo;s try that again</h2>
              <p className="mx-auto mt-4 max-w-[430px] text-sm leading-relaxed text-ink-muted">
                {error ?? "Something went wrong composing your meditation."}
              </p>
              <p className="mt-5 font-mono text-[11px] text-ink-faint">Press the orb to retry</p>
            </>
          )}
        </div>
      </section>

      <p className="mb-12 text-center text-xs text-ink-faint">
        Prefer it ready each morning? A daily auto-compose is coming as an opt-in setting &mdash; for now,
        press the orb whenever you want a sit.
      </p>

      {/* ── Stats ── */}
      <div className="mb-12">
        <CoreStats stats={stats} />
      </div>

      {/* ── Library ── */}
      <div className="mb-4 flex items-baseline justify-between">
        <h3 className="font-display text-xl italic text-ink sm:text-2xl">Your meditations</h3>
        <span className="font-mono text-[11px] text-ink-faint">
          {libItems.length} {libItems.length === 1 ? "session" : "sessions"}
        </span>
      </div>
      {libItems.length > 0 ? (
        <MeditationLibrary items={libItems} currentId={current?.id} playing={playing} onPlay={play} />
      ) : (
        <p className="rounded-panel border border-dashed border-border bg-surface/40 p-8 text-center text-sm text-ink-muted">
          Your composed meditations will collect here. Press the orb above to make your first.
        </p>
      )}

      {/* ── Notification (channel ping) ── */}
      {showToast && today && (
        <CoreToast
          title={today.title}
          onPlay={() => {
            setShowToast(false);
            play(today);
          }}
          onDismiss={() => setShowToast(false)}
        />
      )}

      {/* ── Player ── */}
      {current && (
        <CoreAudioPlayer
          meditation={current}
          playing={playing}
          onTogglePlay={() => setPlaying((p) => !p)}
          onClose={() => {
            setCurrent(null);
            setPlaying(false);
          }}
        />
      )}
    </div>
  );
}
