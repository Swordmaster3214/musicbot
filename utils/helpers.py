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


def escape_title(title: str) -> str:
    """
    Song titles come from outside sources (yt-dlp, Spotify metadata,
    embedded file tags) and can contain literal markdown characters
    (*, _, ~, `, >, |, etc) that were never meant to do anything to
    Discord's renderer. Without escaping, a title like
    "*NSYNC - Bye Bye Bye" breaks right out of a **{title}** wrapper
    and reformats whatever comes after it, or a title with a pipe pair
    turns part of itself into a spoiler tag.

    discord.utils.escape_markdown backslash-escapes the full set of
    characters Discord treats specially, the same thing discord.py
    itself does internally whenever it needs user-supplied text to
    render as plain text. Reaching for that instead of hand-rolling
    our own escape list means we don't have to keep it in sync if
    Discord's markdown dialect ever gains another special character.
    """
    return discord.utils.escape_markdown(title)


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
        description=f"**{escape_title(track.title)}**",
        color=discord.Color.blurple(),
    )
    if track.artist:
        embed.add_field(name=t("embed_artist", lang), value=escape_title(track.artist), inline=True)
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
        lines.append(f"`{i}.` **{escape_title(track.title)}** — {duration}")

    embed.description = "\n".join(lines)
    embed.set_footer(text=t("embed_queue_footer", lang, count=len(tracks), page=page + 1))
    return embed
