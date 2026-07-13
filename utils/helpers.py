"""
Small formatting helpers shared across cogs. Kept separate so the cog
file doesn't get cluttered with string formatting logic.
"""
import discord
from sources.youtube import Track


def format_duration(seconds: int) -> str:
    if seconds is None:
        return "Live/Unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def now_playing_embed(track: Track) -> discord.Embed:
    embed = discord.Embed(
        title="Now Playing",
        description=f"**{track.title}**",
        color=discord.Color.blurple(),
    )
    if track.artist:
        embed.add_field(name="Artist", value=track.artist, inline=True)
    embed.add_field(name="Duration", value=format_duration(track.duration_seconds), inline=True)
    embed.add_field(name="Source", value=track.source.capitalize(), inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def queue_embed(tracks: list[Track], page: int = 0, per_page: int = 10) -> discord.Embed:
    start = page * per_page
    end = start + per_page
    slice_ = tracks[start:end]

    embed = discord.Embed(title="Queue", color=discord.Color.teal())
    if not slice_:
        embed.description = "The queue is empty."
        return embed

    lines = []
    for i, track in enumerate(slice_, start=start + 1):
        duration = format_duration(track.duration_seconds)
        lines.append(f"`{i}.` **{track.title}** — {duration}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(tracks)} track(s) total, page {page + 1}")
    return embed
