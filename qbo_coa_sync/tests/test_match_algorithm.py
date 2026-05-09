"""Matching algorithm tests — pure on the inputs to ``compare._match`` so we don't need a site."""

from qbo_coa_sync.api.compare import _match


def _erp(name, **kw):
    base = {
        "name": name, "account_name": name, "account_number": "", "description": "",
        "root_type": "Asset", "account_type": "", "parent_account": "", "is_group": 0,
        "disabled": 0, "quickbooks_id": "", "quickbooks_sync_token": "", "account_currency": "USD",
    }
    base.update(kw)
    return base


def _qbo(qbo_id, **kw):
    base = {
        "qbo_id": qbo_id, "name": qbo_id, "acct_num": "", "account_type": "Bank",
        "account_sub_type": "", "parent_qbo_id": "", "fully_qualified_name": qbo_id,
        "description": "", "currency": "USD", "active": 1, "sync_token": "0",
    }
    base.update(kw)
    return base


def test_linked_match_wins_over_number():
    erp = [_erp("Cash - X", account_number="1100", quickbooks_id="42")]
    qbo = [_qbo("42", acct_num="9999"), _qbo("43", acct_num="1100")]
    pairs, only_e, only_q = _match(erp, qbo)
    assert len(pairs) == 1
    assert pairs[0][2] == "Linked"
    assert pairs[0][1]["qbo_id"] == "42"
    assert {q["qbo_id"] for q in only_q} == {"43"}


def test_match_by_number():
    erp = [_erp("Cash - X", account_number="1100")]
    qbo = [_qbo("9", acct_num="1100")]
    pairs, only_e, only_q = _match(erp, qbo)
    assert len(pairs) == 1
    assert pairs[0][2] == "Matched by Number"


def test_only_in_qbo():
    erp = []
    qbo = [_qbo("9", acct_num="1100")]
    pairs, only_e, only_q = _match(erp, qbo)
    assert pairs == []
    assert only_q[0]["qbo_id"] == "9"


def test_only_in_erpnext():
    erp = [_erp("Cash - X", account_number="1100")]
    qbo = []
    pairs, only_e, only_q = _match(erp, qbo)
    assert pairs == []
    assert only_e[0]["name"] == "Cash - X"


def test_duplicate_numbers_are_not_auto_linked():
    erp = [
        _erp("Cash - A", account_number="1100"),
        _erp("Cash - B", account_number="1100"),
    ]
    qbo = [_qbo("9", acct_num="1100")]
    pairs, only_e, only_q = _match(erp, qbo)
    assert pairs == [], "Ambiguous numbers must not auto-match"
    assert only_q[0]["qbo_id"] == "9"
    assert {a["name"] for a in only_e} == {"Cash - A", "Cash - B"}


def test_blank_numbers_do_not_collide():
    erp = [_erp("A"), _erp("B")]
    qbo = [_qbo("1"), _qbo("2")]
    pairs, only_e, only_q = _match(erp, qbo)
    assert pairs == []
    assert len(only_e) == 2 and len(only_q) == 2
