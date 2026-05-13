"use client";

import { useEffect, useRef } from "react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "neutral";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const overlayRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      ref={overlayRef}
      className="fixed inset-0 z-[100] flex items-end md:items-center justify-center"
      onClick={(e) => {
        if (e.target === overlayRef.current) onCancel();
      }}
    >
      {/* Backdrop */}
      <div className="absolute inset-0 bg-overlay/60 backdrop-blur-sm animate-[fadeIn_150ms_ease-out]" />

      {/* Dialog */}
      <div
        className="relative z-10 w-full bg-[#111720] border-t border-white/[0.06] md:border md:border-white/[0.06] md:rounded-2xl md:m-4 md:max-w-sm bottom-sheet-enter md:bottom-auto"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
      >
        {/* Handle on mobile */}
        <div className="flex justify-center pt-3 md:hidden">
          <div className="h-1 w-10 rounded-full bg-white/10" />
        </div>

        <div className="p-6 md:p-5">
          <h3
            id="confirm-title"
            className="text-base font-semibold text-ink"
          >
            {title}
          </h3>
          <p className="mt-2 text-sm text-ink-muted leading-relaxed">
            {message}
          </p>

          <div className="mt-6 flex items-center gap-3">
            <button
              type="button"
              onClick={onCancel}
              className="flex-1 rounded-xl border border-white/[0.08] px-4 py-2.5 text-sm font-medium text-ink-muted transition hover:bg-white/[0.04] hover:text-ink min-h-[44px]"
            >
              {cancelLabel}
            </button>
            <button
              type="button"
              onClick={onConfirm}
              className={`flex-1 rounded-xl px-4 py-2.5 text-sm font-medium text-white transition min-h-[44px] ${
                variant === "danger"
                  ? "bg-rose-500/20 text-rose-text border border-rose-border hover:bg-rose-500/30"
                  : "bg-accent/20 text-accent border border-accent/30 hover:bg-accent/30"
              }`}
            >
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
