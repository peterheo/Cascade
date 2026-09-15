"""Small, explicit persistence layer for the state that must survive a restart.

The in-memory implementation keeps the default test and demo behavior unchanged.  The
SQLite implementation intentionally stores validated JSON records rather than commands:
reopening a database restores the last known state and never replays provider writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from cascade.domain.models import EventResult, Incident, Mutation, World
from cascade.execution.models import ExecutionResult
from cascade.memory.preferences import Preference
from cascade.memory.resolutions import ResolutionRecord
from cascade.planning.models import PlanningResult
from cascade.security.approvals import ApprovalRequest

RecordKind = Literal[
    "event",
    "incident",
    "preference",
    "search",
    "plan",
    "incident_plans",
    "approval",
    "execution",
    "ledger_orphan",
]
LogStream = Literal["resolution", "audit"]


@dataclass
class PersistedState:
    """Validated state loaded from a store.

    ``world`` is optional because the simulation store deliberately persists only
    preferences and resolution history.  Its resettable itinerary is supplied by the
    application on every start.
    """

    world: World | None = None
    events: dict[str, tuple[Mutation, EventResult]] = field(default_factory=dict)
    incidents: list[Incident] = field(default_factory=list)
    preferences: dict[str, Preference] = field(default_factory=dict)
    resolutions: list[ResolutionRecord] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    searches: dict[str, PlanningResult] = field(default_factory=dict)
    plan_searches: dict[str, str] = field(default_factory=dict)
    plan_incidents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    executions: dict[str, ExecutionResult] = field(default_factory=dict)
    ledger_orphans: dict[str, dict] = field(default_factory=dict)
    latest_search_id: str | None = None


class StateStore(Protocol):
    def load(self) -> PersistedState | None: ...

    def transaction(self) -> AbstractContextManager[None]: ...

    def save_world(self, world: World) -> None: ...

    def put(self, kind: RecordKind, key: str, record) -> None: ...

    def delete(self, kind: RecordKind, key: str) -> None: ...

    def append(self, stream: LogStream, body: dict) -> None: ...


class MemoryStore:
    """No-op store used by default and by all existing in-memory tests."""

    def load(self) -> None:
        return None

    @contextmanager
    def transaction(self):
        yield

    def save_world(self, world: World) -> None:
        return None

    def put(self, kind: RecordKind, key: str, record) -> None:
        return None

    def delete(self, kind: RecordKind, key: str) -> None:
        return None

    def append(self, stream: LogStream, body: dict) -> None:
        return None


class SqliteStore:
    """SQLite-backed state with one schema version and explicit record kinds."""

    schema_version = 1

    def __init__(
        self,
        path: Path,
        *,
        kinds: frozenset[RecordKind] | None = None,
        streams: frozenset[LogStream] | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.kinds = kinds
        self.streams = streams if streams is not None else frozenset({"resolution", "audit"})
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
            self._check_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS world (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL,
                body JSON NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                body JSON NOT NULL,
                PRIMARY KEY (kind, key)
            );
            CREATE TABLE IF NOT EXISTS log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                stream TEXT NOT NULL,
                at TEXT NOT NULL,
                type TEXT NOT NULL,
                body JSON NOT NULL
            );
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(self.schema_version),),
        )

    def _check_schema(self) -> None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        try:
            version = int(row[0]) if row else 0
        except (TypeError, ValueError) as exc:
            raise ValueError("Cascade persistence schema_version is invalid") from exc
        if version > self.schema_version:
            raise ValueError(
                f"Cascade persistence schema_version {version} is newer than supported "
                f"version {self.schema_version}"
            )

    @contextmanager
    def transaction(self):
        """Commit or roll back an outer transaction; nested calls share it."""

        with self._lock:
            depth = getattr(self._local, "transaction_depth", 0)
            if depth == 0:
                self._connection.execute("BEGIN")
            self._local.transaction_depth = depth + 1
            try:
                yield
            except BaseException:
                if depth == 0:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                if depth == 0:
                    self._connection.execute("COMMIT")
            finally:
                self._local.transaction_depth = depth

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def save_world(self, world: World) -> None:
        # A filtered store is used by simulation, whose itinerary is intentionally
        # resettable and must never replace the demo world on a restart.
        if self.kinds is not None:
            return
        self._connection.execute(
            "INSERT INTO world(id, version, body) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version, body=excluded.body",
            (world.version, self._json(world.model_dump(mode="json"))),
        )

    def put(self, kind: RecordKind, key: str, record) -> None:
        if self.kinds is not None and kind not in self.kinds:
            return
        if kind == "event":
            body = {
                "mutation": record[0].model_dump(mode="json"),
                "result": record[1].model_dump(mode="json"),
            }
        elif isinstance(record, dict):
            body = record
        else:
            body = record.model_dump(mode="json")
        self._connection.execute(
            "INSERT INTO records(kind, key, updated_at, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET updated_at=excluded.updated_at, "
            "body=excluded.body",
            (kind, key, self._now(), self._json(body)),
        )

    def delete(self, kind: RecordKind, key: str) -> None:
        if self.kinds is not None and kind not in self.kinds:
            return
        self._connection.execute("DELETE FROM records WHERE kind = ? AND key = ?", (kind, key))

    def append(self, stream: LogStream, body: dict) -> None:
        if stream not in self.streams:
            return
        self._connection.execute(
            "INSERT INTO log(stream, at, type, body) VALUES (?, ?, ?, ?)",
            (stream, self._now(), str(body.get("type", "")), self._json(body)),
        )

    def load(self) -> PersistedState | None:
        with self._lock:
            world_row = self._connection.execute(
                "SELECT version, body FROM world WHERE id = 1"
            ).fetchone()
            world = None
            if world_row is not None:
                world = World.model_validate(json.loads(world_row["body"])).model_copy(
                    update={"version": world_row["version"]}
                )

            state = PersistedState(world=world)
            for row in self._connection.execute(
                "SELECT kind, key, body FROM records ORDER BY rowid"
            ):
                body = json.loads(row["body"])
                kind = row["kind"]
                if kind == "event":
                    state.events[row["key"]] = (
                        Mutation.model_validate(body["mutation"]),
                        EventResult.model_validate(body["result"]),
                    )
                elif kind == "incident":
                    state.incidents.append(Incident.model_validate(body))
                elif kind == "preference":
                    state.preferences[row["key"]] = Preference.model_validate(body)
                elif kind == "search":
                    state.searches[row["key"]] = PlanningResult.model_validate(body)
                    state.latest_search_id = row["key"]
                elif kind == "plan":
                    state.plan_searches[row["key"]] = str(body["search_id"])
                elif kind == "incident_plans":
                    state.plan_incidents[row["key"]] = tuple(body["plan_ids"])
                elif kind == "approval":
                    state.approvals[row["key"]] = ApprovalRequest.model_validate(body)
                elif kind == "execution":
                    state.executions[row["key"]] = ExecutionResult.model_validate(body)
                elif kind == "ledger_orphan":
                    state.ledger_orphans[row["key"]] = body
                else:
                    raise ValueError(f"unknown persisted record kind: {kind}")
            for row in self._connection.execute("SELECT stream, body FROM log ORDER BY seq"):
                body = json.loads(row["body"])
                if row["stream"] == "resolution":
                    state.resolutions.append(ResolutionRecord.model_validate(body))
                elif row["stream"] == "audit":
                    state.audit.append(body)
                else:
                    raise ValueError(f"unknown persisted log stream: {row['stream']}")

            if (
                state.world is None
                and not state.events
                and not state.incidents
                and not state.preferences
                and not state.resolutions
                and not state.audit
                and not state.searches
                and not state.plan_searches
                and not state.plan_incidents
                and not state.approvals
                and not state.executions
                and not state.ledger_orphans
            ):
                return None
            return state
