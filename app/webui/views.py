"""The pages of the web app. Each is a function; nav.py puts them into the navigation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from webui.client import ApiError, api
from webui.style import opener, page_header

# How each ingest outcome is shown: label, badge colour, Material icon.
OUTCOMES = {
    "approved": ("approved", "green", ":material/check_circle:"),
    "needs_review": ("waits for review", "orange", ":material/rate_review:"),
    "needs_ocr": ("waits for OCR", "blue", ":material/hourglass_top:"),
    "duplicate": ("already stored", "gray", ":material/content_copy:"),
    "failed": ("failed", "red", ":material/error:"),
}
OUTCOME_LABELS = {key: label for key, (label, _, _) in OUTCOMES.items()}

ALERT_KINDS = {
    "price_jump": ("Price up", "orange", ":material/trending_up:"),
    "cheaper_supplier": ("Cheaper elsewhere", "green", ":material/savings:"),
    "over_target": ("Over target", "red", ":material/warning:"),
}

# Chart colours per theme (the same values as .streamlit/config.toml).
PALETTE = {
    "light": {"ok": "#2F6B4F", "over": "#C2412D", "ink": "#1D2A26"},
    "dark": {"ok": "#6CBF94", "over": "#E36A55", "ink": "#E4EBE6"},
}


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    """Run an API call; show its error instead of a stack trace."""
    try:
        return call()
    except ApiError as error:
        st.error(error.detail, icon=":material/cloud_off:")
        return default


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _eur(value: Any) -> str:
    return "–" if value is None else f"{float(value):,.2f} €"


def _colours() -> dict[str, str]:
    theme = getattr(getattr(st.context, "theme", None), "type", None) or "light"
    return PALETTE.get(theme, PALETTE["light"])


def reviewer() -> str:
    return st.session_state.get("reviewer") or "reviewer"


def _go(name: str) -> None:
    from webui import nav  # imported here: nav imports this module

    nav.go(name)


# --- dashboard ----------------------------------------------------------------------------------------


def dashboard() -> None:
    summary = _safe(lambda: api().get("/summary"))
    if summary is None:
        st.title("Dashboard")
        return
    _opener(summary)
    _todo(summary)

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Food cost per dish")
        st.caption("Share of the net menu price spent on ingredients. The mark is each dish's target.")
        _food_cost_chart(_safe(lambda: api().get("/menu"), []))
    with right:
        _alerts_panel()

    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Food purchases per month")
        _purchases_chart(_safe(lambda: api().get("/purchases/monthly", months=12), []))
    with right:
        _price_history()


# Purchase categories, biggest first, each with its own colour (stable from month to month).
CATEGORIES = {
    "meat": ("Meat", "#2F6B4F"), "drinks": ("Drinks", "#4E6E8E"), "dry": ("Dry goods", "#D99A1E"),
    "dairy": ("Dairy", "#C9B98F"), "bakery": ("Bakery", "#A0703C"), "produce": ("Produce", "#8A9A5B"),
    "frozen": ("Frozen", "#9DBBD0"), "veggie": ("Veggie", "#6F9E7A"),
}  # fmt: skip


def _purchases_chart(months: list[dict]) -> None:
    """Net food purchases per month, stacked by category (the monthly Wareneinsatz)."""
    if not months:
        st.caption("Approved invoices show up here, month by month.")
        return
    rows = [{"Month": m["month"][:10], "Category": CATEGORIES.get(k, (k.capitalize(), None))[0], "EUR": float(v),
             "Order": list(CATEGORIES).index(k) if k in CATEGORIES else 99}
            for m in months for k, v in m["by_category"].items()]  # fmt: skip
    names = [label for label, _ in CATEGORIES.values()]
    others = sorted({r["Category"] for r in rows} - set(names))
    colours = [colour for _, colour in CATEGORIES.values()] + ["#9AA59F"] * len(others)
    chart = (
        alt.Chart(pd.DataFrame(rows))
        .mark_bar(width={"band": 0.62}, cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("yearmonth(Month):O", title=None, axis=alt.Axis(format="%b", labelAngle=0)),
            y=alt.Y("sum(EUR):Q", title="EUR, net", axis=alt.Axis(format=",.0f", tickCount=4)),
            color=alt.Color(
                "Category:N",
                scale=alt.Scale(domain=names + others, range=colours),
                legend=alt.Legend(orient="bottom", title=None, columns=4, symbolType="square"),
            ),
            order=alt.Order("Order:Q", sort="ascending"),  # meat at the bottom, small categories on top
            tooltip=[
                alt.Tooltip("yearmonth(Month):O", title="Month", format="%B %Y"),
                "Category",
                alt.Tooltip("sum(EUR):Q", title="EUR", format=",.2f"),
            ],
        )  # fmt: skip
        .properties(height=300)
    )
    st.altair_chart(chart, width="stretch")


def _opener(summary: dict) -> None:
    """The month as one sentence: what was spent on food, and how that compares with the month before."""
    this, last = _num(summary["wareneinsatz_this_month"]), _num(summary["wareneinsatz_last_month"])
    if not summary["latest_month"] or this is None:
        opener("No purchases yet", "Upload your first supplier invoices to see what you spend on food.")
        return
    month = date.fromisoformat(summary["latest_month"])
    previous = (month.replace(day=1) - pd.Timedelta(days=1)).strftime("%B")
    sentence = f"{this:,.0f} € of food bought in {month.strftime('%B')}"
    if last:
        change = (this - last) / last
        sentence += f", {abs(change):.1%} {'more' if change > 0 else 'less'} than in {previous}."
    else:
        sentence += "."
    opener(month.strftime("%B %Y"), sentence)


def _todo(summary: dict) -> None:
    """What needs a person today, as buttons that open the right page."""
    todo = [
        (summary["invoices_waiting_review"], "invoice to review", "invoices to review", ":material/fact_check:",
         "review"),
        (summary["documents_waiting_ocr"], "scan waiting for OCR", "scans waiting for OCR", ":material/hourglass_top:",
         "upload"),
        (summary["suggestions_open"], "ingredient to confirm", "ingredients to confirm", ":material/nutrition:",
         "ingredients"),
        (summary["dishes_over_target"], "dish over target", "dishes over target", ":material/warning:", "menu"),
    ]  # fmt: skip
    open_items = [item for item in todo if item[0]]
    if not open_items:
        st.success("Nothing waits for you. Every invoice is checked.", icon=":material/task_alt:")
        return
    with st.container(horizontal=True, gap="small"):
        for count, one, many, icon, page in open_items:
            if st.button(f"{count} {one if count == 1 else many}", icon=icon, key=f"todo-{page}"):
                _go(page)
    st.write("")


def _food_cost_chart(dishes: list[dict]) -> None:
    """A bullet chart: one bar per dish, a tick at its target, tomato when it is over."""
    if not dishes:
        st.caption("Add recipes to see each dish's food cost: uv run wareneinsatz recipes load")
        return
    priced = [d for d in dishes if d["food_cost_pct"] is not None]
    frame = pd.DataFrame(
        [{"Dish": d["name"], "Food cost": float(d["food_cost_pct"]) / 100,
          "Target": float(d["target_food_cost_pct"]) / 100 if d["target_food_cost_pct"] else None,
          "Cost": _eur(d["cost"]), "Status": "Over target" if d["over_target"] else "On target"}
         for d in priced]
    )  # fmt: skip
    frame = frame.sort_values("Food cost", ascending=False)
    frame["Label at"] = frame[["Food cost", "Target"]].max(axis=1)  # the label sits right of bar and target mark
    colours = _colours()
    base = alt.Chart(frame).encode(
        y=alt.Y(
            "Dish:N",
            sort=list(frame["Dish"]),
            title=None,
            axis=alt.Axis(ticks=False, domain=False, labelLimit=200, labelPadding=8),
        )  # fmt: skip
    )
    top = max(0.5, float(frame["Label at"].max()) * 1.2)
    bars = base.mark_bar(height=16, cornerRadiusEnd=4).encode(
        x=alt.X(
            "Food cost:Q",
            title=None,
            scale=alt.Scale(domain=[0, top]),
            axis=alt.Axis(format="%", tickCount=5, domain=False),
        ),  # fmt: skip
        color=alt.Color(
            "Status:N",
            scale=alt.Scale(domain=["On target", "Over target"], range=[colours["ok"], colours["over"]]),
            legend=alt.Legend(orient="top", title=None, symbolType="circle"),
        ),  # fmt: skip
        tooltip=["Dish", alt.Tooltip("Food cost:Q", format=".1%"), alt.Tooltip("Target:Q", format=".0%"), "Cost"],
    )
    target = base.mark_tick(thickness=2.5, size=26, color=colours["ink"]).encode(x="Target:Q")
    labels = base.mark_text(align="left", dx=9, fontSize=12, color=colours["ink"]).encode(
        x="Label at:Q", text=alt.Text("Food cost:Q", format=".1%")
    )
    chart = (bars + target + labels).properties(height=34 * len(frame) + 30)
    st.altair_chart(chart, width="stretch")
    missing = [d["name"] for d in dishes if d["food_cost_pct"] is None]
    if missing:
        st.caption("No price yet for: " + ", ".join(missing))


def _alerts_panel() -> None:
    head, action = st.columns([3, 2], vertical_alignment="bottom")
    head.subheader("Alerts")
    if action.button("Check prices", icon=":material/refresh:", width="stretch"):
        result = _safe(lambda: api().post("/alerts/check"))
        if result:
            st.toast(f"{result['new']} new alert(s)", icon=":material/notifications:")
    alerts = _safe(lambda: api().get("/alerts"), [])
    if not alerts:
        st.caption("No open alerts. Price jumps and cheaper suppliers appear here.")
        return
    with st.container(height=430, border=False):
        for alert in alerts:
            label, colour, icon = ALERT_KINDS.get(alert["kind"], (alert["kind"], "gray", None))
            with st.container(border=True):
                text_col, button_col = st.columns([7, 1], vertical_alignment="center")
                text_col.badge(label, icon=icon, color=colour)
                text_col.markdown(f"{alert['message']}  \n:gray[{date.fromisoformat(alert['happened_on']):%d %b %Y}]")
                if button_col.button("", icon=":material/check:", key=f"ack-{alert['id']}", type="tertiary",
                                     help="Mark as seen"):  # fmt: skip
                    _safe(lambda alert=alert: api().post(f"/alerts/{alert['id']}/ack", {"by": reviewer()}))
                    st.rerun()


def _price_history() -> None:
    st.subheader("Price history")
    prices = _safe(lambda: api().get("/prices"), [])
    if not prices:
        st.caption("Prices appear once invoices are approved and their ingredients confirmed.")
        return
    names = sorted({(p["ingredient_name"], p["ingredient_key"]) for p in prices})
    default = next((i for i, (_, key) in enumerate(names) if key == "patty-rind-180"), 0)
    name, key = st.selectbox("Ingredient", names, index=default, format_func=lambda item: item[0],
                             label_visibility="collapsed")  # fmt: skip
    points = [p for p in prices if p["ingredient_key"] == key]
    frame = pd.DataFrame(
        {"date": pd.to_datetime([p["issue_date"] for p in points]), "supplier": [p["supplier"] for p in points],
         "price": [float(p["price"]) for p in points]}
    )  # fmt: skip
    st.line_chart(frame, x="date", y="price", color="supplier", x_label="",
                  y_label=f"EUR per {points[0]['display_unit']}", height=270)  # fmt: skip


def _menu_table(dishes: list[dict]) -> None:
    if not dishes:
        st.caption("Add recipes to see each dish's food cost: uv run wareneinsatz recipes load")
        return
    rows = []
    for d in dishes:
        if d["missing"]:
            status = "Missing price: " + ", ".join(d["missing"])
        else:
            status = "Over target" if d["over_target"] else "On target"
        rows.append(
            {
                "Dish": d["name"],
                "Menu price": _num(d["menu_price_gross"]),
                "Net price": _num(d["net_price"]),
                "Cost": _num(d["cost"]),
                "Food cost %": _num(d["food_cost_pct"]),
                "Target %": _num(d["target_food_cost_pct"]),
                "Margin": _num(d["margin"]),
                "Status": status,
            }
        )
    money = st.column_config.NumberColumn(format="%.2f €")
    over = _colours()["over"]
    table = pd.DataFrame(rows).style.map(
        lambda status: f"color: {over}; font-weight: 600" if status != "On target" else "", subset=["Status"]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=35 * (len(rows) + 1) + 3,  # every dish visible, no inner scrolling
        column_config={
            "Menu price": money, "Net price": money, "Cost": money, "Margin": money,
            "Food cost %": st.column_config.ProgressColumn(format="%.1f %%", min_value=0, max_value=50),
            "Target %": st.column_config.NumberColumn(format="%.0f %%"),
        },
    )  # fmt: skip


# --- upload ------------------------------------------------------------------------------------------


def _outcome_badge(outcome: str) -> str:
    label, colour, icon = OUTCOMES.get(outcome, (outcome, "gray", ""))
    return f":{colour}-badge[{icon} {label}]"


def upload() -> None:
    page_header("Upload invoices", "E-invoices (XRechnung, ZUGFeRD) are read exactly. Scans and photos wait for "
                "OCR and always go to review.")  # fmt: skip
    left, right = st.columns([3, 2], gap="large")
    with left:
        files = st.file_uploader(
            "Supplier invoices and till receipts",
            type=["xml", "pdf", "jpg", "jpeg", "png", "heic", "webp", "tif", "tiff"],
            accept_multiple_files=True,
        )
        if files and st.button(f"Upload {len(files)} file(s)", type="primary", icon=":material/upload:"):
            payload = [("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files]
            for result in _safe(lambda: api().post("/documents", files=payload), []):
                with st.container(border=True):
                    st.markdown(f"{_outcome_badge(result['outcome'])} **{result['filename']}**  \n{result['message']}")
                    for issue in result["issues"]:
                        st.caption(f"{issue['severity'].capitalize()}: {issue['message']}")
    with right, st.container(border=True):
        _extraction_panel()
    _failed_documents()


@st.fragment(run_every=5)
def _extraction_panel() -> None:
    """Reads waiting scans and photos with OCR + the local language model; refreshes itself while it runs."""
    st.subheader("Scans and photos")
    status = _safe(lambda: api().get("/extraction"))
    if status is None:
        return
    if status["running"]:
        st.progress(status["done"] / status["total"] if status["total"] else 0.0,
                    text=f"Reading {status['done']} of {status['total']}")  # fmt: skip
        st.caption("About a minute per page with the local model. You can leave this page.")
    else:
        waiting = status["waiting"]
        st.markdown(f"**{waiting}** document(s) wait for OCR." if waiting else "Nothing waits for OCR.")
        if waiting and st.button("Read them now", icon=":material/document_scanner:", type="primary"):
            _safe(lambda: api().post("/extraction"))
            st.rerun()
    if status["error"]:
        st.error(status["error"])
    elif status["finished_at"] and status["outcomes"]:
        done = " ".join(f"{_outcome_badge(k)} {n}" for k, n in status["outcomes"].items())
        st.markdown(f"Last run: {done}")
        if status["outcomes"].get("failed"):
            st.caption("The reasons are listed under Failed documents.")


def _failed_documents() -> None:
    """Every document that could not become an invoice, with the reason, so nothing fails silently."""
    failed = _safe(lambda: api().get("/documents", status="failed", limit=50), [])
    if not failed:
        return
    with st.expander(f"Failed documents ({len(failed)})", icon=":material/error:"):
        st.dataframe(
            [
                {
                    "File": d["filename"],
                    "Received": d["received_at"][:16].replace("T", " "),
                    "Read by": d["extraction_method"] or "-",
                    "Reason": d["error"],
                }
                for d in failed
            ],
            hide_index=True,
            width="stretch",
        )


# --- review ------------------------------------------------------------------------------------------

LINE_COLUMNS = ["line_no", "description", "seller_article_no", "quantity", "unit_raw", "unit_code", "unit_price",
                "allowance_amount", "line_amount", "vat_rate"]  # fmt: skip
LINE_LABELS = {
    "line_no": st.column_config.NumberColumn("No.", width="small"),
    "description": st.column_config.TextColumn("Description", width="large"),
    "seller_article_no": "Article no.", "quantity": "Qty", "unit_raw": "Unit", "unit_code": "Unit code",
    "unit_price": "Unit price", "allowance_amount": "Discount", "line_amount": "Amount", "vat_rate": "VAT %",
    "taxable_amount": "Net amount", "tax_amount": "VAT",
}  # fmt: skip


def review() -> None:
    page_header("Review", "Compare what was read with the page, fix anything wrong, then approve. "
                "Only approved invoices count in prices and costing.")  # fmt: skip
    waiting = _safe(lambda: api().get("/invoices", status="needs_review"), [])
    if not waiting:
        st.success("No invoices wait for review.", icon=":material/task_alt:")
        return
    options = {i["id"]: i for i in waiting}

    def describe(invoice_id: int) -> str:
        i = options[invoice_id]
        errors = f"{i['errors']} error(s)" if i["errors"] else "no errors"
        return (
            f"#{invoice_id} {i['supplier']}, {i['invoice_number'] or 'no number'}, {_eur(i['gross_total'])}, {errors}"
        )

    chosen = st.selectbox(f"{len(waiting)} invoice(s) waiting", list(options), format_func=describe)
    detail = _safe(lambda: api().get(f"/invoices/{chosen}"))
    if detail is None:
        return
    doc_col, form_col = st.columns([2, 3], gap="large")
    with doc_col, st.container(border=True):
        st.caption(f"{detail['filename']}, read by {detail['extraction_method'] or '-'}")
        preview = _safe_preview(detail["document_id"])
        if preview:
            st.image(preview, width="stretch")
    with form_col:
        for issue in detail["issues"]:
            if issue["severity"] == "error":
                st.error(issue["message"], icon=":material/error:")
            else:
                st.warning(issue["message"], icon=":material/info:")
        _review_form(detail)


def _safe_preview(document_id: int) -> bytes | None:
    try:
        return api().get(f"/documents/{document_id}/preview", width=1000)
    except ApiError as error:
        if error.status == 415:  # XML: show the file itself
            content = api().get(f"/documents/{document_id}/file")
            st.code(content.decode("utf-8", errors="replace")[:4000], language="xml", height=520)
        else:
            st.error(error.detail)
        return None


def _review_form(detail: dict) -> None:
    data = detail["data"]
    with st.form(f"review-{detail['id']}", border=True):
        c1, c2, c3 = st.columns(3)
        supplier = c1.text_input("Supplier", data["supplier"]["name"])
        number = c2.text_input("Invoice number", data["invoice_number"] or "")
        issue = c3.date_input("Invoice date", date.fromisoformat(data["issue_date"]) if data["issue_date"] else None)
        c4, c5, c6 = st.columns(3)
        net = c4.text_input("Net total", data["net_total"] or "")
        vat = c5.text_input("VAT total", data["vat_total"] or "")
        gross = c6.text_input("Gross total", data["gross_total"])
        gross_prices = st.checkbox("Prices include VAT (till receipt)", data["prices_include_vat"])
        st.markdown("**Lines**")
        lines = st.data_editor(
            pd.DataFrame([{c: line.get(c) for c in LINE_COLUMNS} for line in data["lines"]], columns=LINE_COLUMNS),
            num_rows="dynamic", hide_index=True, width="stretch", key=f"lines-{detail['id']}",
            column_config=LINE_LABELS,
        )  # fmt: skip
        st.markdown("**VAT breakdown**")
        vat_rows = st.data_editor(
            pd.DataFrame(data["vat_breakdown"], columns=["vat_rate", "taxable_amount", "tax_amount"]),
            num_rows="dynamic", hide_index=True, key=f"vat-{detail['id']}", column_config=LINE_LABELS,
        )  # fmt: skip
        saved = st.form_submit_button("Save and check again", icon=":material/save:")
    if saved:
        corrected = {
            **data,
            "supplier": {**data["supplier"], "name": supplier},
            "invoice_number": number or None,
            "issue_date": issue.isoformat() if issue else None,
            "net_total": net or None,
            "vat_total": vat or None,
            "gross_total": gross,
            "prices_include_vat": gross_prices,
            "lines": [_clean_row(row) for row in lines.to_dict("records") if row.get("description")],
            "vat_breakdown": [
                _clean_row(row) for row in vat_rows.to_dict("records") if row.get("vat_rate") is not None
            ],
        }
        if _safe(lambda: api().put(f"/invoices/{detail['id']}", {"data": corrected, "by": reviewer()})):
            st.toast("Saved and checked again", icon=":material/save:")
            st.rerun()

    with st.container(horizontal=True, gap="small"):
        approve = st.button("Approve", type="primary", icon=":material/check:", key=f"approve-{detail['id']}")
        with st.popover("Reject", icon=":material/close:"):
            reason = st.text_input("Why is it rejected?", key=f"reason-{detail['id']}")
            clicked = st.button("Reject invoice", key=f"reject-{detail['id']}", disabled=not reason)
    if approve and _safe(lambda: api().post(f"/invoices/{detail['id']}/approve", {"by": reviewer()})):
        st.toast("Approved. It now counts in prices and costing.", icon=":material/check_circle:")
        st.rerun()
    if clicked and _safe(lambda: api().post(f"/invoices/{detail['id']}/reject", {"by": reviewer(), "reason": reason})):
        st.toast("Rejected", icon=":material/close:")
        st.rerun()


def _clean_row(row: dict) -> dict:
    """Editor values to JSON: empty cells become null, numbers become strings (exact decimals)."""
    cleaned = {}
    for key, value in row.items():
        if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
            cleaned[key] = None
        elif key == "line_no":
            cleaned[key] = int(value)
        elif isinstance(value, float | int) and not isinstance(value, bool):
            cleaned[key] = str(value)
        else:
            cleaned[key] = value
    return {k: v for k, v in cleaned.items() if v is not None or k in ("seller_article_no", "unit_code", "vat_rate")}


# --- matching ----------------------------------------------------------------------------------------


def matching() -> None:
    page_header("Ingredients", "Link invoice lines to your ingredients. Confirm a supplier's product once and it "
                "is matched automatically from then on.")  # fmt: skip
    with st.container(horizontal=True, gap="medium", vertical_alignment="center"):
        match = st.button("Match new lines", type="primary", icon=":material/join:")
        use_embeddings = st.toggle("Use embeddings (Ollama)", value=True)
    if match:
        matched = _safe(lambda: api().post(f"/matching?embeddings={str(use_embeddings).lower()}"), [])
        st.toast(f"{len(matched)} line(s) matched", icon=":material/join:")
    ingredients = _safe(lambda: api().get("/ingredients"), [])
    keys = [i["key"] for i in ingredients]
    lines = _safe(lambda: api().get("/lines", status="suggested"), []) + _safe(
        lambda: api().get("/lines", status="no_match"), []
    )
    if not lines:
        st.success("Nothing to confirm. Every line has its ingredient.", icon=":material/task_alt:")
        return
    st.caption("Tick the lines that are right, change the ingredient where it is wrong, then confirm.")
    frame = pd.DataFrame(
        [{"confirm": False, "line": line["id"], "supplier": line["supplier"], "description": line["description"],
          "ingredient": line["ingredient_key"], "score": line["match_score"], "method": line["match_method"],
          "amount": _num(line["base_quantity"]), "unit": line["base_unit"], "per unit (optional)": None}
         for line in lines]
    )  # fmt: skip
    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        disabled=["line", "supplier", "description", "score", "method", "amount", "unit"],
        column_config={
            "confirm": st.column_config.CheckboxColumn("✓"),
            "ingredient": st.column_config.SelectboxColumn("Ingredient", options=keys),
            "score": st.column_config.ProgressColumn("Match", format="%.2f", min_value=0, max_value=1),
            "per unit (optional)": st.column_config.NumberColumn(help="Amount of the ingredient in one invoice unit"),
        },
        key="matching-editor",
    )
    chosen = edited[edited["confirm"]]
    if st.button(f"Confirm {len(chosen)} line(s)", disabled=chosen.empty, icon=":material/done_all:"):
        done: set[int] = set()
        for row in chosen.to_dict("records"):
            if row["line"] in done or not row["ingredient"]:
                continue
            per_unit = row["per unit (optional)"]
            body = {"ingredient_key": row["ingredient"], "by": reviewer(),
                    "per_unit": None if per_unit is None or pd.isna(per_unit) else str(per_unit)}  # fmt: skip
            result = _safe(lambda body=body, row=row: api().post(f"/lines/{row['line']}/confirm", body))
            if result:
                done.update(result["confirmed_line_ids"])
        st.toast(f"{len(done)} line(s) confirmed", icon=":material/done_all:")
        st.rerun()


# --- menu --------------------------------------------------------------------------------------------


def menu() -> None:
    page_header("Menu costing", "What every dish costs to make, its share of the net menu price, and the margin "
                "left. Prices come from approved invoices.")  # fmt: skip
    with st.container(horizontal=True, gap="medium", vertical_alignment="bottom"):
        labels = {"latest": "Latest purchase", "average": "90-day average"}
        pricing = st.segmented_control("Prices", list(labels), default="latest", format_func=labels.get)
        until = st.date_input("As of", value=None, help="Prices as they were on this day", width=200)
    params = {"pricing": pricing or "latest", "until": until.isoformat() if until else None}
    dishes = _safe(lambda: api().get("/menu", **params), [])
    _menu_table(dishes)
    if not dishes:
        return

    st.subheader("One dish, item by item")
    names = {d["key"]: d["name"] for d in dishes}
    key = st.selectbox("Dish", list(names), format_func=names.get, label_visibility="collapsed")
    detail = _safe(lambda: api().get(f"/menu/{key}", **params))
    if detail is None:
        return
    pct = f"{float(detail['food_cost_pct']):.1f} %" if detail["food_cost_pct"] is not None else "–"
    cols = st.columns(4)
    cols[0].metric("Cost", _eur(detail["cost"]), border=True)
    cols[1].metric("Net price", _eur(detail["net_price"]), border=True)
    cols[2].metric("Food cost", pct, border=True)
    cols[3].metric("Margin", _eur(detail["margin"]), border=True)
    rows = [{"Item": i["label"], "Amount": f"{float(i['quantity']):g} {i['base_unit']}",
             "Waste %": _num(i["waste_pct"]), "Cost": _num(i["cost"])} for i in detail["items"]]  # fmt: skip
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Cost": st.column_config.NumberColumn(format="%.2f €")})  # fmt: skip
