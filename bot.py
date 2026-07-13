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

INTENTS = discord.Intents.default()
INTENTS.voice_states = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


async def shutdown():
    """
    Runs on SIGINT/SIGTERM. Disconnects any active voice clients first
    so the ffmpeg subprocesses attached to them get torn down cleanly,
    then closes the bot.
    """
    print("\nShutting down...")
    for vc in list(bot.voice_clients):
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    await bot.close()


async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))

    async with bot:
        await bot.load_extension("cogs.music")
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
