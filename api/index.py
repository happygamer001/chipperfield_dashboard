"""
Single Python entrypoint for Vercel.

As of mid-2026, Vercel's Python runtime expects one entrypoint app (Flask,
FastAPI, Django, etc.) rather than treating every file in /api as its own
function. This file is that entrypoint — it's a small Flask app with routes
for each feature:

  GET  /api/recent-logs         -> last N days of Daily Job Logs from Dropbox
  GET/POST /api/typeform-webhook -> real-time Typeform submission receiver
  GET  /api/upload-daily-logs   -> legacy/backup: manually re-run the Gmail scan
  POST /api/publish             -> admin publishes approved items to Calvin's dashboard
  GET  /api/published           -> Calvin's dashboard reads the latest published state
  POST /api/add-note            -> notes and flags from either side, appended in place

See vercel.json for the rewrite rule that routes /api/* here.
"""

import os
import sys
import re
import json
import io
import hmac
import hashlib
import base64
import datetime
from functools import wraps

import requests
import dropbox
import openpyxl
from flask import Flask, request, jsonify, session, send_from_directory

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from dropbox_pdf_utils import (  # noqa: E402
    get_dropbox_client,
    format_date_for_jacque,
    upload_pdf_and_photos,
    get_recent_daily_logs,
    DAYS_TO_SHOW_DEFAULT,
)
from daily_log_uploader import run_gmail_djl_uploader  # noqa: E402
import notion_utils  # noqa: E402

app = Flask(__name__)
# Falls back to a fixed dev key so the app doesn't crash if this isn't set yet —
# but sessions are only truly secure once FLASK_SECRET_KEY is set in Vercel.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-insecure-key-change-me-in-vercel")
app.permanent_session_lifetime = datetime.timedelta(days=14)

# Repo root — where index.html and dashboard.html live, one level up from /api.
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


# ==========================================
# Static pages — served directly by Flask, since Vercel's current Python
# runtime routes all traffic through this app rather than serving these
# as separate static files.
# ==========================================

@app.route("/")
def root_page():
    return send_from_directory(_REPO_ROOT, "index.html")


@app.route("/index.html")
def admin_page():
    return send_from_directory(_REPO_ROOT, "index.html")


@app.route("/dashboard.html")
def dashboard_page():
    return send_from_directory(_REPO_ROOT, "dashboard.html")


DASHBOARD_STATE_PATH = os.environ.get(
    "DASHBOARD_STATE_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/published.json"
)
FABSHOP_REVENUE_PATH = os.environ.get(
    "FABSHOP_REVENUE_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/fabshop_revenue.json"
)

NOTION_BATCH_REPORTS_DB_ID = os.environ.get(
    "NOTION_BATCH_REPORTS_DB_ID", "348304a3-86dc-43df-b7b6-e7316b65f1e5"
)
NOTION_GRAVEL_SALES_DB_ID = os.environ.get(
    "NOTION_GRAVEL_SALES_DB_ID", "82d0c995-bc76-4f0f-927a-2e38ab6c564c"
)
NOTION_PURCHASE_ORDERS_DB_ID = os.environ.get(
    "NOTION_PURCHASE_ORDERS_DB_ID", "d9d315d7-9c68-41a2-a3ae-c752a0876ae1"
)
# Work Orders lives inside a multi-source database. This is the ID of the
# specific "Service Request" data source within it (see notion_utils.query_data_source).
NOTION_WORKORDERS_DATASOURCE_ID = os.environ.get(
    "NOTION_WORKORDERS_DATASOURCE_ID", "52e9dbff-b6db-4374-a77b-31b86c8ce5eb"
)


# ==========================================
# AUTH — two accounts: admin (you) and calvin
# ==========================================

def require_role(*allowed_roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            role = session.get("role")
            if role not in allowed_roles:
                return jsonify({"status": "error", "message": "Not logged in or not authorized"}), 401
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.route("/api/login", methods=["POST"])
def login():
    """
    No credentials — just a role picker. Body: { role: "admin" | "calvin" }.
    This is intentionally not real authentication: anyone with the link can
    pick either role. Fine for now since these URLs aren't shared publicly,
    but worth adding real credentials later if that changes.
    """
    body = request.get_json(force=True) or {}
    role = body.get("role")

    if role not in ("admin", "calvin"):
        return jsonify({"status": "error", "message": "Invalid role"}), 400

    session.permanent = True
    session["role"] = role
    return jsonify({"status": "ok", "role": role})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/session", methods=["GET"])
def get_session():
    return jsonify({"logged_in": "role" in session, "role": session.get("role")})


# ==========================================
# /api/recent-logs
# ==========================================

@app.route("/api/recent-logs", methods=["GET"])
@require_role("admin")
def recent_logs():
    try:
        days = int(request.args.get("days", DAYS_TO_SHOW_DEFAULT))
        logs = get_recent_daily_logs(days=days)
        return jsonify({"days": days, "count": len(logs), "logs": logs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# /api/typeform-webhook
# ==========================================

def verify_signature(raw_body, signature_header):
    secret = os.environ.get("TYPEFORM_WEBHOOK_SECRET")
    if not secret:
        return True  # no secret configured — should be set in production
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    provided = signature_header.split("sha256=", 1)[1]
    return hmac.compare_digest(expected, provided)


def find_answer(answers, keywords):
    for ans in answers:
        title = (ans.get("field", {}).get("title") or "").lower()
        if any(kw in title for kw in keywords):
            return ans
    return None


def answer_text(ans):
    if ans is None:
        return None
    t = ans.get("type")
    if t in ("text", "choice"):
        return ans.get("text") or (ans.get("choice") or {}).get("label")
    if t == "number":
        return str(ans.get("number"))
    if t == "date":
        return ans.get("date")
    return None


def download_file(url):
    token = os.environ.get("TYPEFORM_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers, timeout=20)
    return resp.content if resp.status_code == 200 else None


def process_submission(payload):
    form_response = payload.get("form_response", {})
    answers = form_response.get("answers", [])
    submitted_at = form_response.get("submitted_at", "")

    # --- FIELD MATCHING (adjust keywords here if your form's titles differ) ---
    name_ans = find_answer(answers, ["name"])
    job_ans = find_answer(answers, ["job", "work order"])
    date_ans = find_answer(answers, ["date"])

    log_name = (answer_text(name_ans) or "Unknown").strip()

    raw_job = answer_text(job_ans) or ""
    job_match = re.search(r'\bJ\s*(\d{3,5})\b', raw_job, re.IGNORECASE)
    wo_match = re.search(r'\b(WO|S)\s*(\d{3,5})\b', raw_job, re.IGNORECASE)
    if job_match:
        job_wo = f"J{job_match.group(1)}"
    elif wo_match:
        job_wo = f"{wo_match.group(1).upper()}{wo_match.group(2)}"
    else:
        job_wo = raw_job.strip() or ""

    raw_date = answer_text(date_ans) or submitted_at[:10] or ""
    log_date = format_date_for_jacque(raw_date)

    body_lines = []
    photo_bytes_list = []
    for ans in answers:
        field_title = ans.get("field", {}).get("title", "Field")
        if ans.get("type") == "file_url":
            file_bytes = download_file(ans.get("file_url"))
            if file_bytes:
                photo_bytes_list.append(file_bytes)
            continue
        text_val = answer_text(ans)
        if text_val:
            body_lines.append(f"{field_title}: {text_val}")

    pdf_text = "\n".join(body_lines)
    pdf_title_parts = [log_date, log_name]
    if job_wo:
        pdf_title_parts.append(job_wo)
    pdf_title = " | ".join(pdf_title_parts)

    dbx = get_dropbox_client()
    summary = upload_pdf_and_photos(
        dbx, log_date, log_name, job_wo, pdf_text, pdf_title, photo_bytes_list
    )
    summary["log_name"] = log_name
    summary["job_wo"] = job_wo
    summary["log_date"] = log_date
    return summary


@app.route("/api/typeform-webhook", methods=["GET", "POST"])
def typeform_webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "message": "Typeform webhook endpoint is live. POST only."})

    raw_body = request.get_data()
    signature = request.headers.get("Typeform-Signature")
    if not verify_signature(raw_body, signature):
        return jsonify({"status": "error", "message": "Invalid signature"}), 401

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        summary = process_submission(payload)
        return jsonify({"status": "ok", "summary": summary})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# /api/upload-daily-logs (legacy/backup manual trigger)
# ==========================================

@app.route("/api/upload-daily-logs", methods=["GET"])
@require_role("admin")
def upload_daily_logs():
    try:
        summary = run_gmail_djl_uploader()
        return jsonify({"status": "ok", "summary": summary})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Dashboard storage (Dropbox-backed — no separate database needed)
# ==========================================

def _read_published_state(dbx):
    try:
        _, res = dbx.files_download(DASHBOARD_STATE_PATH)
        return json.loads(res.content)
    except dropbox.exceptions.ApiError:
        return {"published_at": None, "items": []}


def _write_published_state(dbx, state):
    dbx.files_upload(
        json.dumps(state, indent=2).encode("utf-8"),
        DASHBOARD_STATE_PATH,
        mode=dropbox.files.WriteMode.overwrite,
    )


@app.route("/api/publish", methods=["POST"])
@require_role("admin")
def publish():
    try:
        body = request.get_json(force=True)
        new_items = body.get("items", [])

        dbx = get_dropbox_client()
        state = _read_published_state(dbx)

        existing_by_id = {i["id"]: i for i in state.get("items", [])}
        for item in new_items:
            item.setdefault("notes", [])
            existing_by_id[item["id"]] = item  # replace or add

        state = {
            "published_at": datetime.datetime.utcnow().isoformat() + "Z",
            "items": list(existing_by_id.values()),
        }
        _write_published_state(dbx, state)
        return jsonify({"status": "ok", "published_at": state["published_at"], "count": len(new_items)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/published", methods=["GET"])
@require_role("admin", "calvin")
def published():
    try:
        dbx = get_dropbox_client()
        state = _read_published_state(dbx)
        return jsonify(state)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/add-note", methods=["POST"])
@require_role("admin", "calvin")
def add_note():
    """
    Body: { item_id, text, type ("note" or "flag"), category (optional, for flags) }
    Author is taken from the logged-in session, not the request body, so a
    note can't be spoofed as coming from the other person.
    Appends to the item's notes list and re-saves. Used by both Calvin's
    dashboard (notes + flags) and the admin page (replies to Calvin's notes).
    """
    try:
        body = request.get_json(force=True)
        item_id = body.get("item_id")
        if not item_id:
            return jsonify({"status": "error", "message": "item_id is required"}), 400

        dbx = get_dropbox_client()
        state = _read_published_state(dbx)

        target = next((i for i in state.get("items", []) if i["id"] == item_id), None)
        if not target:
            return jsonify({"status": "error", "message": "item not found in published state"}), 404

        target.setdefault("notes", []).append({
            "author": session.get("role"),
            "text": body.get("text", ""),
            "type": body.get("type", "note"),
            "category": body.get("category"),
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        })

        _write_published_state(dbx, state)
        return jsonify({"status": "ok", "item": target})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Notion sources — MCI Batch Reports & Gravel Sales
# ==========================================

@app.route("/api/notion/batch-reports", methods=["GET"])
@require_role("admin")
def notion_batch_reports():
    try:
        pages = notion_utils.query_database(NOTION_BATCH_REPORTS_DB_ID)
        items = []
        for page in pages:
            props = page.get("properties", {})
            name = notion_utils.prop_text(props, "Name") or "Batch report"
            report_date = notion_utils.prop_date(props, "Report Date") or notion_utils.prop_date(props, "Date")
            issues = notion_utils.prop_text(props, "Issues Presented")
            yards = notion_utils.prop_number(props, "Total Yards Out")
            trips = notion_utils.prop_number(props, "Trips Out")
            submitted_by = notion_utils.prop_text(props, "Submitted By")

            has_issue = bool(issues)
            items.append({
                "id": "notion-batch-" + page["id"],
                "type": "general",
                "source": "notion-batch-reports",
                "sourceLabel": "Notion · Daily Batch Reports",
                "job": None,
                "title": name,
                "subtitle": f"{report_date or 'No date'} · {yards or 0} yds · {trips or 0} trips" + (f" · by {submitted_by}" if submitted_by else ""),
                "tagClass": "warn" if has_issue else "ok",
                "tagText": "Issue reported" if has_issue else "On track",
                "summary": issues or "",
                "status": "pending",
            })
        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/notion/gravel-sales", methods=["GET"])
@require_role("admin")
def notion_gravel_sales():
    try:
        pages = notion_utils.query_database(NOTION_GRAVEL_SALES_DB_ID)
        items = []
        for page in pages:
            props = page.get("properties", {})
            sale_id = notion_utils.prop_text(props, "Sale ID") or "Sale"
            customer = notion_utils.prop_text(props, "Customer Name")
            material = notion_utils.prop_select(props, "Material")
            qty = notion_utils.prop_number(props, "Quantity (Tons)")
            total = notion_utils.prop_number(props, "Total Amount")
            status = notion_utils.prop_select(props, "Status")
            invoiced = notion_utils.prop_checkbox(props, "Invoiced")
            notes = notion_utils.prop_text(props, "Notes")

            incomplete = (status == "Incomplete")
            items.append({
                "id": "notion-gravel-" + page["id"],
                "type": "general",
                "source": "notion-gravel-sales",
                "sourceLabel": "Notion · Gravel Sales",
                "job": None,
                "customer": customer,
                "title": f"{sale_id}" + (f" — {customer}" if customer else ""),
                "subtitle": f"{material or 'Material?'} · {qty or 0} tons · ${total or 0:,.0f}" + ("" if invoiced else " · not invoiced"),
                "tagClass": "warn" if incomplete else "ok",
                "tagText": status or "Unknown",
                "summary": notes or "",
                "status": "pending",
            })
        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Customer order lookup — searches ALL Gravel Sales history, not just
# what's currently sitting in the review queue. Available to both roles.
# ==========================================

@app.route("/api/notion/customer-search", methods=["GET"])
@require_role("admin", "calvin")
def customer_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Provide a customer name with ?q="}), 400

    try:
        filter_obj = {
            "property": "Customer Name",
            "rich_text": {"contains": query},
        }
        pages = notion_utils.query_database(NOTION_GRAVEL_SALES_DB_ID, page_size=50, filter_obj=filter_obj)

        results = []
        for page in pages:
            props = page.get("properties", {})
            results.append({
                "sale_id": notion_utils.prop_text(props, "Sale ID"),
                "customer": notion_utils.prop_text(props, "Customer Name"),
                "material": notion_utils.prop_select(props, "Material"),
                "quantity_tons": notion_utils.prop_number(props, "Quantity (Tons)"),
                "total_amount": notion_utils.prop_number(props, "Total Amount"),
                "sale_date": notion_utils.prop_date(props, "Sale Date"),
                "status": notion_utils.prop_select(props, "Status"),
                "invoiced": notion_utils.prop_checkbox(props, "Invoiced"),
                "delivery_address": notion_utils.prop_text(props, "Delivery Address"),
                "notes": notion_utils.prop_text(props, "Notes"),
            })

        results.sort(key=lambda r: r.get("sale_date") or "", reverse=True)
        return jsonify({"count": len(results), "results": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Work order lookup — search by Reporting Contact.
# NOTE: Work Orders (Service Request) lives inside a Notion multi-source
# database, which needs the newer data-sources API endpoint. This has not
# been tested against the live API from this environment — if it errors,
# check the response message and we can adjust the endpoint/version.
# ==========================================

@app.route("/api/notion/workorder-search", methods=["GET"])
@require_role("admin", "calvin")
def workorder_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Provide a contact/customer name with ?q="}), 400

    try:
        filter_obj = {
            "property": "Reporting Contact",
            "rich_text": {"contains": query},
        }
        pages = notion_utils.query_data_source(NOTION_WORKORDERS_DATASOURCE_ID, page_size=50, filter_obj=filter_obj)

        results = []
        for page in pages:
            props = page.get("properties", {})
            results.append({
                "wo_number": notion_utils.prop_number(props, "WO #"),
                "job_number": notion_utils.prop_number(props, "Job #"),
                "reporting_contact": notion_utils.prop_text(props, "Reporting Contact"),
                "work_status": notion_utils.prop_select(props, "Work Status"),
                "service_priority": notion_utils.prop_select(props, "Service Priority"),
                "date": notion_utils.prop_date(props, "Date"),
                "description_of_work": notion_utils.prop_text(props, "Description of Work"),
                "customer_complaint": notion_utils.prop_text(props, "Customer Complaint"),
            })

        results.sort(key=lambda r: r.get("date") or "", reverse=True)
        return jsonify({"count": len(results), "results": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Vendor lookup — search Purchase Orders by vendor, with Ordered->Delivered
# lead time computed per order and a year-over-year trend summary, so Calvin
# can see whether deliveries are taking longer than in previous years.
#
# NOTE: Vendor is a Notion "select" field, not free text, so the API can't
# do a partial-match filter on it server-side. This pulls the full PO
# history (paginated) and matches vendor names client-side instead —
# works well at this database's size, but would need a real query filter
# if the PO list grows very large.
# ==========================================

def _parse_date(d):
    if not d:
        return None
    try:
        return datetime.datetime.fromisoformat(d.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@app.route("/api/notion/vendor-search", methods=["GET"])
@require_role("admin", "calvin")
def vendor_search():
    query = (request.args.get("q") or "").strip().lower()
    if not query:
        return jsonify({"status": "error", "message": "Provide a vendor name with ?q="}), 400

    try:
        all_pages = notion_utils.query_database_all(NOTION_PURCHASE_ORDERS_DB_ID)

        results = []
        for page in all_pages:
            props = page.get("properties", {})
            vendor = notion_utils.prop_select(props, "Vendor") or ""
            if query not in vendor.lower():
                continue

            ordered = notion_utils.prop_date(props, "Ordered")
            delivered = notion_utils.prop_date(props, "Delivered")

            lead_time_days = None
            ordered_d = _parse_date(ordered)
            delivered_d = _parse_date(delivered)
            if ordered_d and delivered_d:
                lead_time_days = (delivered_d - ordered_d).days

            results.append({
                "po_number": notion_utils.prop_text(props, "PO #"),
                "job_number": notion_utils.prop_number(props, "Job #"),
                "vendor": vendor,
                "amount": notion_utils.prop_number(props, "Amount"),
                "status": notion_utils.prop_status(props, "Status"),
                "ordered": ordered,
                "scheduled": notion_utils.prop_date(props, "Scheduled"),
                "delivered": delivered,
                "lead_time_days": lead_time_days,
                "invoiced": notion_utils.prop_checkbox(props, "Invoiced"),
                "description": notion_utils.prop_text(props, "Description"),
            })

        results.sort(key=lambda r: r.get("ordered") or "", reverse=True)

        # Year-over-year average lead time, for spotting a "deliveries are
        # taking longer" trend. Only counts orders with both dates present.
        by_year = {}
        for r in results:
            if r["lead_time_days"] is None or not r["ordered"]:
                continue
            year = r["ordered"][:4]
            by_year.setdefault(year, []).append(r["lead_time_days"])

        yearly_trend = [
            {"year": year, "avg_lead_time_days": round(sum(days) / len(days), 1), "count": len(days)}
            for year, days in sorted(by_year.items())
        ]

        return jsonify({"count": len(results), "results": results, "yearly_trend": yearly_trend})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Fab Shop revenue — weekly file upload, parsed and stored in Dropbox.
# No live Microsoft Graph connection; you upload the workbook manually
# (admin only), the server reads it, Calvin's page just displays the result.
# ==========================================

def _find_sheet(wb, target_name):
    """Matches sheet names loosely (trailing spaces, case) since Excel tab
    names are easy to fat-finger and this shouldn't break on that."""
    target = target_name.strip().lower()
    for name in wb.sheetnames:
        if name.strip().lower() == target:
            return wb[name]
    return None


def _cell_date(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.date().isoformat() if hasattr(value, "date") else value.isoformat()
    return str(value)


def parse_fabshop_workbook(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)

    weekly = []
    weekly_sheet = _find_sheet(wb, "Weekly Labor Value")
    if weekly_sheet:
        for row in weekly_sheet.iter_rows(min_row=2, max_col=6, values_only=True):
            week_of = row[0]
            if not week_of:
                continue
            weekly.append({
                "week_of": str(week_of),
                "billable_hours": row[1] or 0,
                "non_billable_hours": row[2] or 0,
                "mgt_design_hours": row[3] or 0,
                "total_revenue": row[4] or 0,
                "total_man_hours": row[5] or 0,
            })

    daily = []
    daily_sheet = _find_sheet(wb, "Labor Value - Daily")
    if daily_sheet:
        for row in daily_sheet.iter_rows(min_row=2, max_col=6, values_only=True):
            date_val = row[0]
            if not date_val:
                continue
            daily.append({
                "date": _cell_date(date_val),
                "billable_hours": row[1] or 0,
                "non_billable_hours": row[2] or 0,
                "mgt_design_hours": row[3] or 0,
                "total_man_hours": row[4] or 0,
                "estimated_value": row[5] or 0,
            })

    return {"weekly": weekly, "daily": daily, "sheets_found": wb.sheetnames}


@app.route("/api/upload-fabshop", methods=["POST"])
@require_role("admin")
def upload_fabshop():
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file included in upload"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400

    try:
        file_bytes = file.read()
        parsed = parse_fabshop_workbook(file_bytes)

        if not parsed["weekly"] and not parsed["daily"]:
            return jsonify({
                "status": "error",
                "message": f"Couldn't find 'Weekly Labor Value' or 'Labor Value - Daily' sheets. "
                           f"Sheets found in file: {', '.join(parsed['sheets_found'])}"
            }), 400

        payload = {
            "uploaded_at": datetime.datetime.utcnow().isoformat() + "Z",
            "filename": file.filename,
            "weekly": parsed["weekly"],
            "daily": parsed["daily"],
        }

        dbx = get_dropbox_client()
        dbx.files_upload(
            json.dumps(payload, indent=2).encode("utf-8"),
            FABSHOP_REVENUE_PATH,
            mode=dropbox.files.WriteMode.overwrite,
        )

        return jsonify({
            "status": "ok",
            "weekly_rows": len(parsed["weekly"]),
            "daily_rows": len(parsed["daily"]),
            "uploaded_at": payload["uploaded_at"],
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-revenue", methods=["GET"])
@require_role("admin", "calvin")
def fabshop_revenue():
    try:
        dbx = get_dropbox_client()
        try:
            _, res = dbx.files_download(FABSHOP_REVENUE_PATH)
            data = json.loads(res.content)
        except dropbox.exceptions.ApiError:
            data = {"uploaded_at": None, "filename": None, "weekly": [], "daily": []}
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
