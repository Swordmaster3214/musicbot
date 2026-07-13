"""
Wraps the sqlite song cache. Only stores songs when we actually have
metadata for them, per the requirement. No metadata, no cache entry.

This is a straightforward synchronous sqlite3 wrapper. Discord.py runs
an async event loop, so calls into this get pushed to a thread executor
from the cog layer rather than making this whole module async for
what is a pretty small, fast local file.
"""
import sqlite3
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
import difflib

import config


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
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        schema_path = Path(__file__).parent / "schema.sql"
        with open(schema_path, "r") as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    def add(self, title: str, url: str, source: str, artist: str = None,
            duration_seconds: int = None):
        """Insert a song into the cache. Silently ignores duplicates."""
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO song_cache
                   (title, artist, url, source, duration_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, artist, url, source, duration_seconds),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            # cache writes should never crash playback, just log and move on
            print(f"[SongCache] failed to insert '{title}': {e}")

    def exact_match(self, title: str) -> Optional[CachedSong]:
        row = self.conn.execute(
            "SELECT * FROM song_cache WHERE title = ? COLLATE NOCASE LIMIT 1",
            (title,),
        ).fetchone()
        return self._row_to_song(row) if row else None

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
        return results

    def _row_to_song(self, row) -> CachedSong:
        return CachedSong(
            title=row["title"],
            artist=row["artist"],
            url=row["url"],
            source=row["source"],
            duration_seconds=row["duration_seconds"],
        )

    def close(self):
        self.conn.close()
