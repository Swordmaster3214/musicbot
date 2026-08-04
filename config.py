"""
Config loader. Pulls everything from env vars so we never hardcode secrets.
Copy .env.example to .env and fill it in before running.
"""
import os
from dotenv import load_dotenv

from utils.logger import get_logger

load_dotenv()

logger = get_logger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# where the sqlite cache db lives, relative to project root
DB_PATH = os.getenv("DB_PATH", "db/musicbot.sqlite3")

# max queue size per guild, 0 means unlimited (we default to unlimited per requirements)
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "0"))

# how many results to grab when searching youtube for a spotify track match
SPOTIFY_MATCH_SEARCH_DEPTH = int(os.getenv("SPOTIFY_MATCH_SEARCH_DEPTH", "5"))

# how often (seconds) the now-playing embed gets its progress bar/elapsed
# time refreshed while a track is actively playing. This runs as one
# single background loop that checks every guild each tick rather than
# a task per guild, and it skips anything paused or with no known
# duration, so raising the guild count doesn't multiply the request
# rate the way a per-guild timer would. 15s is a reasonable balance
# between "looks live" and not leaning on Discord's edit rate limits.
NOWPLAYING_UPDATE_INTERVAL_SECONDS = int(os.getenv("NOWPLAYING_UPDATE_INTERVAL_SECONDS", "15"))

# discord user ids that skip the playback vote entirely, comma separated.
# this is a plain env var on purpose, so changing who's on the list means
# editing .env and restarting the bot rather than adding another admin
# command that could be run by someone other than the person hosting it
_raw_vote_bypass = os.getenv("VOTE_BYPASS_USER_IDS", "")
VOTE_BYPASS_USER_IDS = {
    int(uid.strip()) for uid in _raw_vote_bypass.split(",") if uid.strip().isdigit()
}

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Set it in your .env file.")

# log the config we actually ended up with (never the token itself),
# so a support request can start with "here's what config.py loaded"
# instead of us guessing whether an env var actually took effect
logger.info(
    f"[config] loaded: db_path={DB_PATH} max_queue_size={MAX_QUEUE_SIZE or 'unlimited'} "
    f"spotify_configured={bool(SPOTIFY_CLIENT_ID)} vote_bypass_count={len(VOTE_BYPASS_USER_IDS)} "
    f"nowplaying_update_interval={NOWPLAYING_UPDATE_INTERVAL_SECONDS}s "
    f"log_level={os.getenv('MUSICBOT_LOG_LEVEL', 'INFO').upper()}"
)
