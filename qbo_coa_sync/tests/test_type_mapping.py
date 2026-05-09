"""Type-mapping resolution tests — pure functions, no Frappe context required.

We pass the rows in directly so we don't have to spin up a Frappe site.
"""

import pytest

from qbo_coa_sync.utils.type_mapping import qbo_to_erpnext, erpnext_to_qbo, TypeMappingError


ROWS = [
    {"qbo_account_type": "Bank", "qbo_account_subtype": "", "erpnext_root_type": "Asset", "erpnext_account_type": "Bank"},
    {"qbo_account_type": "Other Current Asset", "qbo_account_subtype": "", "erpnext_root_type": "Asset", "erpnext_account_type": ""},
    {"qbo_account_type": "Other Current Asset", "qbo_account_subtype": "Inventory", "erpnext_root_type": "Asset", "erpnext_account_type": "Stock"},
    {"qbo_account_type": "Accounts Payable", "qbo_account_subtype": "", "erpnext_root_type": "Liability", "erpnext_account_type": "Payable"},
    {"qbo_account_type": "Income", "qbo_account_subtype": "", "erpnext_root_type": "Income", "erpnext_account_type": "Income Account"},
    {"qbo_account_type": "Other Income", "qbo_account_subtype": "", "erpnext_root_type": "Income", "erpnext_account_type": "Income Account"},
]


def test_specific_subtype_wins():
    assert qbo_to_erpnext("Other Current Asset", "Inventory", ROWS) == ("Asset", "Stock")


def test_falls_back_to_blank_subtype():
    assert qbo_to_erpnext("Other Current Asset", "PrepaidExpenses", ROWS) == ("Asset", None)


def test_unknown_type_raises():
    with pytest.raises(TypeMappingError):
        qbo_to_erpnext("WildlySpeculativeAsset", "", ROWS)


def test_reverse_resolution_simple():
    assert erpnext_to_qbo("Liability", "Payable", ROWS) == ("Accounts Payable", None)


def test_reverse_resolution_falls_back_to_root():
    assert erpnext_to_qbo("Asset", "Cash", ROWS) == ("Other Current Asset", None)


def test_reverse_resolution_picks_blank_subtype_when_multiple_match():
    # Both Income and Other Income map to (Income, Income Account). The reverse should pick
    # one — and it should be deterministic. Whichever wins, its subtype must be blank.
    qbo_type, sub = erpnext_to_qbo("Income", "Income Account", ROWS)
    assert qbo_type in {"Income", "Other Income"}
    assert sub is None


def test_reverse_unknown_raises():
    with pytest.raises(TypeMappingError):
        erpnext_to_qbo("Equity", "Equity", ROWS)
