"""QBO → ERPNext sync: covers create, update existing linked, link by number,
and idempotency on re-run.

These tests stub the Frappe ORM calls used inside ``sync_qbo_to_erpnext`` rather than
booting a real site. They verify the call shape, not Frappe's persistence layer.
"""

from types import SimpleNamespace
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _frappe_only_for():
    """`frappe.only_for` is called at the top of every whitelisted method — stub it."""
    with mock.patch("frappe.only_for"):
        yield


@pytest.fixture
def cache_row():
    return {
        "qbo_id": "42",
        "name_field": "Cash",
        "acct_num": "1100",
        "account_type": "Bank",
        "account_sub_type": "",
        "parent_qbo_id": "",
        "fully_qualified_name": "Cash",
        "description": "",
        "currency": "USD",
        "active": 1,
        "sync_token": "0",
    }


def _settings(company="Acme"):
    return SimpleNamespace(erpnext_company=company)


def test_creates_new_account_when_no_link_no_number_match(cache_row):
    from qbo_coa_sync.api import sync

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.qbo_to_erpnext", return_value=("Asset", "Bank")), \
         mock.patch("qbo_coa_sync.api.sync._qbo_cache", return_value=cache_row), \
         mock.patch("qbo_coa_sync.api.sync._qbo_has_children", return_value=False), \
         mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value=None), \
         mock.patch("qbo_coa_sync.api.sync._ensure_root_parent", return_value="Application of Funds (Assets) - A"), \
         mock.patch("frappe.db.get_value", return_value=None), \
         mock.patch("frappe.get_doc") as get_doc, \
         mock.patch("frappe.new_doc") as new_doc:
        new_acc = SimpleNamespace(
            name="", company="", parent_account="", is_group=0,
            insert=mock.Mock(), save=mock.Mock(),
        )
        # set= no-op
        new_doc.return_value = new_acc

        result = sync.sync_qbo_to_erpnext("42")

        assert new_acc.company == "Acme"
        assert new_acc.parent_account == "Application of Funds (Assets) - A"
        assert new_acc.account_name == "Cash"
        assert new_acc.account_number == "1100"
        assert new_acc.root_type == "Asset"
        assert new_acc.account_type == "Bank"
        assert new_acc.quickbooks_id == "42"
        assert new_acc.quickbooks_sync_token == "0"
        assert new_acc.disabled == 0
        new_acc.insert.assert_called_once()
        assert result["ok"] is True


def test_updates_already_linked_account(cache_row):
    from qbo_coa_sync.api import sync

    existing = SimpleNamespace(
        name="Cash - A", parent_account="Existing Parent", is_group=0,
        save=mock.Mock(),
    )

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.qbo_to_erpnext", return_value=("Asset", "Bank")), \
         mock.patch("qbo_coa_sync.api.sync._qbo_cache", return_value=cache_row), \
         mock.patch("qbo_coa_sync.api.sync._qbo_has_children", return_value=False), \
         mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value="Cash - A"), \
         mock.patch("qbo_coa_sync.api.sync._ensure_root_parent", return_value="Root - A"), \
         mock.patch("frappe.get_doc", return_value=existing):
        result = sync.sync_qbo_to_erpnext("42")

        existing.save.assert_called_once()
        # Existing parent should be preserved (not silently moved).
        assert existing.parent_account == "Existing Parent"
        assert existing.quickbooks_id == "42"
        assert result["erpnext_account"] == "Cash - A"


def test_links_by_number_when_unlinked_account_with_same_number_exists(cache_row):
    from qbo_coa_sync.api import sync

    existing = SimpleNamespace(
        name="Cash - A", parent_account="", is_group=0,
        save=mock.Mock(),
    )

    db_get_value = mock.Mock(side_effect=[
        "Cash - A",  # candidate by number
        None,        # candidate.quickbooks_id is empty
    ])

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.qbo_to_erpnext", return_value=("Asset", "Bank")), \
         mock.patch("qbo_coa_sync.api.sync._qbo_cache", return_value=cache_row), \
         mock.patch("qbo_coa_sync.api.sync._qbo_has_children", return_value=False), \
         mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value=None), \
         mock.patch("qbo_coa_sync.api.sync._ensure_root_parent", return_value="Root - A"), \
         mock.patch("frappe.db.get_value", db_get_value), \
         mock.patch("frappe.get_doc", return_value=existing):
        sync.sync_qbo_to_erpnext("42")

    assert existing.quickbooks_id == "42"
    existing.save.assert_called_once()


def test_idempotent_on_rerun(cache_row):
    """Two consecutive syncs of the same QBO id should both succeed and converge."""
    from qbo_coa_sync.api import sync

    existing = SimpleNamespace(
        name="Cash - A", parent_account="Root - A", is_group=0,
        save=mock.Mock(),
    )

    with mock.patch("frappe.get_single", return_value=_settings()), \
         mock.patch("qbo_coa_sync.api.sync.qbo_to_erpnext", return_value=("Asset", "Bank")), \
         mock.patch("qbo_coa_sync.api.sync._qbo_cache", return_value=cache_row), \
         mock.patch("qbo_coa_sync.api.sync._qbo_has_children", return_value=False), \
         mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value="Cash - A"), \
         mock.patch("qbo_coa_sync.api.sync._ensure_root_parent", return_value="Root - A"), \
         mock.patch("frappe.get_doc", return_value=existing):
        sync.sync_qbo_to_erpnext("42")
        sync.sync_qbo_to_erpnext("42")
    assert existing.save.call_count == 2
    # Final state matches QBO source.
    assert existing.quickbooks_id == "42"
    assert existing.account_name == "Cash"
