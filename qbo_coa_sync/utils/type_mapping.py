"""Resolve QBO ↔ ERPNext account types via the user-editable mapping table.

Most-specific match wins on the QBO side: try ``(qbo_type, qbo_subtype)`` first, then fall back
to ``(qbo_type, "")``. The reverse direction prefers the row whose ``qbo_account_subtype`` is
blank (the canonical fallback) so multiple QBO subtypes that all map back to the same ERPNext
pair don't make the reverse lookup non-deterministic.
"""

from __future__ import annotations

from typing import Iterable

import frappe


class TypeMappingError(frappe.ValidationError):
    pass


def _rows() -> list[dict]:
    settings = frappe.get_single("QuickBooks Settings")
    return [r.as_dict() for r in (settings.account_type_mapping or [])]


def qbo_to_erpnext(qbo_type: str, qbo_subtype: str | None, rows: Iterable[dict] | None = None):
    """Return ``(root_type, account_type)``. Raises if no mapping row matches."""
    rows = list(rows) if rows is not None else _rows()
    if not qbo_type:
        raise TypeMappingError("QBO account type is empty; cannot map.")
    qbo_subtype = (qbo_subtype or "").strip()

    # Most-specific first.
    for row in rows:
        if row.get("qbo_account_type") == qbo_type and (row.get("qbo_account_subtype") or "").strip() == qbo_subtype and qbo_subtype:
            return row.get("erpnext_root_type"), row.get("erpnext_account_type") or None
    # Fallback: same type, blank subtype.
    for row in rows:
        if row.get("qbo_account_type") == qbo_type and not (row.get("qbo_account_subtype") or "").strip():
            return row.get("erpnext_root_type"), row.get("erpnext_account_type") or None

    raise TypeMappingError(
        f"No Account Type Mapping row for QBO type '{qbo_type}'"
        + (f" / subtype '{qbo_subtype}'" if qbo_subtype else "")
        + ". Add a row in QuickBooks Settings → Account Type Mapping."
    )


def erpnext_to_qbo(root_type: str, account_type: str | None, rows: Iterable[dict] | None = None):
    """Return ``(qbo_account_type, qbo_account_subtype)``.

    Prefers a row with a matching ``erpnext_account_type`` AND a blank ``qbo_account_subtype``
    (the canonical fallback). Falls back to root_type-only.
    """
    rows = list(rows) if rows is not None else _rows()
    if not root_type:
        raise TypeMappingError("ERPNext root type is empty; cannot map.")
    account_type = (account_type or "").strip() or None

    candidates = [
        r for r in rows
        if r.get("erpnext_root_type") == root_type
        and (r.get("erpnext_account_type") or None) == account_type
    ]
    if not candidates and account_type:
        # Account_type didn't match — fall back to root_type only.
        candidates = [
            r for r in rows
            if r.get("erpnext_root_type") == root_type and not (r.get("erpnext_account_type") or "")
        ]
    if not candidates:
        raise TypeMappingError(
            f"No Account Type Mapping row for ERPNext root '{root_type}' / type '{account_type or ''}'."
        )

    # Prefer the fallback row (blank subtype) when multiple match.
    candidates.sort(key=lambda r: (bool((r.get("qbo_account_subtype") or "").strip()),))
    chosen = candidates[0]
    return chosen.get("qbo_account_type"), (chosen.get("qbo_account_subtype") or None)
