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
import csv
import hmac
import hashlib
import base64
import datetime
import time
from functools import wraps

import requests
import dropbox
from concurrent.futures import ThreadPoolExecutor
import openpyxl
from flask import Flask, request, jsonify, session, send_from_directory

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from dropbox_pdf_utils import (  # noqa: E402
    get_dropbox_client,
    format_date_for_jacque,
    upload_pdf_and_photos,
    get_recent_daily_logs,
    extract_text_from_pdf_bytes,
    DAYS_TO_SHOW_DEFAULT,
    DROPBOX_BASE_FOLDER,
)
from daily_log_uploader import run_gmail_djl_uploader  # noqa: E402
import notion_utils  # noqa: E402
import form_parsers  # noqa: E402

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
DAILY_LOG_NOTES_PATH = os.environ.get(
    "DAILY_LOG_NOTES_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/daily_log_notes.json"
)
DAILY_LOG_TITLE_OVERRIDES_PATH = os.environ.get(
    "DAILY_LOG_TITLE_OVERRIDES_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/daily_log_title_overrides.json"
)
FIELD_ID_ROLE_MAP_PATH = os.environ.get(
    "FIELD_ID_ROLE_MAP_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/typeform_field_role_map.json"
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


def require_role_or_cron(*allowed_roles):
    """Same as require_role, but also lets Vercel's scheduled Cron trigger
    through — Vercel sends 'Authorization: Bearer <CRON_SECRET>' on cron
    invocations, which carry no login session at all."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            cron_secret = os.environ.get("CRON_SECRET")
            auth_header = request.headers.get("Authorization", "")
            if cron_secret and auth_header == f"Bearer {cron_secret}":
                return fn(*args, **kwargs)
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

def _daily_log_key(log):
    """Stable key for a Dropbox-derived log entry, since these don't have a
    real ID of their own (they're just files in a folder each time)."""
    return f"{log.get('date', '')}|{log.get('name', '')}|{log.get('job_or_wo') or ''}"


def _notes_for_log(all_notes, log):
    """Exact key match first; falls back to any note sharing just date+name,
    since a bulk-imported note's job/WO tag won't always exactly match
    whatever tag ended up in the real Dropbox filename."""
    exact = all_notes.get(_daily_log_key(log))
    if exact:
        return exact
    prefix = f"{log.get('date', '')}|{log.get('name', '')}|"
    combined = []
    for key, notes in all_notes.items():
        if key.startswith(prefix):
            combined.extend(notes)
    return combined


@app.route("/api/recent-logs", methods=["GET"])
@require_role("admin", "calvin")
def recent_logs():
    try:
        days = int(request.args.get("days", DAYS_TO_SHOW_DEFAULT))
        logs = get_recent_daily_logs(days=days)

        dbx = get_dropbox_client()
        all_notes = _read_json_from_dropbox(dbx, DAILY_LOG_NOTES_PATH, {})
        title_overrides = _read_json_from_dropbox(dbx, DAILY_LOG_TITLE_OVERRIDES_PATH, {})

        for log in logs:
            pdf_path = log.get("dropbox_pdf_path")
            if pdf_path and pdf_path in title_overrides:
                override = title_overrides[pdf_path]
                if override.get("name"):
                    log["name"] = override["name"]
                if override.get("job_or_wo") is not None:
                    log["job_or_wo"] = override["job_or_wo"]
                log["title_corrected"] = True
            log["manual_notes"] = _notes_for_log(all_notes, log)

        return jsonify({"days": days, "count": len(logs), "logs": logs})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/daily-log-notes", methods=["POST"])
@require_role("admin", "calvin")
def add_daily_log_note():
    """A manual comment/correction on a Dropbox-derived log entry — a
    stopgap for entries whose real text wasn't captured at upload time."""
    try:
        body = request.get_json(force=True)
        date = body.get("date", "")
        name = body.get("name", "")
        job_or_wo = body.get("job_or_wo") or ""
        text = (body.get("text") or "").strip()
        if not text:
            return jsonify({"status": "error", "message": "Note text is required"}), 400

        key = f"{date}|{name}|{job_or_wo}"
        dbx = get_dropbox_client()
        all_notes = _read_json_from_dropbox(dbx, DAILY_LOG_NOTES_PATH, {})
        all_notes.setdefault(key, []).append({
            "author": body.get("author") or session.get("role", "admin"),
            "text": text,
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
        _write_json_to_dropbox(dbx, DAILY_LOG_NOTES_PATH, all_notes)
        return jsonify({"status": "ok", "notes": all_notes[key]})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def _parse_submit_timestamp(val):
    """Typeform CSV exports 'Submit Date (UTC)' as 'YYYY-MM-DD HH:MM:SS'."""
    if not val:
        return None
    try:
        return datetime.datetime.strptime(val.strip(), "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        return None


def _parse_daily_job_log_csv_row(row):
    """
    Parses one row of a Typeform CSV export of the real 'Daily Job Log' form
    (the one with an actual Job Number field — not the management-log or
    Fab Shop forms). Column names come directly from the export, so this is
    more reliable than the webhook's keyword-guessing on field titles.
    """
    name = (row.get("Your Name") or "").strip()
    raw_date = (row.get("*Date*") or "")[:10]
    try:
        date_iso = datetime.date.fromisoformat(raw_date).isoformat() if raw_date else None
    except ValueError:
        date_iso = None

    job_field = (row.get("*Job number* ") or row.get("*Job number*") or "").strip()
    wo_num = (row.get("Workorder #") or "").strip()
    wo_name = (row.get("Workorder Name") or "").strip()

    job_tags = _extract_job_numbers(job_field)
    if not job_tags:
        job_tags = _extract_job_numbers(wo_num)
    job_wo = ", ".join(job_tags) if job_tags else _clean_or_none(job_field)
    if not job_wo and wo_name:
        job_wo = wo_name

    lines = []
    work_desc = _clean_or_none(row.get("What did your team work on today?"))
    if work_desc:
        lines.append(f"Work: {work_desc}")
    productivity = _clean_or_none(row.get("*Productivity Issues*"))
    if productivity:
        lines.append(f"Productivity issues: {productivity}")
    incident = _clean_or_none(row.get("Incident Report?")) or _clean_or_none(row.get("*Safety Incident*"))
    if incident:
        lines.append(f"Incident: {incident}")
    todo = _clean_or_none(row.get("To do:"))
    if todo:
        lines.append(f"To do: {todo}")
    scope_desc = _clean_or_none(row.get("Please describe additional work:"))
    if scope_desc:
        lines.append(f"Additional work: {scope_desc}")

    return {
        "name": name,
        "date": date_iso,
        "job_or_wo": job_wo,
        "text": "\n".join(lines),
        "submitted_at": _parse_submit_timestamp(row.get("Submit Date (UTC)")),
    }


def _parse_management_log_csv(content):
    """
    Parses the Management Log CSV export positionally rather than by column
    name — several columns (e.g. every 'How long did that take?') repeat
    with the exact same header text, which would silently lose data under
    a name-keyed dict. Also matches the real typo 'Next Projet:' on one slot.
    """
    reader = csv.reader(io.StringIO(content))
    header = next(reader)

    name_idx = None
    date_idx = None
    submit_idx = None
    slot_cols = []  # (index, kind) for every classified project-slot column

    for i, h in enumerate(header):
        hl = h.lower().strip()
        if hl.startswith("your name"):
            name_idx = i
        elif "submit date" in hl:
            submit_idx = i
        elif "date" in hl and name_idx is not None and date_idx is None and i <= name_idx + 2:
            date_idx = i
        else:
            kind = _classify_field(h)
            if kind:
                slot_cols.append((i, kind))

    entries = []
    for row in reader:
        if not row or (name_idx is not None and name_idx >= len(row)):
            continue
        name = (row[name_idx] or "").strip() if name_idx is not None else ""
        raw_date = (row[date_idx] or "")[:10] if date_idx is not None and date_idx < len(row) else ""
        try:
            date_iso = datetime.date.fromisoformat(raw_date).isoformat() if raw_date else None
        except ValueError:
            date_iso = None
        submitted_at = _parse_submit_timestamp(row[submit_idx]) if submit_idx is not None and submit_idx < len(row) else None
        if not name or not date_iso:
            continue

        projects = []
        current = None
        for idx, kind in slot_cols:
            if idx >= len(row):
                continue
            val = (row[idx] or "").strip()
            if kind == "project":
                if not val:
                    current = None
                    continue
                current = {"project": val, "did": "", "duration": "", "next_action": ""}
                projects.append(current)
            elif kind == "did" and current is not None:
                current["did"] = val
            elif kind == "duration" and current is not None:
                current["duration"] = val
            elif kind == "next_action" and current is not None:
                current["next_action"] = val
            # "continuation" (the yes/no "any more projects?") is skipped

        job_wo = None
        for p in projects:
            job_wo = _extract_job_number(p["project"])
            if job_wo:
                break
        if not job_wo:
            job_wo = "Daily Management Log"

        lines = []
        for p in projects:
            if not (p["project"] or p["did"]):
                continue
            line = f"{p['project'] or 'Project'}: {p['did']}"
            if p["duration"]:
                line += f" ({p['duration']})"
            next_clean = _clean_or_none(p["next_action"])
            if next_clean:
                line += f" — Next: {next_clean}"
            lines.append(line)

        entries.append({
            "name": name,
            "date": date_iso,
            "job_or_wo": job_wo,
            "text": "\n".join(lines),
            "submitted_at": submitted_at,
        })

    return entries


def _detect_csv_type(header_row):
    joined = " | ".join(h.lower().replace("*", "") for h in header_row)
    if "job number" in joined:
        return "daily_job_log"
    if "how long did that take" in joined and "next action" in joined:
        return "management_log"
    return "unknown"


@app.route("/api/bulk-import-daily-logs", methods=["POST"])
@require_role("admin")
def bulk_import_daily_logs():
    """
    Upload a Typeform CSV export (Daily Job Log or Management Log — the
    type is auto-detected from the header row) to backfill description
    text on existing entries. Stored the same way as a manual note, tagged
    'CSV Import'. Safe to re-run: skips rows whose exact text was already
    imported.
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file included in upload"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400

    try:
        content = file.read().decode("utf-8-sig")

        # Peek the header to figure out which form this export is from
        header_row = next(csv.reader(io.StringIO(content)))
        csv_type = _detect_csv_type(header_row)

        if csv_type == "daily_job_log":
            reader = csv.DictReader(io.StringIO(content))
            parsed_rows = [_parse_daily_job_log_csv_row(row) for row in reader]
        elif csv_type == "management_log":
            parsed_rows = _parse_management_log_csv(content)
        else:
            return jsonify({
                "status": "error",
                "message": "Couldn't recognize this CSV's form type (no Job Number field, and no repeating project-slot pattern found). Let Claude know what form this is from so support for it can be added."
            }), 400

        dbx = get_dropbox_client()
        all_notes = _read_json_from_dropbox(dbx, DAILY_LOG_NOTES_PATH, {})

        imported = 0
        skipped_no_data = 0
        skipped_duplicate = 0

        for parsed in parsed_rows:
            if not parsed["name"] or not parsed["date"] or not parsed["text"]:
                skipped_no_data += 1
                continue

            key = f"{parsed['date']}|{parsed['name']}|{parsed['job_or_wo'] or ''}"
            existing = all_notes.get(key, [])
            if any(n.get("text") == parsed["text"] for n in existing):
                skipped_duplicate += 1
                continue

            existing.append({
                "author": "CSV Import",
                "text": parsed["text"],
                "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            all_notes[key] = existing
            imported += 1

        _write_json_to_dropbox(dbx, DAILY_LOG_NOTES_PATH, all_notes)
        return jsonify({
            "status": "ok",
            "csv_type": csv_type,
            "imported": imported,
            "skipped_no_data": skipped_no_data,
            "skipped_duplicate": skipped_duplicate,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/bulk-fix-titles", methods=["POST"])
@require_role("admin")
def bulk_fix_titles():
    """
    Corrects the TITLE (name + job/WO) shown on existing entries whose name
    came through as 'Unknown' — unlike the description-text import, this
    changes what's actually displayed at the top of the card. Matching is
    done by closest submit-timestamp on the same date, since a broken
    entry's own name/job fields can't be used to find its real CSV row.
    Best-effort: please review the results rather than trusting blindly.
    """
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file included in upload"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "No file selected"}), 400

    try:
        content = file.read().decode("utf-8-sig")
        header_row = next(csv.reader(io.StringIO(content)))
        csv_type = _detect_csv_type(header_row)

        if csv_type == "daily_job_log":
            reader = csv.DictReader(io.StringIO(content))
            parsed_rows = [_parse_daily_job_log_csv_row(row) for row in reader]
        elif csv_type == "management_log":
            parsed_rows = _parse_management_log_csv(content)
        else:
            return jsonify({
                "status": "error",
                "message": "Couldn't recognize this CSV's form type."
            }), 400

        parsed_rows = [r for r in parsed_rows if r.get("submitted_at") and r.get("date")]

        # Wide window so this can catch older broken entries too, not just
        # whatever the dashboard's normal 5-day view shows.
        logs = get_recent_daily_logs(days=60)
        broken = [
            l for l in logs
            if (l.get("name") or "").strip().lower() == "unknown"
            and l.get("uploaded_at") and l.get("dropbox_pdf_path")
        ]

        dbx = get_dropbox_client()
        overrides = _read_json_from_dropbox(dbx, DAILY_LOG_TITLE_OVERRIDES_PATH, {})

        from collections import defaultdict
        broken_by_date = defaultdict(list)
        for b in broken:
            broken_by_date[b["date"]].append(b)
        csv_by_date = defaultdict(list)
        for i, r in enumerate(parsed_rows):
            csv_by_date[r["date"]].append((i, r))

        matched = 0
        unmatched = 0
        used_csv_rows = set()
        match_details = []

        for date, broken_entries in broken_by_date.items():
            candidates = csv_by_date.get(date, [])
            for b in broken_entries:
                try:
                    b_time = datetime.datetime.fromisoformat(b["uploaded_at"].replace("Z", ""))
                except ValueError:
                    unmatched += 1
                    continue

                best, best_diff = None, None
                for i, r in candidates:
                    if i in used_csv_rows:
                        continue
                    try:
                        r_time = datetime.datetime.fromisoformat(r["submitted_at"])
                    except ValueError:
                        continue
                    diff = abs((b_time - r_time).total_seconds())
                    if best_diff is None or diff < best_diff:
                        best_diff, best = diff, (i, r)

                if best:
                    i, r = best
                    used_csv_rows.add(i)
                    overrides[b["dropbox_pdf_path"]] = {"name": r["name"], "job_or_wo": r["job_or_wo"]}
                    match_details.append({
                        "date": date, "corrected_name": r["name"], "corrected_job_or_wo": r["job_or_wo"],
                        "time_diff_seconds": round(best_diff),
                    })
                    matched += 1
                else:
                    unmatched += 1

        _write_json_to_dropbox(dbx, DAILY_LOG_TITLE_OVERRIDES_PATH, overrides)
        return jsonify({
            "status": "ok",
            "csv_type": csv_type,
            "total_broken_found": len(broken),
            "matched": matched,
            "unmatched": unmatched,
            "match_details": match_details,
        })
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
    return form_parsers.find_answer(answers, keywords)


def find_answer_with_learning(answers, keywords, role_key, learned_map):
    return form_parsers.find_answer_with_learning(answers, keywords, role_key, learned_map)


def answer_text(ans):
    return form_parsers.answer_text(ans)


def download_file(url):
    token = os.environ.get("TYPEFORM_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = requests.get(url, headers=headers, timeout=20)
    return resp.content if resp.status_code == 200 else None


# ==========================================
# The actual structured parsing logic lives in scripts/form_parsers.py now,
# shared with the Gmail-scan path (scripts/daily_log_uploader.py) so both
# produce identically good output instead of the email path being cruder.
# ==========================================

def _extract_job_numbers(text):
    return form_parsers._extract_job_numbers(text)


def _extract_job_number(text):
    return form_parsers._extract_job_number(text)


def _clean_or_none(text):
    return form_parsers._clean_or_none(text)


def _detect_form_type(answers, learned_map=None):
    return form_parsers.detect_form_type(answers, learned_map)


def _collect_photos(answers):
    photo_bytes_list = []
    for ans in answers:
        if ans.get("type") == "file_url":
            file_bytes = download_file(ans.get("file_url"))
            if file_bytes:
                photo_bytes_list.append(file_bytes)
    return photo_bytes_list


def _parse_management_log(answers, log_name, log_date, learned_map=None):
    return form_parsers.parse_management_log(answers, log_name, log_date, learned_map)


def _parse_daily_job_log(answers, log_name, log_date, learned_map=None):
    return form_parsers.parse_daily_job_log(answers, log_name, log_date, learned_map)


def _parse_fab_shop_log(answers, log_name, log_date, learned_map=None):
    return form_parsers.parse_fab_shop_log(answers, log_name, log_date, learned_map)



def process_submission(payload):
    form_response = payload.get("form_response", {})
    answers = form_response.get("answers", [])
    submitted_at = form_response.get("submitted_at", "")

    if not answers:
        raise ValueError("Submission had no answers")

    # Typeform's webhook payload does NOT embed a question's title inside
    # answers[].field — that only ever has {id, type, ref}. The real
    # titles live in a completely separate array, form_response.definition
    # .fields[], keyed by that same field id. Every previous attempt to
    # read a title straight off an answer was structurally looking in the
    # wrong place — this was never a "title sometimes missing" problem.
    # Build the id -> title lookup once and enrich every answer with its
    # real title before any classification logic runs.
    definition_fields = form_response.get("definition", {}).get("fields", [])
    field_id_to_title = {f.get("id"): f.get("title") for f in definition_fields if f.get("id")}
    for ans in answers:
        field_id = ans.get("field", {}).get("id")
        if field_id and field_id_to_title.get(field_id):
            ans["field"]["title"] = field_id_to_title[field_id]

    dbx = get_dropbox_client()
    learned_map = _read_json_from_dropbox(dbx, FIELD_ID_ROLE_MAP_PATH, {})

    name_ans = find_answer_with_learning(answers, ["your name", "name"], "submitter_name", learned_map)
    date_ans = find_answer_with_learning(answers, ["date"], "submitted_date", learned_map)
    log_name = _clean_or_none(answer_text(name_ans)) or "Unknown"
    raw_date = answer_text(date_ans) or submitted_at[:10] or ""
    log_date = format_date_for_jacque(raw_date)

    form_type = _detect_form_type(answers, learned_map)
    if form_type == "management_log":
        job_wo, pdf_text, extra = _parse_management_log(answers, log_name, log_date, learned_map)
    elif form_type == "daily_job_log":
        job_wo, pdf_text, extra = _parse_daily_job_log(answers, log_name, log_date, learned_map)
    elif form_type == "fab_shop_log":
        job_wo, pdf_text, extra = _parse_fab_shop_log(answers, log_name, log_date, learned_map)
    else:
        # Unrecognized form shape — fall back to a generic dump so at least
        # something usable lands in Dropbox instead of silently failing.
        job_wo = ""
        lines = []
        for ans in answers:
            title = ans.get("field", {}).get("title") or "Field (untitled)"
            val = answer_text(ans)
            if val:
                lines.append(f"{title}: {val}")
        pdf_text = "\n".join(lines)
        extra = {"form_type": "unrecognized"}

    _write_json_to_dropbox(dbx, FIELD_ID_ROLE_MAP_PATH, learned_map)

    photo_bytes_list = _collect_photos(answers)

    pdf_title_parts = [log_date, log_name]
    if job_wo:
        pdf_title_parts.append(job_wo)
    pdf_title = " | ".join(pdf_title_parts)

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

@app.route("/api/upload-daily-logs", methods=["GET", "POST"])
@require_role_or_cron("admin")
def upload_daily_logs():
    try:
        if request.method == "POST":
            # Admin button — processes exactly one batch; the frontend
            # loops itself, showing live progress between calls.
            body = request.get_json(silent=True) or {}
            start_index = int(body.get("start_index", 0))
            result = run_gmail_djl_uploader(start_index=start_index, batch_size=10)
            return jsonify({"status": "ok", **result})
        else:
            # GET (the hourly Cron trigger, or a manual browser visit) —
            # can't be interactively driven the way the button's POST loop
            # is, so this loops through batches itself, server-side,
            # stopping once done or once it's used up a safe chunk of the
            # function's time budget (whatever's left gets caught on the
            # next hourly run).
            start_time = time.time()
            start_index = 0
            combined_summary = {"scanned": 0, "pdfs_uploaded": 0, "photos_uploaded": 0, "skipped": 0}
            total = 0
            done = True
            while True:
                result = run_gmail_djl_uploader(start_index=start_index, batch_size=10)
                for k in combined_summary:
                    combined_summary[k] += result["summary"][k]
                total = result["total"]
                done = result["done"]
                if done:
                    break
                start_index = result["next_index"]
                if time.time() - start_time > 45:  # safety margin under the 60s limit
                    break
            return jsonify({"status": "ok", "summary": combined_summary, "done": done, "total": total})
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
# Work Orders — live feed for the admin review queue. Escalated ones get
# tagged distinctly so the frontend can give them a red border.
# ==========================================

@app.route("/api/notion/work-orders", methods=["GET"])
@require_role("admin")
def notion_work_orders():
    try:
        pages = notion_utils.query_data_source(NOTION_WORKORDERS_DATASOURCE_ID, page_size=100)
        items = []
        for page in pages:
            props = page.get("properties", {})
            wo_num = notion_utils.prop_number(props, "WO #")
            job_num = notion_utils.prop_number(props, "Job #")
            contact = notion_utils.prop_text(props, "Reporting Contact")
            status = notion_utils.prop_select(props, "Work Status")
            priority = notion_utils.prop_select(props, "Service Priority")
            date = notion_utils.prop_date(props, "Date")
            desc = notion_utils.prop_text(props, "Description of Work")
            complaint = notion_utils.prop_text(props, "Customer Complaint")

            is_escalated = (status == "Escalated")
            job_key = f"J{int(job_num)}" if job_num else None

            items.append({
                "id": "notion-wo-" + page["id"],
                "notionUrl": page.get("url"),
                "type": "general",
                "source": "notion-workorders",
                "sourceLabel": "Notion · Work Orders",
                "job": job_key,
                "title": (f"WO {int(wo_num)}" if wo_num else "Work order") + (f" — {contact}" if contact else ""),
                "subtitle": f"{status or 'Unknown'}" + (f" · {priority}" if priority else "") + (f" · {date}" if date else ""),
                "tagClass": "escalated" if is_escalated else ("warn" if priority == "Urgent" else "ok"),
                "tagText": status or "Unknown",
                "summary": desc or complaint or "",
                "status": "pending",
            })
        return jsonify({"count": len(items), "items": items})
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


# ==========================================
# Self-correcting filename tool — re-derives each PDF's real name/date/job
# directly from its own already-written text content, no external CSV
# needed. Adapted from the team's own original fix_no_date_files.py
# script (structured extraction first, then its proven regex fallback).
#
# Processes in small batches with a continuation cursor rather than all
# at once — with hundreds of PDFs across many months, doing everything in
# one request blew past Vercel's 60s function limit and got killed
# (FUNCTION_INVOCATION_TIMEOUT) before it could even respond. This can no
# longer time out regardless of archive size: the frontend calls this
# repeatedly, passing back the cursor each time, until done.
# ==========================================

def _collect_all_pdf_entries(dbx):
    """Flat list of (folder_path, entry) for every PDF across all year
    folders — just metadata listing, not downloading, so this stays fast
    even for a large archive."""
    entries_flat = []
    try:
        res = dbx.files_list_folder(DROPBOX_BASE_FOLDER)
        base_entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            base_entries.extend(res.entries)
    except Exception:
        return entries_flat

    year_folders = sorted(
        e.name for e in base_entries
        if isinstance(e, dropbox.files.FolderMetadata) and e.name.isdigit() and len(e.name) == 4
    )

    for year in year_folders:
        folder_path = f"{DROPBOX_BASE_FOLDER}/{year}"
        try:
            res = dbx.files_list_folder(folder_path)
            year_entries = list(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                year_entries.extend(res.entries)
        except Exception:
            continue
        for e in year_entries:
            if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".pdf"):
                entries_flat.append((folder_path, e))

    return entries_flat


def _fix_one_pdf_filename(dbx, folder_path, entry):
    """Downloads, re-parses, and renames a single PDF if needed. Returns
    ('renamed', {...}) / ('skipped_no_change', None) /
    ('skipped_no_date', None) / ('error', message)."""
    try:
        _, res_dl = dbx.files_download(entry.path_display)
        pdf_bytes = res_dl.content
    except Exception as e:
        return ("error", f"{entry.name}: download failed ({e})")

    text = extract_text_from_pdf_bytes(pdf_bytes)

    answers = form_parsers.extract_starred_fields(text)
    has_real_titles = any(a["field"]["title"] for a in answers)

    new_name = None
    new_date = None
    new_job_wo = None

    if answers and has_real_titles:
        name_ans = form_parsers.find_answer(answers, ["your name", "name"])
        date_ans = form_parsers.find_answer(answers, ["date"])
        new_name = form_parsers._clean_or_none(form_parsers.answer_text(name_ans))
        raw_date_text = form_parsers.answer_text(date_ans) or ""
        new_date = format_date_for_jacque(raw_date_text) if raw_date_text else None
        new_job_wo, _pdf_text, _extra = form_parsers.parse_structured_answers(
            answers, new_name or "Unknown", new_date or "No_Date"
        )

    if not new_name or not new_date or new_date == "No_Date":
        raw_date, fallback_name, fallback_job_wo = form_parsers.legacy_regex_extract(text, entry.name)
        if not new_name:
            new_name = fallback_name
        if not new_date or new_date == "No_Date":
            new_date = format_date_for_jacque(raw_date) if raw_date else None
        if not new_job_wo:
            new_job_wo = fallback_job_wo

    if not new_date or new_date == "No_Date" or not new_name:
        return ("skipped_no_date", None)

    parts = [new_date, new_name]
    if new_job_wo:
        parts.append(new_job_wo)
    new_filename = " | ".join(parts) + ".pdf"

    if new_filename == entry.name:
        return ("skipped_no_change", None)

    to_path = f"{folder_path}/{new_filename}"
    try:
        dbx.files_move_v2(entry.path_display, to_path, autorename=True)
        return ("renamed", {"from": entry.name, "to": new_filename})
    except Exception as e:
        return ("error", f"{entry.name}: rename failed ({e})")


@app.route("/api/fix-filenames-from-content", methods=["POST"])
@require_role("admin")
def fix_filenames_from_content():
    try:
        body = request.get_json(silent=True) or {}
        start_index = max(0, int(body.get("start_index", 0)))
        batch_size = 5  # cut way down from 15 — real Dropbox latency in
        # production is apparently higher than local testing could show

        dbx = get_dropbox_client()
        all_entries = _collect_all_pdf_entries(dbx)
        total = len(all_entries)
        batch = all_entries[start_index:start_index + batch_size]

        renamed = []
        skipped_no_change = 0
        skipped_no_date = 0
        errors = []

        # A single malformed/unusual PDF can make pypdf hang indefinitely
        # while parsing it — batching alone only limits how many files run
        # per request, it never protected against ONE file stalling the
        # whole batch forever. Give each file its own hard timeout so a
        # bad file can never block the others or blow the function budget.
        # Note: shutdown(wait=False) is deliberate — a plain 'with' block
        # would still block on exit waiting for every thread to actually
        # finish, even ones already given up on below, defeating the point.
        executor = ThreadPoolExecutor(max_workers=5)
        try:
            future_to_item = {
                executor.submit(_fix_one_pdf_filename, dbx, folder_path, entry): entry
                for folder_path, entry in batch
            }
            for future in future_to_item:
                entry = future_to_item[future]
                try:
                    results_item = future.result(timeout=8)
                except Exception as e:
                    results_item = ("error", f"{entry.name}: timed out or failed ({e})")
                if results_item[0] == "renamed":
                    renamed.append(results_item[1])
                elif results_item[0] == "skipped_no_change":
                    skipped_no_change += 1
                elif results_item[0] == "skipped_no_date":
                    skipped_no_date += 1
                elif results_item[0] == "error":
                    errors.append(results_item[1])
        finally:
            executor.shutdown(wait=False)

        next_index = start_index + len(batch)
        done = next_index >= total or len(batch) == 0

        return jsonify({
            "status": "ok",
            "done": done,
            "next_index": None if done else next_index,
            "total": total,
            "processed_so_far": next_index,
            "renamed_count": len(renamed),
            "renamed": renamed,
            "skipped_no_change": skipped_no_change,
            "skipped_no_date": skipped_no_date,
            "errors": errors,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
