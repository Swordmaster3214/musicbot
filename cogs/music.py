"""
This is the only file that talks directly to discord.py's command
framework. Everything else (queue logic, playback, source resolution,
caching) lives in its own module and gets called from here. Keeps this
file focused purely on "user said X, do Y" mapping.
"""
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import config
from core.queue_manager import QueueManager
from core.player import PlayerManager
from db.cache import SongCache
import sources.youtube as youtube_source
import sources.spotify as spotify_source
import sources.direct as direct_source
from utils.helpers import now_playing_embed, queue_embed
from i18n.strings import t, SUPPORTED_LANGUAGES


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
        player = self.cog.player_manager.get(self.guild_id)
        await player.seek_seconds(-10)
        await interaction.response.send_message(t("seeked_back", self.lang, seconds=10), ephemeral=True)

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="btn_pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.player_manager.get(self.guild_id)
        if player.voice_client and player.voice_client.is_paused():
            player.resume()
            await interaction.response.send_message(t("resumed", self.lang), ephemeral=True)
        else:
            player.pause()
            await interaction.response.send_message(t("paused", self.lang), ephemeral=True)

    @discord.ui.button(label="Skip forward 10s", emoji="⏩", style=discord.ButtonStyle.secondary, custom_id="btn_seek_forward")
    async def seek_forward(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.player_manager.get(self.guild_id)
        await player.seek_seconds(10)
        await interaction.response.send_message(t("seeked_forward", self.lang, seconds=10), ephemeral=True)

    @discord.ui.button(label="Skip song", emoji="⏭️", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_skip_song")
    async def skip_song(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.player_manager.get(self.guild_id)
        await player.skip()
        await interaction.response.send_message(t("skipped", self.lang), ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.shuffle()
        await interaction.response.send_message(t("queue_shuffled", self.lang), ephemeral=True)

    @discord.ui.button(label="Loop Track", emoji="🔂", style=discord.ButtonStyle.secondary, row=1, custom_id="btn_loop_track")
    async def loop_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.loop_current = not queue.loop_current
        state = t("state_enabled", self.lang) if queue.loop_current else t("state_disabled", self.lang)
        await interaction.response.send_message(t("track_loop_state", self.lang, state=state), ephemeral=True)

    @discord.ui.button(label="Loop Queue", emoji="🔁", style=discord.ButtonStyle.secondary, row=2, custom_id="btn_loop_queue")
    async def queue_loop_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.loop_queue = not queue.loop_queue
        state = t("state_enabled", self.lang) if queue.loop_queue else t("state_disabled", self.lang)
        await interaction.response.send_message(t("queue_loop_state", self.lang, state=state), ephemeral=True)

    @discord.ui.button(label="Autoplay", emoji="📻", style=discord.ButtonStyle.secondary, row=2, custom_id="btn_autoplay")
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue = self.cog.queue_manager.get(self.guild_id)
        queue.autoplay = not queue.autoplay
        state = t("state_enabled", self.lang) if queue.autoplay else t("state_disabled", self.lang)
        await interaction.response.send_message(t("autoplay_state", self.lang, state=state), ephemeral=True)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=2, custom_id="btn_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.cog.player_manager.get(self.guild_id)
        await player.stop_and_clear()
        await interaction.response.send_message(t("stopped_cleared", self.lang), ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue_manager = QueueManager()
        self.player_manager = PlayerManager(self.bot, self.queue_manager)
        self.cache = SongCache()

        # tracks the one now playing message per guild so we can edit
        # it in place instead of spamming a new message every track
        self.now_playing_messages: dict[int, discord.Message] = {}
        self.now_playing_channels: dict[int, discord.abc.Messageable] = {}

    # ---------- helpers ----------

    def _lang(self, guild_id: int) -> str:
        """Small wrapper so commands don't have to reach into self.cache directly."""
        return self.cache.get_guild_language(guild_id)

    async def _ensure_voice(self, interaction: discord.Interaction) -> bool:
        """Joins the caller's voice channel if we're not already in one."""
        lang = self._lang(interaction.guild_id)

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send(t("err_not_in_voice", lang))
            return False

        guild_id = interaction.guild_id
        player = self.player_manager.get(guild_id)

        if interaction.guild.voice_client:
            player.voice_client = interaction.guild.voice_client

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
                pass
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
                print(f"[music] stale interaction webhook for guild {guild_id}, reposting now playing message")

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
        if player.voice_client and not player.voice_client.is_playing() and not player.voice_client.is_paused():
            await player.play_next()

    # ---------- play / queue commands ----------

    @app_commands.command(name="play", description="Play or queue a song from YouTube, Spotify, or a direct link")
    @app_commands.describe(query="A search term, YouTube link, Spotify link, or direct audio file link")
    async def play(self, interaction: discord.Interaction, query: str):
        lang = self._lang(interaction.guild_id)

        print(f"\n[PLAY] Command invoked by {interaction.user} (Guild ID: {interaction.guild_id})")
        print(f"[PLAY] Query received: '{query}'")

        await interaction.response.defer()

        try:
            if not await self._ensure_voice(interaction):
                print("[PLAY] Voice verification failed (User not in a channel or bot couldn't connect.)")
                return

            guild_id = interaction.guild_id
            queue = self.queue_manager.get(guild_id)

            print(f"[PLAY] Resolving query through source extractors...")
            tracks = await self._resolve_query(query)
            print(f"[PLAY] Resolution complete. Found {len(tracks)} track(s).")

            if not tracks:
                await interaction.followup.send(t("err_no_results_query", lang))
                return

            if config.MAX_QUEUE_SIZE and len(queue) + len(tracks) > config.MAX_QUEUE_SIZE:
                print(f"[PLAY] Aborting: Queue size limit reached")
                await interaction.followup.send(t("err_max_queue", lang, max=config.MAX_QUEUE_SIZE))
                return

            print(f"[PLAY] Enqueuing tracks and evaluating cache eligibility...")
            queue.add_many(tracks)
            for t_ in tracks:
                if t_.source == "direct" and not direct_source.has_metadata(t_):
                    continue  # no metadata, requirement says don't cache these
                self._cache_if_eligible(t_)

            print("[PLAY] Booting playback loop if player is currently idle...")
            await self._start_playback_if_idle(guild_id)
            print("[PLAY] Playback checks finished successfully.")

            if len(tracks) == 1:
                await interaction.followup.send(t("queued_single", lang, title=tracks[0].title))
            else:
                await interaction.followup.send(t("queued_playlist", lang, count=len(tracks)))
        except Exception as e:
            print(f"\n [CRITICAL ERROR] Exception caught in /play execution chain: {e}")
            import traceback
            traceback.print_exc()

            try:
                await interaction.followup.send(t("err_play_generic", lang, error=e))
            except Exception as followup_err:
                print(f"[ERROR] Could not send failure followup message: {followup_err}")

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

        print(f"\n[SHUFFLEPLAY] Command invoked by {interaction.user} with query: '{query}'")
        await interaction.response.defer()

        try:
            if not await self._ensure_voice(interaction):
                return

            guild_id = interaction.guild_id
            queue = self.queue_manager.get(guild_id)

            print("[SHUFFLEPLAY] Resolving playlist query...")
            tracks = await self._resolve_query(query)
            print(f"[SHUFFLEPLAY] Resolved {len(tracks)} tracks.")

            if not tracks:
                await interaction.followup.send(t("err_no_results_playlist", lang))
                return

            queue.add_many(tracks)
            queue.shuffle()
            print("[SHUFFLEPLAY] Queue randomized.")

            for t_ in tracks:
                if t_.source == "direct" and not direct_source.has_metadata(t_):
                    continue
                self._cache_if_eligible(t_)

            print("[SHUFFLEPLAY] Triggering player...")
            await self._start_playback_if_idle(guild_id)
            await interaction.followup.send(t("queued_shuffled", lang, count=len(tracks)))

        except Exception as e:
            print(f"\n[CRITICAL ERROR] Exception caught in /shuffleplay execution chain: {e}")
            import traceback
            traceback.print_exc()
            try:
                await interaction.followup.send(t("err_shuffleplay_generic", lang, error=e))
            except Exception as followup_err:
                print(f"[ERROR] Could not send failure followup message: {followup_err}")

    @app_commands.command(name="shuffle", description="Shuffle the current queue")
    async def shuffle(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        queue.shuffle()
        await interaction.response.send_message(t("queue_shuffled", lang))

    @app_commands.command(name="queue", description="Show the current queue")
    @app_commands.describe(page="Page number, starting at 1")
    async def show_queue(self, interaction: discord.Interaction, page: int = 1):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        embed = queue_embed(queue.upcoming, page=max(page - 1, 0), lang=lang)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearqueue", description="Clear the entire queue")
    async def clear_queue(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        count = len(queue)
        queue.clear()
        await interaction.response.send_message(t("queue_cleared", lang, count=count))

    @app_commands.command(name="remove", description="Remove a specific track from the queue by its position")
    @app_commands.describe(position="Position in the queue, as shown by /queue")
    async def remove(self, interaction: discord.Interaction, position: int):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        track = queue.remove_at(position - 1)
        if track is None:
            await interaction.response.send_message(t("remove_nothing", lang))
        else:
            await interaction.response.send_message(t("removed_track", lang, title=track.title))

    # ---------- playback control ----------

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        if player.pause():
            await interaction.response.send_message(t("paused", lang))
        else:
            await interaction.response.send_message(t("nothing_playing", lang))

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        if player.resume():
            await interaction.response.send_message(t("resumed", lang))
        else:
            await interaction.response.send_message(t("nothing_paused", lang))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        await player.skip()
        await interaction.response.send_message(t("skipped", lang))

    @app_commands.command(name="seekforward", description="Jump forward within the current song")
    @app_commands.describe(seconds="How many seconds to jump ahead, defaults to 10")
    async def seek_forward(self, interaction: discord.Interaction, seconds: int = 10):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        await player.seek_seconds(seconds)
        await interaction.response.send_message(t("seeked_forward", lang, seconds=seconds))

    @app_commands.command(name="seekback", description="Jump backward within the current song")
    @app_commands.describe(seconds="How many seconds to rewind, defaults to 10")
    async def seek_back(self, interaction: discord.Interaction, seconds: int = 10):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        await player.seek_seconds(-seconds)
        await interaction.response.send_message(t("seeked_back", lang, seconds=seconds))

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        await player.stop_and_clear()
        await interaction.response.send_message(t("stopped_cleared", lang))

    @app_commands.command(name="disconnect", description="Disconnect the bot from voice")
    async def disconnect(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        player = self.player_manager.get(interaction.guild_id)
        await player.disconnect()
        self.queue_manager.reset(interaction.guild_id)
        await interaction.response.send_message(t("disconnected", lang))

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
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        queue.loop_current = not queue.loop_current
        state = t("state_enabled", lang) if queue.loop_current else t("state_disabled", lang)
        await interaction.response.send_message(t("track_loop_state", lang, state=state))

    @app_commands.command(name="loopqueue", description="Toggle looping the entire queue")
    async def loop_queue(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        queue.loop_queue = not queue.loop_queue
        state = t("state_enabled", lang) if queue.loop_queue else t("state_disabled", lang)
        await interaction.response.send_message(t("queue_loop_state", lang, state=state))

    @app_commands.command(name="autoplay", description="Toggle autoplay (keeps queuing similar songs when the queue runs dry)")
    async def autoplay(self, interaction: discord.Interaction):
        lang = self._lang(interaction.guild_id)
        queue = self.queue_manager.get(interaction.guild_id)
        queue.autoplay = not queue.autoplay
        state = t("state_enabled", lang) if queue.autoplay else t("state_disabled", lang)
        await interaction.response.send_message(t("autoplay_state", lang, state=state))

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
        # a mix of languages depending on who ran the command
        self.cache.set_guild_language(interaction.guild_id, language.value)
        await interaction.response.send_message(
            t("language_set", language.value, language=language.name)
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
