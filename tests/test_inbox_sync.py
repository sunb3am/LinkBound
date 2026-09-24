import asyncio
from types import SimpleNamespace

import pytest

from app import db, inbox_sync
from app.inbox_browser import FOLDERS, InboxRow, InboxStateError


class FakeRunner:
    instances = []

    def __init__(self, settings, operator):
        self.send_calls = 0
        self.start_calls = []
        self.instances.append(self)

    async def start(self, *, accept_downloads=None, downloads_path=None):
        self.start_calls.append(accept_downloads)

    def _require_page(self):
        return object()

    async def close(self):
        pass

    async def send_message(self, *args, **kwargs):
        self.send_calls += 1
        raise AssertionError("inbox sync must not send messages")


class FakeInboxBrowser:
    rows_by_folder = {
        "focused": [
            InboxRow(0, "Unread Person", "Unread preview", True, False),
            InboxRow(1, "Read Person", "Read preview", False, False),
        ]
    }
    messages_by_thread = {
        "/messaging/thread/unread": [{
            "source_key": "urn:msg:unread-1", "author_name": "Unread Person",
            "author_url": "https://www.linkedin.com/in/unread-person/", "body": "Hi",
            "has_attachment": True,
            "attachments": [{"index": 0, "filename": "resume.pdf"}],
        }],
        "/messaging/thread/read": [{
            "source_key": "urn:msg:read-1", "author_name": "Me", "body": "Thanks",
        }],
    }
    restore_calls = []

    def __init__(self, page):
        self.folder = None
        self.active_thread = None
        self.unread_by_thread = {
            "/messaging/thread/unread": True,
            "/messaging/thread/read": False,
        }

    async def verify_identity(self, expected_url):
        assert expected_url == "https://linkedin.com/in/me"
        return "https://www.linkedin.com/in/me"

    async def open_list(self, folder):
        if folder == "other":
            raise InboxStateError("folder selector unavailable")
        self.folder = folder

    async def rows(self):
        return self.rows_by_folder.get(self.folder, [])

    async def validate_row(self, row):
        return None

    async def open_row(self, row):
        if row.unread:
            intent = db._conn().execute(
                "SELECT restore_status, thread_key FROM inbox_open_intents ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert tuple(intent) == ("pending", None)
        self.active_thread = (
            "/messaging/thread/unread" if row.unread else "/messaging/thread/read"
        )
        return self.active_thread

    async def messages(self):
        return self.messages_by_thread[self.active_thread]

    async def restore_unread(self, thread_key, folder, baseline):
        assert folder == "focused"
        self.restore_calls.append((thread_key, baseline.index))
        self.unread_by_thread[thread_key] = True
        return True

    async def unread_now(self, folder, baseline):
        assert folder == "focused"
        thread_key = "/messaging/thread/unread" if baseline.unread else "/messaging/thread/read"
        return self.unread_by_thread[thread_key]

    async def download_attachment(self, message_key, index):
        assert (message_key, index) == ("urn:msg:unread-1", 0)
        return "resume.pdf", b"%PDF-1.4\nresume bytes"


class DirectCoordinator:
    async def run_inbound(self, operator, operation):
        return await operation()


class OwnershipCoordinator(DirectCoordinator):
    def __init__(self):
        self.active = False

    async def run_inbound(self, operator, operation):
        assert not self.active
        self.active = True
        try:
            return await operation()
        finally:
            self.active = False


class RecoveryPage:
    def __init__(self, browser):
        self.browser = browser
        self.visited = []

    async def goto(self, url, *, wait_until):
        self.visited.append((url, wait_until))
        self.browser.active_thread = url.removeprefix("https://www.linkedin.com").rstrip("/")


class FailingThenRecoveringBrowser:
    instances = []
    open_lists = []
    restore_calls = []

    def __init__(self, page):
        self.instance_number = len(self.instances)
        self.active_thread = None
        self.page = RecoveryPage(self)
        self.instances.append(self)

    async def verify_identity(self, expected_url):
        assert expected_url == "https://linkedin.com/in/me"
        return "https://www.linkedin.com/in/me"

    async def open_list(self, folder):
        self.open_lists.append(folder)
        assert folder == "focused"

    async def rows(self):
        return [InboxRow(0, "Unread Person", "Unread preview", True, False)]

    async def validate_row(self, row):
        return None

    async def open_row(self, row):
        observation = db._conn().execute(
            "SELECT restore_status, thread_key FROM inbox_open_intents"
        ).fetchone()
        assert tuple(observation) == ("pending", None)
        self.active_thread = "/messaging/thread/unread-recovery"
        return self.active_thread

    async def messages(self):
        return [{
            "source_key": "urn:msg:with-file", "author_name": "Unread Person",
            "body": "Resume attached", "attachments": [{"index": 0, "filename": "resume.pdf"}],
        }]

    async def download_attachment(self, message_key, index):
        assert (message_key, index) == ("urn:msg:with-file", 0)
        raise OSError("attachment download interrupted")

    async def restore_unread(self, thread_key, folder, baseline):
        self.restore_calls.append((self.instance_number, thread_key, folder))
        if self.instance_number == 0:
            raise RuntimeError("page closed during unread restore")
        assert thread_key == self.active_thread
        return True


class TrackingRunner(FakeRunner):
    async def close(self):
        self.closed = True


class UnverifiableUnreadBrowser(FakeInboxBrowser):
    opened = []

    def __init__(self, page):
        super().__init__(page)
        self.page = RecoveryPage(self)

    async def open_row(self, row):
        key = "/messaging/thread/unread" if row.unread else "/messaging/thread/read"
        self.opened.append(key)
        return await super().open_row(row)

    async def restore_unread(self, thread_key, folder, baseline):
        return False


class AllFoldersBrowser(FakeInboxBrowser):
    async def open_list(self, folder):
        self.folder = folder


class CrashBeforeKeyBrowser:
    instances = []

    def __init__(self, page):
        self.number = len(self.instances)
        self.page = RecoveryPage(self)
        self.instances.append(self)

    async def verify_identity(self, expected_url):
        assert expected_url == "https://linkedin.com/in/me"
        return "https://www.linkedin.com/in/me"

    async def open_list(self, folder):
        assert folder == "focused"

    async def rows(self):
        return [InboxRow(0, "Unread Person", "Unread preview", self.number == 0, False)]

    async def validate_row(self, row):
        return None

    async def open_row(self, row):
        intent = db._conn().execute(
            "SELECT restore_status, thread_key FROM inbox_open_intents"
        ).fetchone()
        assert tuple(intent) == ("pending", None)
        if self.number == 0:
            raise RuntimeError("browser closed immediately after click")
        return "/messaging/thread/recovered-without-key"

    async def restore_unread(self, thread_key, folder, baseline):
        if self.number == 0:
            raise RuntimeError("browser already closed")
        assert thread_key == "/messaging/thread/recovered-without-key"
        return True


def test_message_direction_uses_the_verified_account_profile():
    candidate = {
        "author_name": "Candidate", "author_url": "https://www.linkedin.com/in/candidate/",
    }
    assert inbox_sync._contact_url([candidate], None) is None
    assert inbox_sync._direction(candidate, None) == "unknown"

    own = {"author_name": "Me", "author_url": "https://www.linkedin.com/in/me/"}
    own_url = "https://linkedin.com/in/me"
    assert inbox_sync._contact_url([own, candidate], own_url) == "https://linkedin.com/in/candidate"
    assert inbox_sync._direction(own, own_url) == "outbound"
    assert inbox_sync._direction(candidate, own_url) == "inbound"


def test_inbox_scan_requires_a_bound_owned_browser_profile(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    FakeRunner.instances.clear()
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", FakeRunner)
    settings = SimpleNamespace(
        operators={"sender": SimpleNamespace(label="Sender")}, data_dir=tmp_path,
        browser=SimpleNamespace(cdp_url=""),
    )
    try:
        with pytest.raises(ValueError, match="Bind this sender"):
            asyncio.run(inbox_sync.scan_account(settings, DirectCoordinator(), "sender"))
        db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
        settings.browser.cdp_url = "http://localhost:9222"
        with pytest.raises(ValueError, match="owned persistent"):
            asyncio.run(inbox_sync.scan_account(settings, DirectCoordinator(), "sender"))
        assert FakeRunner.instances == []
    finally:
        db.close_db()


def test_inbox_sync_persists_messages_restores_unread_by_thread_and_reports_partial(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
    FakeRunner.instances.clear()
    FakeInboxBrowser.restore_calls.clear()
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", FakeRunner)
    monkeypatch.setattr(inbox_sync, "InboxBrowser", FakeInboxBrowser)
    settings = SimpleNamespace(operators={"sender": SimpleNamespace(label="Me")}, data_dir=tmp_path)

    async def scan_twice():
        first = await inbox_sync.scan_account(settings, DirectCoordinator(), "sender")
        second = await inbox_sync.scan_account(settings, DirectCoordinator(), "sender")
        return first, second

    try:
        first, second = asyncio.run(scan_twice())

        assert first["status"] == second["status"] == "partial"
        assert first["stopped"] and second["stopped"]
        assert first["coverage"]["other"]["status"] == "incomplete"
        conversations = inbox_sync.inbound_store.list_conversations("sender")
        assert {row["thread_key"] for row in conversations} == {
            "/messaging/thread/unread", "/messaging/thread/read",
        }
        assert len(conversations) == 2
        assert sum(row["message_count"] for row in conversations) == 2
        assert sum(row["file_count"] for row in conversations) == 1
        assert {message["source_key"] for row in conversations
                for message in inbox_sync.inbound_store.get_conversation("sender", row["id"])["messages"]} == {
            "urn:msg:unread-1", "urn:msg:read-1",
        }

        conn = db._conn()
        observations = conn.execute(
            "SELECT r.id, c.thread_key, o.linkedin_unread_before_open, o.restore_status, o.restored_at "
            "FROM conversation_scan_observations o "
            "JOIN sync_runs r ON r.id=o.run_id "
            "JOIN conversations c ON c.id=o.conversation_id "
            "ORDER BY r.id, c.thread_key"
        ).fetchall()
        assert len(observations) == 4
        per_run = {}
        for observation in observations:
            per_run.setdefault(observation["id"], {})[observation["thread_key"]] = (
                observation["linkedin_unread_before_open"],
                observation["restore_status"], observation["restored_at"],
            )
        assert len(per_run) == 2
        for values in per_run.values():
            assert values["/messaging/thread/unread"][0:2] == (1, "restored")
            assert values["/messaging/thread/unread"][2]
            assert values["/messaging/thread/read"][0:2] == (0, "not_needed")
            assert values["/messaging/thread/read"][2] is None
        assert len(FakeInboxBrowser.restore_calls) == 2
        assert {key for key, _ in FakeInboxBrowser.restore_calls} == {"/messaging/thread/unread"}
        assert len(FakeRunner.instances) == 2
        assert all(runner.send_calls == 0 for runner in FakeRunner.instances)
    finally:
        db.close_db()


def test_inbox_sync_recovers_unread_after_download_and_restore_failure(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
    TrackingRunner.instances.clear()
    FailingThenRecoveringBrowser.instances.clear()
    FailingThenRecoveringBrowser.open_lists.clear()
    FailingThenRecoveringBrowser.restore_calls.clear()
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", TrackingRunner)
    monkeypatch.setattr(inbox_sync, "InboxBrowser", FailingThenRecoveringBrowser)
    settings = SimpleNamespace(operators={"sender": SimpleNamespace(label="Me")}, data_dir=tmp_path)
    coordinator = OwnershipCoordinator()

    try:
        result = asyncio.run(inbox_sync.scan_account(settings, coordinator, "sender"))

        assert result["status"] == "partial"
        assert result["stopped"] is True
        assert result["coverage"]["focused"]["status"] == "incomplete"
        assert "other" not in result["coverage"]
        assert FailingThenRecoveringBrowser.open_lists == ["focused", "focused"]

        observation = db._conn().execute(
            "SELECT c.thread_key, o.linkedin_unread_before_open, o.restore_status, "
            "o.restored_at, o.error FROM conversation_scan_observations o "
            "JOIN conversations c ON c.id=o.conversation_id"
        ).fetchone()
        assert tuple(observation[:3]) == (
            "/messaging/thread/unread-recovery", 1, "restored",
        )
        assert observation["restored_at"]
        assert observation["error"] == ""
        assert FailingThenRecoveringBrowser.restore_calls == [
            (0, "/messaging/thread/unread-recovery", "focused"),
            (1, "/messaging/thread/unread-recovery", "focused"),
        ]
        assert FailingThenRecoveringBrowser.instances[1].page.visited == [(
            "https://www.linkedin.com/messaging/thread/unread-recovery/",
            "domcontentloaded",
        )]
        assert len(TrackingRunner.instances) == 2
        assert all(runner.closed for runner in TrackingRunner.instances)
        assert [runner.start_calls for runner in TrackingRunner.instances] == [[True], [None]]
        assert coordinator.active is False
    finally:
        db.close_db()


def test_inbox_sync_stops_after_unverified_unread_marker(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
    TrackingRunner.instances.clear()
    UnverifiableUnreadBrowser.opened.clear()
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", TrackingRunner)
    monkeypatch.setattr(inbox_sync, "InboxBrowser", UnverifiableUnreadBrowser)
    settings = SimpleNamespace(operators={"sender": SimpleNamespace(label="Me")}, data_dir=tmp_path)

    try:
        result = asyncio.run(inbox_sync.scan_account(settings, OwnershipCoordinator(), "sender"))

        assert result["status"] == "partial"
        assert result["stopped"] is True
        assert UnverifiableUnreadBrowser.opened == ["/messaging/thread/unread"]
        assert "other" not in result["coverage"]
        assert len(inbox_sync.inbound_store.list_conversations("sender")) == 1
        pending = inbox_sync.inbound_store.pending_unread_restores("sender", result["run_id"])
        assert len(pending) == 1
        assert pending[0]["thread_key"] == "/messaging/thread/unread"
        status = db._conn().execute(
            "SELECT restore_status FROM conversation_scan_observations WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()[0]
        assert status == "failed"
    finally:
        db.close_db()


def test_inbox_sync_recovers_unread_when_browser_closes_before_thread_key(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
    TrackingRunner.instances.clear()
    CrashBeforeKeyBrowser.instances.clear()
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", TrackingRunner)
    monkeypatch.setattr(inbox_sync, "InboxBrowser", CrashBeforeKeyBrowser)
    settings = SimpleNamespace(operators={"sender": SimpleNamespace(label="Me")}, data_dir=tmp_path)

    try:
        result = asyncio.run(inbox_sync.scan_account(settings, OwnershipCoordinator(), "sender"))

        assert result["status"] == "partial"
        assert result["stopped"] is True
        assert "other" not in result["coverage"]
        intent = db._conn().execute("SELECT * FROM inbox_open_intents").fetchone()
        assert intent["thread_key"] == "/messaging/thread/recovered-without-key"
        assert intent["restore_status"] == "restored"
        assert intent["restored_at"]
        assert len(CrashBeforeKeyBrowser.instances) == 2
        assert all(runner.closed for runner in TrackingRunner.instances)
        assert inbox_sync.inbound_store.pending_open_intents("sender") == []
    finally:
        db.close_db()


def test_bounded_scan_reports_partial_coverage_without_a_hard_stop(tmp_path, monkeypatch):
    db.close_db()
    db.init_db(tmp_path / "inbound.sqlite")
    db.create_operator("sender", "Sender", "profiles/sender")
    db.set_operator_self_profile_url("sender", "https://linkedin.com/in/me")
    monkeypatch.setattr(inbox_sync, "LinkedInRunner", FakeRunner)
    monkeypatch.setattr(inbox_sync, "InboxBrowser", AllFoldersBrowser)
    settings = SimpleNamespace(operators={"sender": SimpleNamespace(label="Me")}, data_dir=tmp_path)

    try:
        result = asyncio.run(inbox_sync.scan_account(settings, DirectCoordinator(), "sender"))
        assert result["status"] == "partial"
        assert result["stopped"] is False
        assert set(result["coverage"]) == set(FOLDERS)
        assert all(section["status"] == "incomplete" for section in result["coverage"].values())
    finally:
        db.close_db()
