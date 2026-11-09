from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

from charts import generate_dashboard_chart
from config import (
    CHART_REFRESH_SECONDS,
    DATABASE_PATH,
    DEFAULT_LIVE_CHANNEL_ID,
    DISCORD_TOKEN,
    TIMEFRAMES,
    UPDATE_INTERVAL_SECONDS,
)
from dashboard import (
    LiveDashboardView,
    build_live_dashboard_embed,
    build_user_stats_embed,
)
from database import AnalyticsDatabase, VoiceStateFlags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
)
log = logging.getLogger("vc-analytics")


def extract_voice_flags(vs: discord.VoiceState) -> VoiceStateFlags:
    return VoiceStateFlags(
        is_muted=bool(vs.self_mute or vs.mute),
        is_deafened=bool(vs.self_deaf or vs.deaf),
        is_streaming=bool(vs.self_stream),
        is_video=bool(vs.self_video),
    )


class VCLiveAnalyticsBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)
        self.db = AnalyticsDatabase(DATABASE_PATH)
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._debounce_tasks: dict[int, asyncio.Task[None]] = {}
        self._last_chart_ts: dict[int, float] = {}
        self._cached_chart_bytes: dict[int, bytes] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._guild_locks:
            self._guild_locks[guild_id] = asyncio.Lock()
        return self._guild_locks[guild_id]

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.add_view(LiveDashboardView(self))
        self.add_cog_commands()
        await self.tree.sync()
        log.info("Database initialized & Slash Commands synced globally.")

    def add_cog_commands(self) -> None:
        self.tree.add_command(VCAnalyticsGroup(self))

    async def close(self) -> None:
        if self.live_dashboard_loop.is_running():
            self.live_dashboard_loop.cancel()
        if self.snapshot_loop.is_running():
            self.snapshot_loop.cancel()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "?")

        for guild in self.guilds:
            await self.reconcile_guild_voice_states(guild)
            # Apply default LIVE_CHANNEL_ID from .env if not already configured
            if DEFAULT_LIVE_CHANNEL_ID:
                settings = await self.db.get_guild_settings(guild.id)
                if not settings.get("live_channel_id"):
                    ch = guild.get_channel(DEFAULT_LIVE_CHANNEL_ID)
                    if isinstance(ch, discord.TextChannel):
                        await self.db.set_live_dashboard(guild.id, ch.id, None)

        if not self.live_dashboard_loop.is_running():
            self.live_dashboard_loop.start()
        if not self.snapshot_loop.is_running():
            self.snapshot_loop.start()

    async def reconcile_guild_voice_states(self, guild: discord.Guild) -> None:
        """
        Synchronize DB active_sessions with actual Discord voice channels on startup.
        Ensures 24/7 continuity even across bot restarts.
        """
        now = time.time()
        current_vc_members: dict[int, tuple[discord.Member, discord.VoiceChannel | discord.StageChannel]] = {}

        for channel in guild.voice_channels + guild.stage_channels:
            for member in channel.members:
                if not member.bot and member.voice:
                    current_vc_members[member.id] = (member, channel)

        existing_sessions = await self.db.get_active_sessions(guild.id)
        for sess in existing_sessions:
            if sess.user_id not in current_vc_members:
                await self.db.end_session(guild.id, sess.user_id, now=now)

        for uid, (member, channel) in current_vc_members.items():
            if member.voice:
                flags = extract_voice_flags(member.voice)
                await self.db.start_or_update_session(
                    guild_id=guild.id,
                    user_id=uid,
                    display_name=member.display_name,
                    channel_id=channel.id,
                    channel_name=channel.name,
                    flags=flags,
                    now=now,
                )

        await self.db.update_peak_concurrent(guild.id, len(current_vc_members))
        log.info(
            "Reconciled guild '%s': %d active voice user(s).",
            guild.name,
            len(current_vc_members),
        )

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        guild = member.guild
        now = time.time()

        if after.channel is None:
            # Member disconnected from voice
            await self.db.end_session(guild.id, member.id, now=now)
            log.info("[%s] %s left VC (%s)", guild.name, member.display_name, before.channel)
        else:
            # Member joined, switched channel, or toggled mute/deafen/stream/video
            flags = extract_voice_flags(after)
            await self.db.start_or_update_session(
                guild_id=guild.id,
                user_id=member.id,
                display_name=member.display_name,
                channel_id=after.channel.id,
                channel_name=after.channel.name,
                flags=flags,
                now=now,
            )
            active = await self.db.get_active_sessions(guild.id)
            await self.db.update_peak_concurrent(guild.id, len(active))
            log.info(
                "[%s] %s updated VC in #%s (mute=%s, deaf=%s, stream=%s)",
                guild.name,
                member.display_name,
                after.channel.name,
                flags.is_muted,
                flags.is_deafened,
                flags.is_streaming,
            )

        # Trigger a fast debounced live dashboard update so the live channel reflects changes immediately
        self.schedule_debounced_refresh(guild, force_chart=True)

    def schedule_debounced_refresh(
        self, guild: discord.Guild, force_chart: bool = False
    ) -> None:
        prev = self._debounce_tasks.get(guild.id)
        if prev and not prev.done():
            prev.cancel()

        async def _delayed() -> None:
            try:
                await asyncio.sleep(1.5)
                await self.refresh_guild_dashboard(guild, force_chart=force_chart)
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Error in debounced dashboard refresh for %s", guild.name)

        self._debounce_tasks[guild.id] = asyncio.create_task(_delayed())

    async def refresh_guild_dashboard(
        self,
        guild: discord.Guild,
        force_chart: bool = False,
    ) -> discord.Message | None:
        """
        Update (or create if missing) the 24/7 Live Analytics message in the guild's
        dedicated analytics channel.
        """
        lock = self._get_lock(guild.id)
        async with lock:
            settings = await self.db.get_guild_settings(guild.id)
            channel_id = settings.get("live_channel_id")
            if not channel_id:
                return None

            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return None

            tf_key = settings.get("default_timeframe") or "24h"
            now = time.time()
            chart_filename = "vc_analytics_chart.png"

            embed, raw_data = await build_live_dashboard_embed(
                guild=guild,
                db=self.db,
                timeframe_key=tf_key,
                attach_chart_filename=chart_filename,
                now=now,
            )

            # Re-render the Statbot chart image periodically or when forced
            last_chart = self._last_chart_ts.get(guild.id, 0.0)
            need_new_chart = (
                force_chart
                or (guild.id not in self._cached_chart_bytes)
                or (now - last_chart >= CHART_REFRESH_SECONDS)
            )

            if need_new_chart:
                chart_buf = await generate_dashboard_chart(
                    guild_name=guild.name,
                    timeframe_label=raw_data["timeframe_label"],
                    activity_series=raw_data["activity_series"],
                    leaderboard=raw_data["leaderboard"],
                    channels=raw_data["channels"],
                    live_count=raw_data["live_count"],
                )
                self._cached_chart_bytes[guild.id] = chart_buf.getvalue()
                self._last_chart_ts[guild.id] = now

            chart_bytes = self._cached_chart_bytes[guild.id]
            view = LiveDashboardView(self)

            message_id = settings.get("live_message_id")
            if message_id:
                try:
                    msg = await channel.fetch_message(int(message_id))
                    file = discord.File(io.BytesIO(chart_bytes), filename=chart_filename)
                    await msg.edit(embed=embed, attachments=[file], view=view)
                    return msg
                except discord.NotFound:
                    log.warning("Live dashboard message not found in #%s; recreating...", channel.name)
                except discord.HTTPException as exc:
                    log.warning("Failed to edit live dashboard message: %s", exc)
                    return None

            # Create new persistent live message if none exists or previous was deleted
            try:
                file = discord.File(io.BytesIO(chart_bytes), filename=chart_filename)
                new_msg = await channel.send(embed=embed, file=file, view=view)
                await self.db.set_live_message_id(guild.id, new_msg.id)
                try:
                    await new_msg.pin(reason="24/7 Live Voice Analytics Dashboard")
                except discord.HTTPException:
                    pass
                return new_msg
            except discord.HTTPException as exc:
                log.error("Could not send live dashboard in #%s: %s", channel.name, exc)
                return None

    # ------------------------------------------------------------------
    # 24/7 Background Tasks
    # ------------------------------------------------------------------
    @tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
    async def live_dashboard_loop(self) -> None:
        for guild in self.guilds:
            try:
                settings = await self.db.get_guild_settings(guild.id)
                if settings.get("live_channel_id"):
                    await self.refresh_guild_dashboard(guild, force_chart=False)
            except Exception:
                log.exception("Error updating live dashboard for guild %s", guild.name)

    @live_dashboard_loop.before_loop
    async def before_live_dashboard_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=60)
    async def snapshot_loop(self) -> None:
        now = time.time()
        for guild in self.guilds:
            try:
                await self.db.record_snapshot(guild.id, now=now)
            except Exception:
                log.exception("Error recording snapshot for guild %s", guild.name)

    @snapshot_loop.before_loop
    async def before_snapshot_loop(self) -> None:
        await self.wait_until_ready()


class VCAnalyticsGroup(app_commands.Group):
    """Slash commands (`/vc ...`) for configuring and querying 24/7 Voice Analytics."""

    def __init__(self, bot: VCLiveAnalyticsBot) -> None:
        super().__init__(
            name="vc",
            description="24/7 Live Voice Analytics & Statbot-style VC Time Tracking",
        )
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Set up the dedicated 24/7 live voice analytics channel (creates one if omitted).",
    )
    @app_commands.describe(
        channel="Existing text channel to use (leave blank to auto-create #📊-live-vc-stats)"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    attach_files=True,
                    manage_messages=True,
                ),
            }
            channel = await guild.create_text_channel(
                name="📊-live-vc-stats",
                topic="📡 24/7 Live Voice Channel Analytics & Statbot Leaderboards (Auto-Updated)",
                overwrites=overwrites,
                reason="Created 24/7 Live Voice Analytics Channel",
            )

        await self.bot.reconcile_guild_voice_states(guild)
        await self.bot.db.set_live_dashboard(guild.id, channel.id, None)
        msg = await self.bot.refresh_guild_dashboard(guild, force_chart=True)

        if msg:
            await interaction.followup.send(
                f"✅ **24/7 Live Voice Analytics** is now active in {channel.mention}!\n"
                f"🔗 **Dashboard Message:** {msg.jump_url}\n"
                f"⏱️ Auto-updates every **`{UPDATE_INTERVAL_SECONDS}s`** and immediately on any VC join/leave/mute/stream event.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Configured {channel.mention}, but could not post the dashboard. "
                "Please verify the bot has `Send Messages`, `Embed Links`, and `Attach Files` permissions.",
                ephemeral=True,
            )

    @app_commands.command(
        name="stats",
        description="View detailed live & historical VC analytics for a member.",
    )
    @app_commands.describe(member="The member to inspect (defaults to yourself)")
    async def stats_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        target = member or interaction.user
        embed = await build_user_stats_embed(interaction.guild, target, self.bot.db)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="leaderboard",
        description="Display the voice time leaderboard & Statbot chart for a timeframe.",
    )
    @app_commands.describe(timeframe="Time window to calculate rankings for")
    async def leaderboard_cmd(
        self,
        interaction: discord.Interaction,
        timeframe: Literal["24h", "7d", "30d", "all"] = "24h",
    ) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer()
        chart_filename = "vc_leaderboard.png"
        embed, raw_data = await build_live_dashboard_embed(
            guild=interaction.guild,
            db=self.bot.db,
            timeframe_key=timeframe,
            attach_chart_filename=chart_filename,
        )
        chart_buf = await generate_dashboard_chart(
            guild_name=interaction.guild.name,
            timeframe_label=raw_data["timeframe_label"],
            activity_series=raw_data["activity_series"],
            leaderboard=raw_data["leaderboard"],
            channels=raw_data["channels"],
            live_count=raw_data["live_count"],
        )
        file = discord.File(chart_buf, filename=chart_filename)
        await interaction.followup.send(embed=embed, file=file)

    @app_commands.command(
        name="refresh",
        description="Force an immediate update of the 24/7 live voice analytics channel.",
    )
    async def refresh_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        msg = await self.bot.refresh_guild_dashboard(interaction.guild, force_chart=True)
        if msg:
            await interaction.followup.send(
                f"🔄 Refreshed the live dashboard: {msg.jump_url}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "⚠️ No live dashboard channel is set yet. Run `/vc setup` first!",
                ephemeral=True,
            )

    @app_commands.command(
        name="export",
        description="Export the server's voice session history as a CSV file.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def export_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True)
        async with self.bot.db.conn.execute(
            """
            SELECT user_id, display_name, channel_name, joined_at, left_at,
                   total_sec, active_sec, muted_sec, deafened_sec, stream_sec
              FROM vc_sessions_history
             WHERE guild_id = ?
             ORDER BY left_at DESC
             LIMIT 5000
            """,
            (interaction.guild.id,),
        ) as cur:
            rows = await cur.fetchall()

        str_buf = io.StringIO()
        writer = csv.writer(str_buf)
        writer.writerow(
            [
                "user_id",
                "display_name",
                "channel_name",
                "joined_unix",
                "left_unix",
                "total_sec",
                "active_sec",
                "muted_sec",
                "deafened_sec",
                "stream_sec",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r["user_id"],
                    r["display_name"],
                    r["channel_name"],
                    int(r["joined_at"]),
                    int(r["left_at"]),
                    round(r["total_sec"], 1),
                    round(r["active_sec"], 1),
                    round(r["muted_sec"], 1),
                    round(r["deafened_sec"], 1),
                    round(r["stream_sec"], 1),
                ]
            )

        byte_buf = io.BytesIO(str_buf.getvalue().encode("utf-8"))
        file = discord.File(
            byte_buf, filename=f"vc_analytics_{interaction.guild.id}.csv"
        )
        await interaction.followup.send(
            f"📥 Exported `{len(rows)}` completed voice sessions:",
            file=file,
            ephemeral=True,
        )


def main() -> None:
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your_discord_bot_token_here":
        raise SystemExit(
            "❌ DISCORD_TOKEN is not set! Copy .env.example to .env and paste your Discord Bot Token."
        )
    bot = VCLiveAnalyticsBot()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
