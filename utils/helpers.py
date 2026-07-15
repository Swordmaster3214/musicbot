"""
Small formatting helpers shared across cogs. Kept separate so the cog
file doesn't get cluttered with string formatting logic.

Both embed builders take a lang code so the caller can hand them
whatever the current guild's language setting is, they default to
english so anything that forgets to pass it still works fine.
"""
import discord
from sources.youtube import Track
from i18n.strings import t


def format_duration(seconds: int, lang: str = "en") -> str:
    if seconds is None:
        return t("duration_live_unknown", lang)
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _source_label(source: str, lang: str) -> str:
    # direct links get an actual translated label, youtube and spotify
    # are proper nouns so there's nothing to translate there
    if source == "direct":
        return t("source_direct", lang)
    return source.capitalize()


def now_playing_embed(track: Track, lang: str = "en") -> discord.Embed:
    embed = discord.Embed(
        title=t("embed_now_playing_title", lang),
        description=f"**{track.title}**",
        color=discord.Color.blurple(),
    )
    if track.artist:
        embed.add_field(name=t("embed_artist", lang), value=track.artist, inline=True)
    embed.add_field(name=t("embed_duration", lang), value=format_duration(track.duration_seconds, lang), inline=True)
    embed.add_field(name=t("embed_source", lang), value=_source_label(track.source, lang), inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


def queue_embed(tracks: list[Track], page: int = 0, per_page: int = 10, lang: str = "en") -> discord.Embed:
    start = page * per_page
    end = start + per_page
    slice_ = tracks[start:end]

    embed = discord.Embed(title=t("embed_queue_title", lang), color=discord.Color.teal())
    if not slice_:
        embed.description = t("embed_queue_empty", lang)
        return embed

    lines = []
    for i, track in enumerate(slice_, start=start + 1):
        duration = format_duration(track.duration_seconds, lang)
        lines.append(f"`{i}.` **{track.title}** — {duration}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=t("embed_queue_footer", lang, count=len(tracks), page=page + 1))
    return embed
