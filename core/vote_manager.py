"""
Vote gating for playback commands. Anyone in the bot's voice channel can
trigger a gated action, but if there's more than one non-bot person in
that channel, everyone gets a shot at a single vote button before the
action actually runs. The public message only ever shows a running
count, never who's voted, that's what keeps it anonymous.

Bypass is a fixed list of user ids read out of the environment at
startup. Whoever hosts the bot is the only one who can change it, since
that means editing .env and restarting rather than running a command
that anyone with the right server role could otherwise use.
"""
import asyncio
from typing import Optional

import discord

import config
from i18n.strings import t
from utils.logger import get_logger

logger = get_logger(__name__)


class VoteView(discord.ui.View):
    """
    One of these gets created per vote. Tracks who's voted (by id, never
    shown anywhere), how many votes are needed, and resolves a future
    once the vote passes or the 30 second timeout runs out.
    """

    def __init__(self, threshold: int, lang: str, result: asyncio.Future, description: str = None):
        super().__init__(timeout=30)
        self.threshold = threshold
        self.lang = lang
        self.voters: set[int] = set()
        self.message: Optional[discord.Message] = None
        self.result = result
        # the "so-and-so wants to do X, vote below" line. kept on the view
        # itself so every rebuild of the embed (a new vote coming in, the
        # timeout firing) can put it back, instead of relying on whoever
        # built the first embed to set it once and every later edit
        # silently dropping it
        self.description = description

    def _build_embed(self, passed: bool = False, failed: bool = False) -> discord.Embed:
        if passed:
            title = t("vote_passed_title", self.lang)
            color = discord.Color.green()
        elif failed:
            title = t("vote_failed_title", self.lang)
            color = discord.Color.red()
        else:
            title = t("vote_in_progress_title", self.lang)
            color = discord.Color.gold()

        embed = discord.Embed(title=title, color=color, description=self.description)
        embed.add_field(name=t("vote_tally_label", self.lang), value=f"{len(self.voters)}/{self.threshold}")
        return embed

    @discord.ui.button(label="Vote", emoji="🗳️", style=discord.ButtonStyle.primary)
    async def cast_vote(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.result.done():
            await interaction.response.send_message(t("vote_already_decided", self.lang), ephemeral=True)
            return

        if interaction.user.id in self.voters:
            await interaction.response.send_message(t("vote_already_cast", self.lang), ephemeral=True)
            return

        self.voters.add(interaction.user.id)
        logger.debug(f"[vote] guild {interaction.guild_id}: vote cast, now {len(self.voters)}/{self.threshold}")
        await interaction.response.send_message(
            t("vote_cast_confirm", self.lang, count=len(self.voters), threshold=self.threshold),
            ephemeral=True,
        )

        passed = len(self.voters) >= self.threshold
        if passed and not self.result.done():
            logger.info(f"[vote] guild {interaction.guild_id}: vote passed with {len(self.voters)}/{self.threshold}")
            self.result.set_result(True)
            for item in self.children:
                item.disabled = True
            self.stop()

        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(passed=passed), view=self)
            except discord.HTTPException as e:
                logger.debug(f"[vote] guild {interaction.guild_id}: failed to edit vote message: {e}")

    async def on_timeout(self):
        if not self.result.done():
            logger.info(f"[vote] guild timed out with {len(self.voters)}/{self.threshold}, vote fails")
            self.result.set_result(False)

        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(embed=self._build_embed(failed=True), view=self)
            except discord.HTTPException as e:
                logger.debug(f"[vote] failed to edit timed-out vote message: {e}")


class VoteManager:
    """
    Holds at most one active vote per guild. If a gated command fires
    while a vote's already running, it just gets told to wait instead of
    stacking a second confusing prompt on top of the first one.
    """

    def __init__(self):
        self._active: dict[int, VoteView] = {}

    def is_bypassed(self, user_id: int) -> bool:
        return user_id in config.VOTE_BYPASS_USER_IDS

    def has_active_vote(self, guild_id: int) -> bool:
        return guild_id in self._active

    async def request(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        action_label: str,
        lang: str,
    ) -> bool:
        """
        Runs the whole vote flow for one gated action and returns whether
        it should go ahead. Handles bypass and "nobody else is around to
        vote" as instant passes, sends the vote prompt itself when a real
        vote is needed, and waits on it. Callers just check the return
        value, they don't need to send any vote-related messages of their
        own, just the eventual result of the action.
        """
        guild_id = interaction.guild_id

        if self.is_bypassed(interaction.user.id):
            logger.debug(f"[vote] guild {guild_id}: {interaction.user.id} is on the bypass list, skipping vote")
            return True

        eligible = [m for m in channel.members if not m.bot]
        if len(eligible) <= 1:
            logger.debug(f"[vote] guild {guild_id}: only {len(eligible)} eligible voter(s) in channel, auto-passing")
            return True

        if guild_id in self._active:
            logger.debug(f"[vote] guild {guild_id}: vote already in progress, rejecting new request for '{action_label}'")
            await interaction.followup.send(t("vote_in_progress", lang, action=action_label))
            return False

        threshold = len(eligible) // 2 + 1
        logger.info(f"[vote] guild {guild_id}: starting vote for '{action_label}', threshold {threshold}/{len(eligible)}")
        result: asyncio.Future = asyncio.get_running_loop().create_future()
        description = t("vote_prompt", lang, action=action_label, user=interaction.user.display_name)
        view = VoteView(threshold, lang, result, description=description)
        self._active[guild_id] = view

        try:
            embed = view._build_embed()
            message = await interaction.followup.send(embed=embed, view=view, wait=True)
            view.message = message

            outcome = await result
            logger.info(f"[vote] guild {guild_id}: vote for '{action_label}' resolved to {outcome}")
            return outcome
        finally:
            del self._active[guild_id]
