"use client";

import { useEffect, useState } from "react";

export interface ToastProps {
  message: string;
  type?: "success" | "error";
  durationMs?: number;
  onDismiss?: () => void;
}

type ToastListener = (toast: ToastState) => void;
const _globalListeners = new Set<ToastListener>();

/**
 * Module-level emitter for app-wide toasts.
 *
 * Use this from non-React code (e.g. the QueryClient `onError` default)
 * when you can't easily call `useToast()`. Pair with a single mounted
 * `<GlobalToastHost />` in the providers tree.
 */
export function emitToast(message: string, type: "success" | "error" = "success") {
  _toastCounter += 1;
  const toast: ToastState = { id: _toastCounter, message, type };
  _globalListeners.forEach((l) => l(toast));
}

/**
 * A simple self-dismissing toast notification.
 * Renders fixed at the bottom-center of the screen.
 * Animates in/out via CSS transitions.
 */
export function Toast({ message, type = "success", durationMs = 3000, onDismiss }: ToastProps) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    // Mount → animate in
    const showTimer = setTimeout(() => setVisible(true), 10);

    // Start dismiss animation before calling onDismiss
    const hideTimer = setTimeout(() => setVisible(false), durationMs - 300);

    // After fade-out, call onDismiss
    const dismissTimer = setTimeout(() => onDismiss?.(), durationMs);

    return () => {
      clearTimeout(showTimer);
      clearTimeout(hideTimer);
      clearTimeout(dismissTimer);
    };
  }, [durationMs, onDismiss]);

  const colorClass =
    type === "error"
      ? "bg-rose-600 text-white"
      : "bg-emerald-600 text-white";

  return (
    <div
      role="status"
      aria-live="polite"
      className={[
        "fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-full px-5 py-2.5 text-sm font-medium shadow-lg",
        "transition-all duration-300",
        colorClass,
        visible ? "opacity-100 translate-y-0" : "opacity-0 translate-y-2 pointer-events-none",
      ].join(" ")}
    >
      {message}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  useToast hook — simple imperative API                             */
/* ------------------------------------------------------------------ */

export interface ToastState {
  id: number;
  message: string;
  type: "success" | "error";
}

let _toastCounter = 0;

/**
 * Hook that returns [currentToast, showToast].
 *
 * Usage:
 *   const [toast, showToast] = useToast();
 *   showToast("Saved!", "success");
 *   // In JSX: {toast && <Toast {...toast} onDismiss={...} />}
 */
export function useToast(): [ToastState | null, (message: string, type?: "success" | "error") => void] {
  const [toast, setToast] = useState<ToastState | null>(null);

  const showToast = (message: string, type: "success" | "error" = "success") => {
    _toastCounter += 1;
    setToast({ id: _toastCounter, message, type });
  };

  return [toast, showToast];
}

/**
 * Renders any toast emitted via `emitToast(...)`. Mount once near the root
 * of the app (in `Providers`). Subscribes to the module-level emitter so
 * non-React callers (QueryClient defaults, top-level fetch wrappers) can
 * surface user-visible failures without threading a hook through.
 */
export function GlobalToastHost() {
  const [toast, setToast] = useState<ToastState | null>(null);

  useEffect(() => {
    const listener: ToastListener = (t) => setToast(t);
    _globalListeners.add(listener);
    return () => {
      _globalListeners.delete(listener);
    };
  }, []);

  if (!toast) return null;
  return <Toast key={toast.id} {...toast} onDismiss={() => setToast(null)} />;
}
