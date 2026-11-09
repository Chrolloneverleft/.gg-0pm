# 📡 Discord 24/7 Live Voice Analytics Bot (Statbot-Style)

A real-time, 24/7 Discord Voice Channel analytics bot built with **Python (`discord.py` + `aiosqlite` + `matplotlib`)**.

Unlike standard analytics bots that only update after a user leaves a voice channel, this bot maintains a **live auto-updating dashboard message + Statbot-style dark-mode charts** in a dedicated channel—tracking active sessions, live elapsed timers, mic/mute/deafen/streaming breakdowns, and leaderboards in real time.

---

## ✨ Key Features

- **🎙️ Currently in VC (Live Session Tracker)**:
  - Displays everyone currently in voice channels, grouped by channel.
  - Shows a **live ticking timer** (`02h 14m 38s`) + native Discord relative timestamp (`joined 2 hours ago`).
  - Tracks real-time status badges: `🎙️ Mic ON`, `🔇 Muted`, `🎧 Deaf`, `🖥️ LIVE` (screen share), `📹 Cam`.
  - Breaks down each live session into **Active Mic Time**, **Muted/Deafened Time**, and **Screen Share Time**.
- **📊 Generated Statbot-Style Dark-Mode Charts**:
  - **Voice Activity Timeline**: Area chart comparing active mic minutes vs. muted/deafened minutes.
  - **Top Voice Members**: Stacked horizontal bar chart showing active vs. muted hours (`●` indicator for live users).
  - **Most Active Voice Channels**: Bar chart of top voice channels.
- **🏆 Live-Merged Leaderboards (`24h`, `7d`, `30d`, `All-Time`)**:
  - Ongoing voice sessions are merged with historical database slices in real time—users climb the leaderboard *while* they are still talking in VC.
- **🔄 Crash/Restart Resilient**:
  - On startup (`on_ready`), the bot scans all voice channels in your server and reconciles active sessions automatically.
- **🎛️ Interactive Dashboard Buttons**:
  - Switch between `24h`, `7d`, `30d`, and `All-Time` views, click **Refresh Live**, or click **My VC Stats** for a personal ephemeral profile card.

---

## 🚀 Quick Start

### 1. Install Dependencies
```powershell
cd C:\Users\Ram\.gemini\antigravity\scratch\discord-vc-analytics
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Discord Developer Portal Setup
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Open the **Bot** tab:
   - Enable **Server Members Intent** under *Privileged Gateway Intents*.
   - Click **Reset Token** and copy your Bot Token.
3. Open **OAuth2 -> URL Generator**:
   - Check scopes: `bot`, `applications.commands`
   - Check bot permissions:
     - `Manage Channels` (so `/vc setup` can create the read-only `#📊-live-vc-stats` channel)
     - `View Channels`
     - `Send Messages`
     - `Embed Links`
     - `Attach Files`
     - `Manage Messages` (to pin the live dashboard message)
   - Open the generated URL to invite the bot to your server.

### 3. Configure `.env`
Copy `.env.example` to `.env` and add your token:
```powershell
Copy-Item .env.example .env
```
Edit `.env`:
```env
DISCORD_TOKEN=your_discord_bot_token_here
UPDATE_INTERVAL_SECONDS=15
CHART_REFRESH_SECONDS=60
```

### 4. Run the Bot Locally
```powershell
python bot.py
```

---

## 🚆 Deploying on Railway (24/7 Hosting)

This repository includes a production [Dockerfile](Dockerfile), [railway.json](railway.json), and automatic `RAILWAY_VOLUME_MOUNT_PATH` detection in [config.py](config.py).

### Step 1: Deploy Project to Railway
1. Push this folder to a GitHub repository (or run `railway init` & `railway up` via the Railway CLI).
2. In your [Railway Dashboard](https://railway.app/), click **New Project → Deploy from GitHub repo** and select your repository.

### Step 2: Add Environment Variables
Open your service in Railway → **Variables** tab and add:
- `DISCORD_TOKEN` = `your_discord_bot_token_here`
- *(Optional)* `UPDATE_INTERVAL_SECONDS` = `15`
- *(Optional)* `CHART_REFRESH_SECONDS` = `60`

### Step 3: Attach a Persistent Volume (Important for SQLite!)
So your 7d / 30d / All-Time VC history is preserved across redeployments:
1. Right-click the canvas in Railway (or press `Ctrl+K` / `Cmd+K`) → **Add Volume** and attach it to your bot service.
2. Set the **Mount Path** to:
   ```text
   /app/data
   ```
   *(Railway automatically injects `RAILWAY_VOLUME_MOUNT_PATH=/app/data`, which [config.py](config.py) detects automatically to store `vc_analytics.db` on the persistent disk.)*

---

## 🛠️ Slash Commands

| Command | Description |
| :--- | :--- |
| `/vc setup [channel]` | Creates a dedicated read-only `#📊-live-vc-stats` channel (or uses `channel` if provided) and posts the 24/7 live dashboard. |
| `/vc stats [member]` | Displays a Statbot-style personal voice profile card (`24h`, `7d`, `30d`, `All-Time` + favorite channels). |
| `/vc leaderboard [timeframe]` | Generates an on-demand leaderboard embed + Statbot chart for `24h`, `7d`, `30d`, or `all`. |
| `/vc refresh` | Forces an immediate re-render of the live dashboard message and chart. |
| `/vc export` | Exports up to 5,000 completed voice sessions as a `.csv` file for server admins. |
