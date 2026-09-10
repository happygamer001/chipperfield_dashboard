"""
Typeform webhook receiver.

Typeform POSTs here the instant someone submits a Daily Job Log.
No email, no inbox, no waiting for a scheduled scan.

SETUP REQUIRED IN TYPEFORM (see deployment instructions):
  - Webhook URL: https://<your-vercel-domain>/api/typeform-webhook
  - Secret: set the same value as the TYPEFORM_WEBHOOK_SECRET env var in Vercel

FIELD MATCHING:
  This looks for field titles containing "name", "job" or "work order", and
  "date" to identify those answers, and treats any file-upload answer as a
  photo. Everything else gets included in the PDF body text.
  Since exact field titles depend on your actual Typeform form, double-check
  the FIELD MATCHING section below against your form's real field titles
  after the first test submission (see deployment instructions, step 6).
"""

from http.server import BaseHTTPRequestHandler
import json
import re
import hmac
import hashlib
import base64
import datetime
import sys
import os
import requests

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from dropbox_pdf_utils import (  # noqa: E402
    get_dropbox_client,
    format_date_for_jacque,
    upload_pdf_and_photos,
    require_env,
)


def verify_signature(raw_body, signature_header):
    secret = os.environ.get("TYPEFORM_WEBHOOK_SECRET")
    if not secret:
        # No secret configured — allow through, but this should be set in production.
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    provided = signature_header.split("sha256=", 1)[1]
    return hmac.compare_digest(expected, provided)


def find_answer(answers, keywords):
    """Return the first answer whose field title contains any of the keywords."""
    for ans in answers:
        title = (ans.get("field", {}).get("title") or "").lower()
        if any(kw in title for kw in keywords):
            return ans
    return None


def answer_text(ans):
    if ans is None:
        return None
    t = ans.get("type")
    if t == "text" or t == "choice":
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

    # Build PDF body text from every text/number/choice answer, labeled by field title
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


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length) if content_length else b""

        signature = self.headers.get("Typeform-Signature")
        if not verify_signature(raw_body, signature):
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Invalid signature"}).encode())
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
            summary = process_submission(payload)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "summary": summary}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())

    def do_GET(self):
        # Simple health check so you can confirm the endpoint is live in a browser.
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "message": "Typeform webhook endpoint is live. POST only."}).encode())
