from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.main import create_app
from cascade.connectors import mail as mail_module
from cascade.connectors.mail import ImapMailSource, MailCursor, MailMessage
from cascade.connectors.mail_watcher import MailWatcher
from cascade.persistence import MemoryStore, SqliteStore
from cascade.reasoning.models import PrivacySettings


class FakeSource:
    def __init__(self, cursor, messages=(), error=None):
        self.cursor = cursor
        self.messages = list(messages)
        self.error = error
        self.calls = []

    def fetch_new(self, cursor, limit):
        self.calls.append((cursor, limit))
        if self.error:
            raise self.error
        return self.cursor, self.messages[:limit]


class FakeSemantic:
    def __init__(self, store=None):
        self.privacy = PrivacySettings()
        self.core = SimpleNamespace(world=SimpleNamespace(version=3))
        self.calls = []

    async def extract(self, request):
        self.calls.append(request)
        return SimpleNamespace(status="NEEDS_CONFIRMATION")


def message(uid, message_id="<one@example.test>"):
    return MailMessage(
        uid=uid,
        message_id=message_id,
        subject="Flight update",
        sender="airline@example.test",
        received_at=datetime.now(UTC),
        text="The flight arrives later.",
    )


def test_first_run_sets_cursor_without_backfill_and_deduplicates():
    source = FakeSource(MailCursor(4, 9), [])
    semantic = FakeSemantic()
    watcher = MailWatcher(source, semantic, MemoryStore(), folder="Cascade", poll_seconds=60)
    first = watcher.poll_once()
    assert first.processed == 0
    assert source.calls[0] == (None, 10)
    source.messages = [message(9)]
    source.cursor = MailCursor(4, 10)
    watcher._next_poll = 0
    assert watcher.poll_once().processed == 1
    watcher._next_poll = 0
    second = watcher.poll_once()
    assert second.skipped == 1
    assert source.calls[1][0] == MailCursor(4, 9)


def test_privacy_off_skips_fetch_and_uidvalidity_cursor_can_reset():
    source = FakeSource(MailCursor(8, 22), [message(22)])
    semantic = FakeSemantic()
    semantic.privacy = PrivacySettings(live_inference=False)
    watcher = MailWatcher(source, semantic, MemoryStore(), folder="Cascade", poll_seconds=60)
    assert watcher.poll_once().disabled is True
    assert source.calls == []


def test_imap_source_does_not_backfill_and_resets_uidvalidity(monkeypatch):
    raw_message = EmailMessage()
    raw_message["Message-ID"] = "<imap-one@example.test>"
    raw_message["Subject"] = "Delay"
    raw_message["From"] = "airline@example.test"
    raw_message["Date"] = "Tue, 15 Sep 2026 12:00:00 +0000"
    raw_message.set_content("The flight is delayed.")

    class FakeImap:
        uidvalidity = 7
        all_uids = [1, 2]
        calls = []

        def __init__(self, host, port, timeout):
            self.calls.append((host, port, timeout))

        def login(self, username, password):
            self.calls.append(("LOGIN", username, password))
            return "OK", []

        def select(self, folder, readonly=False):
            self.calls.append(("SELECT", folder, readonly))
            return "OK", [b"2"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return name, [str(self.uidvalidity).encode()]

        def uid(self, command, sequence, criteria):
            self.calls.append((command, sequence, criteria))
            if command == "SEARCH" and criteria == "ALL":
                return "OK", [b" ".join(str(uid).encode() for uid in self.all_uids)]
            if command == "SEARCH":
                assert criteria == "UID 2:*"
                return "OK", [b"2"]
            assert command == "FETCH" and criteria == "(BODY.PEEK[])"
            return "OK", [(b"2 (BODY[] {0})", raw_message.as_bytes()), b")"]

        def logout(self):
            self.calls.append(("LOGOUT",))
            return "OK", []

    monkeypatch.setattr(mail_module.imaplib, "IMAP4_SSL", FakeImap)
    source = ImapMailSource("user@example.test", "app-password", folder="Cascade")

    first_cursor, first_messages = source.fetch_new(None, 10)
    assert first_cursor == MailCursor(7, 2)
    assert first_messages == []

    second_cursor, second_messages = source.fetch_new(MailCursor(7, 1), 10)
    assert second_cursor == MailCursor(7, 2)
    assert len(second_messages) == 1
    assert second_messages[0].text == "The flight is delayed."
    assert ("FETCH", "2", "(BODY.PEEK[])") in FakeImap.calls

    FakeImap.uidvalidity = 8
    reset_cursor, reset_messages = source.fetch_new(MailCursor(7, 2), 10)
    assert reset_cursor == MailCursor(8, 2)
    assert reset_messages == []


def test_mail_watcher_caps_each_poll_at_ten_messages():
    source = FakeSource(
        MailCursor(3, 12),
        [message(uid, f"<message-{uid}@example.test>") for uid in range(1, 13)],
    )
    semantic = FakeSemantic()
    watcher = MailWatcher(source, semantic, MemoryStore(), folder="Cascade", poll_seconds=60)
    result = watcher.poll_once()
    assert result.processed == 10
    assert len(semantic.calls) == 10


def test_mail_watcher_is_disabled_without_credentials(monkeypatch):
    for name in (
        "CASCADE_AUTH_REQUIRED",
        "CASCADE_ICLOUD_USER",
        "CASCADE_ICLOUD_APP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as client:
        assert client.app.state.mail_watcher is None
        assert client.get("/v1/connectors/mail/status").json() == {
            "enabled": False,
            "folder": "Cascade",
            "last_poll_at": None,
            "last_error_class": None,
            "processed_count": 0,
        }


def test_mail_watcher_env_config_is_clamped_and_names_folder(monkeypatch):
    monkeypatch.delenv("CASCADE_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("CASCADE_ICLOUD_USER", "user@example.test")
    monkeypatch.setenv("CASCADE_ICLOUD_APP_PASSWORD", "app-password")
    monkeypatch.setenv("CASCADE_ICLOUD_MAIL_FOLDER", "Inbox/Cascade")
    monkeypatch.setenv("CASCADE_MAIL_POLL_SECONDS", "1")
    app = create_app()
    watcher = app.state.mail_watcher
    assert watcher is not None
    assert watcher.folder == "Inbox/Cascade"
    assert watcher.poll_seconds == 60


def test_errors_back_off_and_success_resets_interval():
    source = FakeSource(MailCursor(1, 1), error=RuntimeError("offline"))
    semantic = FakeSemantic()
    watcher = MailWatcher(source, semantic, MemoryStore(), folder="Cascade", poll_seconds=60)
    assert watcher.poll_once().error_class == "RuntimeError"
    assert watcher._interval == 120
    source.error = None
    watcher._next_poll = 0
    assert watcher.poll_once().error_class is None
    assert watcher._interval == 60


def test_mail_records_omit_body_by_default_and_resume_from_sqlite(tmp_path):
    store = SqliteStore(tmp_path / "mail.db")
    source = FakeSource(MailCursor(2, 7), [message(7)])
    semantic = FakeSemantic()
    watcher = MailWatcher(source, semantic, store, folder="Cascade", poll_seconds=60)
    watcher.poll_once()
    loaded = store.load()
    assert loaded.mail_cursors["Cascade"] == {"uidvalidity": 2, "last_uid": 7}
    record = next(iter(loaded.mail_messages.values()))
    assert "text" not in record and "subject" not in record
    restarted = MailWatcher(
        FakeSource(MailCursor(2, 8), [message(8, "<two@example.test>")]),
        semantic,
        store,
        folder="Cascade",
        poll_seconds=60,
    )
    restarted._next_poll = 0
    restarted.poll_once()
    assert restarted.source.calls[0][0] == MailCursor(2, 7)


def test_mail_records_include_message_fields_when_enabled(tmp_path):
    store = SqliteStore(tmp_path / "mail.db")
    semantic = FakeSemantic()
    semantic.privacy = PrivacySettings(persist_event_text=True)
    watcher = MailWatcher(
        FakeSource(MailCursor(2, 7), [message(7)]),
        semantic,
        store,
        folder="Cascade",
        poll_seconds=60,
    )
    watcher.poll_once()
    record = next(iter(store.load().mail_messages.values()))
    assert record["subject"] == "Flight update"
    assert record["sender"] == "airline@example.test"
    assert record["text"] == "The flight arrives later."
