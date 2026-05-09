"""ERPNext → QBO sync: payload shape, SyncToken round-trip, write-back."""

from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _frappe_only_for():
    with mock.patch("frappe.only_for"):
        yield


def _settings(company="Acme"):
    return SimpleNamespace(erpnext_company=company)


def _erp_doc(**overrides):
    base = dict(
        name="Cash - A", company="Acme", account_name="Cash", account_number="1100",
        qbo_description="Operating cash", root_type="Asset", account_type="Bank",
        parent_account="", quickbooks_id="", quickbooks_sync_token="0", disabled=0,
    )
    base.update(overrides)

    def as_dict():
        return dict(base)
    obj = SimpleNamespace(**base)
    obj.as_dict = as_dict
    obj.save = mock.Mock()
    return obj


class _FakeCacheDoc:
    """Stand-in for ``frappe.get_doc('QuickBooks Account Cache', qbo_id)``."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self._saved = False

    def set(self, k, v):
        setattr(self, k, v)

    def save(self, **_kw):
        self._saved = True


def test_create_sends_required_fields_and_writes_back_id():
    from qbo_coa_sync.api import sync

    erp = _erp_doc()
    fake_client = mock.Mock()
    fake_client.create_account.return_value = {
        "Id": "77", "SyncToken": "1", "Name": "Cash", "AccountType": "Bank", "Active": True,
    }

    # Route get_doc by doctype: Account → erp, QuickBooks Account Cache → cache_doc.
    cache_doc = _FakeCacheDoc(qbo_id="77")

    def get_doc(*args, **kwargs):
        if args and args[0] == "Account":
            return erp
        if args and args[0] == "QuickBooks Account Cache":
            return cache_doc
        # Inserting a new cache doc — frappe.get_doc({"doctype": "..."}).
        if args and isinstance(args[0], dict):
            return cache_doc
        raise AssertionError(f"Unexpected get_doc args: {args} {kwargs}")

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.erpnext_to_qbo", return_value=("Bank", None)), \
         mock.patch("qbo_coa_sync.api.qbo_client.QBOClient", return_value=fake_client), \
         mock.patch("frappe.db.exists", return_value=False), \
         mock.patch("frappe.get_doc", side_effect=get_doc):
        sync.sync_erpnext_to_qbo("Cash - A")

    fake_client.create_account.assert_called_once()
    payload = fake_client.create_account.call_args[0][0]
    assert payload["Name"] == "Cash"
    assert payload["AcctNum"] == "1100"
    assert payload["AccountType"] == "Bank"
    assert payload["Description"] == "Operating cash"
    assert payload["Active"] is True
    assert "Id" not in payload
    assert erp.quickbooks_id == "77"
    assert erp.quickbooks_sync_token == "1"


def test_update_includes_id_synctoken_and_writes_back_new_token():
    from qbo_coa_sync.api import sync

    erp = _erp_doc(quickbooks_id="77", quickbooks_sync_token="3")
    fake_client = mock.Mock()
    fake_client.update_account.return_value = {"Id": "77", "SyncToken": "4", "Name": "Cash"}

    cache_doc = _FakeCacheDoc(qbo_id="77", sync_token="3")

    def get_doc(*args, **kwargs):
        if args and args[0] == "Account":
            return erp
        return cache_doc

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.erpnext_to_qbo", return_value=("Bank", None)), \
         mock.patch("qbo_coa_sync.api.qbo_client.QBOClient", return_value=fake_client), \
         mock.patch("frappe.db.exists", return_value=True), \
         mock.patch("frappe.get_doc", side_effect=get_doc):
        sync.sync_erpnext_to_qbo("Cash - A")

    fake_client.update_account.assert_called_once()
    payload = fake_client.update_account.call_args[0][0]
    assert payload["Id"] == "77"
    assert payload["SyncToken"] == "3"
    assert erp.quickbooks_sync_token == "4"


def test_qbo_client_update_marks_sparse_true(monkeypatch):
    """QBOClient.update_account always sets sparse=True and operation=update."""
    from qbo_coa_sync.api import qbo_client

    captured = {}

    def fake_request(self, method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = kw.get("params")
        captured["json_body"] = kw.get("json_body")
        return {"Account": {"Id": "1", "SyncToken": "1"}}

    monkeypatch.setattr(qbo_client.QBOClient, "_request", fake_request)
    client = qbo_client.QBOClient.__new__(qbo_client.QBOClient)
    client.environment = "Sandbox"
    client.realm_id = "abc"

    client.update_account({"Id": "1", "SyncToken": "0", "Name": "X"})

    assert captured["method"] == "POST"
    assert captured["path"] == "account"
    assert captured["params"] == {"operation": "update"}
    assert captured["json_body"]["sparse"] is True
