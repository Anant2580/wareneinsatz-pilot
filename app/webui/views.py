"""The pages of the web app. Each is a function; streamlit_app.py puts them into the navigation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from webui.client import ApiError, api

OUTCOME_LABELS = {
    "approved": "approved",
    "needs_review": "waits for review",
    "needs_ocr": "waits for OCR",
    "duplicate": "duplicate",
    "failed": "failed",
}


def _safe(call: Callable[[], Any], default: Any = None) -> Any:
    """Run an API call; show its error instead of a stack trace."""
    try:
        return call()
    except ApiError as error:
        st.error(error.detail)
        return default


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _eur(value: Any) -> str:
    return "–" if value is None else f"{float(value):,.2f} €"


def reviewer() -> str:
    return st.session_state.get("reviewer") or "reviewer"


# --- dashboard ----------------------------------------------------------------------------------------


def dashboard() -> None:
    st.title("Dashboard")
    summary = _safe(lambda: api().get("/summary"))
    if summary is None:
        return
    this, last = _num(summary["wareneinsatz_this_month"]), _num(summary["wareneinsatz_last_month"])
    month = summary["latest_month"][:7] if summary["latest_month"] else "–"
    cols = st.columns(4)
    cols[0].metric(f"Wareneinsatz {month}", _eur(this),
                   delta=f"{(this - last) / last:+.1%} vs. month before" if this and last else None,
                   delta_color="inverse", border=True)  # fmt: skip
    cols[1].metric("Invoices to review", summary["invoices_waiting_review"], border=True)
    cols[2].metric("Open alerts", summary["open_alerts"], border=True)
    cols[3].metric("Dishes over target", summary["dishes_over_target"], border=True)
    if summary["documents_waiting_ocr"] or summary["suggestions_open"]:
        st.info(
            f"{summary['documents_waiting_ocr']} scan(s)/photo(s) wait for OCR · "
            f"{summary['suggestions_open']} ingredient suggestion(s) wait for confirmation"
        )

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Food purchases per month")
        months = _safe(lambda: api().get("/purchases/monthly", months=12), [])
        if months:
            rows = [{"month": m["month"][:7], **{k: float(v) for k, v in m["by_category"].items()}} for m in months]
            frame = pd.DataFrame(rows).fillna(0)
            st.bar_chart(
                frame, x="month", y=[c for c in frame.columns if c != "month"], y_label="EUR (net)", stack=True
            )
        else:
            st.caption("No approved purchases yet.")
    with right:
        _alerts_panel()

    st.subheader("Menu: food cost per dish")
    _menu_table(_safe(lambda: api().get("/menu"), []))

    st.subheader("Price history")
    prices = _safe(lambda: api().get("/prices"), [])
    if prices:
        names = sorted({(p["ingredient_name"], p["ingredient_key"]) for p in prices})
        default = next((i for i, (_, key) in enumerate(names) if key == "patty-rind-180"), 0)
        name, key = st.selectbox("Ingredient", names, index=default, format_func=lambda item: item[0])
        points = [p for p in prices if p["ingredient_key"] == key]
        frame = pd.DataFrame(
            {"date": pd.to_datetime([p["issue_date"] for p in points]), "supplier": [p["supplier"] for p in points],
             "price": [float(p["price"]) for p in points]}
        )  # fmt: skip
        st.line_chart(frame, x="date", y="price", color="supplier", y_label=f"EUR per {points[0]['display_unit']}")
    else:
        st.caption("No prices yet: approve invoices and confirm their ingredients.")


def _alerts_panel() -> None:
    st.subheader("Alerts")
    if st.button("Check for new alerts"):
        result = _safe(lambda: api().post("/alerts/check"))
        if result:
            st.toast(f"{result['new']} new alert(s)")
    alerts = _safe(lambda: api().get("/alerts"), [])
    if not alerts:
        st.caption("No open alerts.")
    icons = {"price_jump": "📈", "cheaper_supplier": "💡", "over_target": "⚠️"}

    def show(alert: dict) -> None:
        text_col, button_col = st.columns([5, 1])
        text_col.markdown(f"{icons.get(alert['kind'], '•')} **{alert['happened_on']}** · {alert['message']}")
        if button_col.button("✓", key=f"ack-{alert['id']}", help="Mark as seen"):
            _safe(lambda: api().post(f"/alerts/{alert['id']}/ack", {"by": reviewer()}))
            st.rerun()

    for alert in alerts[:4]:
        show(alert)
    if len(alerts) > 4:
        with st.expander(f"{len(alerts) - 4} more"):
            for alert in alerts[4:]:
                show(alert)


def _menu_table(dishes: list[dict]) -> None:
    if not dishes:
        st.caption("No recipes yet: uv run wareneinsatz recipes load")
        return
    rows = []
    for d in dishes:
        status = (
            "over target"
            if d["over_target"]
            else ("no price yet: " + ", ".join(d["missing"]) if d["missing"] else "ok")
        )
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
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            "Menu price": money, "Net price": money, "Cost": money, "Margin": money,
            "Food cost %": st.column_config.ProgressColumn(format="%.1f %%", min_value=0, max_value=50),
            "Target %": st.column_config.NumberColumn(format="%.0f %%"),
        },
    )  # fmt: skip


# --- upload ------------------------------------------------------------------------------------------


def upload() -> None:
    st.title("Upload invoices")
    st.caption("XRechnung / ZUGFeRD files are read exactly. PDFs without XML and photos wait for OCR.")
    files = st.file_uploader(
        "Supplier invoices and receipts",
        type=["xml", "pdf", "jpg", "jpeg", "png", "heic", "webp", "tif", "tiff"],
        accept_multiple_files=True,
    )
    if files and st.button(f"Upload {len(files)} file(s)", type="primary"):
        payload = [("files", (f.name, f.getvalue(), f.type or "application/octet-stream")) for f in files]
        results = _safe(lambda: api().post("/documents", files=payload), [])
        for result in results:
            label = OUTCOME_LABELS.get(result["outcome"], result["outcome"])
            text = f"**{result['filename']}**: {label} · {result['message']}"
            if result["outcome"] == "failed":
                st.error(text)
            elif result["outcome"] == "duplicate":
                st.info(text)  # not an error: the file is already in, nothing was added twice
            elif result["outcome"] == "approved":
                st.success(text)
            else:
                st.warning(text)
            for issue in result["issues"]:
                st.caption(f"{issue['severity']}: {issue['message']}")

    st.divider()
    _extraction_panel()


@st.fragment(run_every=5)
def _extraction_panel() -> None:
    """Reads waiting scans and photos with OCR + the local language model; refreshes itself while it runs."""
    status = _safe(lambda: api().get("/extraction"))
    if status is None:
        return
    st.subheader("Scans and photos")
    if status["running"]:
        st.progress(status["done"] / status["total"] if status["total"] else 0.0,
                    text=f"Reading {status['done']} of {status['total']} …")  # fmt: skip
    else:
        st.write(f"{status['waiting']} document(s) wait for OCR.")
        if status["waiting"] and st.button("Read them now (OCR + language model)"):
            _safe(lambda: api().post("/extraction"))
            st.rerun()
    if status["error"]:
        st.error(status["error"])
    elif status["finished_at"] and status["outcomes"]:
        done = ", ".join(f"{n} {OUTCOME_LABELS.get(k, k)}" for k, n in status["outcomes"].items())
        if status["outcomes"].get("failed"):
            st.warning(f"Last run: {done}. The reasons are listed under 'Failed documents'.")
        else:
            st.success(f"Last run: {done}")
    _failed_documents()


def _failed_documents() -> None:
    """Every document that could not become an invoice, with the reason, so nothing fails silently."""
    failed = _safe(lambda: api().get("/documents", status="failed", limit=50), [])
    if not failed:
        return
    with st.expander(f"Failed documents ({len(failed)})"):
        st.dataframe(
            [
                {
                    "File": d["filename"],
                    "Received": d["received_at"][:16].replace("T", " "),
                    "Read by": d["extraction_method"] or "-",
                    "Reason": d["error"],
                }
                for d in failed
            ],  # fmt: skip
            hide_index=True,
            width="stretch",
        )


# --- review ------------------------------------------------------------------------------------------

LINE_COLUMNS = ["line_no", "description", "seller_article_no", "quantity", "unit_raw", "unit_code", "unit_price",
                "allowance_amount", "line_amount", "vat_rate"]  # fmt: skip


def review() -> None:
    st.title("Review")
    waiting = _safe(lambda: api().get("/invoices", status="needs_review"), [])
    if not waiting:
        st.success("No invoices waiting for review.")
        return
    options = {i["id"]: i for i in waiting}
    chosen = st.selectbox(
        f"{len(waiting)} invoice(s) waiting",
        list(options),
        format_func=lambda i: (
            f"#{i} {options[i]['supplier']} · {options[i]['invoice_number'] or 'no number'} · "
            f"{_eur(options[i]['gross_total'])} · {options[i]['errors']} error(s)"
        ),  # fmt: skip
    )
    detail = _safe(lambda: api().get(f"/invoices/{chosen}"))
    if detail is None:
        return
    doc_col, form_col = st.columns([2, 3])
    with doc_col:
        st.caption(f"{detail['filename']} · read by {detail['extraction_method'] or '-'}")
        preview = _safe_preview(detail["document_id"])
        if preview:
            st.image(preview, width="stretch")
    with form_col:
        for issue in detail["issues"]:
            (st.error if issue["severity"] == "error" else st.warning)(issue["message"])
        _review_form(detail)


def _safe_preview(document_id: int) -> bytes | None:
    try:
        return api().get(f"/documents/{document_id}/preview", width=1000)
    except ApiError as error:
        if error.status == 415:  # XML: show the file itself
            content = api().get(f"/documents/{document_id}/file")
            st.code(content.decode("utf-8", errors="replace")[:4000], language="xml")
        else:
            st.error(error.detail)
        return None


def _review_form(detail: dict) -> None:
    data = detail["data"]
    with st.form(f"review-{detail['id']}"):
        c1, c2, c3 = st.columns(3)
        supplier = c1.text_input("Supplier", data["supplier"]["name"])
        number = c2.text_input("Invoice number", data["invoice_number"] or "")
        issue = c3.date_input("Invoice date", date.fromisoformat(data["issue_date"]) if data["issue_date"] else None)
        c4, c5, c6 = st.columns(3)
        net = c4.text_input("Net total", data["net_total"] or "")
        vat = c5.text_input("VAT total", data["vat_total"] or "")
        gross = c6.text_input("Gross total", data["gross_total"])
        gross_prices = st.checkbox("Prices include VAT (till receipt)", data["prices_include_vat"])
        st.caption("Lines")
        lines = st.data_editor(
            pd.DataFrame([{c: line.get(c) for c in LINE_COLUMNS} for line in data["lines"]], columns=LINE_COLUMNS),
            num_rows="dynamic", hide_index=True, width="stretch", key=f"lines-{detail['id']}",
        )  # fmt: skip
        st.caption("VAT breakdown")
        vat_rows = st.data_editor(
            pd.DataFrame(data["vat_breakdown"], columns=["vat_rate", "taxable_amount", "tax_amount"]),
            num_rows="dynamic", hide_index=True, key=f"vat-{detail['id']}",
        )  # fmt: skip
        saved = st.form_submit_button("Save corrections and check again")
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
            st.toast("Saved and checked again")
            st.rerun()

    a, b = st.columns(2)
    approve = a.button("Approve", type="primary", key=f"approve-{detail['id']}")
    if approve and _safe(lambda: api().post(f"/invoices/{detail['id']}/approve", {"by": reviewer()})):
        st.toast("Approved: it now counts in prices and costing")
        st.rerun()
    with b.popover("Reject"):
        reason = st.text_input("Why?", key=f"reason-{detail['id']}")
        clicked = st.button("Reject invoice", key=f"reject-{detail['id']}", disabled=not reason)
        if clicked and _safe(
            lambda: api().post(f"/invoices/{detail['id']}/reject", {"by": reviewer(), "reason": reason})
        ):
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
    st.title("Ingredients")
    st.caption("Confirm once per supplier product; the memory matches it automatically next time.")
    c1, c2 = st.columns([1, 3])
    use_embeddings = c2.checkbox("Use embeddings (Ollama)", value=True)
    if c1.button("Match new lines", type="primary"):
        matched = _safe(lambda: api().post(f"/matching?embeddings={str(use_embeddings).lower()}"), [])
        st.toast(f"{len(matched)} line(s) matched")
    ingredients = _safe(lambda: api().get("/ingredients"), [])
    keys = [i["key"] for i in ingredients]
    lines = _safe(lambda: api().get("/lines", status="suggested"), []) + _safe(
        lambda: api().get("/lines", status="no_match"), []
    )
    if not lines:
        st.success("Nothing to confirm.")
        return
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
            "score": st.column_config.NumberColumn(format="%.2f"),
            "per unit (optional)": st.column_config.NumberColumn(help="Amount of the ingredient in one invoice unit"),
        },
        key="matching-editor",
    )
    chosen = edited[edited["confirm"]]
    if st.button(f"Confirm {len(chosen)} line(s)", disabled=chosen.empty):
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
        st.toast(f"{len(done)} line(s) confirmed")
        st.rerun()


# --- menu --------------------------------------------------------------------------------------------


def menu() -> None:
    st.title("Menu costing")
    c1, c2 = st.columns(2)
    pricing = c1.segmented_control("Prices", ["latest", "average"], default="latest",
                                   help="latest purchase, or the 90-day average")  # fmt: skip
    until = c2.date_input("As of (optional)", value=None, help="Prices as they were on this day")
    params = {"pricing": pricing or "latest", "until": until.isoformat() if until else None}
    dishes = _safe(lambda: api().get("/menu", **params), [])
    _menu_table(dishes)
    if not dishes:
        return
    names = {d["key"]: d["name"] for d in dishes}
    key = st.selectbox("Dish", list(names), format_func=names.get)
    detail = _safe(lambda: api().get(f"/menu/{key}", **params))
    if detail is None:
        return
    rows = [{"Item": i["label"], "Amount": f"{float(i['quantity']):g} {i['base_unit']}",
             "Waste %": _num(i["waste_pct"]), "Cost": _num(i["cost"])} for i in detail["items"]]  # fmt: skip
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Cost": st.column_config.NumberColumn(format="%.2f €")})  # fmt: skip
    pct = f"{float(detail['food_cost_pct']):.1f} %" if detail["food_cost_pct"] is not None else "–"
    st.markdown(f"**Cost {_eur(detail['cost'])}** · net price {_eur(detail['net_price'])} · food cost {pct} · "
                f"margin {_eur(detail['margin'])}")  # fmt: skip
