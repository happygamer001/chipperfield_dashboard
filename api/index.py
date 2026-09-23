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


@app.route("/fabshop-portal.html")
def fabshop_portal_page():
    return send_from_directory(_REPO_ROOT, "fabshop-portal.html")


DASHBOARD_STATE_PATH = os.environ.get(
    "DASHBOARD_STATE_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/published.json"
)
FABSHOP_REVENUE_PATH = os.environ.get(
    "FABSHOP_REVENUE_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/fabshop_revenue.json"
)
FABSHOP_DAILY_ENTRIES_PATH = os.environ.get(
    "FABSHOP_DAILY_ENTRIES_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/fabshop_daily_entries.json"
)
FABSHOP_EMPLOYEES_PATH = os.environ.get(
    "FABSHOP_EMPLOYEES_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/fabshop_employees.json"
)
FABSHOP_LEADS_PATH = os.environ.get(
    "FABSHOP_LEADS_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/fabshop_leads.json"
)

NOTION_BATCH_REPORTS_DATASOURCE_ID = os.environ.get(
    "NOTION_BATCH_REPORTS_DATASOURCE_ID", "ec5dda0d-7c82-4c4a-a613-fa28c7702c9c"
)
NOTION_GRAVEL_SALES_DATASOURCE_ID = os.environ.get(
    "NOTION_GRAVEL_SALES_DATASOURCE_ID", "80c0157b-05b2-46f8-9c19-b6f63c6e99af"
)
NOTION_PURCHASE_ORDERS_DATASOURCE_ID = os.environ.get(
    "NOTION_PURCHASE_ORDERS_DATASOURCE_ID", "7d417736-3252-48b3-8335-4f534905085f"
)
# "Current Job Analyses" is a Notion PAGE (not a database) — a running list
# of links to per-job SharePoint budget workbooks, organized by week.
NOTION_JOB_ANALYSES_PAGE_ID = os.environ.get(
    "NOTION_JOB_ANALYSES_PAGE_ID", "1994562c-e56f-8092-af5e-d751067576c1"
)
# "Typeform Leads. FEED HERE" — the sales pipeline / CRM database. This is
# also where Kevin's reports land, since he's one of the Sales Reps.
NOTION_SALES_PIPELINE_DATASOURCE_ID = os.environ.get(
    "NOTION_SALES_PIPELINE_DATASOURCE_ID", "df4a2436-3221-4e35-921f-2c78440188e9"
)
# Kevin's "Daily Management Form" — syncs directly into Notion via a
# separate Typeform-to-Notion connector, unrelated to our own script/webhook.
NOTION_KEVIN_FORM_DATASOURCE_ID = os.environ.get(
    "NOTION_KEVIN_FORM_DATASOURCE_ID", "3ab4562c-e56f-8029-b87a-000b05352beb"
)
# Work Orders lives inside a multi-source database. This is the ID of the
# specific "Service Request" data source within it (see notion_utils.query_data_source).
NOTION_WORKORDERS_DATASOURCE_ID = os.environ.get(
    "NOTION_WORKORDERS_DATASOURCE_ID", "52e9dbff-b6db-4374-a77b-31b86c8ce5eb"
)
# Current Jobs is an inline database within a page (not a standalone
# top-level database), so it uses the newer data-sources endpoint like
# Work Orders does.
NOTION_CURRENT_JOBS_DATASOURCE_ID = os.environ.get(
    "NOTION_CURRENT_JOBS_DATASOURCE_ID", "92caa4f5-2f8f-4d5b-9c6f-c1b937685d4f"
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
@require_role("admin", "calvin")
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
        title = (ans.get("field", {}).get("title") or "").lower().replace("*", "")
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


def _classify_field(field_title):
    """
    This form repeats a group of questions per project: project name, what
    was done, how long, next action, then a yes/no "any more projects?"
    check. The exact wording drifts slightly slot to slot (and Typeform
    embeds a {{field:UUID}} reference in some titles), so this matches on
    the stable keyword rather than the exact title.
    """
    t = field_title.lower()
    if "how long" in t:
        return "duration"
    if "next action" in t:
        return "next_action"
    if "did you do" in t:
        return "did"
    if "any more" in t or "any other" in t:
        return "continuation"
    if "project" in t:
        return "project"
    return None


_NO_VALUE_PLACEHOLDERS = {"n/a", "na", "none", "no", "-", "", "0"}


def _clean_or_none(text):
    """Treats placeholder non-answers ('N/A', 'None', '0' for a no/false checkbox, etc.) as empty."""
    t = (text or "").strip()
    return t if t and t.lower() not in _NO_VALUE_PLACEHOLDERS else ""


def _extract_job_number(text):
    """Pulls a J#### or WO#### pattern out of free text. Falls back to a bare
    3-5 digit number (e.g. '21750, schilky plates, shop' -> J21750), since
    that's how some real submissions write it without a letter prefix."""
    if not text:
        return None
    job_match = re.search(r'\bJ\s*(\d{3,5})\b', text, re.IGNORECASE)
    if job_match:
        return f"J{job_match.group(1)}"
    wo_match = re.search(r'\b(WO|S)\s*(\d{3,5})\b', text, re.IGNORECASE)
    if wo_match:
        return f"{wo_match.group(1).upper()}{wo_match.group(2)}"
    bare_match = re.search(r'\b(\d{3,5})\b', text)
    if bare_match:
        return f"J{bare_match.group(1)}"
    return None


def _detect_form_type(answers):
    """
    Multiple Typeform forms point at this one webhook now, so the first
    step is figuring out which one just submitted, based on which fields
    are present. Checked in order from most to least specific.
    """
    titles = [(a.get("field", {}).get("title") or "").lower().replace("*", "") for a in answers]
    joined = " | ".join(titles)

    if "job number" in joined:
        return "daily_job_log"
    if "how long did that take" in joined and "next action" in joined:
        return "management_log"
    if "what did your team work on today" in joined:
        return "fab_shop_log"
    return "unknown"


def _collect_photos(answers):
    photo_bytes_list = []
    for ans in answers:
        if ans.get("type") == "file_url":
            file_bytes = download_file(ans.get("file_url"))
            if file_bytes:
                photo_bytes_list.append(file_bytes)
    return photo_bytes_list


def _parse_management_log(answers, log_name, log_date):
    """The repeating project-slot form (project / did / how long / next action, up to 7x)."""
    projects = []
    current_project = None

    for ans in answers[2:]:
        field_title = ans.get("field", {}).get("title", "")
        if ans.get("type") == "file_url":
            continue

        kind = _classify_field(field_title)
        text_val = answer_text(ans)

        if kind == "project":
            current_project = {"project": text_val or "", "did": "", "duration": "", "next_action": ""}
            projects.append(current_project)
        elif kind == "did" and current_project is not None:
            current_project["did"] = text_val or ""
        elif kind == "duration" and current_project is not None:
            current_project["duration"] = text_val or ""
        elif kind == "next_action" and current_project is not None:
            current_project["next_action"] = text_val or ""

    job_wo = ""
    for p in projects:
        job_wo = _extract_job_number(p["project"])
        if job_wo:
            break
    if not job_wo:
        job_wo = "Daily Management Log"

    body_lines = []
    has_next_action = False
    for p in projects:
        if not (p["project"] or p["did"]):
            continue
        line = f"{p['project'] or 'Project'}: {p['did']}"
        if p["duration"]:
            line += f" ({p['duration']})"
        next_action_clean = _clean_or_none(p["next_action"])
        if next_action_clean:
            line += f" — Next: {next_action_clean}"
            has_next_action = True
        body_lines.append(line)

    return job_wo, "\n".join(body_lines), {"projects_parsed": len(projects), "has_next_action": has_next_action}


def _parse_daily_job_log(answers, log_name, log_date):
    """The real per-job crew log: has an actual Job # field, work order fields, photos, etc."""
    job_ans = find_answer(answers, ["job number"])
    wo_num_ans = find_answer(answers, ["workorder #", "work order #"])
    wo_name_ans = find_answer(answers, ["workorder name"])
    work_desc_ans = find_answer(answers, ["what did your team work on today"])
    productivity_ans = find_answer(answers, ["productivity issues"])
    incident_ans = find_answer(answers, ["incident report", "safety incident"])
    todo_ans = find_answer(answers, ["to do"])
    scope_desc_ans = find_answer(answers, ["describe additional work"])

    raw_job = answer_text(job_ans) or ""
    job_wo = _extract_job_number(raw_job) or _clean_or_none(raw_job)
    if not job_wo:
        raw_wo = answer_text(wo_num_ans) or ""
        job_wo = _extract_job_number(raw_wo) or _clean_or_none(raw_wo)
    wo_name = _clean_or_none(answer_text(wo_name_ans))
    if not job_wo and wo_name:
        job_wo = wo_name

    lines = []
    work_desc = _clean_or_none(answer_text(work_desc_ans))
    if work_desc:
        lines.append(f"Work: {work_desc}")
    productivity = _clean_or_none(answer_text(productivity_ans))
    if productivity:
        lines.append(f"Productivity issues: {productivity}")
    incident = _clean_or_none(answer_text(incident_ans))
    if incident:
        lines.append(f"Incident: {incident}")
    todo = _clean_or_none(answer_text(todo_ans))
    if todo:
        lines.append(f"To do: {todo}")
    scope_desc = _clean_or_none(answer_text(scope_desc_ans))
    if scope_desc:
        lines.append(f"Additional work: {scope_desc}")

    return job_wo, "\n".join(lines), {"has_incident": bool(incident)}


def _parse_fab_shop_log(answers, log_name, log_date):
    """Fab Shop's own simple team log — no job number, shop-wide."""
    work_desc_ans = find_answer(answers, ["what did your team work on today"])
    materials_ans = find_answer(answers, ["materials used"])
    productivity_ans = find_answer(answers, ["productivity issues"])
    incident_ans = find_answer(answers, ["incident report"])
    start_ans = find_answer(answers, ["start time"])
    stop_ans = find_answer(answers, ["stop time"])

    lines = []
    start_time = _clean_or_none(answer_text(start_ans))
    stop_time = _clean_or_none(answer_text(stop_ans))
    if start_time or stop_time:
        lines.append(f"Hours: {start_time or '?'} – {stop_time or '?'}")
    work_desc = _clean_or_none(answer_text(work_desc_ans))
    if work_desc:
        lines.append(f"Work: {work_desc}")
    materials = _clean_or_none(answer_text(materials_ans))
    if materials:
        lines.append(f"Materials used: {materials}")
    productivity = _clean_or_none(answer_text(productivity_ans))
    if productivity:
        lines.append(f"Productivity issues: {productivity}")
    incident = _clean_or_none(answer_text(incident_ans))
    if incident:
        lines.append(f"Incident: {incident}")

    return "Fab Shop Daily Log", "\n".join(lines), {"has_incident": bool(incident)}


def process_submission(payload):
    form_response = payload.get("form_response", {})
    answers = form_response.get("answers", [])
    submitted_at = form_response.get("submitted_at", "")

    if not answers:
        raise ValueError("Submission had no answers")

    name_ans = find_answer(answers, ["your name", "name"])
    date_ans = find_answer(answers, ["date"])
    log_name = _clean_or_none(answer_text(name_ans)) or "Unknown"
    raw_date = answer_text(date_ans) or submitted_at[:10] or ""
    log_date = format_date_for_jacque(raw_date)

    form_type = _detect_form_type(answers)
    if form_type == "management_log":
        job_wo, pdf_text, extra = _parse_management_log(answers, log_name, log_date)
    elif form_type == "daily_job_log":
        job_wo, pdf_text, extra = _parse_daily_job_log(answers, log_name, log_date)
    elif form_type == "fab_shop_log":
        job_wo, pdf_text, extra = _parse_fab_shop_log(answers, log_name, log_date)
    else:
        # Unrecognized form shape — fall back to a generic dump so at least
        # something usable lands in Dropbox instead of silently failing.
        job_wo = ""
        lines = []
        for ans in answers:
            title = ans.get("field", {}).get("title", "Field")
            val = answer_text(ans)
            if val:
                lines.append(f"{title}: {val}")
        pdf_text = "\n".join(lines)
        extra = {"form_type": "unrecognized"}

    photo_bytes_list = _collect_photos(answers)

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
    summary["form_type"] = form_type
    summary.update(extra)
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

def _read_json_from_dropbox(dbx, path, default):
    try:
        _, res = dbx.files_download(path)
        return json.loads(res.content)
    except dropbox.exceptions.ApiError:
        return default


def _write_json_to_dropbox(dbx, path, data):
    dbx.files_upload(
        json.dumps(data, indent=2).encode("utf-8"),
        path,
        mode=dropbox.files.WriteMode.overwrite,
    )


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
@require_role("admin", "calvin")
def notion_batch_reports():
    try:
        pages = notion_utils.query_data_source(NOTION_BATCH_REPORTS_DATASOURCE_ID)
        items = []
        for page in pages:
            props = page.get("properties", {})
            name = notion_utils.prop_text(props, "Name") or "Batch report"
            report_date = notion_utils.prop_date(props, "Report Date") or notion_utils.prop_date(props, "Date")
            issues_raw = notion_utils.prop_text(props, "Issues Presented")
            yards = notion_utils.prop_number(props, "Total Yards Out")
            trips = notion_utils.prop_number(props, "Trips Out")
            submitted_by = notion_utils.prop_text(props, "Submitted By")

            # "N/A", "None", "-", etc. are placeholder values meaning "no issue",
            # not real issue descriptions — treat them as empty.
            issues_clean = (issues_raw or "").strip()
            no_issue_placeholders = {"n/a", "na", "none", "no", "no issues", "-", ""}
            has_issue = issues_clean.lower() not in no_issue_placeholders

            items.append({
                "id": "notion-batch-" + page["id"],
                "notionUrl": page.get("url"),
                "type": "general",
                "source": "notion-batch-reports",
                "sourceLabel": "Notion · Daily Batch Reports",
                "job": None,
                "title": name,
                "subtitle": f"{report_date or 'No date'} · {yards or 0} yds · {trips or 0} trips" + (f" · by {submitted_by}" if submitted_by else ""),
                "tagClass": "warn" if has_issue else "ok",
                "tagText": "Issue reported" if has_issue else "On track",
                "summary": issues_clean if has_issue else "",
                "status": "pending",
            })
        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/notion/gravel-sales", methods=["GET"])
@require_role("admin")
def notion_gravel_sales():
    try:
        pages = notion_utils.query_data_source(NOTION_GRAVEL_SALES_DATASOURCE_ID)
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
                "notionUrl": page.get("url"),
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

    try:
        filter_obj = {
            "property": "Customer Name",
            "rich_text": {"contains": query},
        } if query else None
        pages = notion_utils.query_data_source(NOTION_GRAVEL_SALES_DATASOURCE_ID, page_size=100, filter_obj=filter_obj)

        results = []
        for page in pages:
            props = page.get("properties", {})
            results.append({
                "sale_id": notion_utils.prop_text(props, "Sale ID"),
                "notion_url": page.get("url"),
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

    try:
        filter_obj = {
            "property": "Reporting Contact",
            "rich_text": {"contains": query},
        } if query else None
        pages = notion_utils.query_data_source(NOTION_WORKORDERS_DATASOURCE_ID, page_size=100, filter_obj=filter_obj)

        results = []
        for page in pages:
            props = page.get("properties", {})
            results.append({
                "wo_number": notion_utils.prop_number(props, "WO #"),
                "notion_url": page.get("url"),
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

    try:
        all_pages = notion_utils.query_data_source_all(NOTION_PURCHASE_ORDERS_DATASOURCE_ID)

        results = []
        for page in all_pages:
            props = page.get("properties", {})
            vendor = notion_utils.prop_select(props, "Vendor") or ""
            if query and query not in vendor.lower():
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
                "notion_url": page.get("url"),
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


def _merge_fabshop_data(dbx):
    """
    Option A: the portal is now the live source of truth. This merges the
    old manually-uploaded workbook data (kept as historical backfill) with
    live daily entries submitted through the portal — live entries win for
    any date both sources have, since they're the more current record.
    Weekly rollups are computed fresh from the merged daily list (grouped
    Mon-Fri) rather than trusting the workbook's own week labels, so both
    sources roll up consistently.
    """
    legacy = _read_json_from_dropbox(dbx, FABSHOP_REVENUE_PATH, {"uploaded_at": None, "filename": None, "weekly": [], "daily": []})
    live_entries = _read_json_from_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, [])

    daily_by_date = {}
    for d in legacy.get("daily", []):
        if not d.get("date"):
            continue
        daily_by_date[d["date"]] = {
            "date": d["date"],
            "billable_hours": d.get("billable_hours", 0) or 0,
            "non_billable_hours": d.get("non_billable_hours", 0) or 0,
            "mgt_design_hours": d.get("mgt_design_hours", 0) or 0,
            "total_man_hours": d.get("total_man_hours", 0) or 0,
            "estimated_value": d.get("estimated_value", 0) or 0,
            "description": None,
            "source": "legacy_upload",
        }

    for entry in live_entries:
        totals = entry.get("totals", {})
        billable = totals.get("billable", 0) or 0
        non_billable = totals.get("non_billable", 0) or 0
        mgt_design = totals.get("mgt_design", 0) or 0
        daily_by_date[entry["date"]] = {
            "date": entry["date"],
            "billable_hours": billable,
            "non_billable_hours": non_billable,
            "mgt_design_hours": mgt_design,
            "total_man_hours": billable + non_billable + mgt_design,
            "estimated_value": entry.get("estimated_value", 0) or 0,
            "description": entry.get("description") or None,
            "source": "portal",
        }

    daily_list = sorted(daily_by_date.values(), key=lambda d: d["date"])

    weekly_by_key = {}
    for d in daily_list:
        try:
            dt = datetime.datetime.fromisoformat(d["date"]).date()
        except ValueError:
            continue
        monday = dt - datetime.timedelta(days=dt.weekday())
        friday = monday + datetime.timedelta(days=4)
        key = monday.isoformat()
        w = weekly_by_key.setdefault(key, {
            "week_of": f"{monday.strftime('%m/%d')} - {friday.strftime('%m/%d')}",
            "_sort_key": key,
            "billable_hours": 0, "non_billable_hours": 0, "mgt_design_hours": 0,
            "total_revenue": 0, "total_man_hours": 0,
        })
        w["billable_hours"] += d["billable_hours"]
        w["non_billable_hours"] += d["non_billable_hours"]
        w["mgt_design_hours"] += d["mgt_design_hours"]
        w["total_revenue"] += d["estimated_value"]
        w["total_man_hours"] += d["total_man_hours"]

    weekly_list = sorted(weekly_by_key.values(), key=lambda w: w["_sort_key"])
    for w in weekly_list:
        del w["_sort_key"]

    latest_times = [t for t in [legacy.get("uploaded_at")] + [e.get("submitted_at") for e in live_entries] if t]
    uploaded_at = max(latest_times) if latest_times else None

    return {
        "uploaded_at": uploaded_at,
        "filename": legacy.get("filename"),
        "weekly": weekly_list,
        "daily": daily_list,
    }


@app.route("/api/fabshop-revenue", methods=["GET"])
@require_role("admin", "calvin")
def fabshop_revenue():
    try:
        dbx = get_dropbox_client()
        data = _merge_fabshop_data(dbx)
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Fab Shop portal — no login yet (by explicit request), so these routes are
# open. Revisit adding a lightweight role gate here once real usage starts.
# ==========================================

DEFAULT_FABSHOP_EMPLOYEES = ["Jon", "Jerron", "Josh", "Caleb"]


@app.route("/api/fabshop-portal/employees", methods=["GET"])
def fabshop_portal_get_employees():
    try:
        dbx = get_dropbox_client()
        employees = _read_json_from_dropbox(dbx, FABSHOP_EMPLOYEES_PATH, DEFAULT_FABSHOP_EMPLOYEES)
        return jsonify({"employees": employees})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/employees", methods=["POST"])
def fabshop_portal_add_employee():
    try:
        body = request.get_json(force=True)
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"status": "error", "message": "Name is required"}), 400

        dbx = get_dropbox_client()
        employees = _read_json_from_dropbox(dbx, FABSHOP_EMPLOYEES_PATH, list(DEFAULT_FABSHOP_EMPLOYEES))
        if name not in employees:
            employees.append(name)
            _write_json_to_dropbox(dbx, FABSHOP_EMPLOYEES_PATH, employees)
        return jsonify({"status": "ok", "employees": employees})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/employees", methods=["DELETE"])
def fabshop_portal_remove_employee():
    try:
        body = request.get_json(force=True)
        name = (body.get("name") or "").strip()

        dbx = get_dropbox_client()
        employees = _read_json_from_dropbox(dbx, FABSHOP_EMPLOYEES_PATH, list(DEFAULT_FABSHOP_EMPLOYEES))
        employees = [e for e in employees if e != name]
        _write_json_to_dropbox(dbx, FABSHOP_EMPLOYEES_PATH, employees)
        return jsonify({"status": "ok", "employees": employees})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/daily-tab", methods=["POST"])
def fabshop_portal_submit_daily_tab():
    try:
        body = request.get_json(force=True)
        date = body.get("date")
        if not date:
            return jsonify({"status": "error", "message": "Date is required"}), 400

        employees = body.get("employees", [])
        totals = {"billable": 0, "non_billable": 0, "mgt_design": 0}
        for emp in employees:
            totals["billable"] += float(emp.get("billable") or 0)
            totals["non_billable"] += float(emp.get("non_billable") or 0)
            totals["mgt_design"] += float(emp.get("mgt_design") or 0)

        dbx = get_dropbox_client()
        entries = _read_json_from_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, [])

        # Preserve any existing comments/notes on this date if resubmitting
        existing = next((e for e in entries if e.get("date") == date), None)
        preserved_notes = existing.get("notes", []) if existing else []

        entry = {
            "date": date,
            "employees": employees,
            "totals": totals,
            "description": body.get("description") or "",
            "new_leads": body.get("new_leads") or "",
            "problems": body.get("problems") or "",
            "estimated_value": float(body.get("estimated_value") or 0),
            "completed_by": body.get("completed_by") or "",
            "submitted_at": datetime.datetime.utcnow().isoformat() + "Z",
            "notes": preserved_notes,
        }

        entries = [e for e in entries if e.get("date") != date]  # replace same-day resubmission
        entries.append(entry)
        entries.sort(key=lambda e: e["date"])
        _write_json_to_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, entries)

        return jsonify({"status": "ok", "entry": entry})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/daily-entries", methods=["GET"])
def fabshop_portal_daily_entries():
    try:
        dbx = get_dropbox_client()
        entries = _read_json_from_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, [])
        entries.sort(key=lambda e: e["date"], reverse=True)
        return jsonify({"count": len(entries), "entries": entries})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/daily-entries/<date>/notes", methods=["POST"])
def fabshop_portal_add_daily_entry_note(date):
    """Comment-style updates on a past Daily Tab entry — lets Jerron (or
    anyone) add context to a historical submission without editing it."""
    try:
        body = request.get_json(force=True)
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"status": "error", "message": "Note text is required"}), 400

        dbx = get_dropbox_client()
        entries = _read_json_from_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, [])
        target = next((e for e in entries if e.get("date") == date), None)
        if not target:
            return jsonify({"status": "error", "message": "No entry found for that date"}), 404

        target.setdefault("notes", []).append({
            "author": body.get("author") or "Fab Shop",
            "text": text,
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
        _write_json_to_dropbox(dbx, FABSHOP_DAILY_ENTRIES_PATH, entries)
        return jsonify({"status": "ok", "entry": target})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/leads", methods=["GET"])
def fabshop_portal_get_leads():
    try:
        dbx = get_dropbox_client()
        leads = _read_json_from_dropbox(dbx, FABSHOP_LEADS_PATH, [])
        leads.sort(key=lambda l: l.get("created_at", ""), reverse=True)
        return jsonify({"count": len(leads), "leads": leads})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/leads", methods=["POST"])
def fabshop_portal_add_lead():
    try:
        body = request.get_json(force=True)
        lead = {
            "id": "lead-" + str(int(datetime.datetime.utcnow().timestamp() * 1000)),
            "company": body.get("company") or "",
            "contact": body.get("contact") or "",
            "notes": body.get("notes") or "",
            "status": body.get("status") or "New",
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
        dbx = get_dropbox_client()
        leads = _read_json_from_dropbox(dbx, FABSHOP_LEADS_PATH, [])
        leads.append(lead)
        _write_json_to_dropbox(dbx, FABSHOP_LEADS_PATH, leads)
        return jsonify({"status": "ok", "lead": lead})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/leads/<lead_id>", methods=["PATCH"])
def fabshop_portal_update_lead(lead_id):
    try:
        body = request.get_json(force=True)
        dbx = get_dropbox_client()
        leads = _read_json_from_dropbox(dbx, FABSHOP_LEADS_PATH, [])
        target = next((l for l in leads if l["id"] == lead_id), None)
        if not target:
            return jsonify({"status": "error", "message": "Lead not found"}), 404
        if "status" in body:
            target["status"] = body["status"]
        if "notes" in body:
            target["notes"] = body["notes"]
        _write_json_to_dropbox(dbx, FABSHOP_LEADS_PATH, leads)
        return jsonify({"status": "ok", "lead": target})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/leads/<lead_id>", methods=["DELETE"])
def fabshop_portal_delete_lead(lead_id):
    try:
        dbx = get_dropbox_client()
        leads = _read_json_from_dropbox(dbx, FABSHOP_LEADS_PATH, [])
        leads = [l for l in leads if l["id"] != lead_id]
        _write_json_to_dropbox(dbx, FABSHOP_LEADS_PATH, leads)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/evaluation-data", methods=["GET"])
def fabshop_portal_evaluation_data():
    """Same merged data as /api/fabshop-revenue, but open (no login) since
    the portal itself has no auth yet — used for the Evaluation graph."""
    try:
        dbx = get_dropbox_client()
        data = _merge_fabshop_data(dbx)
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/fabshop-portal/jerron-logs", methods=["GET"])
def fabshop_portal_jerron_logs():
    try:
        days = int(request.args.get("days", 30))
        logs = get_recent_daily_logs(days=days)
        jerron_logs = [
            l for l in logs
            if "jerron" in (l.get("name") or "").lower()
            and l.get("job_or_wo") in ("Fab Shop Daily Log", "Daily Management Log")
        ]
        return jsonify({"count": len(jerron_logs), "logs": jerron_logs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Current Jobs — the same data reviewed in Monday morning meetings.
# NOTE: like Work Orders, this is an inline database and uses the newer
# data-sources endpoint. Please test and report back if it errors.
# ==========================================

@app.route("/api/notion/current-jobs", methods=["GET"])
@require_role("admin", "calvin")
def notion_current_jobs():
    try:
        pages = notion_utils.query_data_source(NOTION_CURRENT_JOBS_DATASOURCE_ID, page_size=100)
        items = []
        for page in pages:
            props = page.get("properties", {})
            items.append({
                "job_number": notion_utils.prop_text(props, "Job #"),
                "notion_url": page.get("url"),
                "name": notion_utils.prop_text(props, "Name"),
                "job_status": notion_utils.prop_select(props, "Job Status"),
                "stage": notion_utils.prop_select(props, "Stage"),
                "priority": notion_utils.prop_select(props, "Priority"),
                "project_manager": notion_utils.prop_select(props, "Project Manager"),
                "hours_budget": notion_utils.prop_number(props, "Hours budget"),
                "hours_used": notion_utils.prop_number(props, "Hours Used"),
                "total_budget": notion_utils.prop_number(props, "Total Budget"),
                "contract": notion_utils.prop_number(props, "Contract"),
                "next_action": notion_utils.prop_text(props, "Next Action"),
                "pm_next_action": notion_utils.prop_text(props, "PM Next Action"),
                "status_note": notion_utils.prop_text(props, "Status Note"),
                "calvin_review": notion_utils.prop_checkbox(props, "Calvin Review"),
            })

        # Active-feeling jobs first: anything not Won/Lost/Cancelled/Complete-ish
        closed_statuses = {"Won", "Lost", "Cancelled", "Complete", "Expired"}
        items.sort(key=lambda i: (i["job_status"] in closed_statuses if i["job_status"] else False, i["job_number"] or ""))

        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Job budget links — reads the "Current Job Analyses" Notion PAGE (not a
# database), which is a running list of headings (week-of dates) each
# followed by links to per-job SharePoint budget workbooks. Filtered to
# the current calendar month only, since jobs repeat across many weeks.
#
# NOTE: this parses raw Notion page blocks rather than a structured
# database, which is inherently less predictable than the other routes.
# Please test and report back what it returns.
# ==========================================

_DATE_IN_HEADING = re.compile(r'(\d{1,2})/(\d{1,2})/(\d{2,4})')
_JOB_NUMBER_IN_TEXT = re.compile(r'J\d{3,5}', re.IGNORECASE)


def _parse_job_budget_links():
    blocks = notion_utils.get_page_blocks(NOTION_JOB_ANALYSES_PAGE_ID)
    now = datetime.datetime.utcnow().date()
    current_heading_date = None
    by_job = {}

    for block in blocks:
        btype = block.get("type")

        if btype in ("heading_1", "heading_2", "heading_3"):
            text, _ = notion_utils.block_plain_text(block)
            m = _DATE_IN_HEADING.search(text)
            if m:
                mm, dd, yy = m.groups()
                yy = int(yy)
                if yy < 100:
                    yy += 2000
                try:
                    current_heading_date = datetime.date(yy, int(mm), int(dd))
                except ValueError:
                    current_heading_date = None
            else:
                current_heading_date = None  # a non-date heading, e.g. "Purchase Order Review"
            continue

        if not current_heading_date:
            continue
        if current_heading_date.year != now.year or current_heading_date.month != now.month:
            continue

        text, url = notion_utils.block_plain_text(block)
        if not url or not text:
            continue
        job_match = _JOB_NUMBER_IN_TEXT.search(text)
        if not job_match:
            continue

        job_number = job_match.group(0).upper()
        by_job.setdefault(job_number, []).append({
            "date": current_heading_date.isoformat(),
            "filename": text,
            "url": url,
        })

    return by_job


@app.route("/api/notion/job-budgets", methods=["GET"])
@require_role("admin", "calvin")
def job_budgets():
    try:
        data = _parse_job_budget_links()
        return jsonify({"count": len(data), "budgets_by_job": data})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _parse_latest_job_analyses():
    """
    Current Jobs, redefined: not the full Current Jobs database (which
    includes leads, prospects, and everything else), but specifically
    whichever job numbers appear under the MOST RECENT dated heading on
    the Current Job Analyses page — that's what's actually being actively
    tracked week to week, updated every Friday/Monday.
    """
    blocks = notion_utils.get_page_blocks(NOTION_JOB_ANALYSES_PAGE_ID)
    as_of_date = None
    as_of_label = None
    jobs = []
    found_latest_heading = False

    for block in blocks:
        btype = block.get("type")

        if btype in ("heading_1", "heading_2", "heading_3"):
            text, _ = notion_utils.block_plain_text(block)
            m = _DATE_IN_HEADING.search(text)
            if m:
                if found_latest_heading:
                    break  # hit the *next* dated heading — stop, we only want the first (latest) one
                mm, dd, yy = m.groups()
                yy = int(yy)
                if yy < 100:
                    yy += 2000
                try:
                    as_of_date = datetime.date(yy, int(mm), int(dd))
                    as_of_label = text
                    found_latest_heading = True
                except ValueError:
                    pass
            continue

        if not found_latest_heading:
            continue  # skip anything before the first dated heading (e.g. "Purchase Order Review")

        text, url = notion_utils.block_plain_text(block)
        if not url or not text:
            continue
        job_match = _JOB_NUMBER_IN_TEXT.search(text)
        if not job_match:
            continue

        jobs.append({
            "job_number": job_match.group(0).upper(),
            "filename": text,
            "url": url,
        })

    return {
        "as_of": as_of_date.isoformat() if as_of_date else None,
        "as_of_label": as_of_label,
        "jobs": jobs,
    }


@app.route("/api/notion/latest-job-analyses", methods=["GET"])
@require_role("admin", "calvin")
def latest_job_analyses():
    try:
        data = _parse_latest_job_analyses()
        return jsonify(data)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Purchase Orders grouped by Job # and a category derived from the actual
# Description text (its first meaningful word) rather than a fixed guess
# list — so grouping reflects whatever terminology is really being used.
# ==========================================

_PO_STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "of", "to", "new", "replacement",
    "part", "parts", "misc", "miscellaneous", "item", "items", "with",
}


def _category_from_description(description):
    if not description:
        return "Other"
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", description)
    for w in words:
        if w.lower() not in _PO_STOPWORDS:
            return w.capitalize()
    return "Other"


@app.route("/api/notion/po-by-job", methods=["GET"])
@require_role("admin", "calvin")
def po_by_job():
    try:
        # Only show POs for jobs that are actually current (per the same
        # "latest heading" definition used for Current Jobs) — otherwise
        # this fills up with completed jobs from long ago.
        latest = _parse_latest_job_analyses()
        current_job_keys = {j["job_number"] for j in latest.get("jobs", [])}

        pages = notion_utils.query_data_source_all(NOTION_PURCHASE_ORDERS_DATASOURCE_ID)
        grouped = {}

        for page in pages:
            props = page.get("properties", {})
            job_number_raw = notion_utils.prop_number(props, "Job #")
            if not job_number_raw:
                continue
            job_key = f"J{int(job_number_raw)}"

            if current_job_keys and job_key not in current_job_keys:
                continue  # not one of the currently active jobs — skip

            name = notion_utils.prop_text(props, "Name") or ""
            description = notion_utils.prop_text(props, "Description") or ""
            category = _category_from_description(description)

            grouped.setdefault(job_key, {}).setdefault(category, []).append({
                "po_number": notion_utils.prop_text(props, "PO #"),
                "vendor": notion_utils.prop_select(props, "Vendor"),
                "name": name,
                "description": description,
                "amount": notion_utils.prop_number(props, "Amount"),
                "status": notion_utils.prop_status(props, "Status"),
                "ordered": notion_utils.prop_date(props, "Ordered"),
                "delivered": notion_utils.prop_date(props, "Delivered"),
                "notion_url": page.get("url"),
            })

        return jsonify({"count": len(grouped), "purchase_orders_by_job": grouped, "current_jobs_considered": sorted(current_job_keys)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Sales pipeline (Typeform Leads. FEED HERE) — filtered to the statuses
# Jacque said Calvin cares about: NEW, Initial Takeoff, Proposal to review.
# This also covers Kevin's reports, since Kevin is one of the Sales Reps
# feeding this same database.
# ==========================================

_SALES_PIPELINE_STATUSES = ["NEW", "Initial Takeoff", "Proposal to review"]


@app.route("/api/notion/sales-pipeline", methods=["GET"])
@require_role("admin", "calvin")
def sales_pipeline():
    try:
        filter_obj = {
            "and": [
                {"property": "Inactive", "checkbox": {"equals": False}},
                {"or": [
                    {"property": "Project Status", "select": {"equals": s}}
                    for s in _SALES_PIPELINE_STATUSES
                ]},
            ]
        }
        pages = notion_utils.query_data_source(NOTION_SALES_PIPELINE_DATASOURCE_ID, page_size=100, filter_obj=filter_obj)

        items = []
        for page in pages:
            props = page.get("properties", {})
            items.append({
                "project": notion_utils.prop_text(props, "Project"),
                "customer_name": notion_utils.prop_text(props, "Customer Name"),
                "job_number": notion_utils.prop_text(props, "Job #"),
                "project_status": notion_utils.prop_select(props, "Project Status"),
                "sales_rep": notion_utils.prop_select(props, "Sales Rep"),
                "priority": notion_utils.prop_select(props, "Priority"),
                "next_action": notion_utils.prop_text(props, "Next Action"),
                "est_contract": notion_utils.prop_number(props, "Est Contract"),
                "scope_of_work": notion_utils.prop_text(props, "Scope of Work"),
                "notion_url": page.get("url"),
            })

        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==========================================
# Kevin's Daily Management Form — same 7-project-slot shape as the Garage
# "Daily Management Log", but a separate Notion database entirely, synced
# directly from Typeform (not through our script/webhook).
# ==========================================

_KEVIN_FORM_SLOT_FIELDS = [
    ("1 What project did you work on today? ", "1 What did you do for this project? (1)", "1 How long did that take? (2) 1", "1 What is the next action for this project? (2) 1"),
    ("2 What project did you work on today? (3)", "2 What did you do for this project? (2)", "2 How long did that take? (2)", "2 What is the next action for this project? (2)"),
    ("3 What project did you work on today? (4)", "3 What did you do for this project? (3)", "3 How long did that take? (4) 1", "3 What is the next action for this project? (4) 1"),
    ("4 What project did you work on today? (5)", "4 What did you do for this project? (4)", "4 How long did that take? (4)", "4 What is the next action for this project? (4)"),
    ("5 What project did you work on today? (6)", "5 What did you do for this project? (5)", "5 How long did that take? (5)", "5 What is the next action for this project? (6)"),
    ("6 What project did you work on today? (7)", "6 What did you do for this project? (6)", "6 How long did that take? (7)", "6 What is the next action for this project? (7)"),
    ("7 What project did you work on today? (8)", "7 What did you do for this project? (7)", "7 How long did that take? (8)", "7 What is the next action for this project? (8)"),
]


@app.route("/api/notion/kevin-management-form", methods=["GET"])
@require_role("admin", "calvin")
def kevin_management_form():
    try:
        pages = notion_utils.query_data_source(NOTION_KEVIN_FORM_DATASOURCE_ID, page_size=30)
        items = []

        for page in pages:
            props = page.get("properties", {})
            date = notion_utils.prop_date(props, "Date")
            name = notion_utils.prop_people(props, "Name") or "Kevin"

            lines = []
            has_next_action = False
            for project_f, did_f, how_long_f, next_f in _KEVIN_FORM_SLOT_FIELDS:
                project = notion_utils.prop_text(props, project_f)
                did = notion_utils.prop_text(props, did_f)
                how_long = notion_utils.prop_text(props, how_long_f)
                next_action = notion_utils.prop_text(props, next_f)

                if not (project or did):
                    continue

                line = f"{project or 'Project'}: {did or ''}"
                if how_long:
                    line += f" ({how_long})"
                if next_action:
                    line += f" — Next: {next_action}"
                    has_next_action = True
                lines.append(line)

            items.append({
                "id": "kevin-mgmt-" + page["id"],
                "notionUrl": page.get("url"),
                "date": date,
                "name": name,
                "summary": "\n".join(lines),
                "has_next_action": has_next_action,
            })

        items.sort(key=lambda i: i["date"] or "", reverse=True)
        return jsonify({"count": len(items), "items": items})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
