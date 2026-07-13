"""
Wraps the actual discord voice client for a guild. This is the layer
that talks to ffmpeg and discord's voice gateway, the cog just calls
into this and doesn't need to know how any of it works under the hood.
"""
import asyncio
import time
from typing import Optional, Callable, Awaitable

import discord

from core.queue_manager import GuildQueue
from sources.youtube import Track
import sources.youtube as youtube_source
import sources.direct as direct_source


class GuildPlayer:
    def __init__(self, guild_id: int, queue: GuildQueue, bot: discord.Client):
        self.guild_id = guild_id
        self.queue = queue
        self.bot = bot
        self.voice_client: Optional[discord.VoiceClient] = None
        # called whenever a new track starts playing, async so the cog
        # can update the now playing message and wait on it if needed
        self.on_track_start: Optional[Callable[[Track], Awaitable[None]]] = None
        self._skip_event = asyncio.Event()
        # when true, the after-playback callback skips advancing the queue.
        # needed for manual jumps (seeking) where we've already restarted
        # playback ourselves and don't want a double advance.
        self._suppress_auto_advance = False

        # playback position tracking, used for seeking. accum holds
        # seconds already played before the current segment, segment_start
        # is the wall clock time the current segment began. no segment
        # start means we're paused or stopped.
        self._position_accum = 0.0
        self._segment_start = None

    def is_connected(self) -> bool:
        guild = self.bot.get_guild(self.guild_id)
        if guild and guild.voice_client:
            self.voice_client = guild.voice_client
        return self.voice_client is not None and self.voice_client.is_connected()

    async def connect(self, channel: discord.VoiceChannel):
        guild_vc = channel.guild.voice_client
        if guild_vc and guild_vc.is_connected():
            self.voice_client = guild_vc
            await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect()

    async def disconnect(self):
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
        self.voice_client = None

    def is_connected(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_connected()

    def current_position(self) -> float:
        """Seconds into the current track, accounting for pauses."""
        if self._segment_start is not None:
            return self._position_accum + (time.time() - self._segment_start)
        return self._position_accum

    async def play_next(self):
        """
        Pulls the next track off the queue and plays it. Gets called
        automatically when a song finishes, via the after= callback
        discord.py gives us on VoiceClient.play.
        """
        track = self.queue.next()
        if track is None:
            return  # queue's empty, just sit connected and idle

        source_module = direct_source if track.source == "direct" else youtube_source
        audio_source = await source_module.get_playable_source(track)

        self.voice_client.play(audio_source, after=self._make_after_callback())
        self._position_accum = 0.0
        self._segment_start = time.time()

        if self.on_track_start:
            await self.on_track_start(track)

    def _make_after_callback(self):
        """
        Builds the after= callback for VoiceClient.play. Centralized here
        so every place that starts playback shares the same logic, and
        the suppress flag only needs to be checked in one spot.
        """
        def _after_playback(error):
            if error:
                print(f"[player] playback error: {error}")

            if self._suppress_auto_advance:
                self._suppress_auto_advance = False
                return

            fut = asyncio.run_coroutine_threadsafe(
                self.play_next(), self.bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                print(f"[player] error advancing queue: {e}")

        return _after_playback

    def pause(self) -> bool:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            # freeze the position we've accumulated so far, the clock
            # stops ticking until resume() starts a new segment
            self._position_accum = self.current_position()
            self._segment_start = None
            return True
        return False

    def resume(self) -> bool:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self._segment_start = time.time()
            return True
        return False

    def stop(self):
        """Stops playback outright, does not touch the queue."""
        if self.voice_client:
            self.voice_client.stop()

    async def skip(self):
        """Skips the current track, triggers play_next via the after callback."""
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        else:
            await self.play_next()

    async def seek_seconds(self, delta: float):
        """
        Jumps forward or backward within the current track by delta
        seconds (negative to rewind). This is the actual seek behind
        the +10/-10 buttons and commands.

        ffmpeg has no way to seek a stream it's already decoding, so
        we stop the current process and start a fresh one with -ss
        pointed at the new offset. The suppress flag keeps the after
        callback from thinking the track ended and advancing the queue.
        """
        track = self.queue.current
        if track is None or self.voice_client is None:
            return

        new_position = self.current_position() + delta
        new_position = max(0.0, new_position)
        if track.duration_seconds:
            # leave a one second buffer so we don't seek right past the end
            new_position = min(new_position, max(track.duration_seconds - 1, 0))

        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self._suppress_auto_advance = True
            self.voice_client.stop()

        source_module = direct_source if track.source == "direct" else youtube_source
        audio_source = await source_module.get_playable_source(track, start_seconds=new_position)

        self.voice_client.play(audio_source, after=self._make_after_callback())
        self._position_accum = new_position
        self._segment_start = time.time()


class PlayerManager:
    """Holds a GuildPlayer per guild, created lazily alongside its queue."""

    def __init__(self, queue_manager):
        self.queue_manager = queue_manager
        self.bot = bot
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self._players:
            queue = self.queue_manager.get(guild_id)
            self._players[guild_id] = GuildPlayer(guild_id, queue)
        return self._players[guild_id]
