"""
Handles direct links to audio files (mp3, flac, wav, ogg, m4a).
Reads embedded tags with mutagen when present, that's the only case
we're allowed to cache per the requirements. No tags, no cache entry,
we just play it with whatever filename we can scrape from the url.
"""
import asyncio
import io
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
from mutagen import File as MutagenFile

from sources.youtube import Track, FFMPEG_OPTS

AUDIO_EXTENSIONS = (".mp3", ".flac", ".wav", ".ogg", ".m4a", ".opus", ".aac")


def is_direct_audio_link(url: str) -> bool:
    return url.lower().split("?")[0].endswith(AUDIO_EXTENSIONS)


async def resolve_direct_link(url: str) -> Track:
    """
    Downloads just enough of the file header to read tags, without
    pulling the whole file down. Falls back to the filename if no
    tags are found or the read fails for any reason.
    """
    title = None
    artist = None
    duration = None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                # grab the first chunk, most tag formats (ID3, etc)
                # live at the start of the file
                chunk = await resp.content.read(256 * 1024)

        audio = MutagenFile(io.BytesIO(chunk))
        if audio is not None and audio.tags:
            title = _get_tag(audio.tags, ["TIT2", "title", "\xa9nam"])
            artist = _get_tag(audio.tags, ["TPE1", "artist", "\xa9ART"])
            if audio.info and hasattr(audio.info, "length"):
                duration = int(audio.info.length)
    except Exception as e:
        print(f"[direct] metadata read failed for {url}: {e}")

    if not title:
        title = _filename_from_url(url)

    return Track(
        title=title,
        url=url,
        stream_url=url,
        duration_seconds=duration,
        artist=artist,
        source="direct",
    )


def _get_tag(tags, keys):
    for key in keys:
        if key in tags:
            val = tags[key]
            if isinstance(val, list):
                val = val[0]
            return str(val)
    return None


def _filename_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    name = re.sub(r"\.\w+$", "", name)  # strip extension
    name = name.replace("_", " ").replace("%20", " ")
    return name or "Unknown track"


def has_metadata(track: Track) -> bool:
    """Used by the caller to decide whether this track is cache-eligible."""
    return track.artist is not None or track.duration_seconds is not None


async def get_playable_source(track: Track, start_seconds: float = 0):
    import discord

    before_options = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        before_options = f"-ss {start_seconds} {before_options}"

    opts = {"before_options": before_options, "options": FFMPEG_OPTS["options"]}
    return discord.FFmpegPCMAudio(track.stream_url, **opts)
