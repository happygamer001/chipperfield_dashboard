"""
Single Python entrypoint for Vercel.

As of mid-2026, Vercel's Python runtime expects one entrypoint app (Flask,
FastAPI, Django, etc.) rather than treating every file in /api as its own
function. This file is that entrypoint — it's a small Flask app with three
routes, one per feature:

  GET  /api/recent-logs         -> last N days of Daily Job Logs from Dropbox
  GET/POST /api/typeform-webhook -> real-time Typeform submission receiver
  GET  /api/upload-daily-logs   -> legacy/backup: manually re-run the Gmail scan

See vercel.json for the rewrite rule that routes /api/* here.
"""

import os
import sys
import re
import json
import hmac
import hashlib
import base64

import requests
from flask import Flask, request, jsonify

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from dropbox_pdf_utils import (  # noqa: E402
    get_dropbox_client,
    format_date_for_jacque,
    upload_pdf_and_photos,
    get_recent_daily_logs,
    DAYS_TO_SHOW_DEFAULT,
)
from daily_log_uploader import run_gmail_djl_uploader  # noqa: E402

app = Flask(__name__)


# ==========================================
# /api/recent-logs
# ==========================================

@app.route("/api/recent-logs", methods=["GET"])
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
def upload_daily_logs():
    try:
        summary = run_gmail_djl_uploader()
        return jsonify({"status": "ok", "summary": summary})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
