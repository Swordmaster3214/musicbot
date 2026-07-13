"""
Entry point. Run this with `python bot.py` from the project root.
Everything else is loaded as a cog from here, so adding a new feature
later usually just means dropping a new file in cogs/ and adding one
load_extension line below.
"""
import asyncio
import discord
from discord.ext import commands

import config

INTENTS = discord.Intents.default()
INTENTS.message_content = True
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


async def main():
    async with bot:
        await bot.load_extension("cogs.music")
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
