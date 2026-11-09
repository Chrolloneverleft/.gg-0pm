from __future__ import annotations

import asyncio
from datetime import datetime
import io
import os
import tempfile
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from config import format_duration

# Discord Dark Theme Colors
BG_OUTER = "#1E1F22"
BG_PANEL = "#2B2D31"
TEXT_PRIMARY = "#F2F3F5"
TEXT_MUTED = "#949BA4"
GRID_COLOR = "#383A40"

ACCENT_GREEN = "#23A55A"    # Active Mic
ACCENT_YELLOW = "#F0B232"   # Muted / Deafened
ACCENT_PURPLE = "#9B59B6"   # Streaming
ACCENT_BLURPLE = "#5865F2"  # Channels / Primary


def _render_dashboard_chart_sync(
    guild_name: str,
    timeframe_label: str,
    activity_series: list[dict[str, Any]],
    leaderboard: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    live_count: int,
) -> io.BytesIO:
    """Synchronous matplotlib rendering function executed inside a worker thread."""
    plt.rcParams.update(
        {
            "figure.facecolor": BG_OUTER,
            "axes.facecolor": BG_PANEL,
            "axes.edgecolor": GRID_COLOR,
            "axes.labelcolor": TEXT_MUTED,
            "text.color": TEXT_PRIMARY,
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_PRIMARY,
            "font.sans-serif": ["DejaVu Sans", "Segoe UI", "Arial"],
        }
    )

    fig = plt.figure(figsize=(11.5, 6.4), dpi=130)
    gs = fig.add_gridspec(
        2,
        2,
        height_ratios=[1.15, 1.0],
        width_ratios=[1.15, 0.85],
        hspace=0.42,
        wspace=0.28,
        left=0.07,
        right=0.96,
        top=0.88,
        bottom=0.09,
    )

    # Header Banner
    fig.suptitle(
        f"{guild_name}  •  Live Voice Analytics ({timeframe_label})",
        fontsize=14,
        fontweight="bold",
        color=TEXT_PRIMARY,
        x=0.07,
        y=0.955,
        ha="left",
    )
    status_badge = f"● LIVE IN VC: {live_count} USER{'S' if live_count != 1 else ''}"
    fig.text(
        0.96,
        0.955,
        status_badge,
        fontsize=10.5,
        fontweight="bold",
        color=ACCENT_GREEN if live_count > 0 else TEXT_MUTED,
        ha="right",
    )

    # ------------------------------------------------------------------
    # Panel 1 (Top Full-Width): Statbot Activity Area Chart
    # ------------------------------------------------------------------
    ax_time = fig.add_subplot(gs[0, :])
    ax_time.set_title(
        "Voice Activity Timeline (Active Mic vs. Muted/Deafened Minutes)",
        fontsize=10.5,
        fontweight="bold",
        color=TEXT_PRIMARY,
        loc="left",
        pad=8,
    )

    if activity_series:
        times = [
            datetime.fromtimestamp((b["bucket_start"] + b["bucket_end"]) / 2.0)
            for b in activity_series
        ]
        active_vals = [b["active_min"] for b in activity_series]
        muted_vals = [b["muted_min"] for b in activity_series]
        total_vals = [a + m for a, m in zip(active_vals, muted_vals)]

        ax_time.fill_between(
            times,
            0,
            active_vals,
            color=ACCENT_GREEN,
            alpha=0.38,
            label="Active Mic (min)",
        )
        ax_time.plot(times, active_vals, color=ACCENT_GREEN, linewidth=2.0)

        ax_time.fill_between(
            times,
            active_vals,
            total_vals,
            color=ACCENT_YELLOW,
            alpha=0.28,
            label="Muted / Deafened (min)",
        )
        ax_time.plot(times, total_vals, color=ACCENT_YELLOW, linewidth=1.4, linestyle="--")

        max_y = max(total_vals, default=0.0)
        ax_time.set_ylim(0, max(10.0, max_y * 1.2))
        span_hours = (activity_series[-1]["bucket_end"] - activity_series[0]["bucket_start"]) / 3600.0
        if span_hours <= 48:
            ax_time.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        else:
            ax_time.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax_time.legend(
            loc="upper right",
            frameon=True,
            facecolor=BG_OUTER,
            edgecolor=GRID_COLOR,
            fontsize=8.5,
        )
    else:
        ax_time.text(
            0.5,
            0.5,
            "No voice activity recorded yet",
            ha="center",
            va="center",
            color=TEXT_MUTED,
            transform=ax_time.transAxes,
        )

    ax_time.set_ylabel("Voice Mins", fontsize=9)
    ax_time.grid(True, color=GRID_COLOR, linestyle=":", alpha=0.6)
    ax_time.spines["top"].set_visible(False)
    ax_time.spines["right"].set_visible(False)

    # ------------------------------------------------------------------
    # Panel 2 (Bottom Left): Top Voice Members (Stacked Horizontal Bars)
    # ------------------------------------------------------------------
    ax_users = fig.add_subplot(gs[1, 0])
    ax_users.set_title(
        "Top Voice Members (Hours)",
        fontsize=10.5,
        fontweight="bold",
        color=TEXT_PRIMARY,
        loc="left",
        pad=8,
    )

    top_users = leaderboard[:6]
    if top_users:
        top_users_rev = list(reversed(top_users))
        labels = [
            f"{'● ' if u.get('is_live') else ''}{u['display_name'][:16]}"
            for u in top_users_rev
        ]
        active_hrs = [u["active_sec"] / 3600.0 for u in top_users_rev]
        inactive_hrs = [(u["muted_sec"] + u["deafened_sec"]) / 3600.0 for u in top_users_rev]
        totals_sec = [u["total_sec"] for u in top_users_rev]

        y_pos = range(len(labels))
        ax_users.barh(
            y_pos,
            active_hrs,
            color=ACCENT_GREEN,
            height=0.58,
            label="Active",
        )
        ax_users.barh(
            y_pos,
            inactive_hrs,
            left=active_hrs,
            color=ACCENT_YELLOW,
            height=0.58,
            alpha=0.8,
            label="Muted/Deaf",
        )
        ax_users.set_yticks(list(y_pos))
        ax_users.set_yticklabels(labels, fontsize=9)

        max_hr = max((t / 3600.0 for t in totals_sec), default=0.1)
        ax_users.set_xlim(0, max(0.2, max_hr * 1.28))

        for idx, tot_sec in enumerate(totals_sec):
            hr_val = tot_sec / 3600.0
            ax_users.text(
                hr_val + max(0.01, max_hr * 0.03),
                idx,
                format_duration(tot_sec, compact=True),
                va="center",
                ha="left",
                fontsize=8.5,
                color=TEXT_PRIMARY,
            )
    else:
        ax_users.text(
            0.5,
            0.5,
            "Waiting for VC sessions...",
            ha="center",
            va="center",
            color=TEXT_MUTED,
            transform=ax_users.transAxes,
        )
        ax_users.set_yticks([])

    ax_users.set_xlabel("Hours", fontsize=8.5)
    ax_users.grid(True, axis="x", color=GRID_COLOR, linestyle=":", alpha=0.6)
    ax_users.spines["top"].set_visible(False)
    ax_users.spines["right"].set_visible(False)

    # ------------------------------------------------------------------
    # Panel 3 (Bottom Right): Top Voice Channels
    # ------------------------------------------------------------------
    ax_chans = fig.add_subplot(gs[1, 1])
    ax_chans.set_title(
        "Most Active Voice Channels",
        fontsize=10.5,
        fontweight="bold",
        color=TEXT_PRIMARY,
        loc="left",
        pad=8,
    )

    top_chans = channels[:6]
    if top_chans:
        top_chans_rev = list(reversed(top_chans))
        c_labels = [f"# {c['channel_name'][:16]}" for c in top_chans_rev]
        c_hrs = [c["total_sec"] / 3600.0 for c in top_chans_rev]
        c_secs = [c["total_sec"] for c in top_chans_rev]

        y_pos = range(len(c_labels))
        ax_chans.barh(y_pos, c_hrs, color=ACCENT_BLURPLE, height=0.58)
        ax_chans.set_yticks(list(y_pos))
        ax_chans.set_yticklabels(c_labels, fontsize=9)

        max_c_hr = max(c_hrs, default=0.1)
        ax_chans.set_xlim(0, max(0.2, max_c_hr * 1.32))

        for idx, tot_sec in enumerate(c_secs):
            hr_val = tot_sec / 3600.0
            ax_chans.text(
                hr_val + max(0.01, max_c_hr * 0.03),
                idx,
                format_duration(tot_sec, compact=True),
                va="center",
                ha="left",
                fontsize=8.5,
                color=TEXT_PRIMARY,
            )
    else:
        ax_chans.text(
            0.5,
            0.5,
            "No channel activity yet",
            ha="center",
            va="center",
            color=TEXT_MUTED,
            transform=ax_chans.transAxes,
        )
        ax_chans.set_yticks([])

    ax_chans.set_xlabel("Hours", fontsize=8.5)
    ax_chans.grid(True, axis="x", color=GRID_COLOR, linestyle=":", alpha=0.6)
    ax_chans.spines["top"].set_visible(False)
    ax_chans.spines["right"].set_visible(False)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG_OUTER)
    plt.close(fig)
    buf.seek(0)
    return buf


async def generate_dashboard_chart(
    guild_name: str,
    timeframe_label: str,
    activity_series: list[dict[str, Any]],
    leaderboard: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    live_count: int,
) -> io.BytesIO:
    """Render the Statbot-style dark-mode analytics image asynchronously."""
    return await asyncio.to_thread(
        _render_dashboard_chart_sync,
        guild_name,
        timeframe_label,
        activity_series,
        leaderboard,
        channels,
        live_count,
    )
