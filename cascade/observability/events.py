import json
from collections import deque
from datetime import UTC, datetime
from itertools import count
from threading import RLock
from typing import Literal

from cascade.domain.models import Record

StreamEvent = Literal[
    "state.changed",
    "incident.created",
    "incident.updated",
    "recovery.plan.created",
    "approval.required",
    "approval.decided",
    "action.started",
    "action.completed",
    "incident.resolved",
    "preference.changed",
    "heartbeat",
]

# Bounded so a disconnected reader can never grow memory without limit.
MAX_QUEUED = 256
MAX_SUBSCRIBERS = 32


class Notification(Record):
    id: int
    type: StreamEvent
    at: datetime
    world_version: int
    detail: dict

    def encode(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.type}\ndata: {body}\n\n"


class EventStream:
    """Fan-out for in-process notifications. Publishing never blocks the writer.

    Events describe what changed; they carry no authority. A reader still has to
    fetch state through the ordinary endpoints, which keeps versioning honest.
    """

    def __init__(self):
        self.lock = RLock()
        self.sequence = count(1)
        self.subscribers: list[deque[Notification]] = []

    def subscribe(self) -> deque[Notification] | None:
        with self.lock:
            if len(self.subscribers) >= MAX_SUBSCRIBERS:
                return None
            queue: deque[Notification] = deque(maxlen=MAX_QUEUED)
            self.subscribers.append(queue)
            return queue

    def unsubscribe(self, queue: deque[Notification]) -> None:
        with self.lock:
            # `deque.__eq__` compares contents, so an idle queue can compare equal
            # to another subscriber. Remove the exact subscription by identity.
            self.subscribers = [
                candidate for candidate in self.subscribers if candidate is not queue
            ]

    def publish(self, event: StreamEvent, world_version: int, **detail) -> Notification:
        with self.lock:
            notification = Notification(
                id=next(self.sequence),
                type=event,
                at=datetime.now(UTC),
                world_version=world_version,
                detail=detail,
            )
            for queue in self.subscribers:
                # A slow reader loses the oldest notification, never the newest.
                queue.append(notification)
            return notification
