"use client";

import type { CSSProperties, ReactNode } from "react";

import s from "./measure.module.css";

/* THE MEASURE — layout primitives shared by the building screen and the
 * paused → subscribe screen. Presentation only; the scenes own the data. */

export type MeasureCellState = "done" | "current" | "todo";

export interface MeasureCell {
  label: string;
  /** Optional figure above the label. Only ever a real, sourced value. */
  num?: string;
  state: MeasureCellState;
}

/** Inline style for a settling element: nth element lands n × 60 ms later. */
export function settle(index: number): CSSProperties {
  return { "--d": `${index * 60}ms` } as CSSProperties;
}

export function cellClass(state: MeasureCellState): string {
  return [
    s.cell,
    s.label,
    state === "done" ? s.isDone : "",
    state === "current" ? s.isCurrent : "",
  ]
    .filter(Boolean)
    .join(" ");
}

export function MeasureScreen({
  state,
  tag,
  tagStatus,
  labelledBy,
  children,
}: {
  state: "building" | "ready" | "retry" | "paused";
  tag: string;
  tagStatus: string;
  labelledBy: string;
  children: ReactNode;
}) {
  return (
    <section className={s.screen} data-state={state} aria-labelledby={labelledBy}>
      <div className={s.tagrow}>
        <span className={s.wordmark}>NBHD</span>
        <div className={s.tagrowLabels}>
          <span className={s.label}>{tag}</span>
          <span className={s.label}>{tagStatus}</span>
        </div>
      </div>
      <div className={s.frame}>{children}</div>
    </section>
  );
}

export function MeasureMargin({
  title,
  count,
  countOf,
  caption,
  live = false,
}: {
  title: string;
  count: string;
  countOf?: string;
  caption?: string;
  live?: boolean;
}) {
  return (
    <aside className={s.margin}>
      <p className={`${s.label} ${s.marginTitle}`} data-settle style={settle(0)}>
        {title}
      </p>
      <p
        className={s.count}
        data-settle
        style={settle(1)}
        aria-live={live ? "polite" : undefined}
      >
        {count}
        {countOf ? <span className={s.countOf}> / {countOf}</span> : null}
      </p>
      {caption ? (
        <p className={`${s.label} ${s.labelDim}`} data-settle style={settle(2)}>
          {caption}
        </p>
      ) : null}
    </aside>
  );
}

export function MeasureRail({
  percent,
  cells,
  creeping = false,
  stopped = false,
  ariaLabel,
  valueNow,
  settleIndex,
}: {
  percent: number;
  cells: MeasureCell[];
  creeping?: boolean;
  stopped?: boolean;
  ariaLabel: string;
  /** When set, the rail is a progressbar out of `cells.length`. */
  valueNow?: number;
  settleIndex: number;
}) {
  const style = { ...settle(settleIndex), "--p": `${percent}%` } as CSSProperties;
  const className = [
    s.measure,
    creeping ? s.isCreeping : "",
    stopped ? s.isStopped : "",
  ]
    .filter(Boolean)
    .join(" ");
  const progressProps =
    valueNow === undefined
      ? {}
      : {
          role: "progressbar",
          "aria-valuemin": 0,
          "aria-valuemax": cells.length,
          "aria-valuenow": valueNow,
        };

  return (
    <div className={className} data-settle style={style} aria-label={ariaLabel} {...progressProps}>
      <div className={s.measureFill} />
      <div className={s.measureRest} aria-hidden="true" />
      <div className={s.measureTicks} aria-hidden="true">
        {cells.map((cell) => (
          <span
            key={cell.label}
            className={[
              s.tick,
              cell.state === "done" ? s.isDone : "",
              cell.state === "current" ? s.isCurrent : "",
            ]
              .filter(Boolean)
              .join(" ")}
          />
        ))}
      </div>
      <ol className={s.cells}>
        {cells.map((cell) => (
          <li key={cell.label} className={cellClass(cell.state)}>
            <span className={s.cellInner}>
              {cell.num ? <span className={s.cellNum}>{cell.num}</span> : null}
              <span>{cell.label}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export { s as measureStyles };
