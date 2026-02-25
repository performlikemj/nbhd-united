# NBHD United — Brand & Design Guide

Reference for all visual design tokens, typography, spacing, and accessibility standards.

---

## Color Tokens

### Core Palette (Light)

| Token | Value | Usage |
|-------|-------|-------|
| `--ink` | `#12232c` | Primary text (14.7:1 on bg) |
| `--ink-muted` | `rgba(18,35,44, 0.72)` | Secondary text (7.2:1 on bg) |
| `--ink-faint` | `rgba(18,35,44, 0.48)` | Tertiary text, placeholders |
| `--bg` | `#f8f6ef` | Page background |
| `--mist` | `#f6f4ee` | Slightly darker background |
| `--surface` | `#ffffff` | Cards, panels |
| `--surface-elevated` | `#ffffff` | Elevated cards with shadow |
| `--surface-hover` | `rgba(18,35,44, 0.05)` | Hover state for surfaces |
| `--card` | `rgba(252,252,248, 0.95)` | Semi-transparent card bg |
| `--accent` | `#1d4ed8` | Primary action, links (6.9:1 on white) |
| `--accent-hover` | `#1e40af` | Accent hover state |
| `--signal` | `#5fbaaf` | Decorative teal, non-text |
| `--signal-text` | `#0f766e` | Teal for text usage (5.1:1 on white) |
| `--border` | `rgba(18,35,44, 0.12)` | Default borders |
| `--border-strong` | `rgba(18,35,44, 0.25)` | Emphasized borders |
| `--overlay` | `rgba(18,35,44, 0.55)` | Modal/drawer backdrop |

### Core Palette (Dark)

| Token | Value |
|-------|-------|
| `--ink` | `#e2e8f0` |
| `--ink-muted` | `rgba(226,232,240, 0.72)` |
| `--ink-faint` | `rgba(226,232,240, 0.42)` |
| `--bg` | `#0b0f13` |
| `--surface` | `#161b22` |
| `--surface-elevated` | `#1e252e` |
| `--accent` | `#60a5fa` |
| `--overlay` | `rgba(0,0,0, 0.70)` |

### Status Colors (WCAG AA Verified)

| Status | Background | Text | Contrast |
|--------|-----------|------|----------|
| Emerald | `#ecfdf5` | `#065f46` | 7.8:1 |
| Rose | `#fff1f2` | `#9f1239` | 7.2:1 |
| Amber | `#fffbeb` | `#92400e` | 7.0:1 |
| Sky | `#f0f9ff` | `#075985` | 7.5:1 |
| Slate | `#f1f5f9` | `#334155` | 7.7:1 |
| Indigo | `#eef2ff` | `#3730a3` | 8.2:1 |
| Violet | `#f5f3ff` | `#5b21b6` | 8.6:1 |
| Orange | `#fff7ed` | `#9a3412` | 6.5:1 |

### Semantic Colors

| Token | Light | Dark | Usage |
|-------|-------|------|-------|
| `--rose-bg` | `#fff1f2` | `rgba(159,18,57, 0.15)` | Error backgrounds |
| `--rose-text` | `#9f1239` | `#fda4af` | Error text |
| `--rose-border` | `#fecaca` | `rgba(244,63,94, 0.25)` | Error borders |
| `--amber-bg` | `#fffbeb` | `rgba(146,64,14, 0.15)` | Warning backgrounds |
| `--amber-text` | `#92400e` | `#fcd34d` | Warning text |
| `--emerald-bg` | `#ecfdf5` | `rgba(6,95,70, 0.15)` | Success backgrounds |
| `--emerald-text` | `#065f46` | `#6ee7b7` | Success text |

---

## Typography

### Font Families

| Role | Stack | CSS Variable |
|------|-------|-------------|
| Body | "Avenir Next", "Trebuchet MS", "Verdana", sans-serif | `--font-body` |
| Mono | "IBM Plex Mono", "Consolas", "Menlo", monospace | `--font-mono` |

### Type Scale

All sizes in `rem` for user preference scaling.

| Level | Size | Line Height | Weight | Usage |
|-------|------|-------------|--------|-------|
| H1 | `clamp(2.25rem, 5vw + 0.5rem, 4.5rem)` | 1.08 | 700 | Page titles |
| H2 | `clamp(1.5rem, 3vw, 2.25rem)` | 1.15 | 700 | Section headings |
| H3 | `clamp(1.125rem, 2vw, 1.375rem)` | 1.2 | 600 | Card titles |
| Body | `1rem` (16px) | 1.6 | 400 | Default text |
| Small | `0.75rem` (12px) | 1.5 | 400 | Captions, metadata |
| Mono | `0.8125rem` (13px) | 1.6 | 400 | Code, data |
| Eyebrow | `0.6875rem` (11px) | 1.4 | 500 | Labels, uppercase |

---

## Spacing

### Fluid Scale (clamp-based)

| Token | Value | ~Mobile | ~Desktop |
|-------|-------|---------|----------|
| `--space-xs` | `clamp(0.25rem, 0.5vw, 0.5rem)` | 4px | 8px |
| `--space-sm` | `clamp(0.5rem, 1vw, 0.75rem)` | 8px | 12px |
| `--space-md` | `clamp(0.75rem, 1.5vw + 0.25rem, 1.5rem)` | 12px | 24px |
| `--space-lg` | `clamp(1.25rem, 2.5vw + 0.25rem, 2.5rem)` | 20px | 40px |
| `--space-xl` | `clamp(2rem, 4vw + 0.5rem, 5rem)` | 32px | 80px |
| `--page-pad` | `clamp(1rem, 4vw, 2.5rem)` | 16px | 40px |

### Fixed Scale (Tailwind defaults for component-level)

`4px` base unit: 1 (4px), 2 (8px), 3 (12px), 4 (16px), 5 (20px), 6 (24px), 8 (32px)

---

## Elevation & Shape

| Token | Value |
|-------|-------|
| `--shadow-panel` | `0 20px 55px rgba(18,31,38, 0.14)` |
| `rounded-panel` | `1.25rem` (20px) border radius |
| `--bg-gradient` | Warm radial gradients (peach + teal) over linear cream |

---

## Animations

| Name | Duration | Easing | Usage |
|------|----------|--------|-------|
| `reveal` | 420ms | ease-out | Staggered component entrance |
| `pulseGrid` | 7s | ease-in-out infinite | Background grid pulse |

All animations respect `prefers-reduced-motion: reduce`.

---

## Accessibility Standards

### Contrast
- All text/background pairs meet **WCAG 2.1 AA** (4.5:1 normal, 3:1 large)
- Status colors verified with ratios documented above

### Focus
- Custom `:focus-visible` ring: `2px solid var(--accent)` with `3px` offset
- Never remove focus outlines — only restyle with `:focus-visible`

### Touch Targets
- Minimum **44×44px** for all interactive elements (buttons, links, toggles)
- Toolbar buttons: 36×36px acceptable (dense editor pattern)

### Motion
- `@media (prefers-reduced-motion: reduce)` disables all animations
- Transitions reduced to 0.01ms

### Semantics
- Skip-to-content link on all pages
- ARIA landmarks: `role="navigation"`, `role="main"`, `role="contentinfo"`
- `aria-label` on all icon-only buttons
- `aria-current="page"` on active nav links
- `aria-expanded` on toggles/menus

### Color Independence
- Status is always conveyed through text labels, never color alone
- Decorative elements use `aria-hidden="true"`

---

## Breakpoints

| Name | Width | Usage |
|------|-------|-------|
| sm | 640px | Small tablets, landscape phones |
| md | 768px | Tablets, app shell hamburger |
| lg | 1024px | Desktop, journal sidebar |
| xl | 1280px | Large desktop |

Mobile-first approach: base styles = mobile, add complexity at larger breakpoints.
