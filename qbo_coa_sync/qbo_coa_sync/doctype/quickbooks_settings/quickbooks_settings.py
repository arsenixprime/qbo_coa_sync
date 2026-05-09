import frappe
from frappe import _
from frappe.model.document import Document


# Default account type mapping rows seeded from the spec.
DEFAULT_TYPE_MAPPING = [
    ("Bank", "", "Asset", "Bank"),
    ("Other Current Asset", "", "Asset", ""),
    ("Other Current Asset", "Inventory", "Asset", "Stock"),
    ("Fixed Asset", "", "Asset", "Fixed Asset"),
    ("Other Asset", "", "Asset", ""),
    ("Accounts Receivable", "", "Asset", "Receivable"),
    ("Equity", "", "Equity", ""),
    ("Expense", "", "Expense", "Expense Account"),
    ("Other Expense", "", "Expense", "Expense Account"),
    ("Cost of Goods Sold", "", "Expense", "Cost of Goods Sold"),
    ("Income", "", "Income", "Income Account"),
    ("Other Income", "", "Income", "Income Account"),
    ("Accounts Payable", "", "Liability", "Payable"),
    ("Credit Card", "", "Liability", ""),
    ("Long Term Liability", "", "Liability", ""),
    ("Other Current Liability", "", "Liability", ""),
]


class QuickBooksSettings(Document):
    def onload(self):
        self.set_onload("redirect_uri_value", build_redirect_uri())

    def before_save(self):
        # Always reflect the canonical redirect URI for this site.
        self.redirect_uri = build_redirect_uri()


def build_redirect_uri() -> str:
    site_url = frappe.utils.get_url()
    return f"{site_url.rstrip('/')}/api/method/qbo_coa_sync.api.oauth.callback"


@frappe.whitelist()
def seed_default_type_mapping():
    frappe.only_for(["System Manager"])
    settings = frappe.get_single("QuickBooks Settings")
    if settings.account_type_mapping:
        frappe.msgprint(_("Account Type Mapping already populated; nothing to seed."))
        return
    for qbo_type, qbo_subtype, root_type, account_type in DEFAULT_TYPE_MAPPING:
        settings.append(
            "account_type_mapping",
            {
                "qbo_account_type": qbo_type,
                "qbo_account_subtype": qbo_subtype or None,
                "erpnext_root_type": root_type,
                "erpnext_account_type": account_type or None,
            },
        )
    settings.save()
    frappe.msgprint(_("Seeded {0} default mapping rows.").format(len(DEFAULT_TYPE_MAPPING)))
