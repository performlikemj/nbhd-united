"""Server-side chart rendering for Telegram and LINE delivery.

Generates branded PNG charts from tenant data using matplotlib.
Charts are triggered by [[chart:type]] markers in assistant responses.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.ticker as ticker  # noqa: E402
import numpy as np  # noqa: E402

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# ── Brand Palette ──────────────────────────────────────────────────────────

TEAL = "#5fbaaf"
DARK_TEAL = "#0f766e"
INK = "#12232c"
INK_MUTED = "#3d4f58"
INK_FAINT = "#6b7b84"
MIST = "#f6f4ee"
ROSE = "#e11d48"
PURPLE = "#7c3aed"
SEPARATOR = "#e8e4dc"

# Chat-optimized size: readable on phone screens
FIG_WIDTH = 7
FIG_HEIGHT = 3.5
DPI = 150


# ── Shared Setup ───────────────────────────────────────────────────────────

def _setup_axes(ax: plt.Axes, title: str = "") -> None:
    """Apply brand styling to axes."""
    ax.set_facecolor(MIST)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color(SEPARATOR)
    ax.spines["left"].set_color(SEPARATOR)
    ax.tick_params(colors=INK_MUTED, labelsize=8)
    if title:
        ax.set_title(title, fontsize=11, fontweight="bold", color=INK, pad=10)


def _render_to_png(fig: plt.Figure) -> bytes:
    """Render a matplotlib figure to PNG bytes."""
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight",
                facecolor=MIST, edgecolor="none", pad_inches=0.15)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _currency_fmt(val: float, _pos: int = 0) -> str:
    """Format a number as compact currency ($1.2K, $45K, etc.)."""
    if abs(val) >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if abs(val) >= 1_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


# ── Chart Renderers ────────────────────────────────────────────────────────

def _render_payoff_timeline(tenant: Tenant) -> bytes | None:
    """Area chart: projected debt balance vs actual snapshots."""
    from apps.finance.models import PayoffPlan, FinanceSnapshot

    plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).first()
    if not plan or not plan.schedule_json:
        return None

    schedule = plan.schedule_json
    if not schedule:
        return None

    # Build projected timeline
    plan_start = plan.created_at.date().replace(day=1)
    proj_dates = []
    proj_values = []
    for entry in schedule:
        month_offset = entry.get("month", 0)
        d = date(plan_start.year + (plan_start.month + month_offset - 1) // 12,
                 (plan_start.month + month_offset - 1) % 12 + 1, 1)
        proj_dates.append(d)
        proj_values.append(float(entry.get("total_remaining", 0)))

    if not proj_dates:
        return None

    # Build actual timeline from snapshots
    snapshots = list(
        FinanceSnapshot.objects.filter(tenant=tenant)
        .order_by("date")
        .values_list("date", "total_debt")
    )
    actual_dates = [s[0] for s in snapshots]
    actual_values = [float(s[1]) for s in snapshots]

    # Render
    fig, ax = plt.subplots(figsize=(FIG_WIDTH, FIG_HEIGHT))
    _setup_axes(ax, "Debt Payoff Timeline")

    # Projected area
    ax.fill_between(proj_dates, proj_values, alpha=0.12, color=TEAL)
    ax.plot(proj_dates, proj_values, color=TEAL, linewidth=1.5,
            linestyle="--", label="Projected", alpha=0.7)

    # Actual line
    if actual_dates:
        ax.plot(actual_dates, actual_values, color=PURPLE, linewidth=2,
                marker="o", markersize=4, label="Actual")

    ax.yaxis.set_major_formatter(ticker.FuncFormatter(_currency_fmt))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=max(1, len(proj_dates) // 6)))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)
    ax.legend(fontsize=8, frameon=False, labelcolor=INK_MUTED)
    fig.set_facecolor(MIST)

    return _render_to_png(fig)


def _render_debt_vs_savings(tenant: Tenant) -> bytes | None:
    """Bar chart: debt vs savings over last 12 months."""
    from apps.finance.models import FinanceSnapshot

    snapshots = list(
        FinanceSnapshot.objects.filter(tenant=tenant)
        .order_by("date")[:12]
        .values("date", "total_debt", "total_savings")
    )
    if not snapshots:
        return None

    dates = [s["date"] for s in snapshots]
    debt = [float(s["total_debt"]) for s in snapshots]
    savings = [float(s["total_savings"]) for s in snapshots]
    labels = [d.strftime("%b") for d in dates]

    x = np.arange(len(labels))
    width = 0.38

    fig, ax = plt.subplots(figsize=(FIG_WIDTH, FIG_HEIGHT))
    _setup_axes(ax, "Debt vs Savings")

    ax.bar(x - width / 2, debt, width, label="Debt", color=ROSE, alpha=0.8)
    ax.bar(x + width / 2, savings, width, label="Savings", color=TEAL, alpha=0.8)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(_currency_fmt))
    ax.legend(fontsize=8, frameon=False, labelcolor=INK_MUTED)
    fig.set_facecolor(MIST)

    return _render_to_png(fig)


def _render_momentum_grid(tenant: Tenant, days: int = 30) -> bytes | None:
    """Colored grid showing daily activity streak."""
    from apps.billing.models import UsageRecord
    from apps.journal.models import Document, JournalEntry
    from django.db.models import Count

    today = date.today()
    start = today - timedelta(days=days - 1)

    message_counts = dict(
        UsageRecord.objects.filter(
            tenant=tenant, created_at__date__gte=start,
        ).values_list("created_at__date").annotate(count=Count("id"))
    )
    journal_dates = set(
        JournalEntry.objects.filter(
            tenant=tenant, date__gte=start,
        ).values_list("date", flat=True)
    )
    doc_dates = set(
        Document.objects.filter(
            tenant=tenant, kind=Document.Kind.DAILY,
            created_at__date__gte=start,
        ).values_list("created_at__date", flat=True)
    )
    all_journal = journal_dates | doc_dates

    # Build grid data: 0 = inactive, 1 = active, 2 = active + journal
    # Calculate streak from today backwards
    values = []
    streak = 0
    streak_counting = True
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        mc = message_counts.get(d, 0)
        hj = d in all_journal
        if mc > 0 or hj:
            val = 2 if hj else 1
        else:
            val = 0
        values.append(val)

    # Count streak from end
    for v in reversed(values):
        if v > 0 and streak_counting:
            streak += 1
        else:
            streak_counting = False

    # Render as a grid: 6 columns x 5 rows (for 30 days)
    cols = 6
    rows = (days + cols - 1) // cols
    padded = values + [0] * (rows * cols - len(values))
    grid = np.array(padded).reshape(rows, cols)

    fig, ax = plt.subplots(figsize=(FIG_WIDTH, 2.2))
    ax.set_facecolor(MIST)
    fig.set_facecolor(MIST)

    # Draw cells
    from matplotlib.patches import FancyBboxPatch
    cell_size = 0.85
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            if idx >= days:
                continue
            v = grid[r, c]
            if v == 2:
                color = TEAL
            elif v == 1:
                color = DARK_TEAL
                alpha_val = 0.4
            else:
                color = SEPARATOR

            rect = FancyBboxPatch(
                (c - cell_size / 2, rows - 1 - r - cell_size / 2),
                cell_size, cell_size,
                boxstyle="round,pad=0.08",
                facecolor=color if v != 1 else TEAL,
                alpha=1.0 if v == 2 else (0.4 if v == 1 else 0.5),
                edgecolor="none",
            )
            ax.add_patch(rect)

    ax.set_xlim(-0.6, cols - 0.4)
    ax.set_ylim(-0.6, rows - 0.4)
    ax.set_aspect("equal")
    ax.axis("off")

    title = f"Last {days} Days"
    if streak > 0:
        title += f"  •  {streak}-day streak"
    ax.set_title(title, fontsize=11, fontweight="bold", color=INK, pad=8)

    # Legend
    ax.text(0, -0.9, "● Active + Journal", fontsize=7, color=TEAL,
            transform=ax.transData)
    ax.text(2.2, -0.9, "● Active", fontsize=7, color=INK_FAINT,
            transform=ax.transData)
    ax.text(3.8, -0.9, "● Inactive", fontsize=7, color=SEPARATOR,
            transform=ax.transData)

    return _render_to_png(fig)


def _render_mood_trend(tenant: Tenant, days: int = 30) -> bytes | None:
    """Line chart: mood over time, color-coded by energy."""
    from apps.journal.models import JournalEntry

    start = date.today() - timedelta(days=days - 1)
    entries = list(
        JournalEntry.objects.filter(
            tenant=tenant, date__gte=start,
        ).order_by("date").values("date", "mood", "energy")
    )
    if not entries:
        return None

    # Map moods to a numeric sentiment scale (best-effort)
    MOOD_SCORES = {
        "great": 5, "amazing": 5, "fantastic": 5, "excellent": 5,
        "happy": 4, "good": 4, "excited": 4, "grateful": 4, "hopeful": 4,
        "optimistic": 4, "content": 4, "proud": 4, "motivated": 4,
        "calm": 3, "okay": 3, "neutral": 3, "fine": 3, "steady": 3,
        "tired": 2, "meh": 2, "low": 2, "bored": 2, "restless": 2,
        "stressed": 2, "overwhelmed": 2, "scattered": 2,
        "sad": 1, "anxious": 1, "frustrated": 1, "angry": 1,
        "down": 1, "depressed": 1, "burned out": 1, "exhausted": 1,
    }
    ENERGY_COLORS = {"high": TEAL, "medium": PURPLE, "low": ROSE}

    dates = []
    scores = []
    colors = []
    for e in entries:
        mood = e["mood"].lower().strip()
        score = MOOD_SCORES.get(mood, 3)  # default neutral
        dates.append(e["date"])
        scores.append(score)
        colors.append(ENERGY_COLORS.get(e["energy"], INK_MUTED))

    fig, ax = plt.subplots(figsize=(FIG_WIDTH, FIG_HEIGHT))
    _setup_axes(ax, "Mood Trend")

    # Line connecting points
    ax.plot(dates, scores, color=INK_MUTED, linewidth=1, alpha=0.4, zorder=1)

    # Colored scatter by energy level
    for d, s, c in zip(dates, scores, colors):
        ax.scatter(d, s, color=c, s=40, zorder=2, edgecolors="white", linewidth=0.5)

    ax.set_ylim(0.5, 5.5)
    ax.set_yticks([1, 2, 3, 4, 5])
    ax.set_yticklabels(["Low", "Down", "Okay", "Good", "Great"], fontsize=7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 6)))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)

    # Energy legend
    for label, color in [("High energy", TEAL), ("Medium", PURPLE), ("Low", ROSE)]:
        ax.scatter([], [], color=color, s=25, label=label)
    ax.legend(fontsize=7, frameon=False, labelcolor=INK_MUTED, loc="lower left")
    fig.set_facecolor(MIST)

    return _render_to_png(fig)


# ── Dispatch ───────────────────────────────────────────────────────────────

_RENDERERS: dict[str, Any] = {
    "payoff_timeline": _render_payoff_timeline,
    "debt_vs_savings": _render_debt_vs_savings,
    "momentum_grid": _render_momentum_grid,
    "mood_trend": _render_mood_trend,
}


def render_chart(chart_type: str, tenant: Tenant, params: dict | None = None) -> bytes | None:
    """Render a chart by type for a tenant. Returns PNG bytes or None."""
    renderer = _RENDERERS.get(chart_type)
    if not renderer:
        logger.warning("Unknown chart type: %s", chart_type)
        return None
    try:
        kwargs = {}
        if params and "days" in params:
            try:
                kwargs["days"] = int(params["days"])
            except (ValueError, TypeError):
                pass
        return renderer(tenant, **kwargs)
    except Exception:
        logger.exception("Chart rendering failed for %s (tenant=%s)", chart_type, tenant.id)
        return None
