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
from utils.logger import get_logger

logger = get_logger(__name__)


class GuildPlayer:
    def __init__(self, guild_id: int, queue: GuildQueue, bot):
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
        # when true, the next advance ignores track loop. set right
        # before an explicit skip or stop, so a looped track doesn't
        # just come right back after the user asked to move past it.
        self._force_ignore_loop = False

        self._connect_lock = asyncio.Lock()

        # guards the whole "check if idle, pop next track, resolve its
        # stream, start playback" sequence. without this, two /play
        # commands landing at the same time can both see the player as
        # idle and both try to call voice_client.play(), and discord.py
        # throws "already playing" on the second one after the track has
        # already been popped off the queue, so it just vanishes.
        self._playback_lock = asyncio.Lock()

        # holds whatever resolution work (yt-dlp stream lookup, or an
        # autoplay mix pull) is currently in flight during play_next, so
        # stop_and_clear can cancel it. without this, hitting stop while
        # a track is still resolving does nothing, and the track starts
        # playing anyway a few seconds later once the lookup finishes.
        self._resolve_task: Optional[asyncio.Task] = None

        # holds the background prewarm lookup for whatever track is
        # sitting next in the queue, so it can be cancelled if the
        # queue changes out from under it (skip, remove, shuffle)
        # before that lookup finishes.
        self._prewarm_task: Optional[asyncio.Task] = None
        self._prewarm_track: Optional[Track] = None

        # playback position tracking, used for seeking. accum holds
        # seconds already played before the current segment, segment_start
        # is the wall clock time the current segment began. no segment
        # start means we're paused or stopped.
        self._position_accum = 0.0
        self._segment_start = None

    def is_connected(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_connected()

    async def connect(self, channel: discord.VoiceChannel):
        """
        Connects to (or moves to, or reuses an existing connection to)
        the given channel.

        The logging in here is specifically to chase down the "bot
        thinks it's already in a channel when it isn't" issue. The
        root of that class of bug is almost always
        channel.guild.voice_client: discord.py hands back whatever
        VoiceClient object it still has on record for the guild, and
        that object can be stale (its gateway session died, or it was
        never properly cleaned up after a disconnect) without
        is_connected() necessarily telling the full story right away.
        Logging the before/after state on every connect attempt is
        what lets us actually see that mismatch happen next time,
        instead of just hearing "the bot said it was already
        connected but it wasn't" secondhand.
        """
        async with self._connect_lock:
            guild_vc = channel.guild.voice_client
            logger.debug(
                f"[connect] guild={channel.guild.id} requested_channel={channel.id} "
                f"discord.py's guild.voice_client={guild_vc!r} "
                f"(connected={guild_vc.is_connected() if guild_vc else None}, "
                f"channel={getattr(guild_vc, 'channel', None)}) "
                f"our_tracked_voice_client={self.voice_client!r}"
            )

            if guild_vc and guild_vc.is_connected():
                logger.info(
                    f"[connect] guild {channel.guild.id}: reusing existing voice "
                    f"connection (currently in {guild_vc.channel})"
                )
                self.voice_client = guild_vc
                if self.voice_client.channel != channel:
                    logger.info(
                        f"[connect] guild {channel.guild.id}: moving from "
                        f"{self.voice_client.channel} to {channel}"
                    )
                    await self.voice_client.move_to(channel)
            else:
                if guild_vc is not None:
                    logger.warning(
                        f"[connect] guild {channel.guild.id}: discord.py still had a "
                        f"voice client on record ({guild_vc!r}) but is_connected() was "
                        f"False, treating it as stale and reconnecting fresh instead "
                        f"of trusting it"
                    )
                logger.info(f"[connect] guild {channel.guild.id}: opening a fresh voice connection to {channel}")
                self.voice_client = await channel.connect()

            await channel.guild.change_voice_state(channel=channel, self_deaf=True)
            logger.debug(
                f"[connect] guild {channel.guild.id}: connect() finished, "
                f"voice_client={self.voice_client!r} connected={self.is_connected()}"
            )

    async def disconnect(self):
        logger.info(
            f"[disconnect] guild {self.guild_id}: disconnect requested, "
            f"current voice_client={self.voice_client!r}"
        )
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            logger.info(f"[disconnect] guild {self.guild_id}: disconnected cleanly")
        else:
            logger.warning(
                f"[disconnect] guild {self.guild_id}: disconnect() called but "
                f"voice_client was already None or not connected, nothing to do"
            )
        self.voice_client = None

    def current_position(self) -> float:
        """Seconds into the current track, accounting for pauses."""
        if self._segment_start is not None:
            return self._position_accum + (time.time() - self._segment_start)
        return self._position_accum

    async def play_next(self, ignore_loop: bool = False):
        """
        Pulls the next track off the queue and plays it. Gets called
        automatically when a song finishes, via the after= callback
        discord.py gives us on VoiceClient.play.

        Everything from the idle check through the actual play() call
        happens under _playback_lock, so if two callers land here at
        the same time (two /play commands racing, or a natural advance
        overlapping a manual one), only the first actually starts a
        track. The second sees is_playing() true once it gets the lock
        and just backs off, its track stays queued and gets picked up
        on the next natural advance.
        """
        async with self._playback_lock:
            if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
                logger.debug(f"[play_next] guild {self.guild_id}: already playing/paused, backing off")
                return

            track = self.queue.next(ignore_loop=ignore_loop)
            logger.debug(f"[play_next] guild {self.guild_id}: queue.next() -> {track.title if track else None}")

            try:
                if track is None and self.queue.autoplay:
                    logger.info(f"[play_next] guild {self.guild_id}: queue is empty, autoplay is on, pulling a mix track")
                    self._resolve_task = asyncio.ensure_future(self._pull_autoplay_track())
                    track = await self._resolve_task

                if track is None:
                    logger.debug(f"[play_next] guild {self.guild_id}: nothing to play, staying idle")
                    return  # queue's empty and autoplay is off (or came up dry), just sit idle

                # if the prewarm step already resolved this exact track,
                # get_playable_source below will see stream_url set and
                # skip straight to building the ffmpeg source instead of
                # doing the lookup all over again
                source_module = direct_source if track.source == "direct" else youtube_source
                logger.info(f"[play_next] guild {self.guild_id}: resolving playable source for '{track.title}'")
                started = time.time()
                self._resolve_task = asyncio.ensure_future(source_module.get_playable_source(track))
                audio_source = await self._resolve_task
                logger.debug(
                    f"[play_next] guild {self.guild_id}: source ready for '{track.title}' "
                    f"in {time.time() - started:.2f}s"
                )
            except asyncio.CancelledError:
                # stop_and_clear cancelled us mid-lookup, bail out quietly
                # instead of starting a track nobody asked for anymore
                logger.debug(f"[play_next] guild {self.guild_id}: resolution cancelled, likely a stop mid-lookup")
                return
            except Exception as e:
                logger.error(f"[play_next] guild {self.guild_id}: failed to resolve source for '{track.title}': {e}")
                raise
            finally:
                self._resolve_task = None

            if not self.voice_client or not self.voice_client.is_connected():
                logger.error(
                    f"[play_next] guild {self.guild_id}: about to play '{track.title}' but "
                    f"voice_client is {self.voice_client!r} (connected="
                    f"{self.voice_client.is_connected() if self.voice_client else None}), "
                    f"this will likely fail"
                )

            self.voice_client.play(audio_source, after=self._make_after_callback())
            self._position_accum = 0.0
            self._segment_start = time.time()
            logger.info(f"[play_next] guild {self.guild_id}: now playing '{track.title}'")
            self.schedule_prewarm()

        if self.on_track_start:
            await self.on_track_start(track)

    def schedule_prewarm(self):
        """
        Schedules stream resolution for the next track in the queue.
        Safe to call at any time (e.g., when new items are added mid-song).
        Identifies if the target track is already being prewarmed to avoid
        pointlessly cancelling and restarting active background tasks.
        """
        # If the queue is empty, ensure any running prewarm task is cleaned up
        if not self.queue.upcoming:
            if self._prewarm_task is not None and not self._prewarm_task.done():
                logger.debug(f"[prewarm] guild {self.guild_id}: queue emptied, cancelling in-flight prewarm")
                self._prewarm_task.cancel()
            self._prewarm_track = None
            return

        next_track = self.queue.upcoming[0]

        # Direct links and already resolved tracks don't need prewarming
        if next_track.source == "direct" or next_track.stream_url:
            if self._prewarm_task is not None and not self._prewarm_task.done():
                self._prewarm_task.cancel()
            self._prewarm_track = None
            return

        # If a prewarm task is active...
        if self._prewarm_task is not None and not self._prewarm_task.done():
            # If it's already working on this exact track, let it keep running!
            if self._prewarm_track == next_track:
                return
            # Otherwise, the track at the top of the queue changed, so cancel the stale task
            logger.debug(
                f"[prewarm] guild {self.guild_id}: next-up track changed to "
                f"'{next_track.title}', cancelling stale prewarm for "
                f"'{self._prewarm_track.title if self._prewarm_track else None}'"
            )
            self._prewarm_task.cancel()

        logger.debug(f"[prewarm] guild {self.guild_id}: scheduling prewarm for '{next_track.title}'")
        self._prewarm_track = next_track
        self._prewarm_task = asyncio.ensure_future(self._prewarm(next_track))

    async def _prewarm(self, track: Track):
        try:
            started = time.time()
            await youtube_source.resolve_stream(track)
            logger.debug(f"[prewarm] guild {self.guild_id}: '{track.title}' prewarmed in {time.time() - started:.2f}s")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # not fatal, play_next just resolves it the normal way when
            # this track's turn actually comes up
            logger.warning(f"[prewarm] guild {self.guild_id}: prewarm failed for '{track.title}': {e}")

    async def _pull_autoplay_track(self) -> Optional[Track]:
        """
        Queue ran dry with autoplay on. Seeds a youtube mix off the last
        track that actually played and grabs a batch from it, filtering
        out anything already played this session so we don't loop back
        onto the same handful of songs. If this batch eventually runs
        out too, whichever track ends up last in history becomes the
        seed for the next pull, so the chain just keeps going.
        """
        if not self.queue.history:
            logger.debug(f"[autoplay] guild {self.guild_id}: nothing's ever played, no seed to work from")
            return None  # nothing's ever played, no seed to work from

        seed = self.queue.history[-1]
        already_played = {t.url for t in self.queue.history}
        if self.queue.current:
            already_played.add(self.queue.current.url)

        mix_tracks = await youtube_source.get_mix_tracks(seed.url, already_played)
        if not mix_tracks:
            logger.warning(f"[autoplay] guild {self.guild_id}: mix came back empty for seed '{seed.title}', giving up for now")
            return None

        self.queue.add_many(mix_tracks)
        logger.info(f"[autoplay] guild {self.guild_id}: added {len(mix_tracks)} mix track(s) seeded from '{seed.title}'")
        return self.queue.next()

    def _make_after_callback(self):
        """
        Builds the after= callback for VoiceClient.play. Centralized here
        so every place that starts playback shares the same logic, and
        the suppress/ignore_loop flags only need to be checked in one spot.
        """
        def _after_playback(error):
            if error:
                logger.error(f"[player] guild {self.guild_id}: playback error reported by discord.py: {error}")

            if self._suppress_auto_advance:
                logger.debug(f"[player] guild {self.guild_id}: after-callback fired but auto-advance is suppressed (manual seek)")
                self._suppress_auto_advance = False
                return

            ignore_loop = self._force_ignore_loop
            self._force_ignore_loop = False

            fut = asyncio.run_coroutine_threadsafe(
                self.play_next(ignore_loop=ignore_loop), self.bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                logger.error(f"[player] guild {self.guild_id}: error advancing queue: {e}")

        return _after_playback

    def pause(self) -> bool:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            # freeze the position we've accumulated so far, the clock
            # stops ticking until resume() starts a new segment
            self._position_accum = self.current_position()
            self._segment_start = None
            logger.debug(f"[player] guild {self.guild_id}: paused at {self._position_accum:.1f}s")
            return True
        return False

    def resume(self) -> bool:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            self._segment_start = time.time()
            logger.debug(f"[player] guild {self.guild_id}: resumed from {self._position_accum:.1f}s")
            return True
        return False

    def stop(self):
        """Stops playback outright, does not touch the queue."""
        if self.voice_client:
            self.voice_client.stop()

    async def stop_and_clear(self):
        """
        Full stop. Halts playback, wipes the queue, and turns off both
        loop flags. Loop settings shouldn't be able to keep the queue
        going after someone explicitly hits stop, that would defeat
        the whole point of the button.

        Also cancels any resolution work play_next might currently be
        doing in the background (a yt-dlp stream lookup, an autoplay
        mix pull), plus any prewarm lookup running ahead for the next
        track. Without cancelling the resolve task, stop only stops
        what's audibly playing right now, and a track that was mid-
        lookup when stop was pressed just starts playing on its own a
        moment later once that lookup finishes, even though the queue
        is supposedly empty. The prewarm task is lower stakes since it
        never starts playback on its own, but no reason to let it keep
        burning a yt-dlp round trip against a queue that just got wiped.
        """
        logger.info(f"[player] guild {self.guild_id}: stop_and_clear requested")
        self._force_ignore_loop = True
        self.queue.loop_current = False
        self.queue.loop_queue = False

        if self._resolve_task is not None and not self._resolve_task.done():
            logger.debug(f"[player] guild {self.guild_id}: cancelling in-flight resolve task")
            self._resolve_task.cancel()

        if self._prewarm_task is not None and not self._prewarm_task.done():
            logger.debug(f"[player] guild {self.guild_id}: cancelling in-flight prewarm task")
            self._prewarm_task.cancel()

        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()

        self.queue.clear()
        self.queue.current = None

    async def skip(self):
        """
        Skips the current track, ignoring track loop. Queue loop is
        untouched, so if the queue is set to loop, skipping the last
        looped track still rolls back around to the start of history
        like normal.
        """
        logger.info(f"[player] guild {self.guild_id}: skip requested")
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self._force_ignore_loop = True
            self.voice_client.stop()
        else:
            await self.play_next(ignore_loop=True)

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
            logger.debug(f"[seek] guild {self.guild_id}: seek requested but nothing is playing")
            return

        new_position = self.current_position() + delta
        new_position = max(0.0, new_position)
        if track.duration_seconds:
            # leave a one second buffer so we don't seek right past the end
            new_position = min(new_position, max(track.duration_seconds - 1, 0))

        logger.info(f"[seek] guild {self.guild_id}: seeking '{track.title}' by {delta:+.0f}s to {new_position:.1f}s")

        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self._suppress_auto_advance = True
            self.voice_client.stop()

        # Seeking reuses the stream url we already resolved instead of
        # asking yt-dlp to re-extract it, that network round trip was
        # what made seeking feel so slow. We only actually need a fresh
        # url when a track first starts playing.
        if track.source == "direct":
            audio_source = await direct_source.get_playable_source(track, start_seconds=new_position)
        else:
            audio_source = youtube_source.get_playable_source_from_cache(track, start_seconds=new_position)

        self.voice_client.play(audio_source, after=self._make_after_callback())
        self._position_accum = new_position
        self._segment_start = time.time()


class PlayerManager:
    """Holds a GuildPlayer per guild, created lazily alongside its queue."""

    def __init__(self, bot, queue_manager):
        self.bot = bot
        self.queue_manager = queue_manager
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self._players:
            logger.debug(f"[player_manager] creating new GuildPlayer for guild {guild_id}")
            queue = self.queue_manager.get(guild_id)
            self._players[guild_id] = GuildPlayer(guild_id, queue, self.bot)
        return self._players[guild_id]
