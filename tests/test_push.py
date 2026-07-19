import json

from httpx import ASGITransport, AsyncClient
from pywebpush import WebPushException

from agentdeck import push as push_mod
from agentdeck.app import create_app
from agentdeck.config import AppConfig, HistoryConfig, PushConfig
from agentdeck.db import Db
from agentdeck.push import PushService


def _svc(tmp_path, *, enabled=True, subject="mailto:you@example.com"):
    db = Db(tmp_path / "push.db")
    return PushService(PushConfig(enabled=enabled, subject=subject), db), db


def test_push_generates_and_persists_vapid_keys(tmp_path):
    svc, db = _svc(tmp_path)
    assert not svc.enabled  # no keypair until started
    svc.start()
    assert svc.enabled
    key = svc.public_key
    assert key and len(key) > 80  # base64url uncompressed P-256 point
    assert db.load_vapid_keys() is not None
    # a second service on the same DB reuses the stored keypair
    svc2 = PushService(PushConfig(enabled=True), db)
    svc2.start()
    assert svc2.public_key == key


def test_vapid_key_is_signable(tmp_path):
    # Regression (issue #7): the loaded VAPID key must be usable by pywebpush's
    # signer. Passing a raw PEM *string* to webpush routes through
    # Vapid.from_string and fails ("could not deserialize key data"); we hold a
    # Vapid instance, so it must be able to sign the auth header.
    svc, _ = _svc(tmp_path)
    svc.start()
    assert svc._vapid is not None
    headers = svc._vapid.sign(
        {"sub": "mailto:you@example.com", "aud": "https://push.example", "exp": 9999999999}
    )
    assert headers.get("Authorization")


def test_push_disabled_is_noop(tmp_path):
    svc, db = _svc(tmp_path, enabled=False)
    svc.start()
    assert not svc.enabled
    assert svc.public_key is None
    assert svc.send_to_all("hi") == 0
    assert db.load_vapid_keys() is None  # no keys generated while disabled


def test_subscribe_and_unsubscribe(tmp_path):
    svc, db = _svc(tmp_path)
    svc.start()
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
    assert svc.subscribe(sub) is True
    assert db.load_push_subscriptions() == [sub]
    assert svc.subscribe({"endpoint": "x"}) is False  # missing keys
    assert svc.subscribe("nope") is False
    svc.unsubscribe(sub["endpoint"])
    assert db.load_push_subscriptions() == []


def test_send_to_all_prunes_gone_subscriptions(tmp_path, monkeypatch):
    svc, db = _svc(tmp_path)
    svc.start()
    good = {"endpoint": "https://push.example/good", "keys": {"p256dh": "k", "auth": "a"}}
    gone = {"endpoint": "https://push.example/gone", "keys": {"p256dh": "k", "auth": "a"}}
    svc.subscribe(good)
    svc.subscribe(gone)

    calls = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_webpush(*, subscription_info, data, vapid_private_key, vapid_claims):
        calls.append(subscription_info["endpoint"])
        assert json.loads(data)["title"] == "New attention"
        assert vapid_claims["sub"] == "mailto:you@example.com"
        assert vapid_private_key  # the loaded PEM is passed through
        if subscription_info["endpoint"].endswith("gone"):
            raise WebPushException("gone", response=_Resp(410))

    monkeypatch.setattr(push_mod, "webpush", fake_webpush)
    sent = svc.send_to_all("New attention", "body", "/sessions/x")
    assert sent == 1  # one delivered, one gone
    assert set(calls) == {good["endpoint"], gone["endpoint"]}
    # the 410 endpoint is pruned; the good one remains
    assert [s["endpoint"] for s in db.load_push_subscriptions()] == [good["endpoint"]]


def _app(tmp_path, *, enabled, subject=""):
    config = AppConfig(
        history=HistoryConfig(enabled=True, db_path=str(tmp_path / "app.db")),
        push=PushConfig(enabled=enabled, subject=subject),
    )
    app = create_app(config)
    app.state.push.start()  # lifespan doesn't run under ASGITransport
    return app


async def test_push_routes_subscribe_and_unsubscribe(tmp_path):
    app = _app(tmp_path, enabled=True, subject="mailto:you@example.com")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        pk = await c.get("/push/public-key")
        assert pk.status_code == 200
        assert pk.json()["enabled"] is True and pk.json()["key"]
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
        r = await c.post("/push/subscribe", json=sub, headers={"origin": "http://test"})
        assert r.status_code == 201
        assert app.state.db.load_push_subscriptions() == [sub]
        u = await c.post(
            "/push/unsubscribe",
            json={"endpoint": sub["endpoint"]},
            headers={"origin": "http://test"},
        )
        assert u.status_code == 204
        assert app.state.db.load_push_subscriptions() == []


async def test_push_subscribe_rejected_when_disabled(tmp_path):
    app = _app(tmp_path, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/push/public-key")).json() == {"enabled": False, "key": None}
        r = await c.post(
            "/push/subscribe",
            json={"endpoint": "x", "keys": {"p256dh": "k", "auth": "a"}},
            headers={"origin": "http://test"},
        )
        assert r.status_code == 503
