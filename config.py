from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Bot configuration
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
DEFAULT_LIVE_CHANNEL_ID: int | None = (
    int(os.getenv("LIVE_CHANNEL_ID", "").strip())
    if os.getenv("LIVE_CHANNEL_ID", "").strip().isdigit()
    else None
)

# Refresh intervals (clamped to safe minimums for Discord API rate limits)
UPDATE_INTERVAL_SECONDS: int = max(10, int(os.getenv("UPDATE_INTERVAL_SECONDS", "15")))
CHART_REFRESH_SECONDS: int = max(30, int(os.getenv("CHART_REFRESH_SECONDS", "60")))

# Database path — automatically detects Railway Volume mount path if attached
_explicit_db_path = os.getenv("DATABASE_PATH", "").strip()
_railway_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()

if _explicit_db_path:
    _p = Path(_explicit_db_path)
    DATABASE_PATH: Path = _p if _p.is_absolute() else (BASE_DIR / _p)
elif _railway_volume:
    DATABASE_PATH = Path(_railway_volume) / "vc_analytics.db"
else:
    DATABASE_PATH = BASE_DIR / "data" / "vc_analytics.db"

DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Statbot & Discord Dark Theme Palette
COLOR_LIVE_GREEN = 0x23A55A
COLOR_BLURPLE = 0x5865F2
COLOR_IDLE_GRAY = 0x2B2D31
COLOR_WARNING = 0xF0B232
COLOR_STREAM_PURPLE = 0x9B59B6

# Timeframe definitions in hours (None = All-Time)
TIMEFRAMES: dict[str, tuple[str, int | None]] = {
    "24h": ("Last 24 Hours", 24),
    "7d": ("Last 7 Days", 24 * 7),
    "30d": ("Last 30 Days", 24 * 30),
    "all": ("All-Time", None),
}


def format_duration(seconds: float | int, compact: bool = False) -> str:
    """Format duration in seconds into a clean human-readable timer (e.g., 02h 14m 38s)."""
    total_sec = max(0, int(round(seconds)))
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if compact:
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        if minutes > 0:
            return f"{minutes}m {secs}s"
        return f"{secs}s"

    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m {secs:02d}s"
    if hours > 0:
        return f"{hours:02d}h {minutes:02d}m {secs:02d}s"
    return f"{minutes:02d}m {secs:02d}s"
