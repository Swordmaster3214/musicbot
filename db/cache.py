"""
Wraps the sqlite song cache. Only stores songs when we actually have
metadata for them, per the requirement. No metadata, no cache entry.

This is a straightforward synchronous sqlite3 wrapper. Discord.py runs
an async event loop, so calls into this get pushed to a thread executor
from the cog layer rather than making this whole module async for
what is a pretty small, fast local file.

Also holds the per-guild settings table (currently just language),
it's the same tiny sqlite file so there's no reason to spin up a
second store just for one column.
"""
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import difflib

import config
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CachedSong:
    title: str
    artist: Optional[str]
    url: str
    source: str
    duration_seconds: Optional[int]


class SongCache:
    def __init__(self, db_path: str = None):
        path = Path(db_path or config.DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[cache] opening sqlite db at {path}")
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r") as f:
            self.conn.executescript(f.read())
        self.conn.commit()
        logger.debug("[cache] schema applied/verified")

    def add(self, title: str, url: str, source: str, artist: str = None,
            duration_seconds: int = None):
        """Insert a song into the cache. Silently ignores duplicates."""
        try:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO song_cache
                   (title, artist, url, source, duration_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, artist, url, source, duration_seconds),
            )
            self.conn.commit()
            if cur.rowcount:
                logger.debug(f"[cache] added '{title}' (source={source})")
            else:
                logger.debug(f"[cache] '{title}' already cached, skipped")
        except sqlite3.Error as e:
            # cache writes should never crash playback, just log and move on
            logger.warning(f"[cache] failed to insert '{title}': {e}")

    def exact_match(self, title: str) -> Optional[CachedSong]:
        row = self.conn.execute(
            "SELECT * FROM song_cache WHERE title = ? COLLATE NOCASE LIMIT 1",
            (title,),
        ).fetchone()
        result = self._row_to_song(row) if row else None
        logger.debug(f"[cache] exact_match('{title}') -> {'hit' if result else 'miss'}")
        return result

    def fuzzy_search(self, query: str, limit: int = 5) -> list[CachedSong]:
        """
        Grabs all titles and does a fuzzy match in python rather than
        relying on sqlite's LIKE, since we want typo tolerance and
        partial matches, not just substring matches.
        """
        rows = self.conn.execute("SELECT * FROM song_cache").fetchall()
        if not rows:
            return []

        titles = [row["title"] for row in rows]
        close = difflib.get_close_matches(query, titles, n=limit, cutoff=0.4)

        # also catch simple substring matches that difflib might miss
        substring_hits = [t for t in titles if query.lower() in t.lower()]

        seen = []
        for t in close + substring_hits:
            if t not in seen:
                seen.append(t)
            if len(seen) >= limit:
                break

        results = []
        for title in seen:
            row = next(r for r in rows if r["title"] == title)
            results.append(self._row_to_song(row))
        logger.debug(f"[cache] fuzzy_search('{query}') -> {len(results)} result(s) out of {len(rows)} cached title(s)")
        return results

    def _row_to_song(self, row) -> CachedSong:
        return CachedSong(
            title=row["title"],
            artist=row["artist"],
            url=row["url"],
            source=row["source"],
            duration_seconds=row["duration_seconds"],
        )

    # ---------- guild settings ----------

    def get_guild_language(self, guild_id: int) -> str:
        """Returns the guild's chosen language, or 'en' if never set."""
        row = self.conn.execute(
            "SELECT language FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row["language"] if row else "en"

    def set_guild_language(self, guild_id: int, language: str):
        self.conn.execute(
            """INSERT INTO guild_settings (guild_id, language) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET language = excluded.language""",
            (guild_id, language),
        )
        self.conn.commit()
        logger.info(f"[cache] guild {guild_id} language set to '{language}'")

    def close(self):
        logger.info("[cache] closing sqlite connection")
        self.conn.close()
