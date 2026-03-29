# tapo-catfood-monitor

A Discord bot that monitors your cat's food bowl using a Tapo camera and Google Gemini AI. It takes snapshots through Home Assistant, sends them to Gemini for analysis, and alerts you on Discord when the bowl is running low.

Runs entirely on **Gemini's free tier** -- at the default 10-minute polling interval, it uses ~144 requests/day out of the 1,000/day free limit.

<img width="509" height="399" alt="Image" src="https://github.com/user-attachments/assets/34a58520-fdfc-4b28-8a89-e246e05ddaea" />

## How it works

1. The bot grabs a camera snapshot via the Home Assistant API
2. The image is sent to Google Gemini for vision analysis
3. Gemini returns whether food is present and an estimated fill level (0-100%)
4. If the food drops below the configured threshold, the bot sends a Discord alert with the image
5. When the bowl is refilled, it sends a recovery notification

If Home Assistant, the camera, or Gemini become unreachable, the bot alerts you after 3 consecutive failures and notifies you again when the connection recovers.

## Discord commands

| Command | Description |
|---------|-------------|
| `!start` | Start continuous monitoring (alerts only when food is low) |
| `!stop` | Stop monitoring |
| `!check` | One-time food level check with image |
| `!status` | Show bot status and connection health |

## Prerequisites

- [Home Assistant](https://www.home-assistant.io/) with a Tapo camera integrated
- A [Discord bot](https://discord.com/developers/applications) with **Message Content Intent** enabled
- A [Google Gemini API key](https://aistudio.google.com/apikey) (free tier)
- Docker and Docker Compose

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/YOUR_USERNAME/tapo-catfood-monitor.git
   cd tapo-catfood-monitor
   ```

2. Copy the example env and fill in your values:
   ```bash
   cp .env.example .env
   nano .env
   ```

3. Start the bot:
   ```bash
   docker compose up -d
   ```

4. Check the logs:
   ```bash
   docker logs -f cat-food-monitor
   ```

## Configuration

All configuration is done through environment variables in `.env`. See `.env.example` for the template.

### Required

| Variable | Description |
|----------|-------------|
| `HA_TOKEN` | Home Assistant long-lived access token |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `DISCORD_CHANNEL_ID` | Discord channel ID for alerts |
| `GEMINI_API_KEY` | Google Gemini API key |
| `CAMERA_NAME` | Your Tapo camera name in Home Assistant (used to derive `camera.{name}_live_view` and `switch.{name}`) |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `HA_URL` | `http://homeassistant:8123` | Home Assistant URL |
| `POLL_INTERVAL_SEC` | `600` | How often to check the bowl (seconds) |
| `LOW_FOOD_THRESHOLD` | `15` | Food level (%) that triggers an alert |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Gemini model to use |
| `CAMERA_PRIVACY_MODE` | `false` | Turn camera on/off for each snapshot |
| `CAMERA_WAKE_SEC` | `15` | Seconds to wait after turning camera on |

### Privacy mode

When `CAMERA_PRIVACY_MODE=true`, the bot will:
- Turn the camera **on** via its Home Assistant switch entity before each snapshot
- Wait `CAMERA_WAKE_SEC` seconds for the camera to initialize
- Take the snapshot
- Turn the camera **off** immediately after

This keeps the camera off between checks. The bot includes retry logic in case the camera needs extra time to start streaming.

### Staying within Gemini free tier

The free tier for `gemini-2.5-flash-lite` allows **1,000 requests/day**. Some reference polling intervals:

| Interval | Requests/day | % of free limit |
|----------|-------------|-----------------|
| 5 min | 288 | 29% |
| 10 min (default) | 144 | 14% |
| 15 min | 96 | 10% |
| 30 min | 48 | 5% |

## Discord bot setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Go to **Bot** > create a bot > copy the token
4. Under **Bot** > **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2** > **URL Generator**, select the `bot` scope with permissions: Send Messages, Read Message History, Embed Links, Attach Files
6. Open the generated URL to invite the bot to your server

## Timezone

The Docker container defaults to UTC. Set your timezone in `.env`:

```
TZ=America/New_York
```
