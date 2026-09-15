"""Cursor-backed mail polling that feeds the existing extraction boundary."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cascade.connectors.mail import MailCursor, MailFetchError, MailSource
from cascade.persistence import StateStore
from cascade.reasoning.models import NaturalEventRequest
from cascade.reasoning.nebius import ReasoningError
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
        # Poison tracking is intentionally process-local. A permanent malformed
        # message is skipped only after three consecutive attempts; transient
        # provider failures keep the cursor parked for retry.
        self._poison_failures: dict[tuple[int, int], int] = {}
        saved_cursor = loaded.mail_cursors.get(folder) if loaded else None
        self._cursor = (
            MailCursor(int(saved_cursor["uidvalidity"]), int(saved_cursor["last_uid"]))
            if saved_cursor
            else None
        )

    def seconds_until_next_poll(self) -> float:
        return max(0.0, self._next_poll - time.monotonic())

    def status(self) -> dict:
        recent = [
            {
                "extraction_id": body.get("extraction_id"),
                "status": body.get("status"),
                "received_at": body.get("received_at"),
            }
            for body in list(self._messages.values())[-20:]
        ]
        return {
            "enabled": True,
            "folder": self.folder,
            "last_poll_at": self.last_poll_at,
            "last_error_class": self.last_error_class,
            "processed_count": self.processed_count,
            "recent": recent,
        }

    def _failure(self, exc: Exception, processed: int = 0, skipped: int = 0) -> PollResult:
        self.processed_count += processed
        self.last_error_class = type(exc).__name__
        logger.error("mail poll failed: %s", type(exc).__name__)
        self._interval = min(900, self._interval * 2)
        self._next_poll = time.monotonic() + self._interval
        return PollResult(
            processed=processed,
            skipped=skipped,
            error_class=self.last_error_class,
        )

    def _checkpoint(self, uidvalidity: int, uid: int) -> MailCursor:
        current = self._cursor
        if current is None or current.uidvalidity != uidvalidity:
            return MailCursor(uidvalidity, uid)
        return MailCursor(uidvalidity, max(current.last_uid, uid))

    def _persist_cursor(self, cursor: MailCursor) -> None:
        with self.store.transaction():
            self.store.put(
                "mail_cursor",
                self.folder,
                {"uidvalidity": cursor.uidvalidity, "last_uid": cursor.last_uid},
            )

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        return isinstance(exc, (ReasoningError, TimeoutError, ConnectionError, OSError))

    def _record_failed_message(self, message, digest: str, checkpoint: MailCursor) -> None:
        body = {
            "status": "FAILED",
            "event_id": f"mail_{digest[:24]}",
            "received_at": message.received_at.astimezone(UTC).isoformat(),
            "error_class": "permanent_extraction_failure",
        }
        with self.store.transaction():
            self.store.put("mail_message", digest, body)
            self.store.put(
                "mail_cursor",
                self.folder,
                {"uidvalidity": checkpoint.uidvalidity, "last_uid": checkpoint.last_uid},
            )
        self._messages[digest] = body
        self._cursor = checkpoint

    async def poll_once(self) -> PollResult:
        if not self.semantic.privacy.live_inference:
            self.last_poll_at = datetime.now(UTC).isoformat()
            self._next_poll = time.monotonic() + self.poll_seconds
            return PollResult(disabled=True)
        if self.seconds_until_next_poll() > 0:
            return PollResult()
        self.last_poll_at = datetime.now(UTC).isoformat()

        fetch_error: MailFetchError | None = None
        try:
            cursor, messages = await asyncio.to_thread(self.source.fetch_new, self._cursor, 10)
        except MailFetchError as exc:
            cursor, messages, fetch_error = exc.cursor, exc.messages, exc
        except Exception as exc:
            return self._failure(exc)

        processed = skipped = 0
        try:
            seen = set(self._messages)
            for message in sorted(messages[:10], key=lambda item: item.uid):
                digest = hashlib.sha256(message.message_id.encode()).hexdigest()
                checkpoint = self._checkpoint(cursor.uidvalidity, message.uid)
                if digest in seen:
                    self._persist_cursor(checkpoint)
                    self._cursor = checkpoint
                    skipped += 1
                    continue
                event_id = f"mail_{digest[:24]}"
                request = NaturalEventRequest(
                    event_id=event_id,
                    expected_version=self.semantic.core.world.version,
                    text=message.text[:12_000],
                    apply=False,
                )
                try:
                    result = await self.semantic.extract(request)
                except Exception as exc:
                    key = (cursor.uidvalidity, message.uid)
                    if not self._is_transient(exc):
                        attempts = self._poison_failures.get(key, 0) + 1
                        self._poison_failures[key] = attempts
                        if attempts >= 3:
                            self._record_failed_message(message, digest, checkpoint)
                            self._poison_failures.pop(key, None)
                            processed += 1
                            seen.add(digest)
                            continue
                    else:
                        # A transient error is retryable and must not consume the
                        # poison budget for this UID.
                        self._poison_failures.pop(key, None)
                    return self._failure(exc, processed, skipped)
                body = {
                    "status": result.status,
                    "event_id": event_id,
                    "extraction_id": result.id,
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
                with self.store.transaction():
                    self.store.put("mail_message", digest, body)
                    self.store.put(
                        "mail_cursor",
                        self.folder,
                        {"uidvalidity": checkpoint.uidvalidity, "last_uid": checkpoint.last_uid},
                    )
                self._messages[digest] = body
                self._cursor = checkpoint
                seen.add(digest)
                processed += 1

            if not messages:
                self._persist_cursor(cursor)
                self._cursor = cursor
            if fetch_error is not None:
                return self._failure(fetch_error, processed, skipped)
        except Exception as exc:
            return self._failure(exc, processed, skipped)

        self.processed_count += processed
        self.last_error_class = None
        self._interval = self.poll_seconds
        self._next_poll = time.monotonic() + self._interval
        return PollResult(processed=processed, skipped=skipped)
