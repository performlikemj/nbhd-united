"use client";

import Link from "next/link";
import { useState } from "react";

import { useCheckoutMutation } from "@/lib/queries";
import type { Tenant } from "@/lib/types";

import {
  MeasureMargin,
  MeasureRail,
  MeasureScreen,
  measureStyles as s,
  settle,
  type MeasureCell,
} from "./measure-screen";

// Plan price as shown on /settings/billing and the commerce disclosure.
const PLAN_PRICE = "$12/mo";

/** "12 Sep", with the year added only when it isn't this year. */
function shortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  }).format(date);
}

/* THE MEASURE — paused → subscribe. Shown instead of the building screen when
 * the tenant is suspended. Every figure on this screen is sourced: the only
 * counts we can stand behind are "0 deleted", the date the space was built,
 * and (for lapsed trials) the date the trial ended. Nothing is estimated. */
export function PausedScene({ tenant }: { tenant: Tenant | null }) {
  const checkoutMutation = useCheckoutMutation();
  const [checkoutError, setCheckoutError] = useState("");

  const trialEnded = shortDate(
    tenant?.is_trial && tenant.trial_days_remaining === 0
      ? tenant.trial_ends_at
      : null,
  );
  const builtOn = shortDate(tenant?.provisioned_at);

  const subscribe = async () => {
    setCheckoutError("");
    try {
      const result = await checkoutMutation.mutateAsync();
      window.location.assign(result.url);
    } catch {
      setCheckoutError("Couldn’t open checkout right now. Try again shortly.");
    }
  };

  const cells: MeasureCell[] = [
    { label: "Journal", state: "done" },
    { label: "Conversations", state: "done" },
    { label: "Goals", state: "done" },
    builtOn
      ? { label: "Built", num: builtOn, state: "done" }
      : { label: "Settings", state: "done" },
    trialEnded
      ? { label: "Trial ended", num: trialEnded, state: "current" }
      : { label: "Paused", state: "current" },
  ];

  return (
    <MeasureScreen
      state="paused"
      tag="02 · Paused → Subscribe"
      tagStatus={trialEnded ? `Trial ended ${trialEnded}` : "Subscription paused"}
      labelledBy="h-measure-paused"
    >
      <MeasureMargin title="Paused" count="0" caption="Deleted" />

      <div className={s.content}>
        <div className={s.lead}>
          <h1
            className={s.display}
            id="h-measure-paused"
            data-settle
            style={settle(3)}
          >
            <span className={s.key}>Kept</span> exactly as you left it.
          </h1>
          <div className={s.actions} data-settle style={settle(4)}>
            <Link href="/settings/billing" className={s.link}>
              Billing settings
            </Link>
            <button
              type="button"
              className={s.btn}
              onClick={subscribe}
              disabled={checkoutMutation.isPending}
            >
              {checkoutMutation.isPending ? (
                "Redirecting…"
              ) : (
                <>
                  Subscribe<span className={s.price}>· {PLAN_PRICE}</span>
                </>
              )}
            </button>
          </div>
        </div>

        <MeasureRail
          percent={100}
          cells={cells}
          ariaLabel="What is kept"
          settleIndex={5}
        />

        <p className={s.para} data-settle style={settle(6)}>
          Your assistant is paused with your subscription. Nothing was deleted
          and nothing will be. When you’re ready, it picks up where you stopped.
          {checkoutError ? (
            <>
              {" "}
              <span className={s.alert} role="alert">
                {checkoutError}
              </span>
            </>
          ) : null}
        </p>
      </div>
    </MeasureScreen>
  );
}
