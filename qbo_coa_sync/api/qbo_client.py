"""Hand-rolled QBO Accounting API v3 client.

Uses ``requests`` directly — no ``intuit-oauth`` or ``python-quickbooks``. Token refresh is
delegated to :mod:`qbo_coa_sync.api.oauth` so the same lifecycle works for both the OAuth flow
endpoints and any caller that needs a live access token.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import frappe
import requests

# Endpoints — verify against current Intuit docs at deploy time.
SANDBOX_API_BASE = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_API_BASE = "https://quickbooks.api.intuit.com"

DEFAULT_TIMEOUT = 30
DEFAULT_PAGE_SIZE = 1000


class QBOAPIError(Exception):
    def __init__(self, status_code: int, fault_code: str | None, message: str, payload: dict | None = None):
        super().__init__(f"QBO API {status_code} ({fault_code or '-'}): {message}")
        self.status_code = status_code
        self.fault_code = fault_code
        self.message = message
        self.payload = payload or {}


def _api_base(environment: str) -> str:
    return PRODUCTION_API_BASE if (environment or "").lower().startswith("prod") else SANDBOX_API_BASE


def _extract_fault(resp: requests.Response) -> tuple[str | None, str]:
    try:
        body = resp.json()
    except ValueError:
        return None, resp.text[:500]
    fault = body.get("Fault") or {}
    errors = fault.get("Error") or []
    if errors:
        first = errors[0]
        return str(first.get("code") or ""), first.get("Detail") or first.get("Message") or "QBO error"
    return None, body.get("message") or resp.text[:500]


class QBOClient:
    def __init__(self, settings=None):
        self.settings = settings or frappe.get_single("QuickBooks Settings")
        if not self.settings.realm_id:
            frappe.throw("QuickBooks is not connected. Connect from QuickBooks Settings first.")
        self.environment = self.settings.environment or "Sandbox"
        self.realm_id = self.settings.realm_id

    # -- low-level HTTP -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        from qbo_coa_sync.api.oauth import get_valid_access_token
        return {
            "Authorization": f"Bearer {get_valid_access_token(self.settings)}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _company_url(self, path: str) -> str:
        return f"{_api_base(self.environment)}/v3/company/{self.realm_id}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None, retry_on_5xx: bool = True) -> dict:
        url = self._company_url(path)
        params = dict(params or {})
        params.setdefault("minorversion", "70")
        backoffs = [0, 1, 3] if retry_on_5xx and method.upper() == "GET" else [0]
        last_exc: Exception | None = None
        for delay in backoffs:
            if delay:
                time.sleep(delay)
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as e:
                last_exc = e
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = self._build_error(resp)
                continue
            if resp.status_code >= 400:
                err = self._build_error(resp)
                self._log_error(method, url, resp)
                raise err
            try:
                return resp.json()
            except ValueError:
                return {}
        if isinstance(last_exc, QBOAPIError):
            self._log_error(method, url, last_exc.payload.get("__response"))
            raise last_exc
        raise QBOAPIError(0, None, f"QBO request failed: {last_exc}")

    def _build_error(self, resp: requests.Response) -> QBOAPIError:
        code, msg = _extract_fault(resp)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        payload["__response"] = resp  # for downstream logging
        return QBOAPIError(resp.status_code, code, msg, payload)

    def _log_error(self, method: str, url: str, resp: requests.Response | None):
        try:
            status = getattr(resp, "status_code", "?")
            text = getattr(resp, "text", "")[:2000] if resp is not None else ""
            frappe.log_error(
                title="QBO API Error",
                message=f"{method} {url}\nStatus: {status}\nBody: {text}",
            )
        except Exception:
            pass

    # -- public ---------------------------------------------------------------

    def query(self, sql: str) -> list[dict]:
        """Run a QBO SQL-ish query, page through results, return the inner array.

        Determines the result key from the first response (e.g. ``Account``).
        """
        results: list[dict] = []
        start = 1
        page_size = DEFAULT_PAGE_SIZE
        while True:
            paged = f"{sql} STARTPOSITION {start} MAXRESULTS {page_size}"
            data = self._request("GET", "query", params={"query": paged})
            qr = data.get("QueryResponse") or {}
            inner_key = next((k for k in qr.keys() if k not in ("startPosition", "maxResults", "totalCount")), None)
            if not inner_key:
                break
            batch = qr.get(inner_key) or []
            results.extend(batch)
            if len(batch) < page_size:
                break
            start += len(batch)
        return results

    def get_account(self, qbo_id: str) -> dict:
        data = self._request("GET", f"account/{quote(str(qbo_id))}")
        return data.get("Account") or {}

    def create_account(self, payload: dict) -> dict:
        data = self._request("POST", "account", json_body=payload)
        return data.get("Account") or {}

    def update_account(self, payload: dict) -> dict:
        # Sparse update — only changed fields plus Id + SyncToken.
        body = dict(payload)
        body.setdefault("sparse", True)
        data = self._request("POST", "account", params={"operation": "update"}, json_body=body)
        return data.get("Account") or {}

    def get_company_info(self) -> dict:
        data = self._request("GET", f"companyinfo/{self.realm_id}")
        return data.get("CompanyInfo") or {}

    def list_accounts(self, include_inactive: bool = True) -> list[dict]:
        sql = "SELECT * FROM Account"
        if not include_inactive:
            sql += " WHERE Active = true"
        return self.query(sql)


def serialize_qbo_account(acc: dict) -> dict:
    """Flatten a QBO Account object to the columns we cache."""
    return {
        "qbo_id": str(acc.get("Id")),
        "name_field": acc.get("Name") or "",
        "acct_num": acc.get("AcctNum") or "",
        "account_type": acc.get("AccountType") or "",
        "account_sub_type": acc.get("AccountSubType") or "",
        "parent_qbo_id": (acc.get("ParentRef") or {}).get("value") or "",
        "fully_qualified_name": acc.get("FullyQualifiedName") or "",
        "description": acc.get("Description") or "",
        "currency": (acc.get("CurrencyRef") or {}).get("value") or "",
        "active": 1 if acc.get("Active") else 0,
        "sync_token": str(acc.get("SyncToken") or "0"),
    }


def refresh_account_cache() -> int:
    """Wipe and rebuild the QuickBooks Account Cache. Returns row count."""
    from frappe.utils import now_datetime

    client = QBOClient()
    accounts = client.list_accounts()
    now = now_datetime()

    # Wipe in a single statement — the cache is transient.
    frappe.db.delete("QuickBooks Account Cache")

    for raw in accounts:
        flat = serialize_qbo_account(raw)
        flat["last_pulled_at"] = now
        doc = frappe.get_doc({"doctype": "QuickBooks Account Cache", **flat})
        doc.insert(ignore_permissions=True)

    settings = frappe.get_single("QuickBooks Settings")
    settings.last_pulled_qbo_at = now
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return len(accounts)
