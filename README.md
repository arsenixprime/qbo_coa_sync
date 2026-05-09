# QBO CoA Sync

A standalone Frappe v15 app that connects an ERPNext company to a QuickBooks Online (QBO) company and lets an administrator **compare and synchronize the Chart of Accounts** between the two systems through a side-by-side UI. All sync is operator-driven — there is no automatic conflict resolution, no scheduler, no webhook receiver.

## Prerequisites

- **Frappe v15** and **ERPNext v15** (latest stable on the v15 branch).
- **Python 3.10+** (matches whatever your v15 bench provides).
- An **Intuit Developer** account at <https://developer.intuit.com/> with at least one app registered, plus a QuickBooks Online sandbox company for testing.

## Install

```bash
# From your bench directory
bench get-app https://github.com/<your-fork>/qbo_coa_sync
bench --site <site> install-app qbo_coa_sync
bench --site <site> migrate
```

The app installs three custom fields on `Account`: `quickbooks_id` (unique), `quickbooks_sync_token` (hidden), and `qbo_description`.

## Register your app in the Intuit Developer portal

1. Go to **<https://developer.intuit.com/>** → My Apps → your app.
2. Open the **Keys & OAuth** tab.
3. You'll see two sub-tabs: **Development** (sandbox) and **Production**. Configure both if you intend to switch environments later.
4. Under **Redirect URIs**, add the exact URL shown on the **QuickBooks Settings** page in your ERPNext site:

   ```
   https://<your-erpnext-host>/api/method/qbo_coa_sync.api.oauth.callback
   ```

   The Settings page has a "Copy" button next to the redirect URI field for this. **The string must match exactly** — Intuit will refuse the OAuth flow on any mismatch.
5. The required scope is `com.intuit.quickbooks.accounting`.
6. Copy the **Client ID** and **Client Secret** from this page into **QuickBooks Settings** in ERPNext, set **Environment** to Sandbox or Production, and save.

## Walkthrough

> Screenshots: replace these placeholders with images once you have a sandbox connected.

1. **Settings → Connect.** Open *QuickBooks Settings*. The header banner shows *Not Connected*. Click **Connect to QuickBooks** — a popup opens at Intuit. Sign in, choose the sandbox company, click *Connect*. The popup closes; the form reloads as *Connected*. Click **Test Connection** — you should see the sandbox company's legal name.

2. **Refresh from QBO.** Click **Open CoA Comparison** (or go to `/app/coa-compare`). On first open the table is empty — click **Refresh from QBO** in the toolbar. The QBO Account Cache fills with all sandbox accounts.

3. **Bulk Link by Number.** If your ERPNext company already has account numbers that match QBO, click **Bulk Link by Number** in the page menu — every unambiguous number match becomes a *Linked* row.

4. **Per-row sync.** For any unlinked or differing row, click `←` to pull the QBO version into ERPNext, or `→` to push the ERPNext version up to QBO. The row-actions menu (`⋯`) on unmatched rows offers **Link manually…** with autocomplete on the other side's unmatched candidates.

5. **Inline edits.** Double-click any editable cell (account number, name, description on either side) to edit in place. Enter commits; Escape cancels. ERPNext-side edits write to the local Account; QBO-side edits push a sparse update to QBO and refresh the cache row.

## Default Account Type Mapping

The first time you open Settings, click **Tools → Seed Default Type Mapping** to populate a sensible default mapping table. Mapping resolution at sync time is *most-specific wins*:

1. Try `(qbo_account_type, qbo_account_subtype)` exactly.
2. Fall back to `(qbo_account_type, "")` (the row whose subtype is blank).
3. If neither matches, the sync errors out with a clear message — add a row in *QuickBooks Settings → Account Type Mapping* and retry.

For ERPNext → QBO the lookup is reversed: pick the row whose `(erpnext_root_type, erpnext_account_type)` matches; if multiple rows match, prefer the one with a blank `qbo_account_subtype` (the canonical fallback) so reverse resolution is deterministic.

## Troubleshooting

The QBO API returns errors as a `Fault.Error[]` array; the comparison page surfaces these inline. Common codes:

| Code | What it means | Recovery |
|------|---------------|----------|
| **3200** | Invalid auth — token expired/revoked, or app no longer authorized for the realm. | Reconnect from *QuickBooks Settings*. If you switched Environment, you must reconnect — the realm and tokens are environment-specific. |
| **5010** | Stale object — the `SyncToken` you sent isn't current. Someone else (or another tab) updated this account in QBO since your last refresh. | Click **Refresh from QBO** in the comparison page, then retry the sync. |
| **6000** | Business validation — e.g. account name conflict, parent of wrong type, currency change attempted. The `Detail` field has the human-readable reason. | Read the message in the page's red error banner. Common cases: duplicate account name in QBO, attempt to set parent to an account of a different `AccountType`, or attempting to change `CurrencyRef` (QBO disallows currency changes after creation). |
| **610** | Object not found — the QBO Id you referenced no longer exists. | Refresh from QBO; the cache may be out of date. |
| HTTP **429** | Rate limited. The client retries idempotent GETs with backoff but POSTs surface immediately. | Wait a few seconds and retry. |
| HTTP **5xx** | Intuit API hiccup. The client auto-retries idempotent GETs once. | Retry; check Intuit's status page if persistent. |

If the client and Intuit's live docs disagree on URLs, scopes, or `AccountType` enum values, **the live docs win**. Verify against <https://developer.intuit.com/app/developer/qbo/docs/api/accounting/most-commonly-used/account> before shipping changes.

### Logs

All non-2xx QBO responses get logged to ERPNext's **Error Log** (`/app/error-log`) under the title **QBO API Error**, with the request URL, status, and response body (tokens redacted). Sync row failures log under **QBO Sync Row Failed**.

## v1 scope

What this app **is**:

- On-demand, operator-driven CoA sync between one ERPNext company and one QBO realm.
- Side-by-side comparison with hierarchy, diff highlighting, manual pairing, inline edits.
- Per-row and bulk sync in either direction, top-down by hierarchy.

What this app **is not** (yet):

- No background scheduler — nothing runs unless an operator clicks something.
- No webhook receiver — QBO changes aren't pushed; you click *Refresh from QBO*.
- No audit trail / change history doctype — the comparison cache is transient and gets wiped on every refresh.
- No multi-company / multi-realm — one ERPNext company linked to one QBO realm.
- No sync of anything other than the Chart of Accounts (no Customers, Vendors, Items, Journal Entries, etc.).
- No conflict auto-resolution — the operator always picks the direction.

## Development

Run unit tests:

```bash
# In the bench directory
bench --site <test-site> run-tests --app qbo_coa_sync
```

Many of the unit tests use `unittest.mock` to stub `frappe.get_doc`, `frappe.db.*`, and `requests.post`, so most of the suite runs without a live Frappe context. The OAuth state, type-mapping resolution, matching algorithm, hierarchy ordering, and sync payload shapes are all covered.

## License

MIT — see [license.txt](license.txt).
