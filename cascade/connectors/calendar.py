"""Small, read-first CalDAV connector for the personal iCloud calendar."""

from __future__ import annotations

import hashlib
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar, Event, vDatetime

from cascade.domain.models import Commitment, SourceRef
from cascade.persistence import StateStore
from cascade.tools.gateway import ToolAction, ToolResult
from cascade.tools.ledger import LedgerBackend, ProviderLedgerView


class CalendarError(RuntimeError):
    pass


class CalendarConflict(CalendarError):
    """The resource changed between the check and the conditional write."""


class UnsupportedTimezone(CalendarError):
    pass


@dataclass(frozen=True)
class CalendarEvent:
    commitment: Commitment
    link: dict
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarSnapshot:
    found: bool
    events: tuple[CalendarEvent, ...] = ()
    imported: int = 0
    skipped_all_day: int = 0
    skipped_recurring: int = 0
    stale_retained: int = 0
    error_class: str | None = None


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element.iter() if _local(item) == name), None)


def _text(element: ET.Element, name: str) -> str | None:
    child = _child(element, name)
    return child.text.strip() if child is not None and child.text else None


def _property_href(element: ET.Element, property_name: str) -> str | None:
    prop = _child(element, property_name)
    return _text(prop, "href") if prop is not None else None


def _href(base: str, value: str) -> str:
    return urljoin(base, value)


def _safe_href(collection: str, href: str) -> bool:
    root = urlsplit(collection)
    target = urlsplit(href)
    if target.scheme != root.scheme or target.netloc != root.netloc:
        return False
    root_path = posixpath.normpath(unquote(root.path)).rstrip("/") + "/"
    target_path = posixpath.normpath(unquote(target.path))
    return target_path.startswith(root_path)


def _validate_discovered_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.port is not None
        or not (host == "caldav.icloud.com" or host.endswith(".icloud.com"))
    ):
        raise CalendarError("CalDAV discovery returned an unsafe URL")
    return url


def _event_interval(event: Event) -> tuple[datetime | date, datetime | date] | None:
    start_prop = event.get("DTSTART")
    if start_prop is None:
        return None
    start = start_prop.dt
    if isinstance(start, date) and not isinstance(start, datetime):
        return start, start
    end_prop = event.get("DTEND")
    if end_prop is not None:
        end = end_prop.dt
    elif event.get("DURATION") is not None:
        end = start + event.decoded("DURATION")
    else:
        end = start + timedelta(hours=1)
    return start, end


def _kind(event: Event) -> str:
    raw = event.get("X-CASCADE-KIND") or event.get("CATEGORIES")
    if raw is not None:
        value = str(raw).split(",", 1)[0].strip().lower()
        if value in {"flight", "transfer", "hotel", "restaurant", "ticket", "meeting"}:
            return value
    return "meeting"


def _calendar_event(calendar_data: str, href: str, calendar_href: str, etag: str | None):
    parsed = Calendar.from_ical(calendar_data)
    event = next((item for item in parsed.walk() if item.name == "VEVENT"), None)
    if event is None:
        return None
    uid = str(event.get("UID", "")).strip()
    interval = _event_interval(event)
    if not uid or interval is None:
        return None
    start, end = interval
    # The caller counts all-day and recurring entries before constructing events.
    if isinstance(start, date) and not isinstance(start, datetime):
        return "all_day"
    if event.get("RRULE") is not None:
        return "recurring"
    if not isinstance(end, datetime):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
    if end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    commitment_id = "ical_" + hashlib.sha256(uid.encode()).hexdigest()[:16]
    intent_id = f"intent_{commitment_id}"
    title = str(event.get("SUMMARY", "Calendar event")).strip() or "Calendar event"
    commitment = Commitment(
        id=commitment_id,
        kind=_kind(event),
        title=title,
        intent_id=intent_id,
        start_at=start,
        end_at=end,
        source=SourceRef(source="icloud_calendar", external_id=uid),
    )
    return CalendarEvent(
        commitment=commitment,
        link={"calendar_href": calendar_href, "href": href, "event_uid": uid, "etag": etag},
        metadata={
            key: str(event.get(key)).strip()
            for key in ("STATUS", "LOCATION", "DESCRIPTION")
            if event.get(key) is not None and str(event.get(key)).strip()
        },
    )


def _replace_datetime(event: Event, name: str, value: datetime) -> None:
    old = event.get(name)
    tzid = old.params.get("TZID") if old is not None else None
    if tzid:
        try:
            value = value.astimezone(ZoneInfo(str(tzid))).replace(tzinfo=None)
        except (KeyError, ValueError) as exc:
            raise UnsupportedTimezone("unsupported_timezone") from exc
        prop = vDatetime(value)
        prop.params["TZID"] = str(tzid)
        event[name] = prop
    else:
        event[name] = vDatetime(value)


def _validate_event_timezone(event: Event) -> None:
    for name in ("DTSTART", "DTEND"):
        prop = event.get(name)
        tzid = prop.params.get("TZID") if prop is not None else None
        if tzid:
            try:
                ZoneInfo(str(tzid))
            except (KeyError, ValueError) as exc:
                raise UnsupportedTimezone("unsupported_timezone") from exc


class ICloudCalendarClient:
    def __init__(
        self,
        username: str,
        app_password: str,
        *,
        calendar_name: str = "Cascade Trip",
        base_url: str = "https://caldav.icloud.com/",
        transport: httpx.BaseTransport | None = None,
    ):
        self.username = username
        self.app_password = app_password
        self.calendar_name = calendar_name
        self.base_url = base_url
        self.transport = transport
        self.calendar_href: str | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            auth=(self.username, self.app_password),
            transport=self.transport,
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
        )

    @staticmethod
    def _request_xml(client: httpx.Client, method: str, url: str, body: str, headers=None):
        response = client.request(
            method,
            url,
            content=body.encode(),
            headers={"Content-Type": "application/xml; charset=utf-8", **(headers or {})},
        )
        if response.status_code not in (200, 207):
            raise CalendarError(f"CalDAV {method} failed ({response.status_code})")
        return response

    def discover(self) -> str | None:
        principal_xml = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
            "<d:prop><d:current-user-principal/></d:prop></d:propfind>"
        )
        home_xml = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:">'
            '<d:prop><c:calendar-home-set xmlns:c="urn:ietf:params:xml:ns:caldav"/>'
            "</d:prop></d:propfind>"
        )
        list_xml = (
            '<?xml version="1.0"?><d:propfind xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:displayname/>'
            "<d:resourcetype/></d:prop></d:propfind>"
        )
        with self._client() as client:
            first = self._request_xml(
                client, "PROPFIND", self.base_url, principal_xml, {"Depth": "0"}
            )
            principal = _property_href(ET.fromstring(first.text), "current-user-principal")
            if not principal:
                return None
            principal_url = _href(self.base_url, principal)
            _validate_discovered_url(principal_url)
            second = self._request_xml(client, "PROPFIND", principal_url, home_xml, {"Depth": "0"})
            home = _property_href(ET.fromstring(second.text), "calendar-home-set")
            if not home:
                return None
            home_url = _href(principal_url, home)
            _validate_discovered_url(home_url)
            third = self._request_xml(client, "PROPFIND", home_url, list_xml, {"Depth": "1"})
            root = ET.fromstring(third.text)
            for response in (item for item in root.iter() if _local(item) == "response"):
                display = _text(response, "displayname")
                href = _text(response, "href")
                if display == self.calendar_name and href:
                    self.calendar_href = _href(home_url, href)
                    _validate_discovered_url(self.calendar_href)
                    return self.calendar_href
        return None

    def list_events(self) -> CalendarSnapshot:
        calendar_href = self.calendar_href or self.discover()
        if not calendar_href:
            return CalendarSnapshot(found=False)
        report_xml = (
            '<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:getetag/>'
            '<c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
            "</c:calendar-query>"
        )
        with self._client() as client:
            response = self._request_xml(
                client, "REPORT", calendar_href, report_xml, {"Depth": "1"}
            )
        events: list[CalendarEvent] = []
        all_day = recurring = 0
        root = ET.fromstring(response.text)
        for item in (node for node in root.iter() if _local(node) == "response"):
            href = _text(item, "href")
            data = _text(item, "calendar-data")
            if not href or not data or not _safe_href(calendar_href, _href(calendar_href, href)):
                continue
            result = _calendar_event(
                data, _href(calendar_href, href), calendar_href, _text(item, "getetag")
            )
            if result == "all_day":
                all_day += 1
            elif result == "recurring":
                recurring += 1
            elif isinstance(result, CalendarEvent):
                events.append(result)
        return CalendarSnapshot(
            found=True,
            events=tuple(events),
            imported=len(events),
            skipped_all_day=all_day,
            skipped_recurring=recurring,
        )

    def ensure_href(self, href: str) -> None:
        if not self.calendar_href or not _safe_href(self.calendar_href, href):
            raise CalendarError("calendar resource is outside the selected collection")

    def get(self, href: str) -> tuple[str, str | None]:
        self.ensure_href(href)
        with self._client() as client:
            response = client.get(href)
        if response.status_code != 200:
            raise CalendarError(f"CalDAV GET failed ({response.status_code})")
        return response.text, response.headers.get("etag")

    def put(self, href: str, body: str, etag: str | None) -> str | None:
        self.ensure_href(href)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        with self._client() as client:
            response = client.put(href, content=body.encode(), headers=headers)
        if response.status_code == 412:
            raise CalendarConflict("calendar event changed since it was checked")
        if response.status_code not in (200, 201, 204):
            raise CalendarError(f"CalDAV PUT failed ({response.status_code})")
        return response.headers.get("etag")


class ICloudCalendarProvider:
    kind = "icloud"

    def __init__(
        self,
        client: ICloudCalendarClient,
        store: StateStore,
        links: dict[str, dict],
        ledger: LedgerBackend | None = None,
    ):
        self.client = client
        self.store = store
        self.links = links
        self.ledger = ProviderLedgerView(ledger, self.kind) if ledger is not None else {}
        self._undos: dict[str, dict] = {}

    def _link(self, action: ToolAction) -> dict:
        link = self.links.get(action.commitment_id)
        if link is None:
            raise CalendarError(f"no calendar link for {action.commitment_id}")
        return link

    @staticmethod
    def _result(
        action: ToolAction,
        *,
        success: bool,
        detail: str,
        href: str | None = None,
        start=None,
        end=None,
        side_effect=False,
        verified=False,
    ) -> ToolResult:
        return ToolResult(
            success=success,
            provider=action.provider,
            operation=action.operation,
            external_reference=href,
            side_effect=side_effect,
            verified=verified,
            raw_result_ref=f"icloud:{action.id}",
            detail=detail,
            observed_start_at=start,
            observed_end_at=end,
        )

    def _read(self, action: ToolAction):
        link = self._link(action)
        body, etag = self.client.get(link["href"])
        return link, body, etag

    def _observed(self, action: ToolAction, body: str, href: str):
        parsed = Calendar.from_ical(body)
        event = next((item for item in parsed.walk() if item.name == "VEVENT"), None)
        if event is None:
            return self._result(
                action, success=False, detail="VEVENT missing after read-back", href=href
            )
        try:
            _validate_event_timezone(event)
        except UnsupportedTimezone:
            return self._result(action, success=False, detail="unsupported_timezone", href=href)
        link = self.links.get(action.commitment_id)
        if link and str(event.get("UID", "")).strip() != str(link.get("event_uid", "")):
            return self._result(
                action, success=False, detail="calendar event UID mismatch", href=href
            )
        interval = _event_interval(event)
        if (
            interval is None
            or not isinstance(interval[0], datetime)
            or not isinstance(interval[1], datetime)
        ):
            return self._result(
                action, success=False, detail="event interval unavailable", href=href
            )
        return self._result(
            action,
            success=True,
            detail="calendar event read",
            href=href,
            start=interval[0],
            end=interval[1],
        )

    def check(self, action: ToolAction) -> ToolResult:
        try:
            link, body, etag = self._read(action)
            if link.get("etag") and etag and link["etag"] != etag:
                return self._result(
                    action, success=False, detail="calendar event changed", href=link["href"]
                )
            return self._observed(action, body, link["href"])
        except Exception as exc:
            return self._result(
                action, success=False, detail=f"Calendar check failed: {type(exc).__name__}"
            )

    def apply(self, action: ToolAction) -> ToolResult:
        recorded = self.ledger.get(action.idempotency_key)
        if recorded is not None:
            return self._result(
                action,
                success=True,
                detail="Idempotent replay; the existing calendar write was reused.",
                href=recorded.get("reference"),
                side_effect=False,
            )
        put_attempted = False
        try:
            link, body, etag = self._read(action)
            if not etag:
                return self._result(
                    action,
                    success=False,
                    detail="calendar event has no ETag; refusing an unguarded write",
                    href=link["href"],
                )
            parsed = Calendar.from_ical(body)
            event = next((item for item in parsed.walk() if item.name == "VEVENT"), None)
            if event is None:
                raise CalendarError("VEVENT missing")
            _validate_event_timezone(event)
            if str(event.get("UID", "")).strip() != str(link.get("event_uid", "")):
                raise CalendarError("calendar event UID does not match the imported link")
            start_prop = event.get("DTSTART")
            end_prop = event.get("DTEND")
            if start_prop is None:
                raise CalendarError("event has no writable interval")
            new_start, new_end = action.postcondition.start_at, action.postcondition.end_at
            _replace_datetime(event, "DTSTART", new_start)
            old_duration = event.decoded("DURATION") if event.get("DURATION") is not None else None
            if old_duration is not None and end_prop is None:
                new_duration = new_end - new_start
                if new_duration != old_duration:
                    event.pop("DURATION", None)
                    _replace_datetime(event, "DTEND", new_end)
            elif end_prop is not None:
                _replace_datetime(event, "DTEND", new_end)
            else:
                _replace_datetime(event, "DTEND", new_end)
            updated = parsed.to_ical().decode()
            # Persist the intent through the autocommit provider ledger before PUT.
            intent = {
                "reference": link["href"],
                "operation": action.operation,
                "start_at": new_start,
                "end_at": new_end,
                "refund": 0,
                "pending": True,
                "prior_ics": body,
                "prior_etag": etag,
                "commitment_id": action.commitment_id,
            }
            self.ledger[action.idempotency_key] = intent
            undo_record = {
                "href": link["href"],
                "prior_ics": body,
                "prior_etag": etag,
                "new_etag": None,
                "commitment_id": action.commitment_id,
                "event_uid": link.get("event_uid"),
            }
            self._undos[action.idempotency_key] = undo_record
            self.store.put("calendar_undo", action.idempotency_key, undo_record)
            try:
                put_attempted = True
                new_etag = self.client.put(link["href"], updated, etag)
            except CalendarConflict:
                return self._result(
                    action,
                    success=False,
                    detail="calendar event changed before the conditional write (412)",
                    href=link["href"],
                )
            if not new_etag:
                _, new_etag = self.client.get(link["href"])
            self._undos[action.idempotency_key] = {**undo_record, "new_etag": new_etag}
            self.store.put(
                "calendar_undo", action.idempotency_key, self._undos[action.idempotency_key]
            )
            return self._result(
                action,
                success=True,
                detail="calendar event updated",
                href=link["href"],
                start=new_start,
                end=new_end,
                side_effect=True,
            )
        except UnsupportedTimezone:
            return self._result(action, success=False, detail="unsupported_timezone")
        except CalendarError as exc:
            return self._result(action, success=False, detail=str(exc))
        except Exception as exc:
            if put_attempted:
                raise
            return self._result(
                action, success=False, detail=f"Calendar write refused: {type(exc).__name__}"
            )

    def verify(self, action: ToolAction) -> ToolResult:
        link, body, etag = self._read(action)
        result = self._observed(action, body, link["href"])
        if etag:
            self.links[action.commitment_id] = {**link, "etag": etag}
            self.store.put("calendar_link", action.commitment_id, self.links[action.commitment_id])
            recorded = self.ledger.get(action.idempotency_key)
            if recorded is not None:
                finalized = {
                    **recorded,
                    "pending": False,
                    "new_etag": etag,
                    "verification_failed": not result.success,
                }
                replace = getattr(self.ledger, "replace", None)
                if replace is None:
                    self.ledger[action.idempotency_key] = finalized
                else:
                    replace(action.idempotency_key, finalized)
            undo = self._undos.get(action.idempotency_key)
            if undo is not None:
                self._undos[action.idempotency_key] = {**undo, "new_etag": etag}
                self.store.put(
                    "calendar_undo", action.idempotency_key, self._undos[action.idempotency_key]
                )
        return result

    def undo(self, idempotency_key: str) -> dict:
        loaded = self.store.load()
        record = self._undos.get(idempotency_key) or (
            loaded.calendar_undos.get(idempotency_key) if loaded else None
        )
        if record is None:
            ledger_record = self.ledger.get(idempotency_key)
            if ledger_record is not None and ledger_record.get("prior_ics"):
                record = {
                    "href": ledger_record.get("reference"),
                    "prior_ics": ledger_record["prior_ics"],
                    "new_etag": ledger_record.get("new_etag"),
                    "commitment_id": ledger_record.get("commitment_id"),
                }
        if record is None:
            raise KeyError(idempotency_key)
        _, etag = self.client.get(record["href"])
        expected = record.get("new_etag")
        if not etag:
            raise CalendarError("calendar event has no ETag; refusing an unguarded undo")
        if expected and etag and expected != etag:
            raise CalendarConflict("calendar event changed after execution")
        new_etag = self.client.put(record["href"], record["prior_ics"], etag)
        parsed = Calendar.from_ical(record["prior_ics"])
        event = next((item for item in parsed.walk() if item.name == "VEVENT"), None)
        interval = _event_interval(event) if event is not None else None
        return {
            "status": "UNDONE",
            "href": record["href"],
            "etag": new_etag,
            "commitment_id": record.get("commitment_id"),
            "start_at": interval[0] if interval and isinstance(interval[0], datetime) else None,
            "end_at": interval[1] if interval and isinstance(interval[1], datetime) else None,
        }


class CalendarWatcher:
    """Periodic synchronizer; all network I/O happens outside Cascade's lock."""

    def __init__(
        self,
        client: ICloudCalendarClient,
        service,
        *,
        poll_seconds: int,
        persist_event_text: Callable[[], bool] | None = None,
    ):
        self.client = client
        self.service = service
        self.persist_event_text = persist_event_text or (lambda: False)
        self.poll_seconds = poll_seconds
        self._interval = poll_seconds
        self._next_poll = 0.0
        self.last_sync_at: str | None = None
        self.last_error_class: str | None = None
        self.calendar_found = False
        self.imported = 0
        self.skipped_all_day = 0
        self.skipped_recurring = 0
        self.stale_retained = 0

    def seconds_until_next_poll(self) -> float:
        import time

        return max(0.0, self._next_poll - time.monotonic())

    def sync_once(self) -> dict:
        import time
        from datetime import UTC, datetime

        if self.seconds_until_next_poll() > 0:
            return self.status()
        try:
            sync_started_version = self.service.world.version
            snapshot = self.client.list_events()
            self.calendar_found = snapshot.found
            self.last_sync_at = datetime.now(UTC).isoformat()
            if not snapshot.found:
                self.last_error_class = "calendar_not_found"
                self._interval = min(900, self._interval * 2)
                self._next_poll = time.monotonic() + self._interval
                return self.status()
            merged = self.service.sync_calendar(
                snapshot.events,
                skipped_all_day=snapshot.skipped_all_day,
                skipped_recurring=snapshot.skipped_recurring,
                sync_started_version=sync_started_version,
                persist_event_text=self.persist_event_text(),
            )
            if merged.get("stale"):
                self._next_poll = time.monotonic() + self._interval
                return self.status()
            self.imported = merged["imported"]
            self.skipped_all_day = merged["skipped_all_day"]
            self.skipped_recurring = merged["skipped_recurring"]
            self.stale_retained = merged["stale_retained"]
            self.last_error_class = None
            self._interval = self.poll_seconds
        except Exception as exc:
            self.last_error_class = type(exc).__name__
            self._interval = min(900, self._interval * 2)
        self._next_poll = time.monotonic() + self._interval
        return self.status()

    def status(self) -> dict:
        return {
            "enabled": True,
            "calendar_found": self.calendar_found,
            "last_sync_at": self.last_sync_at,
            "imported": self.imported,
            "skipped_all_day": self.skipped_all_day,
            "skipped_recurring": self.skipped_recurring,
            "stale_retained": self.stale_retained,
            "last_error_class": self.last_error_class,
        }
