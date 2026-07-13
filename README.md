# Self-Hosted Discord Music Bot

Streams from YouTube, Spotify (resolved via YouTube), and direct audio
file links. One queue per server, unlimited length, full playback
controls, and a SQLite cache that remembers songs by title for fast
re-search.

## Setup (Arch / Arch-based)

1. Install system dependencies:
   ```bash
   sudo pacman -S python python-pip ffmpeg
   ```

2. Clone/extract the project, then set up a virtual environment:
   ```bash
   cd musicbot
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Copy the env template and fill in your credentials:
   ```bash
   cp .env.example .env
   ```
   - `DISCORD_TOKEN`: from the [Discord Developer Portal](https://discord.com/developers/applications). Make sure the bot has the `applications.commands` and `bot` scopes, and the `Server Members` + `Message Content` privileged intents enabled.
   - `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`: from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard). Only needed if you want Spotify link support.

4. Run it:
   ```bash
   python bot.py
   ```

Slash commands sync automatically on startup (can take up to an hour to
show up globally the first time, instant in servers if you switch to
guild-specific sync during testing).

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Search or play from a YouTube/Spotify/direct link |
| `/shuffleplay <query>` | Queue a playlist and shuffle it immediately |
| `/shuffle` | Shuffle the current queue |
| `/queue [page]` | View the queue |
| `/clearqueue` | Clear the entire queue |
| `/remove <position>` | Remove a specific track |
| `/pause` / `/resume` | Pause/resume playback |
| `/skip` | Skip current track |
| `/skipforward` / `/skipback` | Jump 10 tracks forward/back |
| `/stop` | Stop and clear queue |
| `/disconnect` | Leave voice |
| `/nowplaying` | Show current track info |
| `/loop` | Toggle looping current track |
| `/loopqueue` | Toggle looping the whole queue |
| `/findcached <query>` | Fuzzy search previously cached song titles |

## Project structure

```
musicbot/
├── bot.py                  entry point
├── config.py                env var loading
├── cogs/music.py             slash commands (discord interface layer)
├── core/
│   ├── queue_manager.py     per-guild queue, shuffle, history
│   └── player.py             voice client + playback control
├── sources/
│   ├── youtube.py           yt-dlp search/stream resolution
│   ├── spotify.py           spotify metadata -> youtube resolution
│   └── direct.py             direct file links + tag reading
├── db/
│   ├── schema.sql
│   └── cache.py               sqlite title/link cache
└── utils/helpers.py           embeds, formatting
```

## Notes on the cache

Per design, a song only gets written to the cache when real metadata
is available: YouTube/Spotify resolved tracks always have a title, and
direct file links only get cached if the file actually has embedded
tags (artist or duration). Untagged direct links still play fine, they
just won't show up in `/findcached`.

## Ideas for later (not yet implemented)

- Volume control
- Autoplay/radio mode (queue similar tracks when the queue runs dry)
- Per-user favorites/playlists saved across sessions
- Web dashboard for queue management outside of Discord
