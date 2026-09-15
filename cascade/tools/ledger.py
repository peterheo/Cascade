"""Provider ledger backends used to make writes idempotent across restarts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, MutableMapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol


class LedgerBackend(Protocol):
    def get(self, provider: str, key: str) -> dict | None: ...

    def set(self, provider: str, key: str, value: dict) -> None: ...

    def contains(self, provider: str, key: str) -> bool: ...

    def items(self, provider: str) -> Iterator[tuple[str, dict]]: ...

    def all_entries(self) -> Iterator[tuple[str, str, dict]]: ...


class ProviderLedgerView(MutableMapping[str, dict]):
    """A provider-scoped mapping over a shared ledger backend."""

    def __init__(self, backend: LedgerBackend, provider: str):
        self.backend = backend
        self.provider = provider

    def __getitem__(self, key: str) -> dict:
        value = self.backend.get(self.provider, key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: dict) -> None:
        self.backend.set(self.provider, key, value)

    def __delitem__(self, key: str) -> None:
        raise TypeError("provider ledger entries are append-only")

    def __iter__(self):
        return (key for key, _ in self.backend.items(self.provider))

    def __len__(self) -> int:
        return sum(1 for _ in self.backend.items(self.provider))

    def get(self, key: str, default=None):
        value = self.backend.get(self.provider, key)
        return default if value is None else value

    def contains(self, key: str) -> bool:
        return self.backend.contains(self.provider, key)

    def replace(self, key: str, value: dict) -> None:
        replace = getattr(self.backend, "replace", None)
        if replace is None:
            self.backend.set(self.provider, key, value)
        else:
            replace(self.provider, key, value)


class SqliteLedger:
    """Autocommit ledger; each provider write is durable before apply returns."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        # The provider ledger is the durable side of the write protocol.  Keep its
        # autocommit visible before ``apply`` returns, including across a crash.
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                provider TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                body JSON NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (provider, idempotency_key)
            )
            """
        )

    @staticmethod
    def _encode(value: dict) -> dict:
        return {
            **value,
            "start_at": value.get("start_at").isoformat() if value.get("start_at") else None,
            "end_at": value.get("end_at").isoformat() if value.get("end_at") else None,
            "refund": str(value.get("refund", Decimal("0"))),
        }

    @staticmethod
    def _decode(value: dict) -> dict:
        return {
            **value,
            "start_at": datetime.fromisoformat(value["start_at"])
            if value.get("start_at")
            else None,
            "end_at": datetime.fromisoformat(value["end_at"]) if value.get("end_at") else None,
            "refund": Decimal(str(value.get("refund", "0"))),
        }

    def get(self, provider: str, key: str) -> dict | None:
        row = self._connection.execute(
            "SELECT body FROM ledger WHERE provider = ? AND idempotency_key = ?",
            (provider, key),
        ).fetchone()
        return self._decode(json.loads(row[0])) if row else None

    def set(self, provider: str, key: str, value: dict) -> None:
        self._connection.execute(
            "INSERT INTO ledger(provider, idempotency_key, body, recorded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider, idempotency_key) DO NOTHING",
            (
                provider,
                key,
                json.dumps(self._encode(value), separators=(",", ":"), ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )

    def replace(self, provider: str, key: str, value: dict) -> None:
        self._connection.execute(
            "INSERT INTO ledger(provider, idempotency_key, body, recorded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider, idempotency_key) DO UPDATE SET body=excluded.body, "
            "recorded_at=excluded.recorded_at",
            (
                provider,
                key,
                json.dumps(self._encode(value), separators=(",", ":"), ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )

    def contains(self, provider: str, key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM ledger WHERE provider = ? AND idempotency_key = ?",
            (provider, key),
        ).fetchone()
        return row is not None

    def items(self, provider: str) -> Iterator[tuple[str, dict]]:
        rows = self._connection.execute(
            "SELECT idempotency_key, body FROM ledger WHERE provider = ? ORDER BY rowid",
            (provider,),
        )
        for row in rows:
            yield row[0], self._decode(json.loads(row[1]))

    def all_entries(self) -> Iterator[tuple[str, str, dict]]:
        rows = self._connection.execute(
            "SELECT provider, idempotency_key, body FROM ledger ORDER BY rowid"
        )
        for row in rows:
            yield row[0], row[1], self._decode(json.loads(row[2]))

    def __iter__(self):
        return self.all_entries()

    def __len__(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM ledger").fetchone()
        return int(row[0])
