"""
Entry point. Run this with `python bot.py` from the project root.
Everything else is loaded as a cog from here, so adding a new feature
later usually just means dropping a new file in cogs/ and adding one
load_extension line below.
"""
import asyncio
import os
import signal

import discord
from discord.ext import commands

import config
from utils.logger import get_logger

logger = get_logger(__name__)

INTENTS = discord.Intents.default()
INTENTS.voice_states = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guild(s): {[g.id for g in bot.guilds]}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")


@bot.event
async def on_disconnect():
    # discord.py fires this on any gateway drop, not just a clean
    # shutdown. logging it helps tell "the bot's voice connections got
    # cut because the whole gateway session dropped" apart from "voice
    # specifically misbehaved", which matters when chasing the
    # occasional stale-voice-client issue
    logger.warning("Gateway connection dropped (on_disconnect fired)")


@bot.event
async def on_resumed():
    logger.info("Gateway session resumed")


async def shutdown():
    """
    Runs on SIGINT/SIGTERM. Disconnects any active voice clients first
    so the ffmpeg subprocesses attached to them get torn down cleanly,
    then closes the bot.
    """
    logger.info("Shutting down...")
    active_vcs = list(bot.voice_clients)
    logger.info(f"Disconnecting {len(active_vcs)} active voice client(s)")
    for vc in active_vcs:
        try:
            await vc.disconnect(force=True)
        except Exception as e:
            logger.warning(f"Error disconnecting voice client for guild {getattr(vc.channel, 'guild', None)}: {e}")
    await bot.close()


async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))

    async with bot:
        await bot.load_extension("cogs.music")
        logger.info("Loaded cogs.music, starting bot...")
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    # Belt and suspenders: asyncio.run()'s own cleanup waits on the
    # default thread pool executor before it lets the process exit, so
    # a still-running yt-dlp extraction can hold the terminal open for
    # a while after ctrl-c. Force the exit once we get here instead.
    os._exit(0)
