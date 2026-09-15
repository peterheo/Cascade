"""Read-only iCloud IMAP access with cursor-based polling."""

from __future__ import annotations

import email
import email.utils
import html
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from typing import Protocol


@dataclass(frozen=True)
class MailCursor:
    uidvalidity: int
    last_uid: int


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    subject: str
    sender: str
    received_at: datetime
    text: str


class MailFetchError(RuntimeError):
    """A body fetch failed after zero or more messages were read successfully."""

    def __init__(self, cursor: MailCursor, messages: list[MailMessage]):
        super().__init__("mail message fetch failed")
        self.cursor = cursor
        self.messages = messages


class MailSource(Protocol):
    def fetch_new(
        self, cursor: MailCursor | None, limit: int
    ) -> tuple[MailCursor, list[MailMessage]]: ...


def _uidvalidity(client: imaplib.IMAP4_SSL) -> int:
    response = client.response("UIDVALIDITY")
    values = response[1] if response and len(response) > 1 else []
    raw = values[0] if values else b"0"
    return int(raw.decode() if isinstance(raw, bytes) else raw)


def _uids(data) -> list[int]:
    raw = data[0] if data else b""
    return [int(value) for value in (raw or b"").split()]


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        body = part.get_payload()
        return body if isinstance(body, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _message_text(message: Message) -> str:
    plain: list[str] = []
    markup: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_decode_part(part))
        elif content_type == "text/html":
            markup.append(_decode_part(part))
    markup_text = "\n".join(markup)
    markup_text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", markup_text)
    text = "\n".join(plain) if plain else re.sub(r"<[^>]+>", " ", markup_text)
    return html.unescape(text).strip()[:20_000]


class ImapMailSource:
    def __init__(self, username: str, password: str, *, folder: str = "Cascade"):
        self.username = username
        self.password = password
        self.folder = folder

    def fetch_new(
        self, cursor: MailCursor | None, limit: int
    ) -> tuple[MailCursor, list[MailMessage]]:
        client = imaplib.IMAP4_SSL("imap.mail.me.com", 993, timeout=30)
        try:
            client.login(self.username, self.password)
            status, _ = client.select(self.folder, readonly=True)
            if status != "OK":
                raise RuntimeError("mail folder could not be selected")
            uidvalidity = _uidvalidity(client)
            status, data = client.uid("SEARCH", None, "ALL")
            if status != "OK":
                raise RuntimeError("mail search failed")
            all_uids = _uids(data)
            current_max = max(all_uids, default=0)
            if cursor is None or cursor.uidvalidity != uidvalidity:
                return MailCursor(uidvalidity, current_max), []
            if current_max <= cursor.last_uid:
                return MailCursor(uidvalidity, cursor.last_uid), []
            start = cursor.last_uid + 1
            status, data = client.uid("SEARCH", None, f"UID {start}:*")
            if status != "OK":
                raise RuntimeError("mail UID search failed")
            uids = sorted(_uids(data))[: max(0, limit)]
            messages: list[MailMessage] = []
            last_uid = cursor.last_uid
            for uid in uids:
                status, fetched = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise MailFetchError(MailCursor(uidvalidity, last_uid), messages)
                raw = next(
                    (part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1),
                    None,
                )
                if not isinstance(raw, bytes):
                    raise MailFetchError(MailCursor(uidvalidity, last_uid), messages)
                parsed = email.message_from_bytes(raw)
                try:
                    received = email.utils.parsedate_to_datetime(parsed.get("Date", ""))
                except (TypeError, ValueError):
                    received = datetime.now(UTC)
                if received is None:
                    received = datetime.now(UTC)
                if received.tzinfo is None:
                    received = received.replace(tzinfo=UTC)
                messages.append(
                    MailMessage(
                        uid=uid,
                        message_id=parsed.get("Message-ID", f"imap-{uidvalidity}-{uid}").strip(),
                        subject=parsed.get("Subject", "").strip(),
                        sender=parsed.get("From", "").strip(),
                        received_at=received,
                        text=_message_text(parsed),
                    )
                )
                last_uid = uid
            return MailCursor(uidvalidity, last_uid), messages
        finally:
            try:
                client.logout()
            except Exception:
                pass
