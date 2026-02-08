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
          <div className="rounded-panel border border-rose-200 bg-rose-50 p-6 shadow-panel">
            <p className="font-mono text-xs uppercase tracking-[0.24em] text-rose-400">
              Runtime Error
            </p>
            <h2 className="mt-2 text-xl font-semibold text-rose-900">
              Something went wrong
            </h2>
            <p className="mt-2 text-sm text-rose-800">
              {this.state.error?.message || "An unexpected error occurred."}
            </p>
            <button
              type="button"
              onClick={() => this.setState({ hasError: false, error: null })}
              className="mt-4 rounded-full bg-rose-900 px-4 py-2 text-sm text-white transition hover:bg-rose-800"
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
