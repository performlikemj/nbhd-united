"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { isLoggedIn } from "@/lib/auth";

const plans = [
  {
    name: "Starter",
    price: "$9",
    model: "Kimi K2.5",
    features: [
      "Private AI assistant via Telegram",
      "Journaling & daily notes",
      "Scheduled tasks & reminders",
      "7-day free trial",
    ],
  },
  {
    name: "Pro",
    price: "$29",
    model: "Claude Sonnet 4",
    features: [
      "Everything in Starter",
      "Advanced reasoning model",
      "Higher usage limits",
      "Priority support",
    ],
    highlight: true,
  },
  {
    name: "Ultra",
    price: "$99",
    model: "Claude Opus 4",
    features: [
      "Everything in Pro",
      "Most capable AI model",
      "Highest usage limits",
      "Early access to new features",
    ],
  },
];

export default function LandingPage() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (isLoggedIn()) {
      router.replace("/journal");
    } else {
      setReady(true);
    }
  }, [router]);

  if (!ready) return null;

  return (
    <div className="flex min-h-screen flex-col">
      {/* Hero */}
      <header className="flex flex-col items-center px-6 pt-20 pb-16 text-center">
        <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink-muted">
          Neighborhood United
        </p>
        <h1 className="mt-4 max-w-2xl text-4xl font-bold leading-tight text-ink sm:text-5xl">
          Your AI-powered personal&nbsp;assistant
        </h1>
        <p className="mt-4 max-w-xl text-lg text-ink-muted">
          A private AI assistant delivered through Telegram. Journal your
          thoughts, schedule tasks, get briefings, and stay organized — all
          through natural conversation.
        </p>
        <div className="mt-8 flex gap-4">
          <Link
            href="/signup"
            className="rounded-full bg-accent px-6 py-3 text-sm font-medium text-white transition hover:bg-accent/85"
          >
            Start free trial
          </Link>
          <Link
            href="/login"
            className="rounded-full border border-border-strong px-6 py-3 text-sm font-medium text-ink transition hover:border-border-strong hover:bg-surface-hover"
          >
            Sign in
          </Link>
        </div>
      </header>

      {/* Features */}
      <section className="mx-auto grid w-full max-w-4xl gap-6 px-6 py-12 sm:grid-cols-2 lg:grid-cols-3">
        {[
          {
            icon: "💬",
            title: "Telegram-native",
            desc: "Chat with your AI assistant in a private Telegram conversation. No new apps to install.",
          },
          {
            icon: "📓",
            title: "Journaling & Notes",
            desc: "Capture thoughts, daily reflections, and long-term memory — all searchable and organized.",
          },
          {
            icon: "⏰",
            title: "Scheduled Tasks",
            desc: "Set up morning briefings, reminders, recurring check-ins, and automated workflows.",
          },
          {
            icon: "🧠",
            title: "Personal Knowledge",
            desc: "Your assistant remembers context across conversations — preferences, projects, and goals.",
          },
          {
            icon: "🔒",
            title: "Private & Secure",
            desc: "Each subscriber gets a dedicated AI instance. Your data is never shared with other users.",
          },
          {
            icon: "⚡",
            title: "Choose Your Model",
            desc: "Pick the AI model that fits your needs — from fast and affordable to the most capable available.",
          },
        ].map((f) => (
          <div
            key={f.title}
            className="rounded-panel border border-border bg-surface-elevated p-5"
          >
            <span className="text-2xl">{f.icon}</span>
            <h3 className="mt-2 font-semibold text-ink">{f.title}</h3>
            <p className="mt-1 text-sm text-ink-muted">{f.desc}</p>
          </div>
        ))}
      </section>

      {/* Pricing */}
      <section className="mx-auto w-full max-w-4xl px-6 py-12">
        <h2 className="mb-8 text-center text-2xl font-bold text-ink">Plans</h2>
        <div className="grid gap-6 sm:grid-cols-3">
          {plans.map((plan) => (
            <div
              key={plan.name}
              className={`rounded-panel border p-6 ${
                plan.highlight
                  ? "border-accent bg-accent/5 shadow-lg"
                  : "border-border bg-surface-elevated"
              }`}
            >
              <h3 className="text-lg font-semibold text-ink">{plan.name}</h3>
              <p className="mt-1 text-sm text-ink-muted">{plan.model}</p>
              <p className="mt-3 text-3xl font-bold text-ink">
                {plan.price}
                <span className="text-base font-normal text-ink-muted">/mo</span>
              </p>
              <ul className="mt-4 space-y-2">
                {plan.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm text-ink-muted">
                    <span className="mt-0.5 text-emerald-500">✓</span>
                    {f}
                  </li>
                ))}
              </ul>
              <Link
                href="/signup"
                className={`mt-6 block rounded-full px-4 py-2.5 text-center text-sm font-medium transition ${
                  plan.highlight
                    ? "bg-accent text-white hover:bg-accent/85"
                    : "border border-border-strong text-ink hover:bg-surface-hover"
                }`}
              >
                Get started
              </Link>
            </div>
          ))}
        </div>
      </section>

      {/* Footer */}
      <footer className="mt-auto border-t border-border px-6 py-8">
        <div className="mx-auto flex max-w-4xl flex-col items-center gap-4 sm:flex-row sm:justify-between">
          <p className="font-mono text-xs uppercase tracking-[0.24em] text-ink-faint">
            Neighborhood United
          </p>
          <div className="flex gap-6 text-sm text-ink-muted">
            <Link href="/legal/terms" className="transition hover:text-ink">
              Terms
            </Link>
            <Link href="/legal/privacy" className="transition hover:text-ink">
              Privacy
            </Link>
            <Link href="/legal/refund" className="transition hover:text-ink">
              Refunds
            </Link>
            <a href="mailto:mj@bywayofmj.com" className="transition hover:text-ink">
              Contact
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
