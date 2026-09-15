"""Cursor-backed mail polling that feeds the existing extraction boundary."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cascade.connectors.mail import MailCursor, MailSource
from cascade.persistence import StateStore
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.service import SemanticService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    processed: int = 0
    skipped: int = 0
    disabled: bool = False
    error_class: str | None = None


class MailWatcher:
    def __init__(
        self,
        source: MailSource,
        semantic: SemanticService,
        store: StateStore,
        *,
        folder: str,
        poll_seconds: int,
    ):
        self.source = source
        self.semantic = semantic
        self.store = store
        self.folder = folder
        self.poll_seconds = poll_seconds
        self._interval = poll_seconds
        self._next_poll = 0.0
        self.last_poll_at: str | None = None
        self.last_error_class: str | None = None
        self.processed_count = 0
        loaded = store.load()
        self._messages = dict(loaded.mail_messages) if loaded else {}
        saved_cursor = loaded.mail_cursors.get(folder) if loaded else None
        self._cursor = (
            MailCursor(int(saved_cursor["uidvalidity"]), int(saved_cursor["last_uid"]))
            if saved_cursor
            else None
        )

    def seconds_until_next_poll(self) -> float:
        return max(0.0, self._next_poll - time.monotonic())

    def status(self) -> dict:
        return {
            "enabled": True,
            "folder": self.folder,
            "last_poll_at": self.last_poll_at,
            "last_error_class": self.last_error_class,
            "processed_count": self.processed_count,
        }

    def poll_once(self) -> PollResult:
        if not self.semantic.privacy.live_inference:
            self.last_poll_at = datetime.now(UTC).isoformat()
            self._next_poll = time.monotonic() + self.poll_seconds
            return PollResult(disabled=True)
        if self.seconds_until_next_poll() > 0:
            return PollResult()
        self.last_poll_at = datetime.now(UTC).isoformat()
        try:
            cursor, messages = self.source.fetch_new(self._cursor, 10)
            processed = skipped = 0
            recorded: list[tuple[str, dict]] = []
            seen = set(self._messages)
            with self.store.transaction():
                for message in messages[:10]:
                    digest = hashlib.sha256(message.message_id.encode()).hexdigest()
                    if digest in seen:
                        skipped += 1
                        continue
                    seen.add(digest)
                    event_id = f"mail_{digest[:24]}"
                    request = NaturalEventRequest(
                        event_id=event_id,
                        expected_version=self.semantic.core.world.version,
                        text=message.text[:12_000],
                        apply=False,
                    )
                    try:
                        result = asyncio.run(self.semantic.extract(request))
                    except Exception as exc:
                        status = "ERROR"
                        logger.error("mail message processing failed: %s", type(exc).__name__)
                    else:
                        status = result.status
                    body = {
                        "status": status,
                        "event_id": event_id,
                        "received_at": message.received_at.astimezone(UTC).isoformat(),
                    }
                    if self.semantic.privacy.persist_event_text:
                        body.update(
                            {
                                "subject": message.subject,
                                "sender": message.sender,
                                "text": message.text,
                            }
                        )
                    self.store.put("mail_message", digest, body)
                    recorded.append((digest, body))
                    processed += 1
                self.store.put(
                    "mail_cursor",
                    self.folder,
                    {"uidvalidity": cursor.uidvalidity, "last_uid": cursor.last_uid},
                )
            self._messages.update(recorded)
            self._cursor = cursor
            self.processed_count += processed
            self.last_error_class = None
            self._interval = self.poll_seconds
            self._next_poll = time.monotonic() + self._interval
            return PollResult(processed=processed, skipped=skipped)
        except Exception as exc:
            self.last_error_class = type(exc).__name__
            logger.error("mail poll failed: %s", type(exc).__name__)
            self._interval = min(900, self._interval * 2)
            self._next_poll = time.monotonic() + self._interval
            return PollResult(error_class=self.last_error_class)
