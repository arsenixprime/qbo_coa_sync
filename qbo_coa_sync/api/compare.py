"""Build a side-by-side comparison of ERPNext and QBO chart-of-accounts trees.

Match priority (first match wins, never re-match):
  1. Linked: Account.quickbooks_id is set and matches a cached qbo_id.
  2. By account number: both sides have a non-empty number and they're equal.
     Skipped if either side has duplicate numbers.
  3. Unmatched: only on one side, or unmatched after the above.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

import frappe

# Diff field flags
SAME = "same"
DIFFERS = "differs"
MISSING = "missing"

STATUS_LINKED = "Linked"
STATUS_NUMBER = "Matched by Number"
STATUS_ONLY_QBO = "Only in QBO"
STATUS_ONLY_ERPNEXT = "Only in ERPNext"
STATUS_UNMATCHED = "Unmatched"


def _settings():
    return frappe.get_single("QuickBooks Settings")


def _row_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _erpnext_accounts(company: str) -> list[dict]:
    fields = [
        "name", "account_name", "account_number", "qbo_description as description",
        "root_type", "account_type", "parent_account", "is_group", "disabled",
        "quickbooks_id", "quickbooks_sync_token", "account_currency",
    ]
    return frappe.get_all(
        "Account",
        filters={"company": company},
        fields=fields,
        order_by="lft asc",
    )


def _qbo_cache_rows() -> list[dict]:
    return frappe.get_all(
        "QuickBooks Account Cache",
        fields=[
            "qbo_id", "name_field as name", "acct_num", "account_type",
            "account_sub_type", "parent_qbo_id", "fully_qualified_name",
            "description", "currency", "active", "sync_token",
        ],
        order_by="fully_qualified_name asc",
        limit_page_length=0,
    )


# ---- matching ---------------------------------------------------------------


def _match(erp_accounts: list[dict], qbo_accounts: list[dict]):
    """Return ``(pairs, only_erp, only_qbo)`` where ``pairs`` is a list of
    ``(erp_or_None, qbo_or_None, status)`` triples — but pairs always has both sides set
    (the only_* lists hold the singletons).
    """
    qbo_by_id = {q["qbo_id"]: q for q in qbo_accounts}

    # Account-number index — skip ambiguous numbers entirely.
    erp_num_counts: dict[str, int] = defaultdict(int)
    qbo_num_counts: dict[str, int] = defaultdict(int)
    for a in erp_accounts:
        if a.get("account_number"):
            erp_num_counts[a["account_number"]] += 1
    for q in qbo_accounts:
        if q.get("acct_num"):
            qbo_num_counts[q["acct_num"]] += 1
    erp_by_num = {a["account_number"]: a for a in erp_accounts if a.get("account_number") and erp_num_counts[a["account_number"]] == 1}
    qbo_by_num = {q["acct_num"]: q for q in qbo_accounts if q.get("acct_num") and qbo_num_counts[q["acct_num"]] == 1}

    used_erp: set[str] = set()
    used_qbo: set[str] = set()
    pairs: list[tuple[dict, dict, str]] = []

    # Pass 1: linked.
    for a in erp_accounts:
        qid = a.get("quickbooks_id")
        if qid and qid in qbo_by_id and qid not in used_qbo:
            pairs.append((a, qbo_by_id[qid], STATUS_LINKED))
            used_erp.add(a["name"])
            used_qbo.add(qid)

    # Pass 2: by number.
    for num, a in erp_by_num.items():
        if a["name"] in used_erp:
            continue
        q = qbo_by_num.get(num)
        if not q or q["qbo_id"] in used_qbo:
            continue
        pairs.append((a, q, STATUS_NUMBER))
        used_erp.add(a["name"])
        used_qbo.add(q["qbo_id"])

    only_erp = [a for a in erp_accounts if a["name"] not in used_erp]
    only_qbo = [q for q in qbo_accounts if q["qbo_id"] not in used_qbo]
    return pairs, only_erp, only_qbo


# ---- diff -------------------------------------------------------------------


def _diff_pair(erp: dict, qbo: dict, qbo_to_erp_index: dict[str, dict]) -> dict[str, str]:
    diff: dict[str, str] = {}
    diff["name"] = SAME if (erp.get("account_name") or "").strip() == (qbo.get("name") or "").strip() else DIFFERS
    diff["account_number"] = SAME if (erp.get("account_number") or "") == (qbo.get("acct_num") or "") else DIFFERS
    diff["description"] = SAME if (erp.get("description") or "").strip() == (qbo.get("description") or "").strip() else DIFFERS

    # Parent: matched parent on QBO side should map to ERPNext parent_account name.
    erp_parent = erp.get("parent_account") or ""
    qbo_parent_qbo_id = qbo.get("parent_qbo_id") or ""
    if not erp_parent and not qbo_parent_qbo_id:
        diff["parent"] = SAME
    elif qbo_parent_qbo_id and qbo_to_erp_index.get(qbo_parent_qbo_id) and qbo_to_erp_index[qbo_parent_qbo_id].get("name") == erp_parent:
        diff["parent"] = SAME
    else:
        diff["parent"] = DIFFERS

    # Active: ERPNext disabled=0 ↔ QBO active=true
    erp_active = not bool(erp.get("disabled"))
    qbo_active = bool(qbo.get("active"))
    diff["active"] = SAME if erp_active == qbo_active else DIFFERS

    # Type — compare via mapping; if mapping resolves QBO type to the same root_type/account_type
    # ERPNext has, mark as same; otherwise differs.
    try:
        from qbo_coa_sync.utils.type_mapping import qbo_to_erpnext

        mapped_root, mapped_at = qbo_to_erpnext(qbo.get("account_type") or "", qbo.get("account_sub_type") or "")
        erp_root = erp.get("root_type") or ""
        erp_at = erp.get("account_type") or ""
        if mapped_root == erp_root and (mapped_at or "") == (erp_at or ""):
            diff["type"] = SAME
        else:
            diff["type"] = DIFFERS
    except Exception:
        diff["type"] = DIFFERS

    return diff


def _format_erp(a: dict) -> dict:
    return {
        "name": a["name"],
        "account_name": a.get("account_name"),
        "account_number": a.get("account_number"),
        "description": a.get("description"),
        "root_type": a.get("root_type"),
        "account_type": a.get("account_type"),
        "parent_account": a.get("parent_account"),
        "is_group": bool(a.get("is_group")),
        "disabled": bool(a.get("disabled")),
        "quickbooks_id": a.get("quickbooks_id"),
        "currency": a.get("account_currency"),
    }


def _format_qbo(q: dict) -> dict:
    return {
        "qbo_id": q.get("qbo_id"),
        "acct_num": q.get("acct_num"),
        "name": q.get("name"),
        "description": q.get("description"),
        "account_type": q.get("account_type"),
        "account_sub_type": q.get("account_sub_type"),
        "parent_qbo_id": q.get("parent_qbo_id"),
        "active": bool(q.get("active")),
        "sync_token": q.get("sync_token"),
        "currency": q.get("currency"),
    }


# ---- tree assembly ----------------------------------------------------------


def _build_unified_tree(pairs, only_erp, only_qbo, erp_accounts, qbo_accounts):
    """Return a flat ordered list of rows with stable parent links and depth.

    Each row dict has: row_id, status, depth, erpnext, qbo, diff, _children.
    """
    qbo_by_id = {q["qbo_id"]: q for q in qbo_accounts}
    erp_by_name = {a["name"]: a for a in erp_accounts}

    # Index: which row owns a given ERPNext name / qbo_id.
    rows_by_erp_name: dict[str, dict] = {}
    rows_by_qbo_id: dict[str, dict] = {}
    qbo_to_erp_match: dict[str, dict] = {}
    rows: list[dict] = []

    def _new_row(erp: dict | None, qbo: dict | None, status: str) -> dict:
        seed_parts = []
        if erp:
            seed_parts.append("erp:" + erp["name"])
        if qbo:
            seed_parts.append("qbo:" + qbo["qbo_id"])
        seed_parts.append(status)
        row = {
            "row_id": _row_id("|".join(seed_parts)),
            "status": status,
            "depth": 0,
            "erpnext": _format_erp(erp) if erp else None,
            "qbo": _format_qbo(qbo) if qbo else None,
            "diff": None,
            "_children": [],
            "_erp_name": erp["name"] if erp else None,
            "_qbo_id": qbo["qbo_id"] if qbo else None,
            "_erp_parent": (erp.get("parent_account") if erp else None),
            "_qbo_parent": (qbo.get("parent_qbo_id") if qbo else None),
        }
        if erp:
            rows_by_erp_name[erp["name"]] = row
        if qbo:
            rows_by_qbo_id[qbo["qbo_id"]] = row
        if erp and qbo:
            qbo_to_erp_match[qbo["qbo_id"]] = erp
        rows.append(row)
        return row

    for erp, qbo, status in pairs:
        _new_row(erp, qbo, status)
    for erp in only_erp:
        _new_row(erp, None, STATUS_ONLY_ERPNEXT)
    for qbo in only_qbo:
        _new_row(None, qbo, STATUS_ONLY_QBO)

    # Compute diff once we have qbo→erp index.
    for row in rows:
        if row["erpnext"] and row["qbo"]:
            erp = erp_by_name.get(row["_erp_name"], {})
            qbo = qbo_by_id.get(row["_qbo_id"], {})
            row["diff"] = _diff_pair(erp, qbo, qbo_to_erp_match)

    # Determine parent row for each.
    def _parent_row(row) -> dict | None:
        # Prefer the matched-parent route: if QBO parent is matched, use that row.
        qbo_parent = row.get("_qbo_parent")
        if qbo_parent and qbo_parent in rows_by_qbo_id:
            return rows_by_qbo_id[qbo_parent]
        erp_parent = row.get("_erp_parent")
        if erp_parent and erp_parent in rows_by_erp_name:
            return rows_by_erp_name[erp_parent]
        return None

    roots: list[dict] = []
    for row in rows:
        p = _parent_row(row)
        if p is row or p is None:
            roots.append(row)
        else:
            p["_children"].append(row)

    # Sort children: by status priority (matched first), then number, then name.
    status_order = {STATUS_LINKED: 0, STATUS_NUMBER: 1, STATUS_ONLY_ERPNEXT: 2, STATUS_ONLY_QBO: 3, STATUS_UNMATCHED: 4}

    def _sort_key(row):
        erp = row.get("erpnext") or {}
        qbo = row.get("qbo") or {}
        num = erp.get("account_number") or qbo.get("acct_num") or ""
        name = (erp.get("account_name") or qbo.get("name") or "").lower()
        return (status_order.get(row["status"], 99), num, name)

    def _sort_recursive(node):
        node["_children"].sort(key=_sort_key)
        for c in node["_children"]:
            _sort_recursive(c)

    roots.sort(key=_sort_key)
    for r in roots:
        _sort_recursive(r)

    # Flatten with depth.
    flat: list[dict] = []

    def _walk(node, depth):
        node["depth"] = depth
        out = {k: v for k, v in node.items() if not k.startswith("_")}
        flat.append(out)
        for c in node["_children"]:
            _walk(c, depth + 1)

    for r in roots:
        _walk(r, 0)

    return flat


# ---- public API -------------------------------------------------------------


@frappe.whitelist()
def get_comparison():
    frappe.only_for(["System Manager", "Accounts Manager"])
    settings = _settings()
    company = settings.erpnext_company
    if not company:
        frappe.throw("Set ERPNext Company in QuickBooks Settings before comparing.")

    erp = _erpnext_accounts(company)
    qbo = _qbo_cache_rows()
    pairs, only_erp, only_qbo = _match(erp, qbo)
    rows = _build_unified_tree(pairs, only_erp, only_qbo, erp, qbo)

    return {
        "company": company,
        "realm_id": settings.realm_id,
        "qbo_last_pulled_at": str(settings.last_pulled_qbo_at) if settings.last_pulled_qbo_at else None,
        "connection_status": settings.connection_status,
        "rows": rows,
        "counts": {
            "linked": sum(1 for r in rows if r["status"] == STATUS_LINKED),
            "by_number": sum(1 for r in rows if r["status"] == STATUS_NUMBER),
            "only_erp": sum(1 for r in rows if r["status"] == STATUS_ONLY_ERPNEXT),
            "only_qbo": sum(1 for r in rows if r["status"] == STATUS_ONLY_QBO),
            "differs": sum(1 for r in rows if r.get("diff") and any(v == DIFFERS for v in r["diff"].values())),
        },
    }


@frappe.whitelist()
def refresh_from_qbo():
    frappe.only_for(["System Manager", "Accounts Manager"])
    from qbo_coa_sync.api.qbo_client import refresh_account_cache

    n = refresh_account_cache()
    return {"ok": True, "count": n}
