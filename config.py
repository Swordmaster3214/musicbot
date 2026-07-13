"""
Config loader. Pulls everything from env vars so we never hardcode secrets.
Copy .env.example to .env and fill it in before running.
"""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# where the sqlite cache db lives, relative to project root
DB_PATH = os.getenv("DB_PATH", "db/musicbot.sqlite3")

# max queue size per guild, 0 means unlimited (we default to unlimited per requirements)
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "0"))

# how many results to grab when searching youtube for a spotify track match
SPOTIFY_MATCH_SEARCH_DEPTH = int(os.getenv("SPOTIFY_MATCH_SEARCH_DEPTH", "5"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in your .env file.")
