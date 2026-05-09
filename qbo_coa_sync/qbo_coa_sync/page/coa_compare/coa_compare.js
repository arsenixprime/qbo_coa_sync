// CoA Compare page — side-by-side ERPNext ↔ QBO Chart of Accounts.
//
// Single unified table (matched parents anchor children of both sides). Rows are indented by
// `depth` from the server. Cells where diff[field] === 'differs' get yellow background;
// cells missing on a side render as a muted em-dash.
//
// State preserved across re-renders: scrollTop, selected row_ids, active filter, search text.

frappe.pages["coa-compare"].on_page_load = function (wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: __("QBO CoA Comparison"),
        single_column: true,
    });

    new CoaCompare(page);
};

const STATUS_LINKED = "Linked";
const STATUS_NUMBER = "Matched by Number";
const STATUS_ONLY_QBO = "Only in QBO";
const STATUS_ONLY_ERPNEXT = "Only in ERPNext";

const STATUS_BADGE = {
    [STATUS_LINKED]:   { cls: "linked",    label: "Linked" },
    [STATUS_NUMBER]:   { cls: "by-number", label: "Matched by #" },
    [STATUS_ONLY_ERPNEXT]: { cls: "only-erp", label: "Only ERPNext" },
    [STATUS_ONLY_QBO]: { cls: "only-qbo",  label: "Only QBO" },
    "Unmatched":       { cls: "unmatched", label: "Unmatched" },
};

const FILTERS = [
    { key: "all",        label: "All" },
    { key: "linked",     label: "Linked" },
    { key: "by_number",  label: "Matched by #" },
    { key: "only_qbo",   label: "Only in QBO" },
    { key: "only_erp",   label: "Only in ERPNext" },
    { key: "differs",    label: "Has differences" },
];

class CoaCompare {
    constructor(page) {
        this.page = page;
        this.data = null;
        this.filter = "all";
        this.search = "";
        this.selected = new Set();
        this.errors = [];
        this.busy_cells = new Set();   // row_id|side|field

        this.build_chrome();
        this.refresh(/*reload_qbo*/ false);
    }

    // ---- chrome ------------------------------------------------------------

    build_chrome() {
        const $body = $(this.page.body).empty();

        this.$toolbar = $('<div class="coa-compare-toolbar"></div>').appendTo($body);
        this.$info = $('<div class="coa-compare-info"></div>').appendTo($body);
        this.$errors = $('<div></div>').appendTo($body);
        this.$tableWrap = $('<div style="overflow:auto; max-height: calc(100vh - 220px);"></div>').appendTo($body);

        // Primary buttons (top right of page header).
        this.page.set_primary_action(__("Refresh from QBO"), () => this.refresh(true), "refresh");
        this.page.add_menu_item(__("Bulk Link by Number"), () => this.bulk_link_by_number());
        this.page.add_menu_item(__("Open QuickBooks Settings"), () => frappe.set_route("Form", "QuickBooks Settings"));

        // Toolbar
        const $bulk = $('<div style="display:flex;gap:6px"></div>').appendTo(this.$toolbar);
        this.$btnSyncToErp = $(`<button class="btn btn-xs btn-default" disabled>← ${__("Sync Selected → ERPNext")}</button>`)
            .on("click", () => this.bulk_sync("qbo_to_erp"))
            .appendTo($bulk);
        this.$btnSyncToQbo = $(`<button class="btn btn-xs btn-default" disabled>${__("Sync Selected → QBO")} →</button>`)
            .on("click", () => this.bulk_sync("erp_to_qbo"))
            .appendTo($bulk);

        $('<span style="flex:1"></span>').appendTo(this.$toolbar);

        const $chips = $('<div style="display:flex;gap:4px"></div>').appendTo(this.$toolbar);
        this.$chips = {};
        FILTERS.forEach(f => {
            const $c = $(`<span class="filter-chip" data-key="${f.key}">${f.label} <span class="count"></span></span>`)
                .on("click", () => { this.filter = f.key; this.update_chips(); this.render_table(); })
                .appendTo($chips);
            this.$chips[f.key] = $c;
        });

        this.$search = $('<input type="search" class="coa-search" placeholder="Search name or number" />')
            .on("input", frappe.utils.debounce(() => {
                this.search = (this.$search.val() || "").trim().toLowerCase();
                this.render_table();
            }, 200))
            .appendTo(this.$toolbar);
    }

    update_chips() {
        Object.entries(this.$chips).forEach(([k, $el]) => $el.toggleClass("active", k === this.filter));
        if (this.data && this.data.counts) {
            const c = this.data.counts;
            this.$chips.all.find(".count").text(`(${this.data.rows.length})`);
            this.$chips.linked.find(".count").text(`(${c.linked || 0})`);
            this.$chips.by_number.find(".count").text(`(${c.by_number || 0})`);
            this.$chips.only_qbo.find(".count").text(`(${c.only_qbo || 0})`);
            this.$chips.only_erp.find(".count").text(`(${c.only_erp || 0})`);
            this.$chips.differs.find(".count").text(`(${c.differs || 0})`);
        }
    }

    update_bulk_buttons() {
        const has = this.selected.size > 0;
        this.$btnSyncToErp.prop("disabled", !has);
        this.$btnSyncToQbo.prop("disabled", !has);
    }

    // ---- data --------------------------------------------------------------

    async refresh(reload_qbo) {
        const scroll = this.$tableWrap[0] ? this.$tableWrap[0].scrollTop : 0;
        try {
            if (reload_qbo) {
                frappe.show_alert({ message: __("Pulling QBO accounts…"), indicator: "blue" });
                await frappe.call({ method: "qbo_coa_sync.api.compare.refresh_from_qbo", freeze: true, freeze_message: __("Pulling QBO accounts…") });
            }
            const r = await frappe.call({ method: "qbo_coa_sync.api.compare.get_comparison" });
            this.data = r.message;
        } catch (e) {
            this.errors.push({ message: this.err_message(e) });
        }
        this.render_info();
        this.render_errors();
        this.render_table();
        this.update_chips();
        if (this.$tableWrap[0]) this.$tableWrap[0].scrollTop = scroll;
    }

    err_message(e) {
        if (!e) return "Unknown error";
        if (e.message && e.message.message) return e.message.message;
        return e.message || String(e);
    }

    render_info() {
        if (!this.data) { this.$info.text("Loading…"); return; }
        const last = this.data.qbo_last_pulled_at;
        const stale = last && (Date.now() - new Date(last.replace(" ", "T")).getTime() > 15 * 60 * 1000);
        this.$info.toggleClass("warn", !!stale);
        const company = frappe.utils.escape_html(this.data.company || "?");
        const realm = frappe.utils.escape_html(this.data.realm_id || "?");
        const lastTxt = last ? frappe.datetime.comment_when(last) : __("never");
        this.$info.html(
            `<b>${company}</b> ↔ QBO realm <b>${realm}</b> · `
            + `Last pulled ${lastTxt}`
            + (stale ? ` <span style="color:#b45309">(stale — click Refresh from QBO)</span>` : "")
        );
    }

    render_errors() {
        this.$errors.empty();
        if (!this.errors.length) return;
        const $b = $('<div class="coa-error-banner"></div>').appendTo(this.$errors);
        const $ul = $('<div><b>Errors:</b><ul></ul></div>');
        this.errors.forEach(e => $ul.find("ul").append(`<li>${frappe.utils.escape_html(e.message)}${e.id ? ` <code>(${frappe.utils.escape_html(e.id)})</code>` : ""}</li>`));
        $b.append($ul);
        $('<button class="btn btn-xs btn-default">Dismiss</button>')
            .on("click", () => { this.errors = []; this.render_errors(); })
            .appendTo($b);
    }

    // ---- table -------------------------------------------------------------

    rows_for_view() {
        if (!this.data) return [];
        const q = this.search;
        return this.data.rows.filter(row => {
            // Filter
            if (this.filter !== "all") {
                if (this.filter === "linked" && row.status !== STATUS_LINKED) return false;
                if (this.filter === "by_number" && row.status !== STATUS_NUMBER) return false;
                if (this.filter === "only_qbo" && row.status !== STATUS_ONLY_QBO) return false;
                if (this.filter === "only_erp" && row.status !== STATUS_ONLY_ERPNEXT) return false;
                if (this.filter === "differs") {
                    if (!row.diff) return false;
                    if (!Object.values(row.diff).some(v => v === "differs")) return false;
                }
            }
            // Search
            if (q) {
                const haystack = [
                    row.erpnext && row.erpnext.account_name, row.erpnext && row.erpnext.account_number,
                    row.qbo && row.qbo.name, row.qbo && row.qbo.acct_num,
                ].filter(Boolean).join(" ").toLowerCase();
                if (!haystack.includes(q)) return false;
            }
            return true;
        });
    }

    render_table() {
        const rows = this.rows_for_view();
        const $tbl = $('<table class="coa-compare-table"></table>');

        // colgroup tunes column widths.
        $tbl.append(`
            <colgroup>
                <col style="width:36px"/>
                <col style="width:108px"/>
                <col style="width:80px"/>
                <col/>
                <col style="width:130px"/>
                <col/>
                <col style="width:80px"/>
                <col style="width:80px"/>
                <col/>
                <col style="width:140px"/>
                <col/>
                <col style="width:50px"/>
            </colgroup>
        `);
        $tbl.append(`
            <thead><tr>
                <th><input type="checkbox" class="coa-select-all"/></th>
                <th>Status</th>
                <th class="col-erp">#</th>
                <th class="col-erp">ERPNext Name</th>
                <th class="col-erp">Type</th>
                <th class="col-erp">Description</th>
                <th>Sync</th>
                <th class="col-qbo">#</th>
                <th class="col-qbo">QBO Name</th>
                <th class="col-qbo">Type</th>
                <th class="col-qbo">Description</th>
                <th></th>
            </tr></thead>
        `);

        const $tbody = $('<tbody></tbody>').appendTo($tbl);

        if (!rows.length) {
            $tbody.append(`<tr><td colspan="12" style="text-align:center;color:#888;padding:24px">${__("No accounts.")}</td></tr>`);
        } else {
            rows.forEach(row => $tbody.append(this.render_row(row)));
        }

        this.$tableWrap.empty().append($tbl);

        // Bind events
        $tbl.find(".coa-select-all").on("change", (e) => {
            const checked = e.target.checked;
            $tbl.find("tbody input.coa-row-select").prop("checked", checked).each((_, el) => {
                const id = $(el).data("rowId");
                if (checked) this.selected.add(id); else this.selected.delete(id);
            });
            this.update_bulk_buttons();
        });

        $tbl.find("tbody").on("change", "input.coa-row-select", (e) => {
            const id = $(e.target).data("rowId");
            if (e.target.checked) this.selected.add(id); else this.selected.delete(id);
            this.update_bulk_buttons();
        });

        $tbl.find("tbody").on("dblclick", "td.editable", (e) => this.start_edit($(e.currentTarget)));

        $tbl.find("tbody").on("click", ".btn-sync-to-erp", (e) => {
            const qbo_id = $(e.currentTarget).data("qboId");
            this.sync_one("qbo_to_erp", qbo_id);
        });
        $tbl.find("tbody").on("click", ".btn-sync-to-qbo", (e) => {
            const name = $(e.currentTarget).data("erpName");
            this.sync_one("erp_to_qbo", name);
        });

        $tbl.find("tbody").on("click", ".btn-row-menu", (e) => {
            e.preventDefault();
            const $tr = $(e.currentTarget).closest("tr");
            const row_id = $tr.data("rowId");
            const row = this.data.rows.find(r => r.row_id === row_id);
            this.open_row_menu(row, $(e.currentTarget));
        });

        // Restore selection state.
        this.selected.forEach(id => {
            $tbl.find(`input.coa-row-select[data-row-id='${id}']`).prop("checked", true);
        });
        this.update_bulk_buttons();
    }

    render_row(row) {
        const erp = row.erpnext;
        const qbo = row.qbo;
        const diff = row.diff || {};
        const indent = "<span class='indent'></span>".repeat(row.depth);
        const badge = STATUS_BADGE[row.status] || STATUS_BADGE.Unmatched;

        const erpEditable = !!erp;
        const qboEditable = !!qbo;
        const erpType = erp ? `${erp.root_type || ""}${erp.account_type ? " · " + erp.account_type : ""}` : "";
        const qboType = qbo ? `${qbo.account_type || ""}${qbo.account_sub_type ? " · " + qbo.account_sub_type : ""}` : "";

        const cell = (val, opts = {}) => {
            const { editable = false, side, field, diffKey, missing = false, prefix = "" } = opts;
            const cls = [];
            if (editable) cls.push("editable");
            if (side === "erp") cls.push("col-erp");
            if (side === "qbo") cls.push("col-qbo");
            if (diff[diffKey] === "differs") cls.push("diff");
            if (missing) cls.push("cell-missing");
            const dataAttrs = editable ? `data-side="${side}" data-field="${field}"` : "";
            const display = missing ? "—" : (val || "");
            return `<td class="${cls.join(" ")}" ${dataAttrs}>${prefix}${frappe.utils.escape_html(display)}</td>`;
        };

        const syncCell = `
            <td class="row-actions">
                ${qbo ? `<button class="btn btn-xs btn-default btn-sync-to-erp" title="Sync QBO → ERPNext" data-qbo-id="${frappe.utils.escape_html(qbo.qbo_id)}">←</button>` : ""}
                ${erp ? `<button class="btn btn-xs btn-default btn-sync-to-qbo" title="Sync ERPNext → QBO" data-erp-name="${frappe.utils.escape_html(erp.name)}">→</button>` : ""}
            </td>`;

        const menuCell = `<td class="row-actions"><button class="btn btn-xs btn-default btn-row-menu" title="More actions">⋯</button></td>`;

        return `<tr data-row-id="${row.row_id}">
            <td><input type="checkbox" class="coa-row-select" data-row-id="${row.row_id}"/></td>
            <td><span class="badge ${badge.cls}">${badge.label}</span></td>
            ${cell(erp ? erp.account_number : null, { editable: erpEditable, side: "erp", field: "account_number", diffKey: "account_number", missing: !erp, prefix: indent })}
            ${cell(erp ? erp.account_name : null,   { editable: erpEditable, side: "erp", field: "account_name",   diffKey: "name", missing: !erp })}
            ${cell(erp ? erpType : null,            { side: "erp", diffKey: "type", missing: !erp })}
            ${cell(erp ? erp.description : null,    { editable: erpEditable, side: "erp", field: "qbo_description", diffKey: "description", missing: !erp })}
            ${syncCell}
            ${cell(qbo ? qbo.acct_num : null,       { editable: qboEditable, side: "qbo", field: "AcctNum",     diffKey: "account_number", missing: !qbo })}
            ${cell(qbo ? qbo.name : null,           { editable: qboEditable, side: "qbo", field: "Name",        diffKey: "name", missing: !qbo })}
            ${cell(qbo ? qboType : null,            { side: "qbo", diffKey: "type", missing: !qbo })}
            ${cell(qbo ? qbo.description : null,    { editable: qboEditable, side: "qbo", field: "Description", diffKey: "description", missing: !qbo })}
            ${menuCell}
        </tr>`;
    }

    // ---- inline edit -------------------------------------------------------

    start_edit($cell) {
        if ($cell.find("input").length) return;
        const original = $cell.text();
        const $input = $(`<input type="text" class="coa-inline-edit" />`).val(original === "—" ? "" : original);
        $cell.empty().append($input);
        $input.focus().select();
        let committed = false;

        const cancel = () => {
            if (committed) return;
            committed = true;
            $cell.text(original);
        };
        const commit = async () => {
            if (committed) return;
            committed = true;
            const value = $input.val();
            if (value === (original === "—" ? "" : original)) { $cell.text(original); return; }
            $cell.html(`${frappe.utils.escape_html(value)} <span class="coa-spinner"></span>`);

            const side = $cell.data("side");
            const field = $cell.data("field");
            const $tr = $cell.closest("tr");
            const row = this.data.rows.find(r => r.row_id === $tr.data("rowId"));
            try {
                if (side === "erp") {
                    await frappe.call({
                        method: "qbo_coa_sync.api.sync.update_erpnext_field",
                        args: { erpnext_account: row.erpnext.name, field, value },
                    });
                } else {
                    await frappe.call({
                        method: "qbo_coa_sync.api.sync.update_qbo_field",
                        args: { qbo_id: row.qbo.qbo_id, field, value },
                    });
                }
                frappe.show_alert({ message: __("Saved"), indicator: "green" });
                this.refresh(false);
            } catch (e) {
                frappe.msgprint({ title: __("Update failed"), message: this.err_message(e), indicator: "red" });
                $cell.text(original);
            }
        };

        $input.on("blur", commit);
        $input.on("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); $input.blur(); }
            else if (e.key === "Escape") { committed = true; cancel(); }
        });
    }

    // ---- per-row sync ------------------------------------------------------

    async sync_one(direction, id) {
        try {
            const method = direction === "qbo_to_erp"
                ? "qbo_coa_sync.api.sync.sync_qbo_to_erpnext"
                : "qbo_coa_sync.api.sync.sync_erpnext_to_qbo";
            const args = direction === "qbo_to_erp" ? { qbo_id: id } : { erpnext_account: id };
            await frappe.call({ method, args, freeze: true });
            frappe.show_alert({ message: __("Synced"), indicator: "green" });
            this.refresh(false);
        } catch (e) {
            this.errors.push({ id, message: this.err_message(e) });
            this.render_errors();
        }
    }

    async bulk_sync(direction) {
        if (!this.selected.size) return;
        // Map row_ids → ids of the right side.
        const ids = [];
        const skipped = [];
        this.selected.forEach(id => {
            const row = this.data.rows.find(r => r.row_id === id);
            if (!row) return;
            if (direction === "qbo_to_erp") {
                if (row.qbo && row.qbo.qbo_id) ids.push(row.qbo.qbo_id);
                else skipped.push(id);
            } else {
                if (row.erpnext && row.erpnext.name) ids.push(row.erpnext.name);
                else skipped.push(id);
            }
        });
        if (!ids.length) {
            frappe.msgprint(__("No rows in the selection have a {0} side.", [direction === "qbo_to_erp" ? "QBO" : "ERPNext"]));
            return;
        }

        const method = direction === "qbo_to_erp"
            ? "qbo_coa_sync.api.sync.bulk_sync_qbo_to_erpnext"
            : "qbo_coa_sync.api.sync.bulk_sync_erpnext_to_qbo";
        const args = direction === "qbo_to_erp" ? { qbo_ids: ids } : { erpnext_accounts: ids };
        try {
            const r = await frappe.call({ method, args, freeze: true, freeze_message: __("Syncing {0} rows…", [ids.length]) });
            const result = r.message || {};
            const ok = (result.ok || []).length;
            const failed = result.failed || [];
            frappe.show_alert({
                message: __("Synced {0}, {1} failed", [ok, failed.length]),
                indicator: failed.length ? "orange" : "green",
            });
            failed.forEach(f => this.errors.push({ id: f.id, message: f.error }));
        } catch (e) {
            this.errors.push({ message: this.err_message(e) });
        }
        this.refresh(false);
    }

    async bulk_link_by_number() {
        try {
            const r = await frappe.call({ method: "qbo_coa_sync.api.sync.bulk_link_by_number", freeze: true });
            const m = r.message || {};
            frappe.msgprint({
                title: __("Bulk Link by Number"),
                message: `Linked: <b>${(m.linked || []).length}</b><br>Skipped: <b>${(m.skipped || []).length}</b>`,
                indicator: "green",
            });
        } catch (e) {
            this.errors.push({ message: this.err_message(e) });
        }
        this.refresh(false);
    }

    // ---- row menu (link / unlink) -----------------------------------------

    open_row_menu(row, $btn) {
        const items = [];
        if (row.erpnext && row.qbo && row.status === STATUS_LINKED) {
            items.push({ label: __("Unlink"), action: () => this.unlink(row.erpnext.name) });
        }
        if (row.erpnext && !row.qbo) {
            items.push({ label: __("Link to QBO account…"), action: () => this.open_link_dialog("erp_to_qbo", row.erpnext.name) });
        }
        if (row.qbo && !row.erpnext) {
            items.push({ label: __("Link to ERPNext account…"), action: () => this.open_link_dialog("qbo_to_erp", row.qbo.qbo_id) });
        }

        if (!items.length) return;

        // Native dropdown — quick and dirty.
        const $menu = $(`<div class="dropdown-menu show" style="position:absolute;display:block;z-index:1000"></div>`);
        items.forEach(it => {
            $('<a class="dropdown-item" href="#"></a>').text(it.label).on("click", (e) => {
                e.preventDefault();
                $menu.remove();
                it.action();
            }).appendTo($menu);
        });
        const off = $btn.offset();
        $menu.css({ top: off.top + 24, left: off.left - 160 });
        $("body").append($menu);
        const closer = (e) => {
            if ($menu[0].contains(e.target)) return;
            $menu.remove();
            $(document).off("click.coa-menu", closer);
        };
        setTimeout(() => $(document).on("click.coa-menu", closer), 0);
    }

    async unlink(erpnext_account) {
        await new Promise(resolve => frappe.confirm(__("Unlink {0} from QBO?", [erpnext_account]), resolve, () => {}));
        try {
            await frappe.call({ method: "qbo_coa_sync.api.sync.unlink", args: { erpnext_account }, freeze: true });
            frappe.show_alert({ message: __("Unlinked"), indicator: "orange" });
        } catch (e) {
            this.errors.push({ id: erpnext_account, message: this.err_message(e) });
        }
        this.refresh(false);
    }

    open_link_dialog(direction, anchor_id) {
        const isErpAnchor = direction === "erp_to_qbo";
        const search_method = isErpAnchor
            ? "qbo_coa_sync.api.sync.search_unmatched_qbo"
            : "qbo_coa_sync.api.sync.search_unmatched_erpnext";

        const dlg = new frappe.ui.Dialog({
            title: __("Link manually"),
            fields: [
                { fieldtype: "Data", fieldname: "search", label: __("Search"), description: __("Filter the candidate list.") },
                { fieldtype: "HTML", fieldname: "list" },
            ],
        });

        const $list = dlg.fields_dict.list.$wrapper;
        const render = (results) => {
            $list.empty();
            if (!results.length) { $list.html("<p style='color:#888'>No candidates.</p>"); return; }
            const $ul = $('<div></div>').appendTo($list);
            results.forEach(row => {
                const id = isErpAnchor ? row.qbo_id : row.name;
                const label = isErpAnchor
                    ? `${row.acct_num ? row.acct_num + " — " : ""}${row.fully_qualified_name || row.name}`
                    : `${row.account_number ? row.account_number + " — " : ""}${row.account_name}`;
                $('<a href="#" class="d-block" style="padding:6px 8px;border-bottom:1px solid #eee"></a>')
                    .text(label)
                    .on("click", async (e) => {
                        e.preventDefault();
                        try {
                            const args = isErpAnchor
                                ? { erpnext_account: anchor_id, qbo_id: id }
                                : { erpnext_account: id, qbo_id: anchor_id };
                            await frappe.call({ method: "qbo_coa_sync.api.sync.manual_link", args, freeze: true });
                            frappe.show_alert({ message: __("Linked"), indicator: "green" });
                            dlg.hide();
                            this.refresh(false);
                        } catch (err) {
                            frappe.msgprint({ title: __("Link failed"), message: this.err_message(err), indicator: "red" });
                        }
                    })
                    .appendTo($ul);
            });
        };

        const fetch_candidates = frappe.utils.debounce(async () => {
            const q = dlg.get_value("search") || "";
            const r = await frappe.call({ method: search_method, args: { query: q } });
            render(r.message || []);
        }, 200);

        dlg.fields_dict.search.df.onchange = fetch_candidates;
        dlg.show();
        fetch_candidates();
    }
}
