import os
import io
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from google import genai
from google.genai import types
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123")
HA_TOKEN = os.environ["HA_TOKEN"]
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CAMERA_NAME = os.environ.get("CAMERA_NAME", "your_camera")
CAMERA_ENTITY = f"camera.{CAMERA_NAME}_live_view"
CAMERA_SWITCH_ENTITY = f"switch.{CAMERA_NAME}"
camera_config_entry_id = ""  # Auto-detected at startup
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "600"))
LOW_FOOD_THRESHOLD = int(os.environ.get("LOW_FOOD_THRESHOLD", "15"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
CAMERA_PRIVACY_MODE = os.environ.get("CAMERA_PRIVACY_MODE", "false").lower() == "true"
CAMERA_WAKE_SEC = int(os.environ.get("CAMERA_WAKE_SEC", "5"))
BASELINE_THRESHOLD = int(os.environ.get("BASELINE_THRESHOLD", "25"))
BASELINE_PATH = Path(os.environ.get("BASELINE_PATH", "/app/data/baseline.jpg"))
QUIET_START_HOUR = os.environ.get("QUIET_START_HOUR")  # e.g. "0" for midnight
QUIET_END_HOUR = os.environ.get("QUIET_END_HOUR")      # e.g. "9" for 9 AM
if QUIET_START_HOUR is not None and QUIET_END_HOUR is not None:
    QUIET_START_HOUR = int(QUIET_START_HOUR)
    QUIET_END_HOUR = int(QUIET_END_HOUR)
else:
    QUIET_START_HOUR = None
    QUIET_END_HOUR = None

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("catfood")

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

ANALYSIS_PROMPT = (
    "Analyze this image of a cat food bowl. Determine:\n"
    "1. Whether there is food visible in the bowl (true/false)\n"
    "2. The estimated food level as a percentage (0-100)\n\n"
    "Respond with ONLY a JSON object in this exact format, no other text:\n"
    '{"food": true, "level": 75}\n\n'
    '- "food": true if any food is visible, false if the bowl appears empty\n'
    '- "level": estimated fullness 0 (empty) to 100 (full)'
)

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Monitoring state
monitoring = False
monitor_task = None
last_analysis = None
last_check_time = None
last_image_bytes = None
ha_down = False
gemini_down = False
camera_down = False
alerted_empty = False
consecutive_errors = 0
error_alerted = False
baseline_alerted = False


def is_quiet_hours():
    """Return True if current time falls within the configured quiet window."""
    if QUIET_START_HOUR is None or QUIET_END_HOUR is None:
        return False
    hour = datetime.now().hour
    if QUIET_START_HOUR <= QUIET_END_HOUR:
        return QUIET_START_HOUR <= hour < QUIET_END_HOUR
    # Wraps midnight, e.g. 22 -> 6
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def snapshot_filename():
    return f"catfood_{int(datetime.now().timestamp())}.jpg"


# ---------------------------------------------------------------------------
# Baseline image comparison
# ---------------------------------------------------------------------------
def save_baseline(image_bytes):
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_bytes(image_bytes)
    log.info("Baseline saved to %s", BASELINE_PATH)


def load_baseline():
    if BASELINE_PATH.exists():
        return BASELINE_PATH.read_bytes()
    return None


def compare_to_baseline(image_bytes):
    """Compare image to baseline. Returns difference percentage (0-100)."""
    baseline_bytes = load_baseline()
    if baseline_bytes is None:
        return None

    baseline_img = Image.open(io.BytesIO(baseline_bytes)).convert("L").resize((64, 64))
    current_img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((64, 64))

    baseline_px = list(baseline_img.getdata())
    current_px = list(current_img.getdata())

    diff = sum(abs(b - c) for b, c in zip(baseline_px, current_px))
    max_diff = 255 * len(baseline_px)
    pct = (diff / max_diff) * 100

    log.info("Baseline drift: %.1f%% (threshold: %d%%)", pct, BASELINE_THRESHOLD)
    return pct




# ---------------------------------------------------------------------------
# Camera power control via Home Assistant
# ---------------------------------------------------------------------------
async def set_camera_power(session, on: bool):
    service = "turn_on" if on else "turn_off"
    url = f"{HA_URL}/api/services/switch/{service}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    payload = {"entity_id": CAMERA_SWITCH_ENTITY}

    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status == 200:
            log.info("Camera %s", "on" if on else "off")
        else:
            body = await resp.text()
            log.error("Camera %s failed: HTTP %d - %s", service, resp.status, body[:200])


# ---------------------------------------------------------------------------
# Camera snapshot via Home Assistant
# ---------------------------------------------------------------------------
async def detect_config_entry_id():
    """Auto-detect the TP-Link config entry ID for the camera."""
    global camera_config_entry_id
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{HA_URL}/api/config/config_entries/entry", headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    entries = await resp.json()
                    for entry in entries:
                        if entry.get("domain") == "tplink" and CAMERA_NAME.lower() in entry.get("title", "").lower():
                            camera_config_entry_id = entry["entry_id"]
                            log.info("Auto-detected config entry: %s (%s)", entry["title"], camera_config_entry_id)
                            return
                    log.warning("Could not find tplink config entry matching '%s'", CAMERA_NAME)
    except Exception as e:
        log.error("Failed to detect config entry ID: %s", e)


async def reload_camera_integration(session, headers):
    """Reload the TP-Link config entry to force HA to fetch a fresh frame."""
    if not camera_config_entry_id:
        return
    url = f"{HA_URL}/api/services/homeassistant/reload_config_entry"
    payload = {"entry_id": camera_config_entry_id}
    async with session.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status == 200:
            log.info("Reloaded camera integration for fresh frame")
        else:
            log.warning("Failed to reload camera integration: HTTP %d", resp.status)


async def get_camera_snapshot():
    global ha_down, camera_down
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        was_already_on = False
        if CAMERA_PRIVACY_MODE:
            state_url = f"{HA_URL}/api/states/{CAMERA_SWITCH_ENTITY}"
            async with session.get(state_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    was_already_on = data.get("state") == "on"
            if not was_already_on:
                await set_camera_power(session, on=True)
                await asyncio.sleep(CAMERA_WAKE_SEC)
            else:
                log.info("Camera already on, skipping power toggle")

        # Reload integration to get a fresh frame from the camera
        await reload_camera_integration(session, headers)
        await asyncio.sleep(CAMERA_WAKE_SEC)

        url = f"{HA_URL}/api/camera_proxy/{CAMERA_ENTITY}?t={int(datetime.now().timestamp())}"
        max_retries = 6 if CAMERA_PRIVACY_MODE else 1
        try:
            for attempt in range(max_retries):
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        if camera_down:
                            camera_down = False
                            log.info("Camera connection restored")
                        if ha_down:
                            ha_down = False
                            log.info("Home Assistant connection restored")
                        return await resp.read()

                    if resp.status == 503 and attempt < max_retries - 1:
                        log.info("Camera not ready, retrying in 5s (%d/%d)", attempt + 1, max_retries)
                        await asyncio.sleep(5)
                        continue

                    body = await resp.text()
                    log.error("Camera snapshot failed: HTTP %d - %s", resp.status, body[:200])
                    if resp.status in (401, 403):
                        ha_down = True
                    else:
                        camera_down = True
                    return None
        finally:
            if CAMERA_PRIVACY_MODE and not was_already_on:
                await set_camera_power(session, on=False)


# ---------------------------------------------------------------------------
# Gemini image analysis
# ---------------------------------------------------------------------------
async def analyze_with_gemini(image_bytes):
    global gemini_down
    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                ANALYSIS_PROMPT,
            ],
        )

        text = response.text.strip()
        # Strip markdown code fences if the model wraps them
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0].strip()

        result = json.loads(text)

        if gemini_down:
            gemini_down = False
            log.info("Gemini API connection restored")

        log.info("Gemini analysis: food=%s, level=%s%%", result.get("food"), result.get("level"))
        return result

    except json.JSONDecodeError:
        log.error("Gemini returned invalid JSON: %s", response.text[:300])
        return None
    except Exception as e:
        log.error("Gemini API error: %s", e)
        gemini_down = True
        return None


# ---------------------------------------------------------------------------
# Full analysis pipeline
# ---------------------------------------------------------------------------
async def analyze_food():
    global last_analysis, last_check_time, last_image_bytes, consecutive_errors, error_alerted
    try:
        image_bytes = await get_camera_snapshot()
        if image_bytes is None:
            return None

        analysis = await analyze_with_gemini(image_bytes)
        if analysis is None:
            return None

        last_analysis = analysis
        last_check_time = datetime.now()
        last_image_bytes = image_bytes
        consecutive_errors = 0
        error_alerted = False

        drift = compare_to_baseline(image_bytes)
        if drift is not None:
            analysis["baseline_drift"] = round(drift, 1)

        return image_bytes, analysis

    except aiohttp.ClientError as e:
        log.error("Network error during analysis: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected error during analysis: %s", e)
        return None


# ---------------------------------------------------------------------------
# Embed builder
# ---------------------------------------------------------------------------
def build_analysis_embed(analysis, manual=False):
    food = analysis.get("food", False)
    level = analysis.get("level", 0)

    if not food or level == 0:
        color = discord.Color.red()
        title = "Cat Food Check: Empty!" if manual else "Bowl is Empty!"
    elif level <= LOW_FOOD_THRESHOLD:
        color = discord.Color.orange()
        title = "Cat Food Check: Low" if manual else "Food Running Low"
    elif level <= 50:
        color = discord.Color.yellow()
        title = "Cat Food Check"
    else:
        color = discord.Color.green()
        title = "Cat Food Check: Looking Good!"

    embed = discord.Embed(title=title, color=color)

    filled = round(level / 10)
    bar = "\u2588" * filled + "\u2591" * (10 - filled)

    embed.add_field(name="Food Present", value="Yes" if food else "No", inline=True)
    embed.add_field(name="Level", value=f"{level}%", inline=True)
    embed.add_field(name="", value=f"`[{bar}]`", inline=False)
    embed.set_footer(text=f"Checked at {datetime.now().strftime('%I:%M %p')}")
    return embed


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------
async def monitor_loop():
    global monitoring, consecutive_errors, error_alerted, alerted_empty, baseline_alerted

    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID) or await bot.fetch_channel(DISCORD_CHANNEL_ID)
    log.info("Monitor loop started (interval=%ds)", POLL_INTERVAL_SEC)

    while monitoring and not bot.is_closed():
        if is_quiet_hours():
            log.debug("Quiet hours active (%02d:00–%02d:00), skipping check", QUIET_START_HOUR, QUIET_END_HOUR)
            await asyncio.sleep(POLL_INTERVAL_SEC)
            continue

        try:
            result = await analyze_food()

            if result is None:
                consecutive_errors += 1
                log.warning("Analysis failed (consecutive errors: %d)", consecutive_errors)

                if consecutive_errors >= 3 and not error_alerted:
                    error_alerted = True
                    issues = []
                    if ha_down:
                        issues.append("Home Assistant unreachable")
                    if camera_down:
                        issues.append("Camera unavailable")
                    if gemini_down:
                        issues.append("Gemini API error")
                    if not issues:
                        issues.append("Unknown error")

                    embed = discord.Embed(
                        title="Cat Food Monitor Issue",
                        description=(
                            f"**{consecutive_errors} consecutive check failures.**\n"
                            f"Issues: {', '.join(issues)}\n\n"
                            "Monitoring continues -- I'll update you when it recovers."
                        ),
                        color=discord.Color.orange(),
                    )
                    await channel.send(embed=embed)
            else:
                if error_alerted:
                    embed = discord.Embed(
                        title="Cat Food Monitor Recovered",
                        description="Connection restored. Monitoring resumed normally.",
                        color=discord.Color.green(),
                    )
                    await channel.send(embed=embed)

                image_bytes, analysis = result
                food = analysis.get("food", False)
                level = analysis.get("level", 0)

                if (not food or level <= LOW_FOOD_THRESHOLD) and not alerted_empty:
                    alerted_empty = True
                    embed = build_analysis_embed(analysis)
                    fname = snapshot_filename()
                    file = discord.File(io.BytesIO(image_bytes), filename=fname)
                    embed.set_image(url=f"attachment://{fname}")
                    await channel.send(embed=embed, file=file)
                    log.warning("Low food alert sent: food=%s, level=%s%%", food, level)

                elif food and level > LOW_FOOD_THRESHOLD and alerted_empty:
                    alerted_empty = False
                    embed = discord.Embed(
                        title="Food Bowl Refilled!",
                        description=f"Food level is back up to **{level}%**.",
                        color=discord.Color.green(),
                    )
                    fname = snapshot_filename()
                    file = discord.File(io.BytesIO(image_bytes), filename=fname)
                    embed.set_image(url=f"attachment://{fname}")
                    await channel.send(embed=embed, file=file)
                    log.info("Food refilled notification: level=%s%%", level)

                # Baseline drift check
                drift = analysis.get("baseline_drift")
                if drift is not None and drift >= BASELINE_THRESHOLD and not baseline_alerted:
                    baseline_alerted = True
                    embed = discord.Embed(
                        title="Camera May Have Moved",
                        description=(
                            f"Image differs **{drift:.1f}%** from baseline "
                            f"(threshold: {BASELINE_THRESHOLD}%).\n\n"
                            "Food readings may be unreliable. "
                            "Use `!baseline` to set a new baseline if the camera was repositioned."
                        ),
                        color=discord.Color.orange(),
                    )
                    fname = snapshot_filename()
                    file = discord.File(io.BytesIO(image_bytes), filename=fname)
                    embed.set_image(url=f"attachment://{fname}")
                    await channel.send(embed=embed, file=file)
                    log.warning("Baseline drift alert: %.1f%%", drift)
                elif drift is not None and drift < BASELINE_THRESHOLD and baseline_alerted:
                    baseline_alerted = False
                    log.info("Baseline drift returned to normal: %.1f%%", drift)

        except Exception as e:
            log.exception("Monitor loop error: %s", e)

        await asyncio.sleep(POLL_INTERVAL_SEC)

    log.info("Monitor loop ended")


# ---------------------------------------------------------------------------
# Discord events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Cat food monitor online as %s", bot.user)
    await detect_config_entry_id()
    channel = bot.get_channel(DISCORD_CHANNEL_ID) or await bot.fetch_channel(DISCORD_CHANNEL_ID)
    embed = discord.Embed(
        title="Cat Food Monitor Online",
        description=(
            "Commands:\n"
            "`!start` - Start monitoring the food bowl\n"
            "`!stop` - Stop monitoring\n"
            "`!check` - One-time food level check\n"
            "`!last` - Show the last checked image\n"
            "`!baseline` - Set baseline image for drift detection\n"
            "`!status` - Show bot status"
        ),
        color=discord.Color.blue(),
    )
    await channel.send(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # Silently ignore unknown commands
    log.error("Command error: %s", error)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.command()
async def start(ctx):
    """Start monitoring cat food bowl."""
    global monitoring, monitor_task, alerted_empty, consecutive_errors, error_alerted

    if monitoring:
        await ctx.send(embed=discord.Embed(
            description="Already monitoring! Use `!stop` to stop.",
            color=discord.Color.orange(),
        ))
        return

    monitoring = True
    alerted_empty = False
    consecutive_errors = 0
    error_alerted = False
    monitor_task = bot.loop.create_task(monitor_loop())
    log.info("Monitoring started by %s", ctx.author)

    desc = (
        f"Checking food bowl every **{POLL_INTERVAL_SEC // 60} minutes**.\n"
        f"I'll alert you if the food drops below **{LOW_FOOD_THRESHOLD}%**."
    )
    if is_quiet_hours():
        desc += (
            f"\n\n**Note:** Quiet hours are active "
            f"({QUIET_START_HOUR:02d}:00\u2013{QUIET_END_HOUR:02d}:00). "
            f"Checks will begin once quiet hours end."
        )

    await ctx.send(embed=discord.Embed(
        title="Monitoring Started",
        description=desc,
        color=discord.Color.green(),
    ))


@bot.command()
async def stop(ctx):
    """Stop monitoring cat food bowl."""
    global monitoring, monitor_task

    if not monitoring:
        await ctx.send(embed=discord.Embed(
            description="Not currently monitoring. Use `!start` to begin.",
            color=discord.Color.orange(),
        ))
        return

    monitoring = False
    if monitor_task:
        monitor_task.cancel()
        monitor_task = None
    log.info("Monitoring stopped by %s", ctx.author)

    await ctx.send(embed=discord.Embed(
        title="Monitoring Stopped",
        description="Cat food monitoring has been stopped.",
        color=discord.Color.red(),
    ))


@bot.command()
async def check(ctx):
    """One-time check of cat food level."""
    global alerted_empty
    log.info("Manual check requested by %s", ctx.author)
    async with ctx.typing():
        result = await analyze_food()

    if result is None:
        await ctx.send(embed=discord.Embed(
            title="Check Failed",
            description="Could not analyze the food bowl. Check logs for details.",
            color=discord.Color.red(),
        ))
        return

    image_bytes, analysis = result
    food = analysis.get("food", False)
    level = analysis.get("level", 0)

    # Sync alert state so monitoring loop doesn't re-alert what user just saw
    if not food or level <= LOW_FOOD_THRESHOLD:
        alerted_empty = True
    else:
        alerted_empty = False

    embed = build_analysis_embed(analysis, manual=True)
    fname = snapshot_filename()
    file = discord.File(io.BytesIO(image_bytes), filename=fname)
    embed.set_image(url=f"attachment://{fname}")
    await ctx.send(embed=embed, file=file)


@bot.command()
async def last(ctx):
    """Show the last checked image and analysis."""
    if last_image_bytes is None or last_analysis is None:
        await ctx.send(embed=discord.Embed(
            description="No checks have been performed yet.",
            color=discord.Color.orange(),
        ))
        return

    embed = build_analysis_embed(last_analysis, manual=True)
    embed.set_footer(text=f"Last checked at {last_check_time.strftime('%I:%M %p')}")
    fname = snapshot_filename()
    file = discord.File(io.BytesIO(last_image_bytes), filename=fname)
    embed.set_image(url=f"attachment://{fname}")
    await ctx.send(embed=embed, file=file)


@bot.command()
async def baseline(ctx):
    """Set baseline image for camera drift detection."""
    log.info("Baseline set requested by %s", ctx.author)
    async with ctx.typing():
        image_bytes = await get_camera_snapshot()

    if image_bytes is None:
        await ctx.send(embed=discord.Embed(
            title="Baseline Failed",
            description="Could not capture snapshot. Check logs for details.",
            color=discord.Color.red(),
        ))
        return

    global baseline_alerted
    save_baseline(image_bytes)
    baseline_alerted = False

    embed = discord.Embed(
        title="Baseline Set",
        description=(
            f"Saved as reference image. I'll alert you if the camera view "
            f"drifts more than **{BASELINE_THRESHOLD}%** from this."
        ),
        color=discord.Color.green(),
    )
    fname = snapshot_filename()
    file = discord.File(io.BytesIO(image_bytes), filename=fname)
    embed.set_image(url=f"attachment://{fname}")
    await ctx.send(embed=embed, file=file)


@bot.command()
async def status(ctx):
    """Show current bot status."""
    lines = [
        f"**Monitoring:** {'Active' if monitoring else 'Inactive'}",
        f"**Poll interval:** {POLL_INTERVAL_SEC // 60} minutes",
        f"**Low food threshold:** {LOW_FOOD_THRESHOLD}%",
        f"**Camera:** `{CAMERA_ENTITY}`",
        f"**Gemini model:** `{GEMINI_MODEL}`",
        f"**Quiet hours:** {f'{QUIET_START_HOUR:02d}:00–{QUIET_END_HOUR:02d}:00' if QUIET_START_HOUR is not None else 'Disabled'}"
        f"{' (active now)' if is_quiet_hours() else ''}",
        f"**Baseline:** {'Set' if load_baseline() else 'Not set'} (threshold: {BASELINE_THRESHOLD}%)",
        f"**HA connected:** {'No' if ha_down else 'Yes'}",
        f"**Gemini connected:** {'No' if gemini_down else 'Yes'}",
        f"**Camera connected:** {'No' if camera_down else 'Yes'}",
    ]
    if last_analysis and last_check_time:
        lines.append(f"**Last check:** {last_check_time.strftime('%I:%M %p')}")
        lines.append(
            f"**Last result:** Food={'Yes' if last_analysis.get('food') else 'No'}, "
            f"Level={last_analysis.get('level', '?')}%"
        )
        drift = last_analysis.get("baseline_drift")
        if drift is not None:
            lines.append(f"**Baseline drift:** {drift}%")

    await ctx.send(embed=discord.Embed(
        title="Cat Food Monitor Status",
        description="\n".join(lines),
        color=discord.Color.blue(),
    ))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
bot.run(DISCORD_BOT_TOKEN, log_handler=None)
