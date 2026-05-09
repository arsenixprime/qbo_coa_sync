app_name = "qbo_coa_sync"
app_title = "QBO CoA Sync"
app_publisher = "Greensight Ag"
app_description = "Compare and sync the Chart of Accounts between QuickBooks Online and ERPNext."
app_email = "james@greensightag.com"
app_license = "MIT"

fixtures = [
    {
        "doctype": "Custom Field",
        "filters": [["module", "=", "QBO CoA Sync"]],
    }
]

# Whitelisted methods reachable without CSRF (OAuth callback comes back via top-level browser
# redirect from Intuit, so it cannot carry an X-Frappe-CSRF-Token).
override_whitelisted_methods = {}

# The OAuth callback must be reachable as GET via /api/method/. Frappe handles whitelisting
# through the @frappe.whitelist(allow_guest=False) decorator on the function itself.
