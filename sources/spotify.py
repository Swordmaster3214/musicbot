"""
Spotify doesn't let anyone stream raw audio through the API, so this
module just pulls track/playlist/album/show/episode metadata (title +
artist, or episode + show name) and hands each one off to the youtube
module to find a matching stream.

This is the same approach basically every self-hosted spotify-capable
discord bot uses under the hood.
"""
import asyncio
import re
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

import config
from sources.youtube import search_or_resolve, Track

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(track|playlist|album|show|episode)/([a-zA-Z0-9]+)"
)

_client: Optional[spotipy.Spotify] = None


def _get_client() -> spotipy.Spotify:
    global _client
    if _client is None:
        if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
            raise RuntimeError(
                "Spotify credentials aren't configured. Set SPOTIFY_CLIENT_ID "
                "and SPOTIFY_CLIENT_SECRET in .env to use spotify links."
            )
        auth = SpotifyClientCredentials(
            client_id=config.SPOTIFY_CLIENT_ID,
            client_secret=config.SPOTIFY_CLIENT_SECRET,
        )
        _client = spotipy.Spotify(client_credentials_manager=auth)
    return _client


def is_spotify_link(url: str) -> bool:
    return bool(SPOTIFY_URL_RE.search(url))


def _extract_spotify_meta(url: str) -> tuple[str, list[dict]]:
    """
    Blocking spotipy calls, runs in an executor. Returns (kind, list of
    {title, artist} dicts) for track/playlist/album/show/episode urls.
    For episodes and shows, "artist" ends up holding the podcast/show
    name since episodes don't really have an artist in the music sense.
    """
    match = SPOTIFY_URL_RE.search(url)
    if not match:
        raise ValueError("Not a valid spotify url")

    kind, item_id = match.groups()
    sp = _get_client()

    if kind == "track":
        track = sp.track(item_id)
        return kind, [_track_to_meta(track)]

    if kind == "playlist":
        results = sp.playlist_items(item_id)
        items = results["items"]
        while results["next"]:
            results = sp.next(results)
            items.extend(results["items"])
        metas = [_track_to_meta(i["track"]) for i in items if i.get("track")]
        return kind, metas

    if kind == "album":
        results = sp.album_tracks(item_id)
        items = results["items"]
        while results["next"]:
            results = sp.next(results)
            items.extend(results["items"])
        metas = [_track_to_meta(t) for t in items]
        return kind, metas

    if kind == "episode":
        episode = sp.episode(item_id)
        show_name = episode.get("show", {}).get("name", "")
        return kind, [{"title": episode["name"], "artist": show_name}]

    if kind == "show":
        show = sp.show(item_id)
        show_name = show.get("name", "")
        results = sp.show_episodes(item_id)
        items = results["items"]
        while results["next"]:
            results = sp.next(results)
            items.extend(results["items"])
        metas = [{"title": e["name"], "artist": show_name} for e in items if e]
        return kind, metas

    raise ValueError(f"Unsupported spotify link type: {kind}")


def _track_to_meta(track: dict) -> dict:
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    return {"title": track["name"], "artist": artists}


async def resolve_spotify_link(url: str) -> list[Track]:
    """
    Pulls metadata for the spotify link, then searches youtube for each
    track or episode. Playlists, albums, and shows get resolved
    concurrently in small batches so we don't hammer youtube with a
    hundred requests at once.
    """
    loop = asyncio.get_event_loop()
    kind, metas = await loop.run_in_executor(None, _extract_spotify_meta, url)

    async def resolve_one(meta: dict) -> Optional[Track]:
        query = f"{meta['title']} {meta['artist']}".strip()
        try:
            results = await search_or_resolve(query)
        except Exception as e:
            print(f"[spotify] failed to resolve '{query}': {e}")
            return None
        if not results:
            return None
        found = results[0]
        found.artist = meta["artist"] or found.artist
        found.source = "spotify"
        return found

    batch_size = 5
    tracks: list[Track] = []
    for i in range(0, len(metas), batch_size):
        batch = metas[i:i + batch_size]
        resolved = await asyncio.gather(*(resolve_one(m) for m in batch))
        tracks.extend(t for t in resolved if t is not None)

    return tracks
