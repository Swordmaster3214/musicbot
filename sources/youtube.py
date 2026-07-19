# sources/youtube.py
import asyncio
from dataclasses import dataclass, field
from typing import Optional
import yt_dlp

YDL_OPTS = {
    "format": "bestaudio",
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
            "player_client" : ["default"]
        }
    },
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# phrases yt-dlp uses in its DownloadError message when a video is
# age gated and youtube wants a signed-in, age-verified account to
# view it. we don't have that (and don't want to start cookie-auth'ing
# as someone's personal account just to play music), so we catch this
# specifically and surface it as its own error instead of letting it
# look like a plain "couldn't find anything" search failure
AGE_RESTRICTION_MARKERS = (
    "confirm your age",
    "age-restricted",
    "sign in to confirm your age",
)


class AgeRestrictedError(Exception):
    """
    Raised when yt-dlp can't pull a video because youtube is gating it
    behind an age-verified, signed-in account. We have no login to
    hand it, so this case can never resolve on its own and shouldn't
    be treated as a generic extraction failure.
    """

    def __init__(self, query: str):
        self.query = query
        super().__init__(f"Age-restricted video, needs sign-in: {query}")


@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    duration_seconds: Optional[int]
    artist: Optional[str] = None
    source: str = "youtube"
    thumbnail: Optional[str] = None
    # discord user id of whoever queued this track. left as None for
    # tracks the bot pulled in on its own (autoplay mixes), and gets
    # stamped on everything else by the cog right after a query
    # resolves, no matter which source module built the track. this is
    # what lets someone skip/pause/seek their own song without a vote.
    requested_by: Optional[int] = None


def _is_age_restriction_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in AGE_RESTRICTION_MARKERS)


def _extract(query: str, is_search: bool, flat: bool = False) -> list[dict]:
    """Runs in a thread executor, yt-dlp is blocking."""
    print(f"  [YT-DLP] Extracting metadata for: '{query}' (Search mode: {is_search}, Flat: {flat})")
    opts = dict(YDL_OPTS)
    if flat:
        # skips resolving an actual playable stream for every entry, just
        # lists them out with whatever metadata the playlist/search API
        # JSON already hands us for free (title, duration, uploader, etc).
        # this is the difference between one request and sixty separate
        # PO token/JS challenge round trips just to build a queue nobody
        # has actually listened to yet
        opts["extract_flat"] = "in_playlist"

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
        err_str = str(e)

        # age gate isn't something a retry or a looser format selector
        # is ever going to fix, so check for it first and bail out with
        # a distinct error before we waste time on the format fallback below
        if _is_age_restriction_error(err_str):
            print(f"  [YT-DLP] Age-restricted video detected for '{query}', no sign-in available to bypass it.")
            raise AgeRestrictedError(query) from e

        # Format unavailability slips through even with the client
        # fallback list above sometimes, since YouTube's rollout isn't
        # all-or-nothing. One more attempt with the loosest possible
        # selector before giving up, better to hand back something
        # playable (even if it's not audio-only) than to fail the
        # whole /play command over a format quirk.
        if "Requested format is not available" in err_str:
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
            except yt_dlp.utils.DownloadError as retry_err:
                retry_err_str = str(retry_err)
                if _is_age_restriction_error(retry_err_str):
                    print(f"  [YT-DLP] Age-restricted video detected for '{query}' during format fallback.")
                    raise AgeRestrictedError(query) from retry_err
                print(f"  [YT-DLP CRITICAL] Fallback extraction also failed for '{query}': {retry_err}")
                raise
            except Exception as retry_err:
                print(f"  [YT-DLP CRITICAL] Fallback extraction also failed for '{query}': {retry_err}")
                raise
        print(f"  [YT-DLP CRITICAL] Extraction failed for target '{query}': {e}")
        raise
    except Exception as e:
        print(f"  [YT-DLP CRITICAL] Extraction failed for target '{query}': {e}")
        raise


def _thumbnail_from_entry(entry: dict) -> Optional[str]:
    """
    Flat entries store thumbnails a bit differently than fully resolved
    ones (a list of candidates instead of one single field), so check
    both spots instead of assuming the shape.
    """
    thumb = entry.get("thumbnail")
    if thumb:
        return thumb
    thumbs = entry.get("thumbnails")
    if thumbs:
        return thumbs[-1].get("url")
    return None


async def search_or_resolve(query: str) -> list[Track]:
    """
    Takes either a search term or a youtube url (video or playlist)
    and returns a list of Track objects ready to queue up.

    This only does a flat listing pass here, it grabs title/duration/
    uploader for every entry but doesn't resolve an actual playable
    stream for any of them yet. That part happens in
    get_playable_source, right before a track actually plays (or
    earlier still, if the player's prewarm step gets to it first).

    We used to resolve a real stream for every single entry up front,
    which meant a 60 track playlist went through the full PO token/JS
    challenge dance sixty times just to build the queue, and then did
    it all again for each song once it actually got played. Now that
    expensive part only happens once, right when a given track is
    about to start, instead of twice for everything in the queue.

    AgeRestrictedError is let through on purpose instead of being
    swallowed into an empty list, callers need to know this specific
    failure mode so they can tell the user what actually happened
    instead of showing a generic "no results" message.
    """
    is_search = not (query.startswith("http://") or query.startswith("https://"))
    loop = asyncio.get_event_loop()

    print(f"[RESOLVER] Handing query off to thread pool executor...")
    try:
        entries = await loop.run_in_executor(None, _extract, query, is_search, True)
    except AgeRestrictedError:
        raise
    except Exception as e:
        print(f"[RESOLVER] Extraction failed entirely for '{query}': {e}")
        return []

    tracks = []
    for entry in entries:
        webpage_url = entry.get("webpage_url") or entry.get("url") or query
        if not webpage_url:
            print(f"[RESOLVER WARNING] Entry skipped; no usable web identifier found: {entry.get('title')}")
            continue

        tracks.append(
            Track(
                title=entry.get("title", "Unknown title"),
                url=webpage_url,
                # left empty on purpose, resolution happens later,
                # either by the prewarm step or by get_playable_source
                # right before playback, so doing it here too would
                # just be the same work twice
                stream_url=None,
                duration_seconds=entry.get("duration"),
                artist=entry.get("uploader") or entry.get("channel"),
                thumbnail=_thumbnail_from_entry(entry),
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


async def resolve_stream(track: Track) -> None:
    """
    Resolves a fresh stream url for a track and stores it right on the
    track object, without building an ffmpeg source out of it. This is
    the part that actually costs time (PO token generation, JS
    challenge solving, m3u8 fetching), split out on its own so it can
    be run ahead of time by the player's prewarm step instead of only
    ever running right as a track is about to play.

    Callers that already have a resolved stream_url on hand (a loop
    replaying the same track, or a prewarm that got there first) can
    skip calling this entirely, that's what get_playable_source checks
    for below.
    """
    print(f"[PLAYER] Resolving stream URL for: '{track.title}'")
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, track.url, False)
    if not entries:
        raise RuntimeError(f"Could not resolve stream for '{track.title}'")

    track.stream_url = entries[0]["url"]


async def get_playable_source(track: Track, start_seconds: float = 0):
    """
    Builds a playable ffmpeg source for a track. If the track already
    has a stream url on it (set ahead of time by the player's prewarm
    step, or left over from a previous loop_current playthrough) this
    skips straight to building the source instead of resolving all
    over again.
    """
    if not track.stream_url:
        await resolve_stream(track)
    return _build_ffmpeg_source(track.stream_url, start_seconds)


def get_playable_source_from_cache(track: Track, start_seconds: float = 0):
    """
    Builds a playable source straight from the track's already-resolved
    stream url, skipping the yt-dlp round trip. Used for in-song
    seeking, since the url was just refreshed moments ago and doesn't
    need to be looked up again. This is what keeps /seekforward and
    /seekback fast instead of hitting youtube on every nudge.
    """
    return _build_ffmpeg_source(track.stream_url, start_seconds)

import re

YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})")


def extract_video_id(url: str) -> Optional[str]:
    """Pulls the 11 char video id out of a youtube url, whatever shape it's in."""
    match = YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


async def get_mix_tracks(seed_url: str, exclude_urls: set[str]) -> list[Track]:
    """
    Builds youtube's auto-generated 'Mix' playlist for a video and does
    a flat listing pass on it, same as any other playlist. There's no
    dedicated related-videos api anymore, but a mix playlist is really
    just youtube's recommendation engine wearing a playlist url, so
    yt-dlp handles it the same way it handles any other playlist link.

    exclude_urls filters out anything we've already played this
    session, since mixes reliably include the seed track itself and
    sometimes earlier tracks from the session too.

    These tracks come back with requested_by left as None on purpose,
    nobody queued them, autoplay pulled them in on its own, so there's
    no single person who should get to skip past one without a vote.
    """
    video_id = extract_video_id(seed_url)
    if not video_id:
        print(f"[autoplay] couldn't pull a video id out of seed url: {seed_url}")
        return []

    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    loop = asyncio.get_event_loop()

    try:
        entries = await loop.run_in_executor(None, _extract, mix_url, False, True)
    except Exception as e:
        print(f"[autoplay] mix extraction failed for seed '{seed_url}': {e}")
        return []

    tracks = []
    for entry in entries:
        webpage_url = entry.get("webpage_url") or entry.get("url")
        if not webpage_url or webpage_url in exclude_urls:
            continue

        tracks.append(
            Track(
                title=entry.get("title", "Unknown title"),
                url=webpage_url,
                stream_url=None,
                duration_seconds=entry.get("duration"),
                artist=entry.get("uploader") or entry.get("channel"),
                thumbnail=_thumbnail_from_entry(entry),
                source="youtube",
            )
        )
    return tracks
