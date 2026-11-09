from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import time
from typing import TYPE_CHECKING, Any

import discord

from config import (
    COLOR_BLURPLE,
    COLOR_LIVE_GREEN,
    TIMEFRAMES,
    UPDATE_INTERVAL_SECONDS,
    format_duration,
)
from database import AnalyticsDatabase, LiveSessionInfo

if TYPE_CHECKING:
    from bot import VCLiveAnalyticsBot


def _progress_bar(ratio: float, length: int = 8) -> str:
    clamped = max(0.0, min(1.0, ratio))
    filled = int(round(clamped * length))
    return "█" * filled + "░" * (length - filled)


def _format_voice_badges(sess: LiveSessionInfo) -> str:
    badges: list[str] = []
    if sess.is_deafened:
        badges.append("🎧 `Deaf`")
    elif sess.is_muted:
        badges.append("🔇 `Muted`")
    else:
        badges.append("🎙️ `Mic ON`")

    if sess.is_streaming:
        badges.append("🖥️ `LIVE`")
    if sess.is_video:
        badges.append("📹 `Cam`")
    return " ".join(badges)


async def build_live_dashboard_embed(
    guild: discord.Guild,
    db: AnalyticsDatabase,
    timeframe_key: str = "24h",
    attach_chart_filename: str | None = "vc_analytics_chart.png",
    now: float | None = None,
) -> tuple[discord.Embed, dict[str, Any]]:
    """
    Construct the 24/7 Live Voice Analytics Discord Embed and return both
    the embed and the raw queried data (for chart generation).
    """
    now = now or time.time()
    if timeframe_key not in TIMEFRAMES:
        timeframe_key = "24h"
    tf_label, tf_hours = TIMEFRAMES[timeframe_key]

    settings = await db.get_guild_settings(guild.id)
    active_sessions = await db.get_active_sessions(guild.id)
    peak_concurrent = await db.update_peak_concurrent(guild.id, len(active_sessions))

    leaderboard = await db.get_leaderboard(guild.id, hours=tf_hours, limit=10, now=now)
    channels = await db.get_channel_breakdown(guild.id, hours=tf_hours, limit=5, now=now)

    chart_hours = tf_hours if tf_hours is not None else 24 * 7
    buckets = 24 if chart_hours <= 48 else 28
    activity_series = await db.get_hourly_activity_series(
        guild.id, hours=chart_hours, buckets=buckets, now=now
    )

    live_count = len(active_sessions)
    active_mics = sum(1 for s in active_sessions if not s.is_muted and not s.is_deafened)
    streaming_now = sum(1 for s in active_sessions if s.is_streaming)
    muted_now = sum(1 for s in active_sessions if s.is_muted or s.is_deafened)

    embed_color = COLOR_LIVE_GREEN if live_count > 0 else COLOR_BLURPLE
    status_dot = "🟢 **LIVE IN VC**" if live_count > 0 else "🌙 **IDLE (0 in VC)**"

    description_lines = [
        f"{status_dot} • Refreshing every **`{UPDATE_INTERVAL_SECONDS}s`** & on voice events",
        (
            f"👥 **In VC:** `{live_count}` (`🎙️ {active_mics}` • `🔇 {muted_now}` • `🖥️ {streaming_now}`)  "
            f"│  🔥 **Peak VC:** `{peak_concurrent}`  │  📅 **View:** `{tf_label}`"
        ),
    ]

    embed = discord.Embed(
        title=f"📡 24/7 Live Voice Analytics — {guild.name}",
        description="\n".join(description_lines),
        color=embed_color,
        timestamp=datetime.now(timezone.utc),
    )

    # ------------------------------------------------------------------
    # Section 1: Currently in Voice Channels (Live Session Timers)
    # ------------------------------------------------------------------
    if active_sessions:
        by_channel: dict[tuple[int, str], list[LiveSessionInfo]] = defaultdict(list)
        for sess in active_sessions:
            by_channel[(sess.channel_id, sess.channel_name)].append(sess)

        live_lines: list[str] = []
        for (cid, cname), members in by_channel.items():
            live_lines.append(f"🔊 **<#{cid}>** (`{len(members)}` in channel)")
            for sess in members:
                totals = sess.compute_live_totals(now)
                badges = _format_voice_badges(sess)
                joined_ts = int(sess.joined_at)
                elapsed_str = format_duration(totals["total_sec"])
                act_str = format_duration(totals["active_sec"], compact=True)
                mut_str = format_duration(
                    totals["muted_sec"] + totals["deafened_sec"], compact=True
                )
                extra_stream = (
                    f" • 🖥️ `{format_duration(totals['stream_sec'], compact=True)}`"
                    if totals["stream_sec"] >= 5
                    else ""
                )
                live_lines.append(
                    f"> <@{sess.user_id}> {badges}\n"
                    f"> └ ⏱️ **`{elapsed_str}`** (joined <t:{joined_ts}:R>) • "
                    f"🎙️ `{act_str}` │ 🔇 `{mut_str}`{extra_stream}"
                )

        live_text = "\n".join(live_lines)
        if len(live_text) > 1020:
            live_text = live_text[:1015] + "\n..."
    else:
        live_text = (
            "*No members are currently in a voice channel.*\n"
            "*Join any voice channel to start your live session timer immediately!*"
        )

    embed.add_field(
        name=f"🎙️ Currently in Voice ({live_count} Active)",
        value=live_text,
        inline=False,
    )

    # ------------------------------------------------------------------
    # Section 2: Voice Time Leaderboard (Includes Live Ongoing Sessions)
    # ------------------------------------------------------------------
    medals = ["🥇", "🥈", "🥉"]
    if leaderboard:
        lb_lines: list[str] = []
        for idx, u in enumerate(leaderboard[:10]):
            rank_icon = medals[idx] if idx < 3 else f"`#{idx + 1:02d}`"
            live_tag = " 🟢" if u.get("is_live") else ""
            tot_s = u["total_sec"]
            act_pct = int(round((u["active_sec"] / tot_s) * 100)) if tot_s > 0 else 0
            stream_part = (
                f" • 🖥️ `{format_duration(u['stream_sec'], compact=True)}`"
                if u["stream_sec"] >= 30
                else ""
            )
            lb_lines.append(
                f"{rank_icon} <@{u['user_id']}>{live_tag} — **`{format_duration(tot_s)}`** "
                f"(`🎙️ {act_pct}%`{stream_part})"
            )
        lb_text = "\n".join(lb_lines)
        if len(lb_text) > 1020:
            lb_text = lb_text[:1015] + "\n..."
    else:
        lb_text = "*No voice time recorded in this timeframe yet.*"

    embed.add_field(
        name=f"🏆 Voice Leaderboard ({tf_label})",
        value=lb_text,
        inline=False,
    )

    # ------------------------------------------------------------------
    # Section 3: Top Voice Channels & Server Breakdown (Side-by-Side)
    # ------------------------------------------------------------------
    if channels:
        max_chan_sec = max((c["total_sec"] for c in channels), default=1.0)
        ch_lines = [
            f"`{_progress_bar(c['total_sec'] / max(1.0, max_chan_sec), 6)}` "
            f"<#{c['channel_id']}> — **`{format_duration(c['total_sec'], compact=True)}`**"
            for c in channels[:5]
        ]
        ch_text = "\n".join(ch_lines)
    else:
        ch_text = "*No channel stats yet.*"

    embed.add_field(
        name=f"🔊 Top Channels ({tf_label})",
        value=ch_text,
        inline=True,
    )

    total_vc_sec = sum(u["total_sec"] for u in leaderboard)
    total_active_sec = sum(u["active_sec"] for u in leaderboard)
    total_muted_sec = sum(u["muted_sec"] + u["deafened_sec"] for u in leaderboard)
    total_stream_sec = sum(u["stream_sec"] for u in leaderboard)

    summary_text = (
        f"⏱️ **Total Time:** `{format_duration(total_vc_sec, compact=True)}`\n"
        f"🎙️ **Active Mic:** `{format_duration(total_active_sec, compact=True)}`\n"
        f"🔇 **Muted/Deaf:** `{format_duration(total_muted_sec, compact=True)}`\n"
        f"🖥️ **Streaming:** `{format_duration(total_stream_sec, compact=True)}`"
    )
    embed.add_field(
        name=f"📊 Totals ({tf_label})",
        value=summary_text,
        inline=True,
    )

    if attach_chart_filename:
        embed.set_image(url=f"attachment://{attach_chart_filename}")

    embed.set_footer(
        text=f"Statbot-Style Live 24/7 Voice Tracker • Timeframe: {tf_label} • Auto-Synced"
    )

    raw_data = {
        "timeframe_key": timeframe_key,
        "timeframe_label": tf_label,
        "active_sessions": active_sessions,
        "leaderboard": leaderboard,
        "channels": channels,
        "activity_series": activity_series,
        "live_count": live_count,
        "settings": settings,
    }
    return embed, raw_data


async def build_user_stats_embed(
    guild: discord.Guild,
    member: discord.Member | discord.User,
    db: AnalyticsDatabase,
) -> discord.Embed:
    """Build a detailed Statbot personal voice profile card across all timeframes."""
    now = time.time()
    stats_24h = await db.get_user_stats(guild.id, member.id, hours=24, now=now)
    stats_7d = await db.get_user_stats(guild.id, member.id, hours=24 * 7, now=now)
    stats_30d = await db.get_user_stats(guild.id, member.id, hours=24 * 30, now=now)
    stats_all = await db.get_user_stats(guild.id, member.id, hours=None, now=now)

    live_sess: LiveSessionInfo | None = stats_all["live_session"]
    color = COLOR_LIVE_GREEN if live_sess else COLOR_BLURPLE

    embed = discord.Embed(
        title=f"🎙️ Voice Analytics Profile — {member.display_name}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    if live_sess:
        live_totals = live_sess.compute_live_totals(now)
        badges = _format_voice_badges(live_sess)
        embed.add_field(
            name="🟢 Currently Live in Voice",
            value=(
                f"🔊 **Channel:** <#{live_sess.channel_id}> ({badges})\n"
                f"⏱️ **Current Session:** **`{format_duration(live_totals['total_sec'])}`** "
                f"(joined <t:{int(live_sess.joined_at)}:R>)\n"
                f"🎙️ **Active:** `{format_duration(live_totals['active_sec'], compact=True)}` │ "
                f"🔇 **Muted/Deaf:** `{format_duration(live_totals['muted_sec'] + live_totals['deafened_sec'], compact=True)}` │ "
                f"🖥️ **Stream:** `{format_duration(live_totals['stream_sec'], compact=True)}`"
            ),
            inline=False,
        )

    embed.add_field(
        name="🕒 Last 24 Hours",
        value=(
            f"**Total:** `{format_duration(stats_24h['total_sec'])}`\n"
            f"**Rank:** `#{stats_24h['rank']}`\n" if stats_24h["rank"] else "**Rank:** `Unranked`\n"
            f"**Active:** `{format_duration(stats_24h['active_sec'], compact=True)}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="📅 Last 7 Days",
        value=(
            f"**Total:** `{format_duration(stats_7d['total_sec'])}`\n"
            f"**Rank:** `#{stats_7d['rank']}`\n" if stats_7d["rank"] else "**Rank:** `Unranked`\n"
            f"**Active:** `{format_duration(stats_7d['active_sec'], compact=True)}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="♾️ All-Time",
        value=(
            f"**Total:** `{format_duration(stats_all['total_sec'])}`\n"
            f"**Rank:** `#{stats_all['rank']}`\n" if stats_all["rank"] else "**Rank:** `Unranked`\n"
            f"**Active:** `{format_duration(stats_all['active_sec'], compact=True)}`"
        ),
        inline=True,
    )

    tot_all = stats_all["total_sec"]
    if tot_all > 0:
        act_pct = int(round((stats_all["active_sec"] / tot_all) * 100))
        mut_pct = 100 - act_pct
        breakdown_val = (
            f"🎙️ **Active Mic:** `{format_duration(stats_all['active_sec'])}` (`{act_pct}%`)\n"
            f"🔇 **Muted / Deafened:** `{format_duration(stats_all['muted_sec'] + stats_all['deafened_sec'])}` (`{mut_pct}%`)\n"
            f"🖥️ **Screen Share:** `{format_duration(stats_all['stream_sec'])}`\n"
            f"🗓️ **Last 30 Days:** `{format_duration(stats_30d['total_sec'])}`"
        )
    else:
        breakdown_val = "*No recorded voice sessions yet.*"

    embed.add_field(
        name="📊 Voice Activity Breakdown (All-Time)",
        value=breakdown_val,
        inline=False,
    )

    if stats_all["top_channels"]:
        ch_lines = [
            f"**{idx}.** `#{cname}` — **`{format_duration(sec, compact=True)}`**"
            for idx, (cname, sec) in enumerate(stats_all["top_channels"], start=1)
        ]
        embed.add_field(
            name="🔊 Favorite Voice Channels",
            value="\n".join(ch_lines),
            inline=False,
        )

    return embed


class LiveDashboardView(discord.ui.View):
    """Persistent interactive controls attached to the 24/7 Live Analytics message."""

    def __init__(self, bot: VCLiveAnalyticsBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _switch_timeframe(
        self, interaction: discord.Interaction, tf_key: str
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer()
        await self.bot.db.set_default_timeframe(interaction.guild.id, tf_key)
        await self.bot.refresh_guild_dashboard(interaction.guild, force_chart=True)

    @discord.ui.button(
        label="Refresh Live",
        emoji="🔄",
        style=discord.ButtonStyle.success,
        custom_id="vc_dash:refresh",
        row=0,
    )
    async def refresh_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer()
        await self.bot.refresh_guild_dashboard(interaction.guild, force_chart=True)

    @discord.ui.button(
        label="24h",
        emoji="🕒",
        style=discord.ButtonStyle.secondary,
        custom_id="vc_dash:tf:24h",
        row=0,
    )
    async def tf_24h_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch_timeframe(interaction, "24h")

    @discord.ui.button(
        label="7d",
        emoji="📅",
        style=discord.ButtonStyle.secondary,
        custom_id="vc_dash:tf:7d",
        row=0,
    )
    async def tf_7d_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch_timeframe(interaction, "7d")

    @discord.ui.button(
        label="30d",
        emoji="🗓️",
        style=discord.ButtonStyle.secondary,
        custom_id="vc_dash:tf:30d",
        row=0,
    )
    async def tf_30d_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch_timeframe(interaction, "30d")

    @discord.ui.button(
        label="All-Time",
        emoji="♾️",
        style=discord.ButtonStyle.secondary,
        custom_id="vc_dash:tf:all",
        row=0,
    )
    async def tf_all_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._switch_timeframe(interaction, "all")

    @discord.ui.button(
        label="My VC Stats",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="vc_dash:mystats",
        row=1,
    )
    async def my_stats_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None:
            return
        embed = await build_user_stats_embed(
            interaction.guild, interaction.user, self.bot.db
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
