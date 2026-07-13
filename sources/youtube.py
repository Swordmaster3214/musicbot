"""
Handles anything that comes from YouTube, direct video/playlist urls
or plain text search terms. Uses yt-dlp under the hood since it's
kept up to date way better than youtube-dl at this point.

We never download files to disk, we just grab the direct audio stream
url and hand that to ffmpeg through discord.py's voice client. Keeps
disk usage flat no matter how many songs get played.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional
import yt_dlp

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",  # helps avoid some ipv6 connection issues
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass
class Track:
    title: str
    url: str            # the page url, good for caching and display
    stream_url: str      # the actual audio stream url ffmpeg will read
    duration_seconds: Optional[int]
    artist: Optional[str] = None
    source: str = "youtube"
    thumbnail: Optional[str] = None


def _extract(query: str, is_search: bool) -> list[dict]:
    """Runs in a thread executor, yt-dlp is blocking."""
    opts = dict(YDL_OPTS)
    with yt_dlp.YoutubeDL(opts) as ydl:
        target = f"ytsearch1:{query}" if is_search else query
        info = ydl.extract_info(target, download=False)

        # playlists come back with an 'entries' key, single tracks don't
        if "entries" in info:
            return [e for e in info["entries"] if e]
        return [info]


async def search_or_resolve(query: str) -> list[Track]:
    """
    Takes either a search term or a youtube url (video or playlist)
    and returns a list of Track objects ready to queue up.
    """
    is_search = not (query.startswith("http://") or query.startswith("https://"))
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, query, is_search)

    tracks = []
    for entry in entries:
        # flat playlist entries sometimes lack a direct stream url,
        # need a second pass to resolve those individually
        stream_url = entry.get("url")
        if not stream_url:
            resolved = await loop.run_in_executor(
                None, _extract, entry.get("webpage_url", entry.get("id", "")), False
            )
            if not resolved:
                continue
            entry = resolved[0]
            stream_url = entry.get("url")

        if not stream_url:
            continue

        tracks.append(
            Track(
                title=entry.get("title", "Unknown title"),
                url=entry.get("webpage_url", query),
                stream_url=stream_url,
                duration_seconds=entry.get("duration"),
                artist=entry.get("uploader"),
                thumbnail=entry.get("thumbnail"),
            )
        )
    return tracks


async def get_playable_source(track: Track, start_seconds: float = 0):
    """
    Refreshes the stream url right before playback since these urls
    expire after a while. Returns an ffmpeg audio source discord.py
    can play directly.

    start_seconds lets us jump into the middle of a stream, which is
    how seeking works here. ffmpeg can't seek a stream that's already
    open, so the caller kills the old process and starts a new one
    at the offset it wants instead.
    """
    import discord

    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, track.url, False)
    if not entries:
        raise RuntimeError(f"Could not re-resolve stream for '{track.title}'")

    fresh_stream_url = entries[0]["url"]

    before_options = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        # -ss before the input tells ffmpeg to seek before decoding starts,
        # much faster than seeking after the input is opened
        before_options = f"-ss {start_seconds} {before_options}"

    opts = {"before_options": before_options, "options": FFMPEG_OPTS["options"]}
    return discord.FFmpegPCMAudio(fresh_stream_url, **opts)
