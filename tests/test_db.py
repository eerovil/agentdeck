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
