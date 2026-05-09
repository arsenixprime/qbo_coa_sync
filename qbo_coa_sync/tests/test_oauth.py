"""OAuth flow tests — state nonce, refresh path, expiry handling.

Mocks the Intuit token endpoint instead of hitting the network.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest


def _settings(**kw):
    base = dict(
        client_id="cid", connection_status="Not Connected",
        realm_id=None, access_token=None, refresh_token=None,
        access_token_expires_at=None, refresh_token_expires_at=None,
        scope="com.intuit.quickbooks.accounting",
    )
    base.update(kw)
    s = SimpleNamespace(doctype="QuickBooks Settings", name="QuickBooks Settings", **base)
    s.save = mock.Mock()
    return s


def test_state_nonce_round_trip():
    from qbo_coa_sync.api import oauth

    cache = {}

    class FakeCache:
        def set_value(self, k, v, expires_in_sec=None): cache[k] = v
        def get_value(self, k): return cache.get(k)
        def delete_value(self, k): cache.pop(k, None)

    with mock.patch("frappe.cache", return_value=FakeCache()):
        oauth._store_state("alice", "abc123")
        assert oauth._consume_state("alice") == "abc123"
        # Single-use: a second consume should yield None.
        assert oauth._consume_state("alice") is None


def test_get_valid_access_token_returns_cached_when_fresh():
    from qbo_coa_sync.api import oauth
    from frappe.utils import now_datetime

    s = _settings(
        access_token="live-token",
        access_token_expires_at=now_datetime() + timedelta(minutes=30),
    )
    with mock.patch("frappe.get_single", return_value=s), \
         mock.patch("qbo_coa_sync.api.oauth._get_secret", return_value="live-token"):
        assert oauth.get_valid_access_token() == "live-token"


def test_get_valid_access_token_refreshes_when_expired(monkeypatch):
    from qbo_coa_sync.api import oauth
    from frappe.utils import now_datetime

    s = _settings(
        access_token="stale", refresh_token="ref",
        access_token_expires_at=now_datetime() - timedelta(minutes=5),
    )

    captured = {}

    def fake_post(url, data=None, auth=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return mock.Mock(
            status_code=200,
            json=lambda: {
                "access_token": "fresh", "refresh_token": "ref2",
                "expires_in": 3600, "x_refresh_token_expires_in": 8640000,
            },
        )

    secrets_seq = ["ref", "fresh"]  # client_secret lookup, then access_token after persist

    def fake_get_secret(doc, field):
        if field == "refresh_token":
            return "ref"
        if field == "client_secret":
            return "secret"
        if field == "access_token":
            return "fresh"
        return None

    with mock.patch("frappe.get_single", return_value=s), \
         mock.patch("qbo_coa_sync.api.oauth._get_secret", side_effect=fake_get_secret), \
         mock.patch("requests.post", side_effect=fake_post):
        token = oauth.get_valid_access_token()

    assert token == "fresh"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["refresh_token"] == "ref"
    assert s.access_token == "fresh"
    assert s.refresh_token == "ref2"
    assert s.connection_status == "Connected"


def test_get_valid_access_token_marks_expired_on_refresh_failure():
    import frappe
    from qbo_coa_sync.api import oauth
    from frappe.utils import now_datetime

    s = _settings(
        access_token=None, refresh_token="ref",
        access_token_expires_at=now_datetime() - timedelta(hours=1),
    )

    def fake_post(*a, **kw):
        return mock.Mock(status_code=400, text="invalid_grant", json=lambda: {})

    with mock.patch("frappe.get_single", return_value=s), \
         mock.patch("qbo_coa_sync.api.oauth._get_secret", side_effect=lambda doc, f: {"refresh_token": "ref", "client_secret": "secret"}.get(f)), \
         mock.patch("frappe.log_error"), \
         mock.patch("requests.post", side_effect=fake_post):
        with pytest.raises(frappe.AuthenticationError):
            oauth.get_valid_access_token()

    assert s.connection_status == "Token Expired"


def test_callback_validates_state_mismatch():
    from qbo_coa_sync.api import oauth

    cache = {}

    class FakeCache:
        def set_value(self, k, v, expires_in_sec=None): cache[k] = v
        def get_value(self, k): return cache.get(k)
        def delete_value(self, k): cache.pop(k, None)

    with mock.patch("frappe.cache", return_value=FakeCache()), \
         mock.patch("frappe.session", SimpleNamespace(user="alice")), \
         mock.patch("qbo_coa_sync.api.oauth._render_close_page") as render:
        oauth._store_state("alice", "expected")
        oauth.callback(code="abc", state="WRONG", realmId="r1")
        render.assert_called_once()
        assert render.call_args[0][0] is False
        assert "state mismatch" in render.call_args[0][1].lower()
