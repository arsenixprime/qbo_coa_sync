"""Hierarchy tests — top-down ordering, parent-resolution refusal."""

from unittest import mock

import pytest

from qbo_coa_sync.api import sync


def test_qbo_topdown_orders_parents_before_children():
    rows = [
        {"qbo_id": "leaf", "parent_qbo_id": "mid"},
        {"qbo_id": "mid", "parent_qbo_id": "root"},
        {"qbo_id": "root", "parent_qbo_id": ""},
    ]
    with mock.patch("frappe.get_all", return_value=rows):
        ordered = sync._qbo_topdown_order(["leaf", "mid", "root"])
    assert ordered.index("root") < ordered.index("mid") < ordered.index("leaf")


def test_qbo_topdown_handles_parent_outside_set():
    # If a parent isn't in the set, that node is treated as a depth-0 root.
    rows = [
        {"qbo_id": "a", "parent_qbo_id": "x"},
        {"qbo_id": "b", "parent_qbo_id": "a"},
    ]
    with mock.patch("frappe.get_all", return_value=rows):
        ordered = sync._qbo_topdown_order(["b", "a"])
    assert ordered == ["a", "b"]


def test_resolve_erp_parent_refuses_orphans():
    """If QBO parent isn't synced yet, _resolve_erp_parent must throw."""
    with mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value=None), \
         mock.patch("frappe.throw", side_effect=RuntimeError("orphan parent")):
        with pytest.raises(RuntimeError):
            sync._resolve_erp_parent("99", "Acme")


def test_resolve_erp_parent_returns_existing():
    with mock.patch("qbo_coa_sync.api.sync._erp_account_by_qbo_id", return_value="Cash - Acme"):
        assert sync._resolve_erp_parent("99", "Acme") == "Cash - Acme"


def test_resolve_erp_parent_none_for_root():
    # No QBO parent → caller should fall through to _ensure_root_parent; here the helper
    # itself returns None.
    assert sync._resolve_erp_parent(None, "Acme") is None
    assert sync._resolve_erp_parent("", "Acme") is None
