"""Unit tests for the Dismissals store — in isolation, round-tripping the real DB."""

from agentdeck.db import Db
from agentdeck.dismissals import _WAITING_DONE_KIND, Dismissals
from agentdeck.triage import AssistantInsight


def test_insight_dismissal_round_trips_through_the_db(tmp_path):
    db = Db(tmp_path / "d.db")
    store = Dismissals.load(db)
    insight = AssistantInsight("codex:t:1", "finished", "PR ready", "review it")
    store.dismiss_insight("codex:t:1", "sig-A", insight)

    reloaded = Dismissals.load(db)  # reload from the real table
    assert reloaded.insight_signature("codex:t:1") == "sig-A"
    assert reloaded.insight("codex:t:1") == insight
    assert reloaded.is_waiting_dismissed("codex:t:1") is False


def test_waiting_dismissal_round_trips_with_exact_sentinel_row(tmp_path):
    db = Db(tmp_path / "d.db")
    Dismissals.load(db).dismiss_waiting("codex:t:2", "msig-1", "Which option?")
    # The on-disk row must stay byte-identical: sentinel kind, "Marked done"
    # headline, detail = the question, evidence_signature = the message signature.
    raw = db.load_assistant_handled()
    assert raw["codex:t:2"] == ("msig-1", _WAITING_DONE_KIND, "Marked done", "Which option?")

    reloaded = Dismissals.load(db)
    assert reloaded.is_waiting_dismissed("codex:t:2") is True
    assert reloaded.insight("codex:t:2") is None  # a waiting dismissal carries no insight


def test_one_row_per_session_and_load_order(tmp_path):
    db = Db(tmp_path / "d.db")
    store = Dismissals.load(db)
    store.dismiss_insight("a", "sa", AssistantInsight("a", "finished", "A", "d"))
    store.dismiss_insight("b", "sb", AssistantInsight("b", "finished", "B", "d"))
    # latest_insight follows handled_at (= insertion) order — newest wins.
    assert Dismissals.load(db).latest_insight()[0] == "b"
    # A re-dismiss overwrites the single row rather than duplicating it.
    store.dismiss_insight("a", "sa2", AssistantInsight("a", "finished", "A2", "d"))
    assert len(db.load_assistant_handled()) == 2


def test_restore_and_drop_delete_the_row(tmp_path):
    db = Db(tmp_path / "d.db")
    store = Dismissals.load(db)
    store.dismiss_insight("a", "sa", AssistantInsight("a", "finished", "A", "d"))
    assert store.restore("a").signature == "sa"
    assert db.load_assistant_handled() == {}
    assert store.restore("a") is None  # idempotent
    store.dismiss_insight("b", "sb", AssistantInsight("b", "finished", "B", "d"))
    store.drop_insight("b")
    assert db.load_assistant_handled() == {}


def test_insight_revert_on_evidence_change():
    store = Dismissals(db=None)  # no persistence needed for the predicate logic
    store.dismiss_insight("k", "sig", AssistantInsight("k", "finished", "PR", "d"))

    def active(sigs):
        return store.active_keys(sigs, lambda _k: None, lambda _s: "")

    assert "k" in active({"k": "sig"})  # signature unchanged -> still dismissed
    assert "k" not in active({"k": "moved"})  # evidence changed -> reverts
    assert "k" in active({})  # not seen this run -> left alone
    store.prune_stale({"k": "moved"}, lambda _k: None, lambda _s: "")
    assert store.insight_keys() == []


def test_waiting_revert_on_message_change():
    store = Dismissals(db=None)
    store.dismiss_waiting("w", "msig", "Q?")

    class _S:
        question = "Q?"

    # session present, message signature unchanged -> still dismissed
    assert "w" in store.active_keys({}, lambda _k: _S(), lambda _s: "msig")
    # a gone session keeps the waiting dismissal in the query (absent, not changed)
    assert "w" in store.active_keys({}, lambda _k: None, lambda _s: "msig")
    # a new message (changed signature) -> prune drops it
    store.prune_stale({}, lambda _k: _S(), lambda _s: "different")
    assert store.is_waiting_dismissed("w") is False
