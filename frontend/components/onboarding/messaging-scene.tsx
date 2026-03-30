"use client";

import { useEffect, useState } from "react";

import {
  useGenerateTelegramLinkMutation,
  useGenerateLineLinkMutation,
  useTelegramStatusQuery,
  useLineStatusQuery,
  useMeQuery,
} from "@/lib/queries";
import type { TelegramLinkResponse, LineLinkResponse } from "@/lib/api";

export function MessagingScene() {
  const { data: me } = useMeQuery();
  const generateTelegram = useGenerateTelegramLinkMutation();
  const generateLine = useGenerateLineLinkMutation();
  const { data: telegramStatus } = useTelegramStatusQuery(true);
  const { data: lineStatus } = useLineStatusQuery(true);

  const [telegramLink, setTelegramLink] = useState<TelegramLinkResponse | null>(null);
  const [lineLink, setLineLink] = useState<LineLinkResponse | null>(null);
  const [telegramSeconds, setTelegramSeconds] = useState(0);
  const [lineSeconds, setLineSeconds] = useState(0);

  const telegramLinked = Boolean(me?.tenant?.user.telegram_chat_id) || Boolean(telegramStatus?.linked);
  const lineLinked = Boolean(lineStatus?.linked);
  const connected = telegramLinked || lineLinked;

  // Telegram countdown
  useEffect(() => {
    if (!telegramLink) { setTelegramSeconds(0); return; }
    const expiresAt = new Date(telegramLink.expires_at).getTime();
    const tick = () => {
      const ms = expiresAt - Date.now();
      if (ms <= 0) { setTelegramLink(null); setTelegramSeconds(0); }
      else setTelegramSeconds(Math.ceil(ms / 1000));
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [telegramLink]);

  // LINE countdown
  useEffect(() => {
    if (!lineLink) { setLineSeconds(0); return; }
    const expiresAt = new Date(lineLink.expires_at).getTime();
    const tick = () => {
      const ms = expiresAt - Date.now();
      if (ms <= 0) { setLineLink(null); setLineSeconds(0); }
      else setLineSeconds(Math.ceil(ms / 1000));
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [lineLink]);

  const handleGenerateTelegram = async () => {
    try {
      const data = await generateTelegram.mutateAsync();
      setTelegramLink(data);
      setLineLink(null);
    } catch { /* mutation error state handles UI */ }
  };

  const handleGenerateLine = async () => {
    try {
      const data = await generateLine.mutateAsync();
      setLineLink(data);
      setTelegramLink(null);
    } catch { /* mutation error state handles UI */ }
  };

  const formatTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  if (connected) return null; // Scene will auto-advance

  const activeLink = telegramLink || lineLink;
  const activePlatform = telegramLink ? "telegram" : lineLink ? "line" : null;

  return (
    <div className="w-full max-w-[580px] flex flex-col items-center text-center">
      <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#5dd9d0] mb-4">
        STEP 2 OF 3
      </span>

      <h1 className="font-display text-3xl sm:text-5xl font-extrabold text-[#e0e3e8] tracking-tight mb-3 leading-tight">
        Connect your messenger
      </h1>

      <p className="text-white/50 text-[15px] max-w-[420px] leading-relaxed mb-10">
        Your assistant lives where you already are. Choose Telegram or LINE to start talking.
      </p>

      {/* Platform selection */}
      {!activeLink && (
        <div className="grid gap-4 sm:grid-cols-2 w-full max-w-[440px]">
          {/* Telegram card */}
          <button
            type="button"
            onClick={handleGenerateTelegram}
            disabled={generateTelegram.isPending}
            className="group flex flex-col items-center gap-3 rounded-[20px] bg-[#12161b]/50 backdrop-blur-sm border border-white/[0.06] p-6 transition hover:border-[#0088cc]/30 hover:shadow-[0_0_20px_rgba(0,136,204,0.12)] disabled:opacity-50"
          >
            <div className="w-14 h-14 rounded-full bg-[#0088cc]/15 flex items-center justify-center group-hover:bg-[#0088cc]/25 transition">
              <svg className="w-7 h-7 text-[#0088cc]" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .38z" />
              </svg>
            </div>
            <span className="text-sm font-semibold text-[#e0e3e8]">
              {generateTelegram.isPending ? "Generating..." : "Connect Telegram"}
            </span>
          </button>

          {/* LINE card */}
          <button
            type="button"
            onClick={handleGenerateLine}
            disabled={generateLine.isPending}
            className="group flex flex-col items-center gap-3 rounded-[20px] bg-[#12161b]/50 backdrop-blur-sm border border-white/[0.06] p-6 transition hover:border-[#06C755]/30 hover:shadow-[0_0_20px_rgba(6,199,85,0.12)] disabled:opacity-50"
          >
            <div className="w-14 h-14 rounded-full bg-[#06C755]/15 flex items-center justify-center group-hover:bg-[#06C755]/25 transition">
              <svg className="w-7 h-7 text-[#06C755]" viewBox="0 0 24 24" fill="currentColor">
                <path d="M19.365 9.863c.349 0 .63.285.63.631 0 .345-.281.63-.63.63H17.61v1.125h1.755c.349 0 .63.283.63.63 0 .344-.281.629-.63.629h-2.386c-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63h2.386c.346 0 .627.285.627.63 0 .349-.281.63-.63.63H17.61v1.125h1.755zm-3.855 3.016c0 .27-.174.51-.432.596-.064.021-.133.031-.199.031-.211 0-.391-.09-.51-.25l-2.443-3.317v2.94c0 .344-.279.629-.631.629-.346 0-.626-.285-.626-.629V8.108c0-.27.173-.51.43-.595.06-.023.136-.033.194-.033.195 0 .375.104.495.254l2.462 3.33V8.108c0-.345.282-.63.63-.63.345 0 .63.285.63.63v4.771zm-5.741 0c0 .344-.282.629-.631.629-.345 0-.627-.285-.627-.629V8.108c0-.345.282-.63.63-.63.346 0 .628.285.628.63v4.771zm-2.466.629H4.917c-.345 0-.63-.285-.63-.629V8.108c0-.345.285-.63.63-.63.348 0 .63.285.63.63v4.141h1.756c.348 0 .629.283.629.63 0 .344-.282.629-.629.629M24 10.314C24 4.943 18.615.572 12 .572S0 4.943 0 10.314c0 4.811 4.27 8.842 10.035 9.608.391.082.923.258 1.058.59.12.301.079.766.038 1.08l-.164 1.02c-.045.301-.24 1.186 1.049.645 1.291-.539 6.916-4.078 9.436-6.975C23.176 14.393 24 12.458 24 10.314" />
              </svg>
            </div>
            <span className="text-sm font-semibold text-[#e0e3e8]">
              {generateLine.isPending ? "Generating..." : "Connect LINE"}
            </span>
          </button>
        </div>
      )}

      {/* Active link — QR + deep link */}
      {activeLink && (
        <div className="w-full max-w-[440px] rounded-[20px] bg-[#12161b]/50 backdrop-blur-sm border border-white/[0.06] p-6 flex flex-col items-center gap-5">
          <p className="text-white/50 text-sm">
            {activePlatform === "telegram" ? "Scan or tap to connect Telegram:" : "Scan or tap to connect LINE:"}
          </p>

          {/* QR code — hidden on small mobile, prominent on desktop */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={activeLink.qr_code}
            alt={`${activePlatform} QR Code`}
            className="hidden sm:block h-44 w-44 rounded-[16px] border border-white/10"
          />

          {/* Deep link — prominent on mobile */}
          <a
            href={activeLink.deep_link}
            target="_blank"
            rel="noopener noreferrer"
            className={`inline-flex items-center gap-2 rounded-full px-6 py-3 text-sm font-semibold text-white transition hover:brightness-110 active:scale-95 ${activePlatform === "telegram" ? "bg-[#0088cc] shadow-[0_0_16px_rgba(0,136,204,0.3)]" : "bg-[#06C755] shadow-[0_0_16px_rgba(6,199,85,0.3)]"}`}
          >
            Open in {activePlatform === "telegram" ? "Telegram" : "LINE"}
          </a>

          {/* Waiting indicator */}
          <div className="flex items-center gap-2">
            <div className={`h-2 w-2 rounded-full animate-pulse ${activePlatform === "telegram" ? "bg-[#0088cc]" : "bg-[#06C755]"}`} />
            <span className="text-xs text-white/40">Waiting for you to connect...</span>
          </div>

          {/* Countdown */}
          {(telegramSeconds > 0 || lineSeconds > 0) && (
            <p className="text-xs text-white/30">
              Link expires in {formatTime(telegramSeconds || lineSeconds)}
            </p>
          )}

          {/* Switch / regenerate */}
          <div className="flex gap-4">
            <button
              type="button"
              onClick={() => { setTelegramLink(null); setLineLink(null); }}
              className="text-xs text-white/40 underline hover:text-white/60"
            >
              Use {activePlatform === "telegram" ? "LINE" : "Telegram"} instead
            </button>
            <button
              type="button"
              onClick={activePlatform === "telegram" ? handleGenerateTelegram : handleGenerateLine}
              disabled={generateTelegram.isPending || generateLine.isPending}
              className="text-xs text-[#c7bfff] underline hover:text-[#c7bfff]/70 disabled:opacity-50"
            >
              Generate new link
            </button>
          </div>

          {/* Mobile hint for QR */}
          <p className="sm:hidden text-[10px] text-white/25 mt-1">
            Or scan the QR code from another device
          </p>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={activeLink.qr_code}
            alt={`${activePlatform} QR Code`}
            className="sm:hidden h-32 w-32 rounded-[12px] border border-white/10 opacity-60"
          />
        </div>
      )}
    </div>
  );
}
