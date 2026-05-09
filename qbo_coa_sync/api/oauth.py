"""QuickBooks Online OAuth 2.0 flow.

Verify the authorize/token URLs and the scope string against current Intuit docs at deploy time
— Intuit revises these. Where this code and the live docs disagree, the live docs win.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

DEFAULT_SCOPE = "com.intuit.quickbooks.accounting"
STATE_CACHE_PREFIX = "qbo_coa_sync:oauth_state:"
STATE_TTL_SECONDS = 600

ACCESS_REFRESH_BUFFER_SECONDS = 60


# ---- helpers ----------------------------------------------------------------


def _settings():
    return frappe.get_single("QuickBooks Settings")


def _get_secret(doc, fieldname: str) -> str | None:
    """Pull a Password-field value, decrypting from the password vault."""
    val = doc.get(fieldname)
    if val and not str(val).startswith("*"):
        return val
    try:
        return get_decrypted_password(doc.doctype, doc.name, fieldname, raise_exception=False)
    except Exception:
        return None


def _basic_auth(client_id: str, client_secret: str) -> tuple[str, str]:
    return (client_id, client_secret)


def _state_key(user: str) -> str:
    return f"{STATE_CACHE_PREFIX}{user}"


def _store_state(user: str, state: str) -> None:
    frappe.cache().set_value(_state_key(user), state, expires_in_sec=STATE_TTL_SECONDS)


def _consume_state(user: str) -> str | None:
    key = _state_key(user)
    val = frappe.cache().get_value(key)
    if val:
        frappe.cache().delete_value(key)
    return val


def _persist_tokens(settings, token_response: dict) -> None:
    """Apply a /tokens/bearer response to settings and save."""
    now = now_datetime()
    access_lifetime = int(token_response.get("expires_in") or 3600)
    refresh_lifetime = int(token_response.get("x_refresh_token_expires_in") or 8640000)  # ~100d
    settings.access_token = token_response.get("access_token")
    if token_response.get("refresh_token"):
        settings.refresh_token = token_response["refresh_token"]
    settings.access_token_expires_at = now + timedelta(seconds=access_lifetime)
    settings.refresh_token_expires_at = now + timedelta(seconds=refresh_lifetime)
    settings.connection_status = "Connected"
    settings.save(ignore_permissions=True)
    # `respond_as_web_page` (used by the OAuth callback) bypasses Frappe's normal
    # end-of-request commit, so write through explicitly. Cheap; safe for refresh-from-API path too.
    frappe.db.commit()


# ---- token lifecycle --------------------------------------------------------


def get_valid_access_token(settings=None) -> str:
    """Return a usable access token, refreshing if necessary.

    Always go through this — never read ``access_token`` directly. Raises
    :class:`frappe.AuthenticationError` if refresh fails (the user must reconnect).
    """
    settings = settings or _settings()
    # Frappe Single docs return datetime fields as strings depending on source — coerce.
    expires_at = frappe.utils.get_datetime(settings.access_token_expires_at) if settings.access_token_expires_at else None
    if expires_at and (expires_at - now_datetime()).total_seconds() > ACCESS_REFRESH_BUFFER_SECONDS:
        token = _get_secret(settings, "access_token")
        if token:
            return token

    refresh_token = _get_secret(settings, "refresh_token")
    if not refresh_token:
        settings.connection_status = "Token Expired"
        settings.save(ignore_permissions=True)
        raise frappe.AuthenticationError(
            "QBO refresh token missing. Reconnect from QuickBooks Settings."
        )

    client_id = settings.client_id
    client_secret = _get_secret(settings, "client_secret")
    if not (client_id and client_secret):
        raise frappe.ValidationError("QuickBooks Client ID / Secret not configured.")

    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=_basic_auth(client_id, client_secret),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        settings.connection_status = "Token Expired"
        settings.save(ignore_permissions=True)
        frappe.log_error(
            title="QBO Token Refresh Failed",
            message=f"Status: {resp.status_code}\nBody: {resp.text[:1000]}",
        )
        raise frappe.AuthenticationError(
            _("QBO token refresh failed. Reconnect from QuickBooks Settings.")
        )
    _persist_tokens(settings, resp.json())
    return _get_secret(settings, "access_token")


# ---- whitelisted endpoints --------------------------------------------------


@frappe.whitelist()
def start_auth():
    """Build the Intuit authorize URL and 302-redirect the browser to it."""
    frappe.only_for("System Manager")
    settings = _settings()
    if not settings.client_id:
        frappe.throw(_("Set Client ID in QuickBooks Settings before connecting."))

    state = secrets.token_urlsafe(32)
    _store_state(frappe.session.user, state)

    from qbo_coa_sync.qbo_coa_sync.doctype.quickbooks_settings.quickbooks_settings import build_redirect_uri

    params = {
        "client_id": settings.client_id,
        "response_type": "code",
        "scope": settings.scope or DEFAULT_SCOPE,
        "redirect_uri": build_redirect_uri(),
        "state": state,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = url


@frappe.whitelist(allow_guest=False)
def callback(code: str | None = None, state: str | None = None, realmId: str | None = None, error: str | None = None, **_kwargs):
    """OAuth redirect target. Validates state, exchanges code for tokens, persists realm."""
    if error:
        return _render_close_page(False, f"QuickBooks returned an error: {error}")
    if not (code and state and realmId):
        return _render_close_page(False, "Missing one of: code, state, realmId")

    expected = _consume_state(frappe.session.user)
    if not expected or expected != state:
        return _render_close_page(False, "OAuth state mismatch — possible CSRF. Try again.")

    settings = _settings()
    client_id = settings.client_id
    client_secret = _get_secret(settings, "client_secret")
    if not (client_id and client_secret):
        return _render_close_page(False, "Client ID / Secret not configured.")

    from qbo_coa_sync.qbo_coa_sync.doctype.quickbooks_settings.quickbooks_settings import build_redirect_uri

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": build_redirect_uri(),
        },
        auth=_basic_auth(client_id, client_secret),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        frappe.log_error(
            title="QBO OAuth Token Exchange Failed",
            message=f"Status: {resp.status_code}\nBody: {resp.text[:1000]}",
        )
        return _render_close_page(False, f"Token exchange failed: HTTP {resp.status_code}")

    settings.realm_id = realmId
    _persist_tokens(settings, resp.json())
    return _render_close_page(True, "Connected to QuickBooks. You can close this window.")


@frappe.whitelist()
def disconnect():
    frappe.only_for("System Manager")
    settings = _settings()
    settings.access_token = None
    settings.refresh_token = None
    settings.access_token_expires_at = None
    settings.refresh_token_expires_at = None
    settings.realm_id = None
    settings.connection_status = "Not Connected"
    settings.save(ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist()
def test_connection():
    frappe.only_for("System Manager")
    from qbo_coa_sync.api.qbo_client import QBOClient

    info = QBOClient().get_company_info()
    name = info.get("CompanyName") or info.get("LegalName") or "(unknown)"
    return {"ok": True, "company_name": name, "legal_name": info.get("LegalName") or name}


# ---- callback HTML ---------------------------------------------------------


def _render_close_page(ok: bool, message: str):
    title = "Connected to QuickBooks" if ok else "QuickBooks connection error"
    safe_message = frappe.utils.escape_html(message)
    html = f"""
<div style="padding: 8px 0">
  <p>{safe_message}</p>
  <p style="color: #6b7280">This window will close in a moment.</p>
</div>
<script>
try {{
  if (window.opener && !window.opener.closed) {{
    window.opener.postMessage({{ source: "qbo_coa_sync", ok: {str(ok).lower()}, message: {frappe.as_json(message)} }}, "*");
    setTimeout(function() {{ window.close(); }}, 600);
  }}
}} catch (e) {{}}
</script>
"""
    frappe.respond_as_web_page(
        title=title,
        html=html,
        success=ok,
        http_status_code=200,
        indicator_color="green" if ok else "red",
    )
