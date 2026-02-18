"use client";

import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="mx-auto w-full max-w-2xl px-4 py-16">
          <div className="rounded-panel border border-rose-border bg-rose-bg p-6 shadow-panel">
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-rose-text">
              Runtime Error
            </p>
            <h2 className="mt-2 text-xl font-semibold text-rose-text">
              Something went wrong
            </h2>
            <p className="mt-2 text-sm text-rose-text">
              {this.state.error?.message || "An unexpected error occurred."}
            </p>
            <button
              type="button"
              onClick={() => this.setState({ hasError: false, error: null })}
              className="mt-4 rounded-full bg-rose-bg px-4 py-2 text-sm text-white transition hover:bg-rose-border"
            >
              Try again
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
