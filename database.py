from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import aiosqlite


@dataclass
class VoiceStateFlags:
    is_muted: bool
    is_deafened: bool
    is_streaming: bool
    is_video: bool

    @property
    def primary_state(self) -> str:
        """Classify primary voice state for active vs muted/deafened breakdown."""
        if self.is_deafened:
            return "deafened"
        if self.is_muted:
            return "muted"
        return "active"


@dataclass
class LiveSessionInfo:
    guild_id: int
    user_id: int
    display_name: str
    channel_id: int
    channel_name: str
    joined_at: float
    last_state_change_at: float
    is_muted: bool
    is_deafened: bool
    is_streaming: bool
    is_video: bool
    accum_active_sec: float
    accum_muted_sec: float
    accum_deafened_sec: float
    accum_stream_sec: float

    def compute_live_totals(self, now: float | None = None) -> dict[str, float]:
        """Return real-time session durations including elapsed time since last state change."""
        now = now or time.time()
        delta = max(0.0, now - self.last_state_change_at)

        active = self.accum_active_sec
        muted = self.accum_muted_sec
        deafened = self.accum_deafened_sec
        stream = self.accum_stream_sec

        if self.is_deafened:
            deafened += delta
        elif self.is_muted:
            muted += delta
        else:
            active += delta

        if self.is_streaming:
            stream += delta

        total = max(0.0, now - self.joined_at)
        return {
            "total_sec": total,
            "active_sec": active,
            "muted_sec": muted,
            "deafened_sec": deafened,
            "stream_sec": stream,
        }


class AnalyticsDatabase:
    """Async SQLite storage engine for 24/7 Discord Voice Analytics."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._init_schema()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database connection is not initialized. Call connect() first.")
        return self._conn

    async def _init_schema(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                live_channel_id INTEGER,
                live_message_id INTEGER,
                default_timeframe TEXT NOT NULL DEFAULT '24h',
                peak_concurrent_users INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                joined_at REAL NOT NULL,
                last_state_change_at REAL NOT NULL,
                is_muted INTEGER NOT NULL DEFAULT 0,
                is_deafened INTEGER NOT NULL DEFAULT 0,
                is_streaming INTEGER NOT NULL DEFAULT 0,
                is_video INTEGER NOT NULL DEFAULT 0,
                accum_active_sec REAL NOT NULL DEFAULT 0,
                accum_muted_sec REAL NOT NULL DEFAULT 0,
                accum_deafened_sec REAL NOT NULL DEFAULT 0,
                accum_stream_sec REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS vc_time_slices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                duration_sec REAL NOT NULL,
                state_type TEXT NOT NULL,
                is_streaming INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_slices_guild_time
                ON vc_time_slices (guild_id, ended_at, started_at);
            CREATE INDEX IF NOT EXISTS idx_slices_guild_user
                ON vc_time_slices (guild_id, user_id, ended_at);

            CREATE TABLE IF NOT EXISTS vc_sessions_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                joined_at REAL NOT NULL,
                left_at REAL NOT NULL,
                total_sec REAL NOT NULL,
                active_sec REAL NOT NULL,
                muted_sec REAL NOT NULL,
                deafened_sec REAL NOT NULL,
                stream_sec REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_history_guild_left
                ON vc_sessions_history (guild_id, left_at);

            CREATE TABLE IF NOT EXISTS vc_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                active_count INTEGER NOT NULL,
                muted_count INTEGER NOT NULL,
                streaming_count INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_guild_ts
                ON vc_snapshots (guild_id, timestamp);
            """
        )
        await self.conn.commit()

    # ------------------------------------------------------------------
    # Guild Settings Management
    # ------------------------------------------------------------------
    async def get_guild_settings(self, guild_id: int) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        await self.conn.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)",
            (guild_id,),
        )
        await self.conn.commit()
        return {
            "guild_id": guild_id,
            "live_channel_id": None,
            "live_message_id": None,
            "default_timeframe": "24h",
            "peak_concurrent_users": 0,
        }

    async def set_live_dashboard(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int | None = None,
    ) -> None:
        await self.get_guild_settings(guild_id)
        await self.conn.execute(
            """
            UPDATE guild_settings
               SET live_channel_id = ?, live_message_id = ?
             WHERE guild_id = ?
            """,
            (channel_id, message_id, guild_id),
        )
        await self.conn.commit()

    async def set_live_message_id(self, guild_id: int, message_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE guild_settings SET live_message_id = ? WHERE guild_id = ?",
            (message_id, guild_id),
        )
        await self.conn.commit()

    async def set_default_timeframe(self, guild_id: int, timeframe: str) -> None:
        await self.get_guild_settings(guild_id)
        await self.conn.execute(
            "UPDATE guild_settings SET default_timeframe = ? WHERE guild_id = ?",
            (timeframe, guild_id),
        )
        await self.conn.commit()

    async def update_peak_concurrent(self, guild_id: int, current_count: int) -> int:
        settings = await self.get_guild_settings(guild_id)
        peak = max(int(settings["peak_concurrent_users"]), current_count)
        if peak > int(settings["peak_concurrent_users"]):
            await self.conn.execute(
                "UPDATE guild_settings SET peak_concurrent_users = ? WHERE guild_id = ?",
                (peak, guild_id),
            )
            await self.conn.commit()
        return peak

    # ------------------------------------------------------------------
    # Real-Time Voice Session Tracking
    # ------------------------------------------------------------------
    async def get_active_sessions(self, guild_id: int) -> list[LiveSessionInfo]:
        async with self.conn.execute(
            """
            SELECT * FROM active_sessions
             WHERE guild_id = ?
             ORDER BY joined_at ASC
            """,
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [
                LiveSessionInfo(
                    guild_id=r["guild_id"],
                    user_id=r["user_id"],
                    display_name=r["display_name"],
                    channel_id=r["channel_id"],
                    channel_name=r["channel_name"],
                    joined_at=r["joined_at"],
                    last_state_change_at=r["last_state_change_at"],
                    is_muted=bool(r["is_muted"]),
                    is_deafened=bool(r["is_deafened"]),
                    is_streaming=bool(r["is_streaming"]),
                    is_video=bool(r["is_video"]),
                    accum_active_sec=r["accum_active_sec"],
                    accum_muted_sec=r["accum_muted_sec"],
                    accum_deafened_sec=r["accum_deafened_sec"],
                    accum_stream_sec=r["accum_stream_sec"],
                )
                for r in rows
            ]

    async def start_or_update_session(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        channel_id: int,
        channel_name: str,
        flags: VoiceStateFlags,
        now: float | None = None,
    ) -> None:
        """
        Handle user joining VC, switching channels, or toggling mute/deafen/stream/video.
        Flushes the completed time slice up to `now` before updating state.
        """
        now = now or time.time()
        async with self.conn.execute(
            "SELECT * FROM active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            existing = await cur.fetchone()

        if existing is None:
            await self.conn.execute(
                """
                INSERT INTO active_sessions (
                    guild_id, user_id, display_name, channel_id, channel_name,
                    joined_at, last_state_change_at,
                    is_muted, is_deafened, is_streaming, is_video,
                    accum_active_sec, accum_muted_sec, accum_deafened_sec, accum_stream_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0)
                """,
                (
                    guild_id,
                    user_id,
                    display_name,
                    channel_id,
                    channel_name,
                    now,
                    now,
                    int(flags.is_muted),
                    int(flags.is_deafened),
                    int(flags.is_streaming),
                    int(flags.is_video),
                ),
            )
            await self.conn.commit()
            return

        # Flush elapsed slice under previous state/channel
        prev_start = float(existing["last_state_change_at"])
        delta = max(0.0, now - prev_start)
        prev_flags = VoiceStateFlags(
            is_muted=bool(existing["is_muted"]),
            is_deafened=bool(existing["is_deafened"]),
            is_streaming=bool(existing["is_streaming"]),
            is_video=bool(existing["is_video"]),
        )

        if delta >= 0.5:
            await self.conn.execute(
                """
                INSERT INTO vc_time_slices (
                    guild_id, user_id, display_name, channel_id, channel_name,
                    started_at, ended_at, duration_sec, state_type, is_streaming
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    display_name,
                    existing["channel_id"],
                    existing["channel_name"],
                    prev_start,
                    now,
                    delta,
                    prev_flags.primary_state,
                    int(prev_flags.is_streaming),
                ),
            )

        accum_active = float(existing["accum_active_sec"])
        accum_muted = float(existing["accum_muted_sec"])
        accum_deafened = float(existing["accum_deafened_sec"])
        accum_stream = float(existing["accum_stream_sec"])

        if prev_flags.is_deafened:
            accum_deafened += delta
        elif prev_flags.is_muted:
            accum_muted += delta
        else:
            accum_active += delta

        if prev_flags.is_streaming:
            accum_stream += delta

        # If the user switched voice channels, log completed channel visit & reset channel timer
        if int(existing["channel_id"]) != channel_id:
            total_visit = max(0.0, now - float(existing["joined_at"]))
            if total_visit >= 1.0:
                await self.conn.execute(
                    """
                    INSERT INTO vc_sessions_history (
                        guild_id, user_id, display_name, channel_id, channel_name,
                        joined_at, left_at, total_sec, active_sec, muted_sec, deafened_sec, stream_sec
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        user_id,
                        display_name,
                        existing["channel_id"],
                        existing["channel_name"],
                        existing["joined_at"],
                        now,
                        total_visit,
                        accum_active,
                        accum_muted,
                        accum_deafened,
                        accum_stream,
                    ),
                )
            # Reset per-visit accumulators for the new channel while keeping continuous tracking
            await self.conn.execute(
                """
                UPDATE active_sessions
                   SET display_name = ?,
                       channel_id = ?,
                       channel_name = ?,
                       joined_at = ?,
                       last_state_change_at = ?,
                       is_muted = ?,
                       is_deafened = ?,
                       is_streaming = ?,
                       is_video = ?,
                       accum_active_sec = 0,
                       accum_muted_sec = 0,
                       accum_deafened_sec = 0,
                       accum_stream_sec = 0
                 WHERE guild_id = ? AND user_id = ?
                """,
                (
                    display_name,
                    channel_id,
                    channel_name,
                    now,
                    now,
                    int(flags.is_muted),
                    int(flags.is_deafened),
                    int(flags.is_streaming),
                    int(flags.is_video),
                    guild_id,
                    user_id,
                ),
            )
        else:
            await self.conn.execute(
                """
                UPDATE active_sessions
                   SET display_name = ?,
                       channel_name = ?,
                       last_state_change_at = ?,
                       is_muted = ?,
                       is_deafened = ?,
                       is_streaming = ?,
                       is_video = ?,
                       accum_active_sec = ?,
                       accum_muted_sec = ?,
                       accum_deafened_sec = ?,
                       accum_stream_sec = ?
                 WHERE guild_id = ? AND user_id = ?
                """,
                (
                    display_name,
                    channel_name,
                    now,
                    int(flags.is_muted),
                    int(flags.is_deafened),
                    int(flags.is_streaming),
                    int(flags.is_video),
                    accum_active,
                    accum_muted,
                    accum_deafened,
                    accum_stream,
                    guild_id,
                    user_id,
                ),
            )

        await self.conn.commit()

    async def end_session(
        self,
        guild_id: int,
        user_id: int,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Finalize an active voice session when a user disconnects from VC."""
        now = now or time.time()
        async with self.conn.execute(
            "SELECT * FROM active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ) as cur:
            existing = await cur.fetchone()

        if existing is None:
            return None

        prev_start = float(existing["last_state_change_at"])
        delta = max(0.0, now - prev_start)
        prev_flags = VoiceStateFlags(
            is_muted=bool(existing["is_muted"]),
            is_deafened=bool(existing["is_deafened"]),
            is_streaming=bool(existing["is_streaming"]),
            is_video=bool(existing["is_video"]),
        )

        if delta >= 0.5:
            await self.conn.execute(
                """
                INSERT INTO vc_time_slices (
                    guild_id, user_id, display_name, channel_id, channel_name,
                    started_at, ended_at, duration_sec, state_type, is_streaming
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    existing["display_name"],
                    existing["channel_id"],
                    existing["channel_name"],
                    prev_start,
                    now,
                    delta,
                    prev_flags.primary_state,
                    int(prev_flags.is_streaming),
                ),
            )

        accum_active = float(existing["accum_active_sec"])
        accum_muted = float(existing["accum_muted_sec"])
        accum_deafened = float(existing["accum_deafened_sec"])
        accum_stream = float(existing["accum_stream_sec"])

        if prev_flags.is_deafened:
            accum_deafened += delta
        elif prev_flags.is_muted:
            accum_muted += delta
        else:
            accum_active += delta

        if prev_flags.is_streaming:
            accum_stream += delta

        total_sec = max(0.0, now - float(existing["joined_at"]))
        if total_sec >= 1.0:
            await self.conn.execute(
                """
                INSERT INTO vc_sessions_history (
                    guild_id, user_id, display_name, channel_id, channel_name,
                    joined_at, left_at, total_sec, active_sec, muted_sec, deafened_sec, stream_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    existing["display_name"],
                    existing["channel_id"],
                    existing["channel_name"],
                    existing["joined_at"],
                    now,
                    total_sec,
                    accum_active,
                    accum_muted,
                    accum_deafened,
                    accum_stream,
                ),
            )

        await self.conn.execute(
            "DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

        return {
            "user_id": user_id,
            "display_name": existing["display_name"],
            "channel_id": existing["channel_id"],
            "channel_name": existing["channel_name"],
            "joined_at": existing["joined_at"],
            "left_at": now,
            "total_sec": total_sec,
            "active_sec": accum_active,
            "muted_sec": accum_muted,
            "deafened_sec": accum_deafened,
            "stream_sec": accum_stream,
        }

    async def record_snapshot(self, guild_id: int, now: float | None = None) -> None:
        """Record a 1-minute live population snapshot for the Statbot activity chart."""
        now = now or time.time()
        sessions = await self.get_active_sessions(guild_id)
        active_count = sum(1 for s in sessions if not s.is_muted and not s.is_deafened)
        muted_count = sum(1 for s in sessions if s.is_muted or s.is_deafened)
        streaming_count = sum(1 for s in sessions if s.is_streaming)

        await self.conn.execute(
            """
            INSERT INTO vc_snapshots (guild_id, timestamp, active_count, muted_count, streaming_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, now, active_count, muted_count, streaming_count),
        )
        await self.conn.commit()
        await self.update_peak_concurrent(guild_id, len(sessions))

    # ------------------------------------------------------------------
    # Analytics & Leaderboard Queries (Merging Historical + Live Sessions)
    # ------------------------------------------------------------------
    async def get_leaderboard(
        self,
        guild_id: int,
        hours: int | None = 24,
        limit: int = 10,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Compute VC leaderboard for a timeframe (e.g. 24h, 7d, 30d, All-Time),
        seamlessly combining completed slices and currently active live VC sessions.
        """
        now = now or time.time()
        cutoff = (now - hours * 3600.0) if hours is not None else 0.0

        user_map: dict[int, dict[str, Any]] = {}

        def _ensure_user(uid: int, name: str) -> dict[str, Any]:
            if uid not in user_map:
                user_map[uid] = {
                    "user_id": uid,
                    "display_name": name,
                    "total_sec": 0.0,
                    "active_sec": 0.0,
                    "muted_sec": 0.0,
                    "deafened_sec": 0.0,
                    "stream_sec": 0.0,
                    "is_live": False,
                }
            else:
                user_map[uid]["display_name"] = name
            return user_map[uid]

        # 1. Historical slices overlapping [cutoff, now]
        async with self.conn.execute(
            """
            SELECT user_id, display_name, started_at, ended_at, state_type, is_streaming
              FROM vc_time_slices
             WHERE guild_id = ? AND ended_at > ?
            """,
            (guild_id, cutoff),
        ) as cur:
            rows = await cur.fetchall()
            for r in rows:
                s_start = max(float(r["started_at"]), cutoff)
                s_end = min(float(r["ended_at"]), now)
                dur = max(0.0, s_end - s_start)
                if dur <= 0:
                    continue
                entry = _ensure_user(int(r["user_id"]), r["display_name"])
                entry["total_sec"] += dur
                st = r["state_type"]
                if st == "deafened":
                    entry["deafened_sec"] += dur
                elif st == "muted":
                    entry["muted_sec"] += dur
                else:
                    entry["active_sec"] += dur
                if r["is_streaming"]:
                    entry["stream_sec"] += dur

        # 2. Currently active live sessions (unflushed portion since last_state_change_at)
        active_sessions = await self.get_active_sessions(guild_id)
        for sess in active_sessions:
            entry = _ensure_user(sess.user_id, sess.display_name)
            entry["is_live"] = True
            s_start = max(sess.last_state_change_at, cutoff)
            dur = max(0.0, now - s_start)
            if dur <= 0:
                continue
            entry["total_sec"] += dur
            if sess.is_deafened:
                entry["deafened_sec"] += dur
            elif sess.is_muted:
                entry["muted_sec"] += dur
            else:
                entry["active_sec"] += dur
            if sess.is_streaming:
                entry["stream_sec"] += dur

        ranked = sorted(
            user_map.values(),
            key=lambda x: x["total_sec"],
            reverse=True,
        )
        return ranked[:limit]

    async def get_channel_breakdown(
        self,
        guild_id: int,
        hours: int | None = 24,
        limit: int = 6,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Compute voice channel usage breakdown including live sessions."""
        now = now or time.time()
        cutoff = (now - hours * 3600.0) if hours is not None else 0.0
        chan_map: dict[int, dict[str, Any]] = {}

        async with self.conn.execute(
            """
            SELECT channel_id, channel_name, started_at, ended_at
              FROM vc_time_slices
             WHERE guild_id = ? AND ended_at > ?
            """,
            (guild_id, cutoff),
        ) as cur:
            rows = await cur.fetchall()
            for r in rows:
                s_start = max(float(r["started_at"]), cutoff)
                s_end = min(float(r["ended_at"]), now)
                dur = max(0.0, s_end - s_start)
                if dur <= 0:
                    continue
                cid = int(r["channel_id"])
                if cid not in chan_map:
                    chan_map[cid] = {
                        "channel_id": cid,
                        "channel_name": r["channel_name"],
                        "total_sec": 0.0,
                    }
                chan_map[cid]["channel_name"] = r["channel_name"]
                chan_map[cid]["total_sec"] += dur

        active_sessions = await self.get_active_sessions(guild_id)
        for sess in active_sessions:
            s_start = max(sess.last_state_change_at, cutoff)
            dur = max(0.0, now - s_start)
            cid = sess.channel_id
            if cid not in chan_map:
                chan_map[cid] = {
                    "channel_id": cid,
                    "channel_name": sess.channel_name,
                    "total_sec": 0.0,
                }
            chan_map[cid]["channel_name"] = sess.channel_name
            chan_map[cid]["total_sec"] += dur

        return sorted(chan_map.values(), key=lambda x: x["total_sec"], reverse=True)[:limit]

    async def get_hourly_activity_series(
        self,
        guild_id: int,
        hours: int = 24,
        buckets: int = 24,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return time-bucketed voice activity (in person-minutes per bucket)
        for Statbot-style area charts over the specified timeframe.
        """
        now = now or time.time()
        total_window = hours * 3600.0
        start_time = now - total_window
        bucket_width = total_window / buckets

        series = [
            {
                "bucket_start": start_time + i * bucket_width,
                "bucket_end": start_time + (i + 1) * bucket_width,
                "active_min": 0.0,
                "muted_min": 0.0,
                "stream_min": 0.0,
            }
            for i in range(buckets)
        ]

        def _accumulate_interval(
            iv_start: float,
            iv_end: float,
            state_type: str,
            is_streaming: bool,
        ) -> None:
            s = max(iv_start, start_time)
            e = min(iv_end, now)
            if e <= s:
                return
            first_idx = max(0, int((s - start_time) // bucket_width))
            last_idx = min(buckets - 1, int((e - start_time) // bucket_width))
            for idx in range(first_idx, last_idx + 1):
                b = series[idx]
                ov_start = max(s, b["bucket_start"])
                ov_end = min(e, b["bucket_end"])
                if ov_end > ov_start:
                    mins = (ov_end - ov_start) / 60.0
                    if state_type in ("muted", "deafened"):
                        b["muted_min"] += mins
                    else:
                        b["active_min"] += mins
                    if is_streaming:
                        b["stream_min"] += mins

        async with self.conn.execute(
            """
            SELECT started_at, ended_at, state_type, is_streaming
              FROM vc_time_slices
             WHERE guild_id = ? AND ended_at > ?
            """,
            (guild_id, start_time),
        ) as cur:
            rows = await cur.fetchall()
            for r in rows:
                _accumulate_interval(
                    float(r["started_at"]),
                    float(r["ended_at"]),
                    r["state_type"],
                    bool(r["is_streaming"]),
                )

        active_sessions = await self.get_active_sessions(guild_id)
        for sess in active_sessions:
            st = "deafened" if sess.is_deafened else ("muted" if sess.is_muted else "active")
            _accumulate_interval(sess.last_state_change_at, now, st, sess.is_streaming)

        return series

    async def get_user_stats(
        self,
        guild_id: int,
        user_id: int,
        hours: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Get comprehensive VC analytics for a single user."""
        now = now or time.time()
        board = await self.get_leaderboard(guild_id, hours=hours, limit=10000, now=now)
        rank = None
        user_entry = {
            "user_id": user_id,
            "display_name": f"User {user_id}",
            "total_sec": 0.0,
            "active_sec": 0.0,
            "muted_sec": 0.0,
            "deafened_sec": 0.0,
            "stream_sec": 0.0,
            "is_live": False,
        }
        for idx, item in enumerate(board, start=1):
            if item["user_id"] == user_id:
                rank = idx
                user_entry = item
                break

        # Top channels for this user
        cutoff = (now - hours * 3600.0) if hours is not None else 0.0
        chan_totals: dict[str, float] = {}
        async with self.conn.execute(
            """
            SELECT channel_name, started_at, ended_at
              FROM vc_time_slices
             WHERE guild_id = ? AND user_id = ? AND ended_at > ?
            """,
            (guild_id, user_id, cutoff),
        ) as cur:
            for r in await cur.fetchall():
                dur = max(0.0, min(float(r["ended_at"]), now) - max(float(r["started_at"]), cutoff))
                if dur > 0:
                    chan_totals[r["channel_name"]] = chan_totals.get(r["channel_name"], 0.0) + dur

        active_sessions = await self.get_active_sessions(guild_id)
        live_session: LiveSessionInfo | None = None
        for sess in active_sessions:
            if sess.user_id == user_id:
                live_session = sess
                dur = max(0.0, now - max(sess.last_state_change_at, cutoff))
                chan_totals[sess.channel_name] = chan_totals.get(sess.channel_name, 0.0) + dur
                break

        top_channels = sorted(chan_totals.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            **user_entry,
            "rank": rank,
            "top_channels": top_channels,
            "live_session": live_session,
        }
