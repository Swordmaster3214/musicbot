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
    "source_address": "0.0.0.0",
    "socket_timeout": 10,         # Prevents yt-dlp from hanging indefinitely on bad sockets
    "retries": 3,                 # Give it a few chances before throwing an error
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    duration_seconds: Optional[int]
    artist: Optional[str] = None
    source: str = "youtube"
    thumbnail: Optional[str] = None


def _extract(query: str, is_search: bool) -> list[dict]:
    """Runs in a thread executor, yt-dlp is blocking."""
    print(f"  [YT-DLP] Extracting metadata for: '{query}' (Search mode: {is_search})")
    opts = dict(YDL_OPTS)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            target = f"ytsearch1:{query}" if is_search else query
            info = ydl.extract_info(target, download=False)

            if not info:
                print(f"  [YT-DLP WARNING] Extraction returned empty info for target: '{target}'")
                return []

            # playlists come back with an 'entries' key, single tracks don't
            if "entries" in info:
                valid_entries = [e for e in info["entries"] if e]
                print(f"  [YT-DLP] Extracted container/playlist. Found {len(valid_entries)} entries.")
                return valid_entries

            print(f"  [YT-DLP] Extracted single track details for: '{info.get('title', 'Unknown')}'")
            return [info]

    except Exception as e:
        print(f"  [YT-DLP CRITICAL] Extraction failed for target '{query}': {e}")
        raise


async def search_or_resolve(query: str) -> list[Track]:
    """
    Takes either a search term or a youtube url (video or playlist)
    and returns a list of Track objects ready to queue up.
    """
    is_search = not (query.startswith("http://") or query.startswith("https://"))
    loop = asyncio.get_event_loop()

    print(f"[RESOLVER] Handing query off to thread pool executor...")
    entries = await loop.run_in_executor(None, _extract, query, is_search)

    tracks = []
    for entry in entries:
        stream_url = entry.get("url")
        if not stream_url:
            print(f"[RESOLVER] Entry missing direct stream URL. Attempting secondary pass on webpage/ID...")
            target_id = entry.get("webpage_url", entry.get("id", ""))
            if not target_id:
                print(f"[RESOLVER WARNING] Entry skipped; no usable web identifiers found: {entry.get('title')}")
                continue

            resolved = await loop.run_in_executor(None, _extract, target_id, False)
            if not resolved:
                continue
            entry = resolved[0]
            stream_url = entry.get("url")

        if not stream_url:
            print(f"[RESOLVER WARNING] Skipping track '{entry.get('title')}'; stream URL could not be resolved.")
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
    """Refreshes the stream url right before playback."""
    import discord

    print(f"[PLAYER] Refreshing stream URL for playback: '{track.title}'")
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, track.url, False)
    if not entries:
        raise RuntimeError(f"Could not re-resolve stream for '{track.title}'")

    fresh_stream_url = entries[0]["url"]

    before_options = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        before_options = f"-ss {start_seconds} {before_options}"

    opts = {"before_options": before_options, "options": FFMPEG_OPTS["options"]}
    return discord.FFmpegPCMAudio(fresh_stream_url, **opts)
