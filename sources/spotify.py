"""
Alternative to the official Spotify Web API for track resolution.

Spotify's Feb 2026 policy change requires Development Mode apps to have
an owner with an active Premium subscription just to keep working at all,
even for the read-only metadata lookups we do here. That's a non-starter
for a bot running under someone's regular account, so instead we scrape
the public embed page Spotify serves at open.spotify.com/embed/... No
login, no API keys, no premium required, since it's the same page a
browser hits to render the little embedded player widget you see on blogs.

This is unofficial and undocumented, so the page structure could change
without warning. If this starts throwing KeyErrors after a Spotify
frontend update, that's almost certainly why, so open one of these embed
urls in a browser and check the __NEXT_DATA__ script tag to see what shifted.
"""
import asyncio
import json
import re
from typing import Optional

import aiohttp

from sources.youtube import search_or_resolve, Track
from utils.logger import get_logger

logger = get_logger(__name__)

SPOTIFY_URL_RE = re.compile(
    r"open\.spotify\.com/(track|playlist|album|show|episode)/([a-zA-Z0-9]+)"
)

EMBED_URL_TMPL = "https://open.spotify.com/embed/{kind}/{item_id}"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def is_spotify_link(url: str) -> bool:
    return bool(SPOTIFY_URL_RE.search(url))


async def _fetch_embed_json(kind: str, item_id: str) -> dict:
    """Grabs the embed page and pulls the JSON blob out of it."""
    url = EMBED_URL_TMPL.format(kind=kind, item_id=item_id)
    logger.debug(f"[spotify] fetching embed page: {url}")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                logger.error(f"[spotify] embed page returned {resp.status} for {url}")
                raise RuntimeError(f"Embed page returned {resp.status} for {url}")
            html = await resp.text()

    match = NEXT_DATA_RE.search(html)
    if not match:
        logger.error(f"[spotify] couldn't find __NEXT_DATA__ on the embed page for {url}, page structure may have changed")
        raise RuntimeError(f"Couldn't find embed data on the page for {url}")

    data = json.loads(match.group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


def _extract_artist(item: dict, context: str) -> str:
    """
    Pulls an artist name out of whatever shape this particular entity
    or trackList item happens to be in. 'subtitle' is the field the
    top-level track/episode entity reliably has, but playlist/album
    trackList items don't always carry it, sometimes the artist only
    shows up under an 'artists' list of {"name": ...} objects instead
    (or, on some responses, 'artistName' directly).

    Silently defaulting to "" here (like a plain item.get("subtitle", ""))
    is exactly what let this go unnoticed before: the query still built
    fine, it just quietly lost the artist half of it, so youtube search
    was left picking a plausible-looking result from title alone. If
    none of the known shapes match, this logs the item's actual keys
    at warning level so a JSON shape change like this is visible next
    time instead of just showing up as "the bot picked the wrong song."
    """
    subtitle = item.get("subtitle")
    if subtitle:
        return subtitle

    artists = item.get("artists")
    if artists:
        names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
        if names:
            return ", ".join(names)

    artist_name = item.get("artistName")
    if artist_name:
        return artist_name

    logger.warning(
        f"[spotify] couldn't find an artist field for {context}, tried "
        f"subtitle/artists/artistName, none present. Item keys were: "
        f"{list(item.keys())}. Falling back to title-only search, which "
        f"is more likely to match the wrong song."
    )
    return ""


def _entity_to_metas(kind: str, entity: dict) -> list[dict]:
    """
    Normalizes whatever shape the entity comes back in into a flat list
    of {title, artist} dicts, same shape the old spotipy version returned.
    """
    if kind in ("track", "episode"):
        return [{"title": entity["title"], "artist": _extract_artist(entity, context=f"track entity '{entity.get('title')}'")}]

    # playlist, album, and show all come back with a trackList
    metas = []
    for item in entity.get("trackList", []):
        title = item.get("title")
        if not title:
            continue
        metas.append({"title": title, "artist": _extract_artist(item, context=f"trackList item '{title}'")})
    return metas


async def resolve_spotify_link(url: str) -> list[Track]:
    match = SPOTIFY_URL_RE.search(url)
    if not match:
        raise ValueError("Not a valid spotify url")

    kind, item_id = match.groups()
    logger.info(f"[spotify] resolving {kind} {item_id} from {url}")
    entity = await _fetch_embed_json(kind, item_id)
    metas = _entity_to_metas(kind, entity)
    logger.debug(f"[spotify] entity for {item_id} yielded {len(metas)} track meta(s) to resolve on youtube")

    async def resolve_one(meta: dict) -> Optional[Track]:
        query = f"{meta['title']} {meta['artist']}".strip()
        if not meta["artist"]:
            logger.debug(f"[spotify] searching youtube title-only (no artist resolved): '{query}'")
        else:
            logger.debug(f"[spotify] searching youtube: '{query}'")
        try:
            results = await search_or_resolve(query)
        except Exception as e:
            logger.warning(f"[spotify] failed to resolve '{query}' on youtube: {e}")
            return None
        if not results:
            logger.debug(f"[spotify] no youtube results for '{query}'")
            return None
        found = results[0]
        found.artist = meta["artist"] or found.artist
        found.source = "spotify"
        return found

    # small batches so we don't hammer youtube with a hundred searches at once
    batch_size = 5
    tracks: list[Track] = []
    for i in range(0, len(metas), batch_size):
        batch = metas[i:i + batch_size]
        resolved = await asyncio.gather(*(resolve_one(m) for m in batch))
        tracks.extend(t for t in resolved if t is not None)

    logger.info(f"[spotify] {item_id} resolved to {len(tracks)}/{len(metas)} playable track(s)")
    return tracks
