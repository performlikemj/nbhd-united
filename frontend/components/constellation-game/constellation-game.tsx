"use client";

import { useEffect, useRef } from "react";

import "./constellation-game.css";
import type { GalaxyData } from "@/lib/constellation-game/encounter-logic";
import { mountGalaxyGame } from "@/lib/constellation-game/galaxy-scene";

/**
 * Mounts the Phaser galaxy game (canvas) plus its DOM overlay shell. This whole
 * component is dynamic-imported (ssr:false) by the /constellation/play route, so
 * Phaser lands in a lazy chunk and never weighs down the rest of the app.
 *
 * The overlay markup carries the `cg-*` ids the scene drives; the scene queries
 * them within this subtree (no global ids), so they can't collide with the app.
 */
export function ConstellationGame({ galaxy }: { galaxy: GalaxyData }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = canvasRef.current;
    const root = rootRef.current;
    if (!host || !root) return;
    const game = mountGalaxyGame(host, root, galaxy);
    return () => {
      game.destroy(true);
    };
  }, [galaxy]);

  return (
    <div ref={rootRef} className="cg-root">
      <div ref={canvasRef} className="cg-canvas" aria-hidden="true" />

      <div id="cg-help" className="cg-help">
        <b>WASD / arrows</b> fly &nbsp;·&nbsp; <b>E</b> land on a star &nbsp;·&nbsp; <b>Esc</b> back
      </div>
      <button id="cg-land-btn" className="cg-land-btn" type="button">🛸 Land here</button>

      {/* landing panel */}
      <div id="cg-panel" className="cg-overlay" role="dialog" aria-modal="true">
        <div className="cg-card">
          <div className="cg-row">
            <span className="cg-badge" id="cg-p-badge">radiant</span>
            <span className="cg-cluster" id="cg-p-cluster">—</span>
          </div>
          <h1 id="cg-p-text">—</h1>
          <div className="cg-meta" id="cg-p-meta" />
          <div className="cg-tags" id="cg-p-tags" />
          <div className="cg-note" id="cg-p-note" style={{ display: "none" }} />
          <div className="cg-copilot">
            <div className="cg-who"><span className="cg-dot" /> Your co-pilot</div>
            <div className="cg-line" id="cg-p-copilot">—</div>
            <div className="cg-stub">In the full version your assistant picks the conversation up from here. This is flight + stars for now.</div>
          </div>
          <div className="cg-actions">
            <button id="cg-p-close" type="button">Back to flight</button>
            <button id="cg-p-close2" className="cg-primary" type="button">Got it</button>
          </div>
        </div>
      </div>

      {/* nega-self encounter sheet */}
      <div id="cg-encounter" className="cg-encounter" role="dialog" aria-modal="true" aria-labelledby="cg-enc-name">
        <div className="cg-enc-card">
          <div className="cg-enc-portrait" aria-hidden="true">
            <div className="cg-shadow-wrap">
              <svg className="cg-shadow" viewBox="0 0 120 120" width="92" height="92">
                <polygon points="16,60 98,26 82,60 98,94" fill="#1c0a15" stroke="#ff4d7e" strokeWidth="2.5" strokeLinejoin="round" />
                <polygon points="32,60 88,42 78,60 88,78" fill="#330e22" />
                <circle className="cg-eye" cx="36" cy="60" r="6" fill="#ff2d55" />
              </svg>
            </div>
          </div>
          <div className="cg-enc-who"><span className="cg-dot" /> <span id="cg-enc-name">Your shadow</span></div>
          <div id="cg-enc-taunt" className="cg-enc-taunt">—</div>
          <div className="cg-enc-prompt">Fire back with a truth you&apos;ve earned</div>
          <div id="cg-enc-choices" className="cg-enc-choices" />
          <div id="cg-enc-outcome" className="cg-enc-outcome" />
          <div className="cg-enc-actions">
            <button id="cg-enc-skip" className="cg-enc-skip" type="button">Not today — slip past</button>
          </div>
        </div>
      </div>
    </div>
  );
}
