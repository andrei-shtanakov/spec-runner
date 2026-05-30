"""Event bus for streaming task execution events to TUI and other subscribers."""

import asyncio
import collections
import contextlib
import threading
import time
from dataclasses import dataclass, field


@dataclass
class TaskEvent:
    """A single event from task execution."""

    task_id: str
    event_type: str  # "output_line", "step_started", "step_completed", "error", "info"
    data: str
    timestamp: float = field(default_factory=time.time)


class EventBus:
    """Pub/sub event bus for task execution events.

    Supports two consumption patterns:
    - asyncio.Queue subscribers (for async consumers in the same event loop)
    - Thread-safe recent events buffer (for TUI polling from a different thread)
    """

    def __init__(self, max_queue_size: int = 1000, max_recent: int = 200) -> None:
        self._subscribers: list[asyncio.Queue[TaskEvent]] = []
        self._max_queue_size = max_queue_size
        # Thread-safe buffer for TUI consumption
        self._lock = threading.Lock()
        self._recent: collections.deque[TaskEvent] = collections.deque(maxlen=max_recent)

    def subscribe(self) -> asyncio.Queue[TaskEvent]:
        """Create a new async subscriber queue."""
        q: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[TaskEvent]) -> None:
        """Remove an async subscriber queue."""
        self._subscribers = [q for q in self._subscribers if q is not queue]

    def publish(self, event: TaskEvent) -> None:
        """Publish event to all subscribers and recent buffer."""
        # Async subscribers
        for q in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

        # Thread-safe recent buffer (for TUI)
        with self._lock:
            self._recent.append(event)

    def drain_recent(self) -> list[TaskEvent]:
        """Drain and return all recent events (thread-safe, for TUI polling)."""
        with self._lock:
            events = list(self._recent)
            self._recent.clear()
            return events

    @property
    def subscriber_count(self) -> int:
        """Number of active async subscribers."""
        return len(self._subscribers)
