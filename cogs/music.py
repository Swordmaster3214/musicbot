"""
This is the only file that talks directly to discord.py's command
framework. Everything else (queue logic, playback, source resolution,
caching, voting) lives in its own module and gets called from here.
Keeps this file focused purely on "user said X, do Y" mapping.
"""
import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.queue_manager import QueueManager
from core.player import PlayerManager
from core.vote_manager import VoteManager
from db.cache import SongCache
import sources.youtube as youtube_source
import sources.spotify as spotify_source
import sources.direct as direct_source
from utils.helpers import now_playing_embed, queue_embed, escape_title
from utils.logger import get_logger
from i18n.strings import t, SUPPORTED_LANGUAGES

logger = get_logger(__name__)


class NowPlayingView(discord.ui.View):
    """
    Button row attached to the now playing embed. This is the whole
    reason the UI feels like a normal music bot instead of a pure
    slash command tool, people can just tap a button instead of typing.

    timeout=None keeps the buttons alive indefinitely, they only stop
    working if the bot restarts (the view isn't persisted across a
    restart in this version, a future pass could add that with a
    custom_id based persistent view if it's worth the complexity).

    Buttons carry a custom_id that doubles as its translation key, so
    the labels can be relabeled in __init__ based on the guild's
    language without touching the decorators themselves.

    Every action here runs through the cog's vote gate first, the same
    as its slash command equivalent, so a button tap can't skip the
    vote (or the ownership bypass) that typing the command would have
    used.
    """

    def __init__(self, cog: "Music", guild_id: int, lang: str = "en"):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.lang = lang

        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id:
                item.label = t(item.custom_id, lang)

    @discord.ui.button(label="Rewind 10s", emoji="⏪", style=discord.ButtonStyle.secondary, custom_id="btn_seek_back")
    async def seek_back(self, interaction: discord.Interaction, button: discord.ui.Button):
        owns = self.cog._owns_current_track(self.guild_id, interaction.user.id)
        if not await self.cog._gate(interaction, self.guild_id, "action_seek_back", self.lang, owns_target=owns):
            return
        player = self.cog.player_manager.get(self.guild_id)
        await player.seek_seconds(-10)
        await interaction.followup.send(t("seeked_back_by", self.lang, seconds=10, user=interaction.user.display_name))

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="btn_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.player_manager.get(self.guild_id)
        is_paused = bool(player.voice_client and player.voice_client.is_paused())
        action_key = "action_resume" if is_paused else "action_pause"
        owns = self.cog._owns_current_track(self.guild_id, interaction.user.id)
        if not await self.cog._gate(interaction, self.guild_id, action_key, self.lang, owns_target=owns):
            return

        if is_paused:
            player.resume()
            await interaction.followup.send(t("resumed_by", self.lang, user=interaction.user.display_name))
        else:
            player.pause()
            await interaction.followup.send(t("paused_by", self.lang, user=interaction.user.display_name))

    @discord.ui.button(label="Skip forward 10s", emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="btn_seek_forward")
    async def seek_forward(self, interaction: discord.Interaction, button: discord.ui.Button):
        owns = self.cog._owns_current_track(self.guild_id, interaction.user.id)
        if not await self.cog._gate(interaction, self.guild_id, "action_seek_forward", self.lang, owns_target=owns):
            return
        player = self.cog.player_manager.get(self.guild_id)
        await player.seek_seconds(10)
        await interaction.followup.send(t("seeked_forward_by", self.lang, seconds=10, user=interaction.user.display_name))

    @discord.ui.button(label="Skip song", emoji="⏭️", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_skip_song")
    async def skip_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        owns = self.cog._owns_current_track(self.guild_id, interaction.user.id)
        if not await self.cog._gate(interaction, self.guild_id, "action_skip", self.lang, owns_target=owns):
            return
        player = self.cog.player_manager.get(self.guild_id)
        await player.skip()
        await interaction.followup.send(t("skipped_by", self.lang, user=interaction.user.display_name))

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        # touches every track in the queue, not just one person's, so
        # ownership doesn't factor in here
        if not await self.cog._gate(interaction, self.guild_id, "action_shuffle", self.lang):
            return
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.shuffle()
        self.cog.player_manager.get(self.guild_id).schedule_prewarm()
        await interaction.followup.send(t("queue_shuffled_by", self.lang, user=interaction.user.display_name))

    @discord.ui.button(label="Loop Track", emoji="🔂", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_loop_track")
    async def loop_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        owns = self.cog._owns_current_track(self.guild_id, interaction.user.id)
        if not await self.cog._gate(interaction, self.guild_id, "action_loop", self.lang, owns_target=owns):
            return
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.loop_current = not queue.loop_current
        state = t("state_enabled", self.lang) if queue.loop_current else t("state_disabled", self.lang)
        await interaction.followup.send(t("track_loop_state_by", self.lang, state=state, user=interaction.user.display_name))

    @discord.ui.button(label="Loop Queue", emoji="🔁", style=discord.ButtonStyle.secondary, row=2, custom_id="btn_loop_queue")
    async def queue_loop_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        # affects the whole queue's replay behavior, not a single track
        if not await self.cog._gate(interaction, self.guild_id, "action_loopqueue", self.lang):
            return
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.loop_queue = not queue.loop_queue
        state = t("state_enabled", self.lang) if queue.loop_queue else t("state_disabled", self.lang)
        await interaction.followup.send(t("queue_loop_state_by", self.lang, state=state, user=interaction.user.display_name))

    @discord.ui.button(label="Autoplay", emoji="📻", style=discord.ButtonStyle.secondary, row=2, custom_id="btn_autoplay")
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.cog._gate(interaction, self.guild_id, "action_autoplay", self.lang):
            return
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.autoplay = not queue.autoplay
        state = t("state_enabled", self.lang) if queue.autoplay else t("state_disabled", self.lang)
        await interaction.followup.send(t("autoplay_state_by", self.lang, state=state, user=interaction.user.display_name))

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=2, custom_id="btn_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # clears everyone's tracks, not just the caller's, so no
        # ownership bypass here either
        if not await self.cog._gate(interaction, self.guild_id, "action_stop", self.lang):
            return
        player = self.cog.player_manager.get(self.guild_id)
        await player.stop_and_clear()
        await interaction.followup.send(t("stopped_cleared_by", self.lang, user=interaction.user.display_name))


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue_manager = QueueManager()
        self.player_manager = PlayerManager(self.bot, self.queue_manager)
        self.cache = SongCache()
        self.vote_manager = VoteManager()

        # tracks the one now playing message per guild so we can edit
        # it in place instead of spamming a new message every track
        self.now_playing_messages: dict[int, discord.Message] = {}
        self.now_playing_channels: dict[int, discord.abc.Messageable] = {}

    # ---------- helpers ----------

    def _lang(self, guild_id: int) -> str:
        """Small wrapper so commands don't have to reach into self.cache directly."""
        return self.cache.get_guild_language(guild_id)

    def _owns_current_track(self, guild_id: int, user_id: int) -> bool:
        """
        Whether user_id is the one who originally queued whatever's
        currently playing. Used to let someone skip/pause/seek/loop
        their own song without kicking off a vote. Tracks the bot
        pulled in itself (autoplay) have no owner, so this is always
        False for those, which is what we want, nobody should get to
        skip a song nobody actually asked for without a vote either.
        """
        queue = self.queue_manager.get(guild_id)
        if queue.current is None:
            return False
        return queue.current.requested_by == user_id

    def _stamp_requester(self, tracks: list, user_id: int):
        """Marks every track in the list as queued by user_id, regardless of which source module built it."""
        for track in tracks:
            track.requested_by = user_id

    async def _gate(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        action_key: str,
        lang: str,
        owns_target: bool = False,
    ) -> bool:
        """
        Runs the vote flow for a gated command or button. Defers the
        interaction if it hasn't already been acknowledged, so both the
        vote prompt (if one's needed) and the eventual result message go
        out as followups no matter which path the caller takes.

        owns_target skips the vote outright, same as being on the
        bypass list, it's what lets someone act on their own queued
        song without waiting on anyone else. Checked before the bypass
        list itself even gets consulted, since it's the more common
        case day to day.

        Returns True if the caller should go ahead and perform the
        action, False if a vote failed, timed out, or one's already
        running. The vote manager handles telling the user about a
        failure or an already-running vote on its own, so callers that
        get False back just need to stop, no extra messaging needed.
        """
        if not interaction.response.is_done():
            await interaction.response.defer()

        if owns_target:
            logger.debug(f"[gate] guild {guild_id}: {interaction.user.id} owns the target track, skipping vote for '{action_key}'")
            return True

        player = self.player_manager.get(guild_id)
        channel = player.voice_client.channel if player.voice_client else None
        if channel is None and interaction.user.voice:
            channel = interaction.user.voice.channel

        if channel is None:
            logger.debug(f"[gate] guild {guild_id}: no resolvable voice channel for gating '{action_key}'")
            await interaction.followup.send(t("err_not_in_voice", lang))
            return False

        action_label = t(action_key, lang)
        return await self.vote_manager.request(interaction, channel, action_label, lang)

    async def _ensure_voice(self, interaction: discord.Interaction) -> bool:
        """Joins the caller's voice channel if we're not already in one."""
        lang = self._lang(interaction.guild_id)

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send(t("err_not_in_voice", lang))
            return False

        guild_id = interaction.guild_id
        player = self.player_manager.get(guild_id)

        # discord.py's own record of the guild's voice client can go
        # stale (gateway session dropped and never got cleaned up,
        # etc), and adopting it blindly here is the root of the "bot
        # thinks it's already connected but isn't" symptom. logging
        # both sides of this before we act on it is what lets us catch
        # a real mismatch the next time it happens.
        discord_side_vc = interaction.guild.voice_client
        logger.debug(
            f"[ensure_voice] guild {guild_id}: discord.py voice_client={discord_side_vc!r} "
            f"(connected={discord_side_vc.is_connected() if discord_side_vc else None}) "
            f"our tracked voice_client={player.voice_client!r} "
            f"user requesting channel={interaction.user.voice.channel.id}"
        )

        if discord_side_vc:
            player.voice_client = discord_side_vc

        await player.connect(interaction.user.voice.channel)

        # remember where to post/update the now playing message, and
        # hook up the callback so the player can drive that update
        self.now_playing_channels[guild_id] = interaction.channel
        player.on_track_start = lambda track, gid=guild_id: self._on_track_start(gid, track)
        return True

    async def _on_track_start(self, guild_id: int, track):
        """
        Fired by the player whenever a new track begins. Posts a fresh
        now playing message the first time, then edits that same
        message in place for every track after that so the channel
        doesn't fill up with one message per song.
        """
        channel = self.now_playing_channels.get(guild_id)
        if channel is None:
            logger.debug(f"[now_playing] guild {guild_id}: no channel on record, can't post now playing message")
            return

        lang = self._lang(guild_id)
        embed = now_playing_embed(track, lang=lang)
        view = NowPlayingView(self, guild_id, lang=lang)

        existing = self.now_playing_messages.get(guild_id)
        if existing is not None:
            try:
                await existing.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                # message got deleted out from under us, just send a new one
                logger.debug(f"[now_playing] guild {guild_id}: existing message was deleted, sending a fresh one")
            except discord.HTTPException as e:
                # covers the interaction webhook token expiring (401,
                # code 50027) on a message that was originally created
                # via interaction.original_response() in /nowplaying.
                # that object edits through the interaction's webhook,
                # which only lives about 15 minutes, so a long enough
                # session eventually hits this. just fall through and
                # send a fresh plain message instead of crashing playback
                if e.code != 50027:
                    raise
                logger.info(f"[now_playing] guild {guild_id}: stale interaction webhook, reposting now playing message")

        message = await channel.send(embed=embed, view=view)
        self.now_playing_messages[guild_id] = message

    def _cache_if_eligible(self, track):
        """
        Only caches tracks that came with real metadata, per the
        requirement. Youtube/spotify resolved tracks always have a
        title so those get cached, direct links only get cached when
        tags were actually found (checked before this is called).
        """
        self.cache.add(
            title=track.title,
            url=track.url,
            source=track.source,
            artist=track.artist,
            duration_seconds=track.duration_seconds,
        )

    async def _start_playback_if_idle(self, guild_id: int):
        player = self.player_manager.get(guild_id)
        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            # The player is busy, meaning songs were added mid-track.
            # Trigger a prewarm evaluation for the new track(s).
            player.schedule_prewarm()
        else:
            await player.play_next()

    # ---------- play / queue commands ----------

    @app_commands.command(name="play", description="Play or queue a song from YouTube, Spotify, or a direct link")
    @app_commands.describe(query="A search term, YouTube link, Spotify link, or direct audio file link")
    async def play(self, interaction: discord.Interaction, query: str):
        lang = self._lang(interaction.guild_id)

        logger.info(f"[play] invoked by {interaction.user} (guild {interaction.guild_id}), query: '{query}'")

        await interaction.response.defer()

        try:
            if not await self._ensure_voice(interaction):
                logger.debug("[play] voice verification failed (user not in a channel or bot couldn't connect)")
                return

            guild_id = interaction.guild_id
            queue = self.queue_manager.get(guild_id)

            logger.debug("[play] resolving query through source extractors...")
            tracks = await self._resolve_query(query)
            logger.info(f"[play] resolution complete, found {len(tracks)} track(s)")

            if not tracks:
                await interaction.followup.send(t("err_no_results_query", lang))
                return

            if config.MAX_QUEUE_SIZE and len(queue) + len(tracks) > config.MAX_QUEUE_SIZE:
                logger.debug("[play] aborting, queue size limit reached")
                await interaction.followup.send(t("err_max_queue", lang, max=config.MAX_QUEUE_SIZE))
                return

            self._stamp_requester(tracks, interaction.user.id)

            queue.add_many(tracks)
            for t_ in tracks:
                if t_.source == "direct" and not direct_source.has_metadata(t_):
                    continue  # no metadata, requirement says don't cache these
                self._cache_if_eligible(t_)

            await self._start_playback_if_idle(guild_id)

            if len(tracks) == 1:
                await interaction.followup.send(t("queued_single", lang, title=escape_title(tracks[0].title)))
            else:
                await interaction.followup.send(t("queued_playlist", lang, count=len(tracks)))
        except youtube_source.AgeRestrictedError:
            # this one gets its own message instead of falling into the
            # generic handler below, since "no results" is misleading
            # when we actually found the video and just can't play it
            # without a signed-in, age-verified account
            logger.warning(f"[play] age-restricted video blocked the request for query: '{query}'")
            await interaction.followup.send(t("err_age_restricted", lang))
        except Exception as e:
            logger.exception(f"[play] exception in /play execution chain for query '{query}': {e}")

            try:
                await interaction.followup.send(t("err_play_generic", lang, error=e))
            except Exception as followup_err:
                logger.error(f"[play] could not send failure followup message: {followup_err}")

    async def _resolve_query(self, query: str):
        if spotify_source.is_spotify_link(query):
            return await spotify_source.resolve_spotify_link(query)
        if direct_source.is_direct_audio_link(query):
            return [await direct_source.resolve_direct_link(query)]

        # check the cache first for plain search terms, saves a yt-dlp round trip
        cached = self.cache.exact_match(query)
        if cached:
            # re-resolve stream freshness happens later at playback time,
            # this just saves us a search
            return await youtube_source.search_or_resolve(cached.url)

        return await youtube_source.search_or_resolve(query)

    @app_commands.command(name="shuffleplay", description="Play a playlist link with the queue shuffled")
    @app_commands.describe(query="A YouTube or Spotify playlist link")
    async def shuffleplay(self, interaction: discord.Interaction, query: str):
        lang = self._lang(interaction.guild_id)

        logger.info(f"[shuffleplay] invoked by {interaction.user} with query: '{query}'")
        await interaction.response.defer()

        try:
            if not await self._ensure_voice(interaction):
                return

            guild_id = interaction.guild_id
            queue = self.queue_manager.get(guild_id)

            tracks = await self._resolve_query(query)
            logger.info(f"[shuffleplay] resolved {len(tracks)} track(s)")

            if not tracks:
                await interaction.followup.send(t("err_no_results_playlist", lang))
                return

            self._stamp_requester(tracks, interaction.user.id)

            queue.add_many(tracks)
            queue.shuffle()

            for t_ in tracks:
                if t_.source == "direct" and not direct_source.has_metadata(t_):
                    continue
                self._cache_if_eligible(t_)

            await self._start_playback_if_idle(guild_id)
            await interaction.followup.send(t("queued_shuffled", lang, count=len(tracks)))

        except youtube_source.AgeRestrictedError:
            logger.warning(f"[shuffleplay] age-restricted video blocked the request for query: '{query}'")
            await interaction.followup.send(t("err_age_restricted", lang))
        except Exception as e:
            logger.exception(f"[shuffleplay] exception in /shuffleplay execution chain for query '{query}': {e}")
            try:
                await interaction.followup.send(t("err_shuffleplay_generic", lang, error=e))
            except Exception as followup_err:
                logger.error(f"[shuffleplay] could not send failure followup message: {followup_err}")

    @app_commands.command(name="shuffle", description="Shuffle the current queue")
    async def shuffle(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # reorders everyone's tracks, so no ownership bypass
        if not await self._gate(interaction, guild_id, "action_shuffle", lang):
            return
        queue = self.queue_manager.get(guild_id)
        queue.shuffle()
        self.player_manager.get(guild_id).schedule_prewarm()
        await interaction.followup.send(t("queue_shuffled", lang))

    @app_commands.command(name="queue", description="Show the current queue")
    @app_commands.describe(page="Page number, starting at 1")
    async def show_queue(self, interaction: discord.Interaction, page: int = 1):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        embed = queue_embed(queue.upcoming, page=max(page - 1, 0), lang=lang)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearqueue", description="Clear the entire queue")
    async def clear_queue(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # wipes everyone's tracks, so no ownership bypass
        if not await self._gate(interaction, guild_id, "action_clearqueue", lang):
            return
        queue = self.queue_manager.get(guild_id)
        count = len(queue)
        queue.clear()
        self.player_manager.get(guild_id).schedule_prewarm()
        await interaction.followup.send(t("queue_cleared", lang, count=count))

    @app_commands.command(name="remove", description="Remove a specific track from the queue by its position")
    @app_commands.describe(position="Position in the queue, as shown by /queue")
    async def remove(self, interaction: discord.Interaction, position: int):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)

        # ownership here is scoped to the specific track being removed,
        # not whatever's currently playing, so we peek at it before the
        # vote gate instead of popping it off first
        queue = self.queue_manager.get(guild_id)
        idx = position - 1
        target = queue.upcoming[idx] if 0 <= idx < len(queue.upcoming) else None
        owns = target is not None and target.requested_by == interaction.user.id

        if not await self._gate(interaction, guild_id, "action_remove", lang, owns_target=owns):
            return

        track = queue.remove_at(idx)
        if track is None:
            await interaction.followup.send(t("remove_nothing", lang))
        else:
            self.player_manager.get(guild_id).schedule_prewarm()
            await interaction.followup.send(t("removed_track", lang, title=escape_title(track.title)))

    # ---------- playback control ----------

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_pause", lang, owns_target=owns):
            return
        player = self.player_manager.get(guild_id)
        if player.pause():
            await interaction.followup.send(t("paused", lang))
        else:
            await interaction.followup.send(t("nothing_playing", lang))

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_resume", lang, owns_target=owns):
            return
        player = self.player_manager.get(guild_id)
        if player.resume():
            await interaction.followup.send(t("resumed", lang))
        else:
            await interaction.followup.send(t("nothing_paused", lang))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_skip", lang, owns_target=owns):
            return
        player = self.player_manager.get(guild_id)
        await player.skip()
        await interaction.followup.send(t("skipped", lang))

    @app_commands.command(name="seekforward", description="Jump forward within the current song")
    @app_commands.describe(seconds="How many seconds to jump ahead, defaults to 10")
    async def seek_forward(self, interaction: discord.Interaction, seconds: int = 10):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_seek_forward", lang, owns_target=owns):
            return
        player = self.player_manager.get(guild_id)
        await player.seek_seconds(seconds)
        await interaction.followup.send(t("seeked_forward", lang, seconds=seconds))

    @app_commands.command(name="seekback", description="Jump backward within the current song")
    @app_commands.describe(seconds="How many seconds to rewind, defaults to 10")
    async def seek_back(self, interaction: discord.Interaction, seconds: int = 10):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_seek_back", lang, owns_target=owns):
            return
        player = self.player_manager.get(guild_id)
        await player.seek_seconds(-seconds)
        await interaction.followup.send(t("seeked_back", lang, seconds=seconds))

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # ends the session for everyone, so no ownership bypass
        if not await self._gate(interaction, guild_id, "action_stop", lang):
            return
        player = self.player_manager.get(guild_id)
        await player.stop_and_clear()
        await interaction.followup.send(t("stopped_cleared", lang))

    @app_commands.command(name="disconnect", description="Disconnect the bot from voice")
    async def disconnect(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # ends the session for everyone, so no ownership bypass
        if not await self._gate(interaction, guild_id, "action_disconnect", lang):
            return
        player = self.player_manager.get(guild_id)
        await player.disconnect()
        self.queue_manager.reset(guild_id)
        await interaction.followup.send(t("disconnected", lang))

    @app_commands.command(name="nowplaying", description="Show the currently playing track")
    async def now_playing(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        if queue.current is None:
            await interaction.response.send_message(t("nothing_playing_now", lang))
            return

        guild_id = interaction.guild_id
        embed = now_playing_embed(queue.current, lang=lang)
        view = NowPlayingView(self, interaction.guild_id, lang=lang)

        self.now_playing_channels[guild_id] = interaction.channel

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        # interaction.original_response() hands back an InteractionMessage,
        # and editing that goes through the interaction's own webhook
        # instead of a normal channel message edit. that webhook token
        # only lasts about 15 minutes, so if we hang onto this object and
        # edit it later from _on_track_start, it works fine for a while
        # and then starts throwing 401 Invalid Webhook Token once the
        # token expires. re-fetching it as a plain channel message here
        # detaches it from the interaction so it can be edited indefinitely.
        message = await interaction.channel.fetch_message(message.id)

        self.now_playing_messages[guild_id] = message

    @app_commands.command(name="loop", description="Toggle looping the current track")
    async def loop(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        owns = self._owns_current_track(guild_id, interaction.user.id)
        if not await self._gate(interaction, guild_id, "action_loop", lang, owns_target=owns):
            return
        queue = self.queue_manager.get(guild_id)
        queue.loop_current = not queue.loop_current
        state = t("state_enabled", lang) if queue.loop_current else t("state_disabled", lang)
        await interaction.followup.send(t("track_loop_state", lang, state=state))

    @app_commands.command(name="loopqueue", description="Toggle looping the entire queue")
    async def loop_queue(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # affects the whole queue's replay behavior, not a single track
        if not await self._gate(interaction, guild_id, "action_loopqueue", lang):
            return
        queue = self.queue_manager.get(guild_id)
        queue.loop_queue = not queue.loop_queue
        state = t("state_enabled", lang) if queue.loop_queue else t("state_disabled", lang)
        await interaction.followup.send(t("queue_loop_state", lang, state=state))

    @app_commands.command(name="autoplay", description="Toggle autoplay (keeps queuing similar songs when the queue runs dry)")
    async def autoplay(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        lang = self._lang(guild_id)
        # a mode switch for the whole session, not a single track
        if not await self._gate(interaction, guild_id, "action_autoplay", lang):
            return
        queue = self.queue_manager.get(guild_id)
        queue.autoplay = not queue.autoplay
        state = t("state_enabled", lang) if queue.autoplay else t("state_disabled", lang)
        await interaction.followup.send(t("autoplay_state", lang, state=state))

    # ---------- cache search ----------

    @app_commands.command(name="findcached", description="Search previously played songs by title")
    @app_commands.describe(query="Part of a song title to search for")
    async def find_cached(self, interaction: discord.Interaction, query: str):
        lang = self._lang(interaction.guild_id)
        await interaction.response.defer()
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, self.cache.fuzzy_search, query, 10)

        if not results:
            await interaction.followup.send(t("no_cached_matches", lang))
            return

        lines = [f"**{r.title}**" + (f" — {r.artist}" if r.artist else "") for r in results]
        await interaction.followup.send("\n".join(lines))

    # ---------- settings ----------

    @app_commands.command(name="language", description="Set the bot's response language for this server")
    @app_commands.describe(language="Language to use for bot responses")
    @app_commands.choices(language=[
        app_commands.Choice(name=name, value=code)
        for code, name in SUPPORTED_LANGUAGES.items()
    ])
    async def language(self, interaction: discord.Interaction, language: app_commands.Choice[str]):
        # this is a per-server setting on purpose, so the whole group
        # gets a consistent experience instead of every message being
        # a mix of languages depending on who ran the command last
        self.cache.set_guild_language(interaction.guild_id, language.value)
        await interaction.response.send_message(
            t("language_set", language.value, language=language.name)
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
