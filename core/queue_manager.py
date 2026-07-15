"""
One queue per guild, so friend group A's session never touches friend
group B's. History is tracked too, that's what makes 'skip back'
possible instead of just being a one-way queue.
"""
import random
from dataclasses import dataclass, field
from typing import Optional

from sources.youtube import Track


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

    def add_many(self, tracks: list[Track]):
        self.upcoming.extend(tracks)

    def shuffle(self):
        random.shuffle(self.upcoming)

    def clear(self):
        self.upcoming.clear()

    def next(self, ignore_loop: bool = False) -> Optional[Track]:
        """Advances the queue, pushes the current track into history."""
        if self.current is not None:
            self.history.append(self.current)

        if self.loop_current and not ignore_loop and self.current is not None:
            return self.current

        if not self.upcoming:
            if self.loop_queue and self.history:
                self.upcoming = self.history.copy()
                self.history.clear()
            else:
                self.current = None
                return None

        self.current = self.upcoming.pop(0)
        return self.current

    def previous(self) -> Optional[Track]:
        """Steps back one track using history."""
        if not self.history:
            return None
        if self.current is not None:
            self.upcoming.insert(0, self.current)
        self.current = self.history.pop()
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
            return self.upcoming.pop(index)
        return None

    def __len__(self):
        return len(self.upcoming)


class QueueManager:
    """Holds a GuildQueue per guild id, created lazily."""

    def __init__(self):
        self._queues: dict[int, GuildQueue] = {}

    def get(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._queues:
            self._queues[guild_id] = GuildQueue()
        return self._queues[guild_id]

    def reset(self, guild_id: int):
        if guild_id in self._queues:
            queue = self._queues[guild_id]
            queue.upcoming.clear()
            queue.history.clear()
            queue.current = None
            queue.loop_current = False
            queue.loop_queue = False
            queue.autoplay = False
        else:
            self._queues[guild_id] = GuildQueue()
