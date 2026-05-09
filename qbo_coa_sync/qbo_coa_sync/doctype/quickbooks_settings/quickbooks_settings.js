// QuickBooks Settings — form scripts.
//
// Renders Connect / Disconnect / Test Connection / Open Comparison / Seed Mapping buttons,
// shows a top-of-form connection status banner, and copies the redirect URI on click.

frappe.ui.form.on("QuickBooks Settings", {
    refresh(frm) {
        render_status_banner(frm);
        wire_buttons(frm);
        wire_redirect_copy(frm);
        frm.set_df_property("redirect_uri", "description",
            "Register this exact URL in your Intuit Developer app's <b>Redirect URIs</b> list — "
            + "for both Sandbox and Production tabs if you'll use both.");
    },

    environment(frm) {
        frappe.show_alert({
            message: __("Environment changed. Reconnect to pick up the new API base."),
            indicator: "orange",
        });
    },
});

function wire_buttons(frm) {
    const status = frm.doc.connection_status || "Not Connected";

    if (status !== "Connected") {
        frm.add_custom_button(__("Connect to QuickBooks"), () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save the form before connecting."));
                return;
            }
            const url = "/api/method/qbo_coa_sync.api.oauth.start_auth";
            const popup = window.open(url, "qbo_oauth", "width=720,height=820");
            if (!popup) {
                frappe.msgprint(__("Popup blocked — allow popups for this site."));
                return;
            }
            const handler = (event) => {
                if (!event.data || event.data.source !== "qbo_coa_sync") return;
                window.removeEventListener("message", handler);
                if (event.data.ok) {
                    frappe.show_alert({ message: __("Connected to QuickBooks"), indicator: "green" });
                } else {
                    frappe.msgprint({ title: __("OAuth error"), message: event.data.message, indicator: "red" });
                }
                frm.reload_doc();
            };
            window.addEventListener("message", handler);
        }).addClass("btn-primary");
    }

    if (status === "Connected") {
        frm.add_custom_button(__("Disconnect"), () => {
            frappe.confirm(__("Disconnect from QuickBooks? Tokens will be cleared."), () => {
                frappe.call({
                    method: "qbo_coa_sync.api.oauth.disconnect",
                    callback: () => frm.reload_doc(),
                });
            });
        });

        frm.add_custom_button(__("Test Connection"), () => {
            frappe.call({
                method: "qbo_coa_sync.api.oauth.test_connection",
                freeze: true,
                freeze_message: __("Calling QuickBooks…"),
                callback: (r) => {
                    if (r.message && r.message.ok) {
                        frappe.msgprint({
                            title: __("Connected"),
                            message: __("QBO company: <b>{0}</b>", [r.message.company_name]),
                            indicator: "green",
                        });
                    }
                },
            });
        });

        frm.add_custom_button(__("Open CoA Comparison"), () => {
            frappe.set_route("coa-compare");
        }).addClass("btn-primary");
    }

    frm.add_custom_button(__("Seed Default Type Mapping"), () => {
        frappe.call({
            method: "qbo_coa_sync.qbo_coa_sync.doctype.quickbooks_settings.quickbooks_settings.seed_default_type_mapping",
            callback: () => frm.reload_doc(),
        });
    }, __("Tools"));
}

function render_status_banner(frm) {
    const status = frm.doc.connection_status || "Not Connected";
    const colors = {
        "Connected": "green",
        "Not Connected": "gray",
        "Token Expired": "orange",
        "Error": "red",
    };
    const color = colors[status] || "gray";
    const realm = frm.doc.realm_id ? ` · Realm <b>${frappe.utils.escape_html(frm.doc.realm_id)}</b>` : "";
    const html = `
        <div style="padding: 10px 14px; border-left: 4px solid var(--${color}-500, #888);
                    background: var(--bg-light-gray, #f8f9fa); margin-bottom: 12px; border-radius: 4px;">
            <b>${frappe.utils.escape_html(status)}</b>${realm}
        </div>`;
    frm.dashboard.set_headline(html);
}

function wire_redirect_copy(frm) {
    if (!frm.fields_dict.redirect_uri || !frm.fields_dict.redirect_uri.$wrapper) return;
    if (frm.$redirect_copy_added) return;
    frm.$redirect_copy_added = true;

    const $btn = $(`<button class="btn btn-xs btn-default" style="margin-left: 8px;">
        ${frappe.utils.icon("copy", "xs")} ${__("Copy")}</button>`);
    $btn.on("click", (e) => {
        e.preventDefault();
        const v = frm.doc.redirect_uri || "";
        if (!v) return;
        navigator.clipboard.writeText(v).then(() => {
            frappe.show_alert({ message: __("Copied"), indicator: "green" });
        });
    });
    frm.fields_dict.redirect_uri.$wrapper.find(".control-input").append($btn);
}
