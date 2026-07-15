-- song cache, only populated when we have real metadata to work with
-- (direct file links with tags, or resolved youtube/spotify metadata)
CREATE TABLE IF NOT EXISTS song_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT,
    url TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'youtube', 'spotify', 'direct'
    duration_seconds INTEGER,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(title, artist, url)
);

-- fast lookup by title, this is the whole point of caching
CREATE INDEX IF NOT EXISTS idx_song_title ON song_cache(title);

-- per-server settings, currently just holds the chosen language.
-- one row per guild, defaults to english if a guild never ran /language
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'en'
);
