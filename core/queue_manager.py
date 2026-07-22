"""
One queue per guild, so friend group A's session never touches friend
group B's. History is tracked too, that's what makes 'skip back'
possible instead of just being a one-way queue.
"""
import random
from dataclasses import dataclass, field
from typing import Optional

from sources.youtube import Track
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class GuildQueue:
    upcoming: list[Track] = field(default_factory=list)
    history: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    loop_current: bool = False
    loop_queue: bool = False
    autoplay: bool = False

    def add(self, track: Track):
        self.upcoming.append(track)
        logger.debug(f"[queue] added '{track.title}', upcoming now {len(self.upcoming)}")

    def add_many(self, tracks: list[Track]):
        self.upcoming.extend(tracks)
        logger.debug(f"[queue] added {len(tracks)} track(s), upcoming now {len(self.upcoming)}")

    def shuffle(self):
        random.shuffle(self.upcoming)
        logger.debug(f"[queue] shuffled {len(self.upcoming)} track(s)")

    def clear(self):
        count = len(self.upcoming)
        self.upcoming.clear()
        logger.debug(f"[queue] cleared {count} track(s)")

    def next(self, ignore_loop: bool = False) -> Optional[Track]:
        """Advances the queue, pushes the current track into history."""
        if self.current is not None:
            self.history.append(self.current)

        if self.loop_current and not ignore_loop and self.current is not None:
            logger.debug(f"[queue] track loop active, replaying '{self.current.title}'")
            return self.current

        if not self.upcoming:
            if self.loop_queue and self.history:
                logger.debug(f"[queue] queue loop active, refilling upcoming from {len(self.history)} track(s) of history")
                self.upcoming = self.history.copy()
                self.history.clear()
            else:
                self.current = None
                return None

        self.current = self.upcoming.pop(0)
        logger.debug(f"[queue] advanced to '{self.current.title}', {len(self.upcoming)} left upcoming")
        return self.current

    def previous(self) -> Optional[Track]:
        """Steps back one track using history."""
        if not self.history:
            return None
        if self.current is not None:
            self.upcoming.insert(0, self.current)
        self.current = self.history.pop()
        logger.debug(f"[queue] stepped back to '{self.current.title}'")
        return self.current

    def skip_forward(self, count: int) -> Optional[Track]:
        """Skips ahead by `count` tracks, dropping the ones in between."""
        for _ in range(max(count - 1, 0)):
            if self.upcoming:
                self.history.append(self.upcoming.pop(0))
            else:
                break
        return self.next()

    def skip_backward(self, count: int) -> Optional[Track]:
        """Goes back `count` tracks in history."""
        track = None
        for _ in range(count):
            track = self.previous()
            if track is None:
                break
        return track

    def remove_at(self, index: int) -> Optional[Track]:
        if 0 <= index < len(self.upcoming):
            track = self.upcoming.pop(index)
            logger.debug(f"[queue] removed '{track.title}' at index {index}")
            return track
        logger.debug(f"[queue] remove_at({index}) out of range, upcoming has {len(self.upcoming)}")
        return None

    def __len__(self):
        return len(self.upcoming)


class QueueManager:
    """Holds a GuildQueue per guild id, created lazily."""

    def __init__(self):
        self._queues: dict[int, GuildQueue] = {}

    def get(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._queues:
            logger.debug(f"[queue_manager] creating new GuildQueue for guild {guild_id}")
            self._queues[guild_id] = GuildQueue()
        return self._queues[guild_id]

    def reset(self, guild_id: int):
        """
        Mutates the existing GuildQueue in place rather than replacing
        it in the dict. GuildPlayer holds a direct reference to the
        GuildQueue object it was built with, so swapping in a brand new
        one here would leave the player pointed at a stale, orphaned
        queue that nothing ever touches again.
        """
        if guild_id in self._queues:
            logger.debug(f"[queue_manager] resetting existing GuildQueue for guild {guild_id} in place")
            queue = self._queues[guild_id]
            queue.upcoming.clear()
            queue.history.clear()
            queue.current = None
            queue.loop_current = False
            queue.loop_queue = False
            queue.autoplay = False
        else:
            logger.debug(f"[queue_manager] reset called for guild {guild_id} with no existing queue, creating one")
            self._queues[guild_id] = GuildQueue()
