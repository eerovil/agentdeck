import sqlite3
from datetime import UTC, datetime

from agentdeck.db import Db, NullDb, make_db
from agentdeck.models import Session, SessionStatus, UsageSnapshot
from agentdeck.state import AppState


def _snap(pct):
    return UsageSnapshot(
        account_key="claude_code:main",
        five_hour_pct=pct,
        five_hour_resets_at=None,
        seven_day_pct=1.0,
        seven_day_resets_at=None,
        fetched_at=datetime.now(UTC),
    )


def test_usage_history_roundtrip(tmp_path):
    db = make_db(True, str(tmp_path / "h.db"), 30)
    try:
        for pct in (10.0, 20.0, 30.0):
            db.record_usage(_snap(pct))
        recent = db.recent_five_hour("claude_code:main", limit=10)
        assert recent == [10.0, 20.0, 30.0]  # chronological
    finally:
        db.close()


def test_latest_usage_returns_most_recent_snapshot(tmp_path):
    db = make_db(True, str(tmp_path / "h.db"), 30)
    try:
        for pct in (10.0, 20.0, 30.0):
            db.record_usage(_snap(pct))
        latest = db.latest_usage("claude_code:main")
        assert latest is not None
        assert latest.five_hour_pct == 30.0
        # Reset times aren't persisted in history.
        assert latest.five_hour_resets_at is None
    finally:
        db.close()


def test_latest_usage_none_when_empty(tmp_path):
    db = make_db(True, str(tmp_path / "h.db"), 30)
    try:
        assert db.latest_usage("claude_code:main") is None
    finally:
        db.close()
    assert NullDb().latest_usage("claude_code:main") is None


def test_sessions_seen_upsert(tmp_path):
    db = make_db(True, str(tmp_path / "h.db"), 30)
    try:
        s = Session(
            key="claude_code:main:sid",
            account_key="claude_code:main",
            session_id="sid",
            status=SessionStatus.IDLE,
            title="T",
        )
        db.upsert_sessions_seen([s])
        db.upsert_sessions_seen([s])  # second time updates, no duplicate row / no error
    finally:
        db.close()


def test_generated_title_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    record = db.record_generated_title("codex:test:sid", "Fix refund errors", "sig-1")
    assert db.load_generated_titles() == {"codex:test:sid": record}
    db.close()

    reopened = Db(path)
    try:
        loaded = reopened.load_generated_titles()["codex:test:sid"]
        assert loaded.title == "Fix refund errors"
        assert loaded.evidence_signature == "sig-1"
        assert loaded.updated_at == record.updated_at
    finally:
        reopened.close()


def test_manual_new_chat_cwd_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    assert db.load_manual_new_chat_cwd() is None
    db.record_manual_new_chat_cwd("/srv/first")
    db.record_manual_new_chat_cwd("/srv/last")
    db.close()

    reopened = Db(path)
    try:
        assert reopened.load_manual_new_chat_cwd() == "/srv/last"
    finally:
        reopened.close()


def test_delegated_session_marker_survives_reopen_and_rescan(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.mark_delegated_session("codex:test:child")
    db.close()

    reopened = Db(path)
    try:
        restored = AppState(db=reopened)
        session = Session(
            key="codex:test:child",
            account_key="codex:test",
            session_id="child",
            status=SessionStatus.IDLE,
        )
        restored.replace_account_sessions("codex:test", [session])
        assert restored.sessions[session.key].is_delegated is True
    finally:
        reopened.close()


def test_delegation_parent_survives_reopen_and_nests(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.mark_delegated_session("codex:test:child", parent_session_id="p")
    db.close()

    reopened = Db(path)
    try:
        # The raw parent session_id is persisted, not a resolved key.
        assert reopened.load_delegation_parents() == {"codex:test:child": "p"}
        restored = AppState(db=reopened)
        assert restored.recorded_delegation_parents == {"codex:test:child": "p"}
        for s in (
            Session(
                key="claude_code:main:p",
                account_key="claude_code:main",
                session_id="p",
                status=SessionStatus.LIVE,
            ),
            Session(
                key="codex:test:child",
                account_key="codex:test",
                session_id="child",
                status=SessionStatus.LIVE,
            ),
        ):
            restored.update_session(s)
        children = restored.session_presentation().children_of
        assert [c.key for c in children["claude_code:main:p"]] == ["codex:test:child"]
    finally:
        reopened.close()


def test_delegated_sessions_migration_adds_parent_column_to_existing_db(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE delegated_sessions (session_key TEXT PRIMARY KEY,"
        " created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO delegated_sessions VALUES (?, ?)",
        ("codex:test:old", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        # Old row survives the ALTER, with a NULL (thus unlisted) parent.
        assert db.load_delegated_sessions() == {"codex:test:old"}
        assert db.load_delegation_parents() == {}
        # The new column is usable: a parent can now be recorded and read back.
        db.record_delegated_session("codex:test:new", "parent-uuid")
        assert db.load_delegation_parents() == {"codex:test:new": "parent-uuid"}
    finally:
        db.close()


def test_assistant_handled_round_trip(tmp_path):
    db = Db(tmp_path / "history.db")
    try:
        db.record_assistant_handled(
            "codex:test:one", "evidence-1", "waiting", "Needs input", "Pick one."
        )
        assert db.load_assistant_handled() == {
            "codex:test:one": ("evidence-1", "waiting", "Needs input", "Pick one.")
        }

        db.record_assistant_handled("codex:test:one", "evidence-2")
        assert db.load_assistant_handled() == {
            "codex:test:one": ("evidence-2", None, None, None)
        }

        db.delete_assistant_handled("codex:test:one")
        assert db.load_assistant_handled() == {}
    finally:
        db.close()


def test_assistant_checkpoint_round_trip(tmp_path):
    db = Db(tmp_path / "history.db")
    try:
        assert db.load_assistant_checkpoint() is None
        checkpoint = {
            "version": 1,
            "view": {"state": "ready", "summary": "One item."},
            "analysis_signature": "material-evidence",
        }
        db.record_assistant_checkpoint(checkpoint)
        assert db.load_assistant_checkpoint() == checkpoint

        replacement = {"version": 1, "view": {"state": "ready", "summary": "Clear."}}
        db.record_assistant_checkpoint(replacement)
        assert db.load_assistant_checkpoint() == replacement
    finally:
        db.close()


def test_assistant_handled_schema_adds_restore_metadata_to_existing_db(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE assistant_handled (session_key TEXT PRIMARY KEY,"
        " evidence_signature TEXT NOT NULL, handled_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO assistant_handled VALUES (?, ?, ?)",
        ("codex:test:old", "old-evidence", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        assert db.load_assistant_handled() == {
            "codex:test:old": ("old-evidence", None, None, None)
        }
        db.record_assistant_handled(
            "codex:test:old", "old-evidence", "waiting", "Restorable", "Details"
        )
        assert db.load_assistant_handled()["codex:test:old"] == (
            "old-evidence",
            "waiting",
            "Restorable",
            "Details",
        )
    finally:
        db.close()


def test_disabled_history_is_nulldb():
    db = make_db(False, "/nonexistent/should-not-be-created.db", 30)
    assert isinstance(db, NullDb)
    db.record_usage(_snap(50.0))  # no-ops
    assert db.recent_five_hour("x") == []


def test_write_and_query_degrade_on_sqlite_error(tmp_path):
    # The durability seam swallows sqlite errors and degrades — this pins the one
    # consolidated contract instead of an arbitrary per-method except.
    db = Db(tmp_path / "degrade.db")

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("forced")

    db._conn = _Boom()
    db.record_usage(_snap(1.0))  # a write must degrade (not raise)
    assert db.recent_five_hour("claude_code:main") == []  # a query returns its default


def test_usage_history_pruned_on_open(tmp_path):
    from datetime import timedelta

    path = tmp_path / "prune.db"
    db = Db(path, retention_days=1)
    old_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db._write(
        "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
        " VALUES (?, ?, ?, ?)",
        ("claude_code:main", old_ts, 5.0, None),
    )
    db.record_usage(_snap(9.0))
    db.close()
    reopened = Db(path, retention_days=1)  # prune runs on open
    assert reopened.recent_five_hour("claude_code:main") == [9.0]  # old row pruned
    reopened.close()


def test_retention_zero_keeps_everything(tmp_path):
    from datetime import timedelta

    path = tmp_path / "keep.db"
    db = Db(path, retention_days=0)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    db._write(
        "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
        " VALUES (?, ?, ?, ?)",
        ("claude_code:main", old_ts, 5.0, None),
    )
    db.close()
    reopened = Db(path, retention_days=0)  # 0 disables pruning
    assert reopened.recent_five_hour("claude_code:main") == [5.0]
    reopened.close()
