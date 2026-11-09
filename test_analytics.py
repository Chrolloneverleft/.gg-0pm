from __future__ import annotations

import asyncio
from pathlib import Path
import time

from charts import generate_dashboard_chart
from config import format_duration
from database import AnalyticsDatabase, VoiceStateFlags


async def run_smoke_test() -> None:
    test_db_path = Path(__file__).parent / "data" / "test_vc_analytics.db"
    if test_db_path.exists():
        test_db_path.unlink()

    db = AnalyticsDatabase(test_db_path)
    await db.connect()

    now = time.time()
    guild_id = 123456789

    # Simulate Alice joining 90 minutes ago (active mic for 60m, then muted for 15m, now active + streaming for 15m)
    await db.start_or_update_session(
        guild_id=guild_id,
        user_id=1001,
        display_name="Alice",
        channel_id=501,
        channel_name="General VC",
        flags=VoiceStateFlags(False, False, False, False),
        now=now - 5400,
    )
    await db.start_or_update_session(
        guild_id=guild_id,
        user_id=1001,
        display_name="Alice",
        channel_id=501,
        channel_name="General VC",
        flags=VoiceStateFlags(True, False, False, False),
        now=now - 1800,
    )
    await db.start_or_update_session(
        guild_id=guild_id,
        user_id=1001,
        display_name="Alice",
        channel_id=501,
        channel_name="General VC",
        flags=VoiceStateFlags(False, False, True, False),
        now=now - 900,
    )

    # Simulate Bob completing a 45m session earlier today
    await db.start_or_update_session(
        guild_id=guild_id,
        user_id=1002,
        display_name="Bob",
        channel_id=502,
        channel_name="Gaming Squad",
        flags=VoiceStateFlags(False, False, False, False),
        now=now - 7200,
    )
    await db.end_session(guild_id=guild_id, user_id=1002, now=now - 4500)

    active = await db.get_active_sessions(guild_id)
    leaderboard = await db.get_leaderboard(guild_id, hours=24, now=now)
    channels = await db.get_channel_breakdown(guild_id, hours=24, now=now)
    series = await db.get_hourly_activity_series(guild_id, hours=24, now=now)

    print(f"Active sessions: {len(active)}")
    for sess in active:
        totals = sess.compute_live_totals(now)
        print(
            f"  [LIVE] {sess.display_name} in #{sess.channel_name}: "
            f"total={format_duration(totals['total_sec'])}, "
            f"active={format_duration(totals['active_sec'])}, "
            f"muted={format_duration(totals['muted_sec'])}, "
            f"stream={format_duration(totals['stream_sec'])}"
        )

    print("Leaderboard (24h):")
    for idx, u in enumerate(leaderboard, 1):
        print(f"  #{idx} {u['display_name']} -> {format_duration(u['total_sec'])} (live={u['is_live']})")

    chart_buf = await generate_dashboard_chart(
        guild_name="Demo Server",
        timeframe_label="Last 24 Hours",
        activity_series=series,
        leaderboard=leaderboard,
        channels=channels,
        live_count=len(active),
    )
    out_chart = Path(__file__).parent / "data" / "sample_chart.png"
    out_chart.write_bytes(chart_buf.getvalue())
    print(f"Saved sample Statbot chart to: {out_chart}")

    await db.close()
    if test_db_path.exists():
        test_db_path.unlink()


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
