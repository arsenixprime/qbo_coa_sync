"""Per-row and bulk sync between cached QBO accounts and ERPNext Accounts.

All mutation entrypoints are whitelisted, role-gated, and wrap each row in
``frappe.db.savepoint`` so a failure on one row doesn't poison a batch.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from qbo_coa_sync.utils.type_mapping import qbo_to_erpnext, erpnext_to_qbo, TypeMappingError

ALLOWED_ERP_INLINE_FIELDS = {"account_name", "account_number", "qbo_description"}
ALLOWED_QBO_INLINE_FIELDS = {"Name", "AcctNum", "Description"}


# ---- helpers ----------------------------------------------------------------


def _settings():
    return frappe.get_single("QuickBooks Settings")


def _company():
    company = _settings().erpnext_company
    if not company:
        frappe.throw(_("Set ERPNext Company in QuickBooks Settings before syncing."))
    return company


def _qbo_cache(qbo_id: str) -> dict:
    row = frappe.db.get_value(
        "QuickBooks Account Cache",
        {"qbo_id": qbo_id},
        [
            "qbo_id", "name_field", "acct_num", "account_type", "account_sub_type",
            "parent_qbo_id", "fully_qualified_name", "description", "currency",
            "active", "sync_token",
        ],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("QBO account {0} not in cache. Click Refresh from QBO.").format(qbo_id))
    return row


def _qbo_has_children(qbo_id: str) -> bool:
    return bool(frappe.db.exists("QuickBooks Account Cache", {"parent_qbo_id": qbo_id}))


def _erp_account_by_qbo_id(qbo_id: str, company: str) -> str | None:
    return frappe.db.get_value("Account", {"quickbooks_id": qbo_id, "company": company}, "name")


def _resolve_erp_parent(qbo_parent_id: str | None, company: str) -> str | None:
    """Given a QBO parent's qbo_id, find the ERPNext Account.name it's linked to.

    Returns None if there is no QBO parent (root account). Raises if there IS a parent but
    we haven't synced it yet — caller should surface this as a clear error.
    """
    if not qbo_parent_id:
        # Root: ERPNext requires every account to have a parent (a root group account).
        # Caller must decide which root group to attach to.
        return None
    parent = _erp_account_by_qbo_id(qbo_parent_id, company)
    if not parent:
        frappe.throw(_(
            "Parent QBO account {0} hasn't been synced to ERPNext yet. "
            "Sync the parent first (or use Bulk Sync All)."
        ).format(qbo_parent_id))
    return parent


def _ensure_root_parent(root_type: str, company: str) -> str:
    """Find a top-level group Account for the given root_type to attach orphans to.

    ERPNext seeds these on Company creation: e.g. 'Application of Funds (Assets) - X' for
    Asset. We just pick any group account at depth 0 of that root_type.
    """
    parent = frappe.db.get_value(
        "Account",
        {"company": company, "root_type": root_type, "is_group": 1, "parent_account": ["in", ("", None)]},
        "name",
    )
    if not parent:
        frappe.throw(_(
            "Couldn't find a root '{0}' group account in company {1}. "
            "Create one in ERPNext first."
        ).format(root_type, company))
    return parent


# ---- per-row: QBO → ERPNext ------------------------------------------------


def _apply_qbo_to_erp_fields(acc, qbo: dict, root_type: str, account_type: str | None):
    acc.account_name = qbo["name_field"]
    acc.account_number = qbo.get("acct_num") or None
    acc.qbo_description = qbo.get("description") or None
    acc.root_type = root_type
    acc.account_type = account_type or None
    acc.disabled = 0 if qbo.get("active") else 1
    acc.quickbooks_id = qbo["qbo_id"]
    acc.quickbooks_sync_token = qbo.get("sync_token") or "0"


@frappe.whitelist()
def sync_qbo_to_erpnext(qbo_id: str):
    frappe.only_for(["System Manager", "Accounts Manager"])
    company = _company()
    qbo = _qbo_cache(qbo_id)

    root_type, account_type = qbo_to_erpnext(qbo.get("account_type") or "", qbo.get("account_sub_type") or "")

    erp_name = _erp_account_by_qbo_id(qbo_id, company)
    if not erp_name and qbo.get("acct_num"):
        # Try number-link as a fallback for first-time sync.
        candidate = frappe.db.get_value(
            "Account",
            {"company": company, "account_number": qbo["acct_num"]},
            "name",
        )
        if candidate and not frappe.db.get_value("Account", candidate, "quickbooks_id"):
            erp_name = candidate

    parent_qbo_id = qbo.get("parent_qbo_id")
    if parent_qbo_id:
        parent_account = _resolve_erp_parent(parent_qbo_id, company)
    else:
        parent_account = _ensure_root_parent(root_type, company)

    is_group = 1 if _qbo_has_children(qbo_id) else 0

    if erp_name:
        acc = frappe.get_doc("Account", erp_name)
        _apply_qbo_to_erp_fields(acc, qbo, root_type, account_type)
        # Don't move accounts under a different parent silently if they already have one;
        # only set parent_account when missing.
        if not acc.parent_account and parent_account:
            acc.parent_account = parent_account
        # is_group can only be flipped from 0→1 if there are no transactions; ignore failures.
        if int(acc.is_group or 0) != is_group:
            acc.is_group = is_group
        acc.save()
    else:
        acc = frappe.new_doc("Account")
        acc.company = company
        acc.parent_account = parent_account
        acc.is_group = is_group
        _apply_qbo_to_erp_fields(acc, qbo, root_type, account_type)
        acc.insert()

    return {"ok": True, "erpnext_account": acc.name, "qbo_id": qbo_id}


# ---- per-row: ERPNext → QBO ------------------------------------------------


def _erp_to_qbo_payload(acc: dict) -> dict:
    qbo_type, qbo_subtype = erpnext_to_qbo(acc.get("root_type"), acc.get("account_type"))
    payload: dict[str, Any] = {
        "Name": acc.get("account_name"),
        "AccountType": qbo_type,
        "Active": not bool(acc.get("disabled")),
    }
    if qbo_subtype:
        payload["AccountSubType"] = qbo_subtype
    if acc.get("account_number"):
        payload["AcctNum"] = acc["account_number"]
    if acc.get("qbo_description"):
        payload["Description"] = acc["qbo_description"]
    # Parent
    parent_qbo_id = None
    if acc.get("parent_account"):
        parent_qbo_id = frappe.db.get_value("Account", acc["parent_account"], "quickbooks_id")
    if parent_qbo_id:
        payload["SubAccount"] = True
        payload["ParentRef"] = {"value": str(parent_qbo_id)}
    return payload


@frappe.whitelist()
def sync_erpnext_to_qbo(erpnext_account: str):
    frappe.only_for(["System Manager", "Accounts Manager"])
    company = _company()
    from qbo_coa_sync.api.qbo_client import QBOClient

    acc = frappe.get_doc("Account", erpnext_account)
    if acc.company != company:
        frappe.throw(_("Account {0} doesn't belong to the configured company.").format(erpnext_account))

    if acc.parent_account:
        parent_qbo_id = frappe.db.get_value("Account", acc.parent_account, "quickbooks_id")
        if not parent_qbo_id and frappe.db.get_value("Account", acc.parent_account, "is_group"):
            # The parent is a group but isn't linked to QBO yet. Reject with a clear error
            # unless this is a root-level account (parent has no QBO equivalent because it's
            # an ERPNext-only root group).
            parent_acc = frappe.get_doc("Account", acc.parent_account)
            if parent_acc.parent_account:
                frappe.throw(_(
                    "Parent ERPNext account {0} isn't linked to QBO. "
                    "Sync the parent first (or use Bulk Sync All)."
                ).format(acc.parent_account))

    payload = _erp_to_qbo_payload(acc.as_dict())
    client = QBOClient()
    if acc.quickbooks_id:
        payload["Id"] = acc.quickbooks_id
        payload["SyncToken"] = acc.quickbooks_sync_token or "0"
        result = client.update_account(payload)
    else:
        result = client.create_account(payload)

    acc.quickbooks_id = str(result.get("Id"))
    acc.quickbooks_sync_token = str(result.get("SyncToken") or "0")
    acc.save()

    # Refresh just this row in the cache.
    from qbo_coa_sync.api.qbo_client import serialize_qbo_account

    flat = serialize_qbo_account(result)
    flat["last_pulled_at"] = now_datetime()
    if frappe.db.exists("QuickBooks Account Cache", flat["qbo_id"]):
        cached = frappe.get_doc("QuickBooks Account Cache", flat["qbo_id"])
        for k, v in flat.items():
            cached.set(k, v)
        cached.save(ignore_permissions=True)
    else:
        frappe.get_doc({"doctype": "QuickBooks Account Cache", **flat}).insert(ignore_permissions=True)

    return {"ok": True, "qbo_id": acc.quickbooks_id, "erpnext_account": acc.name}


# ---- linking ---------------------------------------------------------------


@frappe.whitelist()
def manual_link(erpnext_account: str, qbo_id: str):
    frappe.only_for(["System Manager", "Accounts Manager"])
    company = _company()
    qbo = _qbo_cache(qbo_id)
    acc = frappe.get_doc("Account", erpnext_account)
    if acc.company != company:
        frappe.throw(_("Account doesn't belong to the configured company."))
    if acc.quickbooks_id and acc.quickbooks_id != qbo_id:
        frappe.throw(_("Account is already linked to QBO Id {0}. Unlink first.").format(acc.quickbooks_id))
    # Make sure no other ERPNext account already owns this qbo_id.
    other = frappe.db.get_value("Account", {"quickbooks_id": qbo_id, "name": ["!=", acc.name]}, "name")
    if other:
        frappe.throw(_("QBO Id {0} is already linked to ERPNext account {1}.").format(qbo_id, other))
    acc.quickbooks_id = qbo_id
    acc.quickbooks_sync_token = qbo.get("sync_token") or "0"
    acc.save()
    return {"ok": True}


@frappe.whitelist()
def unlink(erpnext_account: str):
    frappe.only_for(["System Manager", "Accounts Manager"])
    acc = frappe.get_doc("Account", erpnext_account)
    acc.quickbooks_id = None
    acc.quickbooks_sync_token = None
    acc.save()
    return {"ok": True}


# ---- inline edits ----------------------------------------------------------


@frappe.whitelist()
def update_erpnext_field(erpnext_account: str, field: str, value: str | None):
    frappe.only_for(["System Manager", "Accounts Manager"])
    if field not in ALLOWED_ERP_INLINE_FIELDS:
        frappe.throw(_("Field {0} is not editable from this view.").format(field))
    acc = frappe.get_doc("Account", erpnext_account)
    acc.set(field, value or None)
    acc.save()
    return {"ok": True}


@frappe.whitelist()
def update_qbo_field(qbo_id: str, field: str, value: str | None):
    frappe.only_for(["System Manager", "Accounts Manager"])
    if field not in ALLOWED_QBO_INLINE_FIELDS:
        frappe.throw(_("Field {0} is not editable from this view.").format(field))
    from qbo_coa_sync.api.qbo_client import QBOClient, serialize_qbo_account

    cached = _qbo_cache(qbo_id)
    payload = {
        "Id": qbo_id,
        "SyncToken": cached.get("sync_token") or "0",
        field: value or "",
    }
    # AccountType is required on update, even with sparse=true.
    payload["AccountType"] = cached.get("account_type")

    client = QBOClient()
    result = client.update_account(payload)
    flat = serialize_qbo_account(result)
    flat["last_pulled_at"] = now_datetime()
    cached_doc = frappe.get_doc("QuickBooks Account Cache", qbo_id)
    for k, v in flat.items():
        cached_doc.set(k, v)
    cached_doc.save(ignore_permissions=True)
    return {"ok": True, "qbo_id": qbo_id}


# ---- bulk -------------------------------------------------------------------


def _qbo_topdown_order(qbo_ids: list[str]) -> list[str]:
    """Return ``qbo_ids`` ordered so parents come before children."""
    rows = frappe.get_all(
        "QuickBooks Account Cache",
        filters={"qbo_id": ["in", qbo_ids]},
        fields=["qbo_id", "parent_qbo_id"],
        limit_page_length=0,
    )
    by_id = {r["qbo_id"]: r for r in rows}
    depth_cache: dict[str, int] = {}

    def depth(qid: str) -> int:
        if qid in depth_cache:
            return depth_cache[qid]
        parent = by_id.get(qid, {}).get("parent_qbo_id")
        d = 0 if not parent or parent not in by_id else depth(parent) + 1
        depth_cache[qid] = d
        return d

    return sorted(qbo_ids, key=lambda q: depth(q))


def _erp_topdown_order(names: list[str]) -> list[str]:
    rows = frappe.get_all(
        "Account",
        filters={"name": ["in", names]},
        fields=["name", "lft"],
        order_by="lft asc",
        limit_page_length=0,
    )
    return [r["name"] for r in rows]


def _run_bulk(items, runner):
    ok: list[str] = []
    failed: list[dict] = []
    for item in items:
        sp = "qbo_sync_row"
        frappe.db.savepoint(sp)
        try:
            runner(item)
            ok.append(item)
        except Exception as e:
            frappe.db.rollback(save_point=sp)
            frappe.log_error(title="QBO Sync Row Failed", message=f"{item}: {e}\n{frappe.get_traceback()}")
            failed.append({"id": item, "error": str(e)})
    return {"ok": ok, "failed": failed}


@frappe.whitelist()
def bulk_sync_qbo_to_erpnext(qbo_ids):
    frappe.only_for(["System Manager", "Accounts Manager"])
    if isinstance(qbo_ids, str):
        qbo_ids = frappe.parse_json(qbo_ids)
    qbo_ids = _qbo_topdown_order(list(qbo_ids))
    return _run_bulk(qbo_ids, lambda q: sync_qbo_to_erpnext(q))


@frappe.whitelist()
def bulk_sync_erpnext_to_qbo(erpnext_accounts):
    frappe.only_for(["System Manager", "Accounts Manager"])
    if isinstance(erpnext_accounts, str):
        erpnext_accounts = frappe.parse_json(erpnext_accounts)
    names = _erp_topdown_order(list(erpnext_accounts))
    return _run_bulk(names, lambda n: sync_erpnext_to_qbo(n))


@frappe.whitelist()
def bulk_link_by_number():
    """Auto-link accounts where ERPNext.account_number == QBO.acct_num and the number is
    unambiguous on both sides. Skips already-linked accounts."""
    frappe.only_for(["System Manager", "Accounts Manager"])
    company = _company()

    erp_rows = frappe.get_all(
        "Account",
        filters={"company": company, "account_number": ["!=", ""]},
        fields=["name", "account_number", "quickbooks_id"],
        limit_page_length=0,
    )
    qbo_rows = frappe.get_all(
        "QuickBooks Account Cache",
        filters={"acct_num": ["!=", ""]},
        fields=["qbo_id", "acct_num", "sync_token"],
        limit_page_length=0,
    )

    from collections import Counter
    erp_counts = Counter(r["account_number"] for r in erp_rows)
    qbo_counts = Counter(r["acct_num"] for r in qbo_rows)
    qbo_by_num = {r["acct_num"]: r for r in qbo_rows if qbo_counts[r["acct_num"]] == 1}

    linked: list[dict] = []
    skipped: list[dict] = []
    for r in erp_rows:
        if r.get("quickbooks_id"):
            continue
        if erp_counts[r["account_number"]] != 1:
            skipped.append({"name": r["name"], "reason": "duplicate ERPNext account_number"})
            continue
        match = qbo_by_num.get(r["account_number"])
        if not match:
            continue
        # Check QBO id isn't already linked elsewhere.
        existing = frappe.db.get_value("Account", {"quickbooks_id": match["qbo_id"]}, "name")
        if existing:
            skipped.append({"name": r["name"], "reason": f"QBO Id {match['qbo_id']} already linked"})
            continue
        frappe.db.set_value("Account", r["name"], "quickbooks_id", match["qbo_id"])
        frappe.db.set_value("Account", r["name"], "quickbooks_sync_token", match.get("sync_token") or "0")
        linked.append({"name": r["name"], "qbo_id": match["qbo_id"]})
    frappe.db.commit()
    return {"linked": linked, "skipped": skipped}


# ---- search helpers for the UI ---------------------------------------------


@frappe.whitelist()
def search_unmatched_qbo(query: str = "", limit: int = 50):
    frappe.only_for(["System Manager", "Accounts Manager"])
    linked_ids = [r[0] for r in frappe.db.sql("SELECT quickbooks_id FROM tabAccount WHERE quickbooks_id IS NOT NULL AND quickbooks_id != ''")]
    filters: list[Any] = []
    if linked_ids:
        filters.append(["qbo_id", "not in", linked_ids])
    if query:
        filters.append(["name_field", "like", f"%{query}%"])
    return frappe.get_all(
        "QuickBooks Account Cache",
        filters=filters or None,
        fields=["qbo_id", "name_field as name", "acct_num", "fully_qualified_name"],
        limit_page_length=limit,
    )


@frappe.whitelist()
def search_unmatched_erpnext(query: str = "", limit: int = 50):
    frappe.only_for(["System Manager", "Accounts Manager"])
    company = _company()
    filters = {
        "company": company,
        "quickbooks_id": ["in", ("", None)],
        "is_group": 0,
    }
    or_filters = None
    if query:
        or_filters = [
            ["account_name", "like", f"%{query}%"],
            ["account_number", "like", f"%{query}%"],
        ]
    return frappe.get_all(
        "Account",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "account_name", "account_number"],
        limit_page_length=limit,
    )
