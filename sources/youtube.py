# sources/youtube.py
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs
import yt_dlp

from utils.logger import get_logger
from utils.audio_debug import build_logged_ffmpeg_source

logger = get_logger(__name__)

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

# how many times we retry a single extraction after a plain connection
# hiccup (timeout, reset, refused) before giving up on it. these tend
# to clear up on their own a couple seconds later, so a short retry
# loop with a small pause is worth it before we bother the user
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

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

# plain network flakiness, not youtube being picky about anything.
# worth a few retries since a second attempt a couple seconds later
# usually just works
CONNECTION_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "temporary failure in name resolution",
)

# this is a different wall than the age gate above, youtube throws
# this one up when it decides a request looks like a bot rather than
# a person, which in practice usually means we've been hitting it too
# hard and it's rate limiting us. a retry with cookies from a real
# signed-in browser profile sometimes clears it
BOT_CHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you are not a bot",
)

FORBIDDEN_MARKERS = (
    "403",
    "forbidden",
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


class StreamUnavailableError(Exception):
    """
    Raised when yt-dlp just can't get a working stream for a video,
    after retries where retries make sense. Covers persistent 403s and
    connection errors that never cleared up, this is deliberately kept
    separate from AgeRestrictedError since there's no known cause here,
    the video just isn't cooperating right now.
    """

    def __init__(self, query: str):
        self.query = query
        super().__init__(f"Could not resolve a working stream for: {query}")


class RateLimitedError(Exception):
    """
    Raised when youtube throws up its "sign in to confirm you're not a
    bot" wall and a retry with browser cookies didn't get past it
    either. There's nothing smarter to do at that point than back off,
    so this exists to carry a distinct "try again later" message
    instead of looking like every other extraction failure.
    """

    def __init__(self, query: str):
        self.query = query
        super().__init__(f"Rate limited by youtube resolving: {query}")


def _is_age_restriction_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in AGE_RESTRICTION_MARKERS)


def _is_connection_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in CONNECTION_ERROR_MARKERS)


def _is_bot_check_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in BOT_CHECK_MARKERS)


def _is_forbidden_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in FORBIDDEN_MARKERS)


def _log_extraction_result(info: dict, context: str):
    """
    Logs the bits of a resolved yt-dlp info dict that actually help
    when something goes wrong later. format_id/protocol/url host can
    all vary between runs since we use the 'default' (multi-client
    fallback) player_client, and that variability is exactly what's
    made past issues (like the header/client signing mismatch) hard
    to pin down without seeing it logged at resolution time.
    """
    try:
        fmt_id = info.get("format_id")
        protocol = info.get("protocol")
        url = info.get("url")
        host = urlparse(url).netloc if url else None
        logger.debug(
            f"[yt-dlp] {context}: format_id={fmt_id} protocol={protocol} host={host}"
        )
    except Exception as e:
        logger.debug(f"[yt-dlp] couldn't log extraction diagnostics for {context}: {e}")


def _log_stream_url_diagnostics(url: str, track_title: str):
    """
    Googlevideo urls carry an 'expire' query param (unix timestamp).
    Parsing it out tells us exactly how much runway a resolved url had
    left the moment we grabbed it, and lets us flag on the spot if we
    somehow already resolved something that's expired or about to be.
    This is the main diagnostic for the occasional 403s, since a stale
    reused url is the most likely cause of one.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        expire_raw = qs.get("expire", [None])[0]
        host = parsed.netloc

        if expire_raw is None:
            logger.info(f"[stream] '{track_title}' resolved to host={host} (no expire param found on this url)")
            return

        remaining = int(expire_raw) - int(time.time())
        if remaining < 0:
            logger.warning(
                f"[stream] '{track_title}' resolved to host={host} but its expire timestamp "
                f"is already {-remaining}s in the past, this url is dead on arrival"
            )
        elif remaining < 60:
            logger.warning(
                f"[stream] '{track_title}' resolved to host={host} with only {remaining}s "
                f"left before it expires"
            )
        else:
            logger.info(f"[stream] '{track_title}' resolved to host={host}, expires in {remaining}s")
    except Exception as e:
        logger.debug(f"couldn't parse stream url diagnostics for '{track_title}': {e}")


def _extract_attempt(query: str, is_search: bool, flat: bool = False, extra_opts: dict = None) -> list[dict]:
    """
    Runs in a thread executor, yt-dlp is blocking. This is a single
    attempt with no retry logic in it at all, that lives in _extract
    below. extra_opts lets a caller layer something on top of the
    normal options for one specific attempt (right now that's just
    the firefox cookie retry for the sign-in/bot check case).
    """
    logger.debug(f"[yt-dlp] extracting metadata for: '{query}' (search={is_search}, flat={flat})")
    opts = dict(YDL_OPTS)
    if flat:
        # skips resolving an actual playable stream for every entry, just
        # lists them out with whatever metadata the playlist/search API
        # JSON already hands us for free (title, duration, uploader, etc).
        # this is the difference between one request and sixty separate
        # PO token/JS challenge round trips just to build a queue nobody
        # has actually listened to yet
        opts["extract_flat"] = "in_playlist"
    if extra_opts:
        opts.update(extra_opts)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            target = f"ytsearch1:{query}" if is_search else query
            info = ydl.extract_info(target, download=False)

            if not info:
                logger.warning(f"[yt-dlp] extraction returned empty info for target: '{target}'")
                return []

            # playlists come back with an 'entries' key, single tracks don't
            if "entries" in info:
                valid_entries = [e for e in info["entries"] if e]
                logger.debug(f"[yt-dlp] extracted container/playlist, found {len(valid_entries)} entries")
                return valid_entries

            logger.debug(f"[yt-dlp] extracted single track: '{info.get('title', 'Unknown')}'")
            if not flat:
                _log_extraction_result(info, context=f"'{info.get('title', query)}'")
            return [info]

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)

        # age gate isn't something a retry or a looser format selector
        # is ever going to fix, so check for it first and bail out with
        # a distinct error before we waste time on the format fallback below
        if _is_age_restriction_error(err_str):
            logger.warning(f"[yt-dlp] age-restricted video detected for '{query}', no sign-in available to bypass it")
            raise AgeRestrictedError(query) from e

        # Format unavailability slips through even with the client
        # fallback list above sometimes, since YouTube's rollout isn't
        # all-or-nothing. One more attempt with the loosest possible
        # selector before giving up, better to hand back something
        # playable (even if it's not audio-only) than to fail the
        # whole /play command over a format quirk.
        if "Requested format is not available" in err_str:
            logger.warning(f"[yt-dlp] format fallback triggered for '{query}', retrying with format='best'")
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
                    if not flat:
                        _log_extraction_result(info, context=f"'{info.get('title', query)}' (format fallback)")
                    return [info]
            except yt_dlp.utils.DownloadError as retry_err:
                retry_err_str = str(retry_err)
                if _is_age_restriction_error(retry_err_str):
                    logger.warning(f"[yt-dlp] age-restricted video detected for '{query}' during format fallback")
                    raise AgeRestrictedError(query) from retry_err
                logger.error(f"[yt-dlp] fallback extraction also failed for '{query}': {retry_err}")
                raise
            except Exception as retry_err:
                logger.error(f"[yt-dlp] fallback extraction also failed for '{query}': {retry_err}")
                raise
        logger.error(f"[yt-dlp] extraction failed for target '{query}': {e}")
        raise
    except Exception as e:
        logger.error(f"[yt-dlp] extraction failed for target '{query}': {e}")
        raise


def _extract(query: str, is_search: bool, flat: bool = False) -> list[dict]:
    """
    Thin retry wrapper around _extract_attempt. Still runs synchronously
    in a thread executor, same as before.

    Three different failure shapes get handled here instead of all of
    them just bubbling up as one generic extraction failure:

    - plain network flakiness (timeouts, connection resets, refused
      connections) gets a few retries with a short pause between them,
      since these usually clear up on their own a couple seconds later
      and it's a fixable problem on our end
    - youtube's "sign in to confirm you're not a bot" wall gets one
      retry with cookies pulled from the local firefox profile, since
      that sometimes clears it. If that still doesn't work we give up
      with RateLimitedError instead of hammering youtube again while
      it's already telling us to back off
    - a video that comes back forbidden (403), or a connection error
      that never cleared up after the retries above, raises
      StreamUnavailableError. This is deliberately its own thing
      separate from AgeRestrictedError, there's no specific known cause
      here, the video just isn't playable from us right now
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _extract_attempt(query, is_search, flat=flat)
        except AgeRestrictedError:
            raise
        except yt_dlp.utils.DownloadError as e:
            err_str = str(e)
            last_error = e

            if _is_bot_check_error(err_str):
                logger.warning(f"[yt-dlp] hit youtube's sign-in/bot check for '{query}', trying once with firefox cookies")
                try:
                    return _extract_attempt(query, is_search, flat=flat, extra_opts={"cookiesfrombrowser": ("firefox",)})
                except Exception as cookie_err:
                    logger.error(f"[yt-dlp] cookie retry also failed for '{query}': {cookie_err}")
                    raise RateLimitedError(query) from cookie_err

            if _is_connection_error(err_str):
                if attempt < MAX_RETRIES:
                    logger.warning(
                        f"[yt-dlp] connection error on attempt {attempt}/{MAX_RETRIES} "
                        f"for '{query}': {e}, retrying in {RETRY_BACKOFF_SECONDS}s"
                    )
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                logger.error(f"[yt-dlp] connection error persisted after {MAX_RETRIES} attempts for '{query}': {e}")
                raise StreamUnavailableError(query) from e

            if _is_forbidden_error(err_str):
                logger.error(f"[yt-dlp] forbidden/403 resolving '{query}': {e}")
                raise StreamUnavailableError(query) from e

            # some other download error we don't have specific handling
            # for, no point retrying something we don't understand
            raise
        except Exception:
            raise

    # the loop above always returns or raises, this is just a safety
    # net in case that ever stops being true
    raise StreamUnavailableError(query) from last_error


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
    # wall clock time (time.time()) that stream_url was last resolved.
    # only used for logging/diagnostics right now, it tells us how old
    # a reused stream url was when something like a seek or a delayed
    # play_next reused it instead of resolving fresh, which is the
    # prime suspect whenever a track suddenly throws a 403 mid-playback.
    stream_resolved_at: Optional[float] = None


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

    AgeRestrictedError, StreamUnavailableError, and RateLimitedError
    are all let through on purpose instead of being swallowed into an
    empty list, callers need to know which specific failure mode this
    was so they can tell the user what actually happened instead of
    showing a generic "no results" message.
    """
    is_search = not (query.startswith("http://") or query.startswith("https://"))
    loop = asyncio.get_event_loop()

    logger.debug(f"[resolver] handing query '{query}' off to thread pool executor")
    try:
        entries = await loop.run_in_executor(None, _extract, query, is_search, True)
    except (AgeRestrictedError, StreamUnavailableError, RateLimitedError):
        raise
    except Exception as e:
        logger.error(f"[resolver] extraction failed entirely for '{query}': {e}")
        return []

    tracks = []
    for entry in entries:
        webpage_url = entry.get("webpage_url") or entry.get("url") or query
        if not webpage_url:
            logger.warning(f"[resolver] entry skipped, no usable web identifier found: {entry.get('title')}")
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
    logger.info(f"[resolver] '{query}' resolved to {len(tracks)} track(s)")
    return tracks


def _build_ffmpeg_source(stream_url: str, start_seconds: float = 0, track_title: str = "unknown", on_forbidden=None):
    before_options = FFMPEG_OPTS["before_options"]
    if start_seconds > 0:
        before_options = f"-ss {start_seconds} {before_options}"

    return build_logged_ffmpeg_source(
        stream_url,
        before_options=before_options,
        options=FFMPEG_OPTS["options"],
        track_title=track_title,
        on_forbidden=on_forbidden,
    )


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

    AgeRestrictedError, StreamUnavailableError, and RateLimitedError
    all propagate straight out of here, nothing here swallows them, the
    caller (play_next, or the prewarm task) decides what to do about it.
    """
    logger.info(f"[resolve] resolving stream url for: '{track.title}'")
    started = time.time()
    loop = asyncio.get_event_loop()
    entries = await loop.run_in_executor(None, _extract, track.url, False)
    if not entries:
        logger.error(f"[resolve] could not resolve stream for '{track.title}'")
        raise StreamUnavailableError(track.title)

    track.stream_url = entries[0]["url"]
    track.stream_resolved_at = time.time()
    logger.debug(f"[resolve] '{track.title}' resolved in {track.stream_resolved_at - started:.2f}s")
    _log_stream_url_diagnostics(track.stream_url, track.title)


async def get_playable_source(track: Track, start_seconds: float = 0, on_forbidden=None):
    """
    Builds a playable ffmpeg source for a track. If the track already
    has a stream url on it (set ahead of time by the player's prewarm
    step, or left over from a previous loop_current playthrough) this
    skips straight to building the source instead of resolving all
    over again.

    on_forbidden is handed straight through to the ffmpeg stderr
    watcher, it gets called if ffmpeg reports a 403 partway through
    actually streaming this track (separate from a 403 hit while
    resolving the stream url up front, which raises StreamUnavailableError
    instead and never gets this far).
    """
    if not track.stream_url:
        await resolve_stream(track)
    else:
        age = time.time() - track.stream_resolved_at if track.stream_resolved_at else None
        if age is not None:
            logger.debug(f"[play] '{track.title}' reusing a stream url resolved {age:.1f}s ago")
            if age > 300:
                logger.warning(
                    f"[play] '{track.title}' is about to play on a stream url that's "
                    f"{age:.0f}s old, this is a plausible cause if it 403s"
                )
    return _build_ffmpeg_source(track.stream_url, start_seconds, track_title=track.title, on_forbidden=on_forbidden)


def get_playable_source_from_cache(track: Track, start_seconds: float = 0, on_forbidden=None):
    """
    Builds a playable source straight from the track's already-resolved
    stream url, skipping the yt-dlp round trip. Used for in-song
    seeking, since the url was just refreshed moments ago and doesn't
    need to be looked up again. This is what keeps /seekforward and
    /seekback fast instead of hitting youtube on every nudge.

    That said, "just refreshed moments ago" isn't guaranteed, if
    someone seeks long after the track started, this is reusing
    whatever url got resolved at track start (or prewarm time), and
    that url can be old enough to have expired. Logging the age here
    is the main lever we have for telling a stale-url 403 apart from
    anything else when it happens on a seek specifically.
    """
    age = time.time() - track.stream_resolved_at if track.stream_resolved_at else None
    if age is not None:
        logger.debug(f"[seek] '{track.title}' seeking on a stream url resolved {age:.1f}s ago")
        if age > 300:
            logger.warning(
                f"[seek] '{track.title}' is seeking on a stream url that's "
                f"{age:.0f}s old, this is a plausible cause if it 403s"
            )
    else:
        logger.debug(f"[seek] '{track.title}' has no recorded resolve time for its stream url")
    return _build_ffmpeg_source(track.stream_url, start_seconds, track_title=track.title, on_forbidden=on_forbidden)


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

    Any resolution failure here (including our own retry/rate-limit
    errors) just means autoplay comes up dry for this pull, so it's
    caught broadly and turned into an empty list rather than crashing
    the autoplay loop.
    """
    video_id = extract_video_id(seed_url)
    if not video_id:
        logger.warning(f"[autoplay] couldn't pull a video id out of seed url: {seed_url}")
        return []

    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    logger.info(f"[autoplay] pulling mix for seed video id {video_id}")
    loop = asyncio.get_event_loop()

    try:
        entries = await loop.run_in_executor(None, _extract, mix_url, False, True)
    except Exception as e:
        logger.error(f"[autoplay] mix extraction failed for seed '{seed_url}': {e}")
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
    logger.info(f"[autoplay] mix for seed {video_id} yielded {len(tracks)} usable track(s) after filtering")
    return tracks
