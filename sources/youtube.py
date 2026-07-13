import asyncio
from dataclasses import dataclass
from typing import Optional
import yt_dlp

YDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "cookiesfrombrowser": ("firefox",),
    "noplaylist": False,
    "quiet": False,
    "no_warnings": False,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",
    "socket_timeout": 10,         # Prevents yt-dlp from hanging indefinitely on bad sockets
    "retries": 3,                 # Give it a few chances before throwing an error
    "extractor_args": {
        "youtube": {
            "player_client" : ["default", "mweb", "android", "ios", "web"]
        }
    },
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

    except yt_dlp.utils.DownloadError as e:
        # Format unavailability slips through even with the client
        # fallback list above sometimes, since YouTube's rollout isn't
        # all-or-nothing. One more attempt with the loosest possible
        # selector before giving up, better to hand back something
        # playable (even if it's not audio-only) than to fail the
        # whole /play command over a format quirk.
        if "Requested format is not available" in str(e):
            print(f"  [YT-DLP] Format fallback triggered for '{query}', retrying with format='best'...")
            fallback_opts = dict(opts)
            fallback_opts["format"] = "best"
            try:
                with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                    target = f"ytsearch1:{query}" if is_search else query
                    info = ydl.extract_info(target, download=False)
                    if not info:
                        return []
                    if "entries" in info:
                        return [e for e in info["entries"] if e]
                    return [info]
            except Exception as retry_err:
                print(f"  [YT-DLP CRITICAL] Fallback extraction also failed for '{query}': {retry_err}")
                raise
        print(f"  [YT-DLP CRITICAL] Extraction failed for target '{query}': {e}")
        raise
    except Exception as e:
        print(f"  [YT-DLP CRITICAL] Extraction failed for target '{query}': {e}")
        raise


async def search_or_resolve(query: str) -> list[Track]:
    """
    Takes either a search term or a youtube url (video or playlist)
    and returns a list of Track objects ready to queue up. Individual
    entries that fail extraction (SABR/PO-token gating, region locks,
    etc) are skipped rather than blowing up the whole search, since
    with this stuff being a moving target on YouTube's end, we can
    expect a handful of tracks to be temporarily unresolvable no
    matter how up to date our extraction setup is.
    """
    is_search = not (query.startswith("http://") or query.startswith("https://"))
    loop = asyncio.get_event_loop()

    print(f"[RESOLVER] Handing query off to thread pool executor...")
    try:
        entries = await loop.run_in_executor(None, _extract, query, is_search)
    except Exception as e:
        print(f"[RESOLVER] Extraction failed entirely for '{query}': {e}")
        return []

    tracks = []
    for entry in entries:
        stream_url = entry.get("url")
        if not stream_url:
            print(f"[RESOLVER] Entry missing direct stream URL. Attempting secondary pass on webpage/ID...")
            target_id = entry.get("webpage_url", entry.get("id", ""))
            if not target_id:
                print(f"[RESOLVER WARNING] Entry skipped; no usable web identifiers found: {entry.get('title')}")
                continue

            try:
                resolved = await loop.run_in_executor(None, _extract, target_id, False)
            except Exception as e:
                print(f"[RESOLVER] Secondary extraction failed for '{entry.get('title')}', skipping: {e}")
                continue
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

def _build_ffmpeg_source(stream_url: str, start_seconds: float = 0):
    import discord

    before_options = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        before_options = f"-ss {start_seconds} {before_options}"

    opts = {"before_options": before_options, "options": FFMPEG_OPTS["options"]}
    return discord.FFmpegPCMAudio(stream_url, **opts)

async def get_playable_source(track: Track, start_seconds: float = 0):
    """Refreshes the stream url right before playback."""
    import discord

    print(f"[PLAYER] Refreshing stream URL for playback: '{track.title}'")
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, track.url, False)
    if not entries:
        raise RuntimeError(f"Could not re-resolve stream for '{track.title}'")

    fresh_stream_url = entries[0]["url"]

    track.stream_url = fresh_stream_url

    return _build_ffmpeg_source(fresh_stream_url, start_seconds)

def get_playable_source_from_cache(track: Track, start_seconds: float = 0):
    """
    Builds a playable source straight from the track's already-resolved
    stream url, skipping the yt-dlp round trip. Used for in-song
    seeking, since the url was just refreshed moments ago and doesn't
    need to be looked up again. This is what keeps /seekforward and
    /seekback fast instead of hitting youtube on every nudge.
    """
    return _build_ffmpeg_source(track.stream_url, start_seconds)
