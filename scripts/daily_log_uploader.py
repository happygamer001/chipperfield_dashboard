"""
LEGACY / BACKUP PATH — as of the Typeform webhook going live (api/typeform-webhook.py),
this script is no longer the primary way logs get into Dropbox. Typeform now posts
submissions directly and in real time.

Keep this around as a backup/backfill tool: if the webhook ever misses a submission
(e.g. a Vercel outage), you can still run this manually to catch anything sitting in
the inbox. It's no longer on the daily cron schedule.
"""

import imaplib
import email
from email.header import decode_header
import re
import os
import io
import datetime
import dropbox
import requests
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ==========================================
# CONFIGURATION
# ==========================================
# All sensitive values now come from environment variables.
# Locally: set them in a .env file (never commit it) or export them in your shell.
# In GitHub Actions / Vercel: set them as Secrets / Environment Variables in project settings.

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
# EMAIL_USER / EMAIL_PASS / Dropbox secrets are intentionally NOT read here.
# This module gets imported by api/index.py alongside routes that don't need
# email credentials at all (the Typeform webhook, the recent-logs lookup) —
# if we required them at import time, a single missing env var would crash
# every route in the app, not just this one. They're read lazily inside
# run_gmail_djl_uploader() instead, only when this specific feature runs.

DROPBOX_BASE_FOLDER = os.environ.get(
    "DROPBOX_BASE_FOLDER", "/Chipperfield Ag/Chipperfield/Daily Job Logs"
)

# Rolling search window instead of a fixed date, so the scan doesn't grow slower every month.
# Overridable via env var if you ever need to re-scan further back (e.g. a one-off backfill).
SEARCH_WINDOW_DAYS = int(os.environ.get("SEARCH_WINDOW_DAYS", "7"))


def _get_search_since_date():
    """IMAP wants DD-Mon-YYYY. Rolls forward automatically every day it runs."""
    since = datetime.date.today() - datetime.timedelta(days=SEARCH_WINDOW_DAYS)
    return since.strftime("%d-%b-%Y")


# ==========================================
# DROPBOX CLIENT
# ==========================================

def get_dropbox_client():
    # Reads secrets lazily, at call time — see note above.
    dropbox_app_key = _require_env("DROPBOX_APP_KEY")
    dropbox_app_secret = _require_env("DROPBOX_APP_SECRET")
    dropbox_refresh_token = _require_env("DROPBOX_REFRESH_TOKEN")

    dbx_team = dropbox.DropboxTeam(
        oauth2_refresh_token=dropbox_refresh_token,
        app_key=dropbox_app_key,
        app_secret=dropbox_app_secret,
        timeout=300.0,
    )
    team_members = dbx_team.team_members_list().members
    my_member_id = team_members[0].profile.team_member_id
    dbx_user = dbx_team.as_user(my_member_id)

    account_info = dbx_user.users_get_current_account()
    root_namespace_id = account_info.root_info.root_namespace_id

    return dbx_user.with_path_root(dropbox.common.PathRoot.root(root_namespace_id))


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def format_date_for_jacque(raw_date_str):
    """Converts dates into Jacque's YYMMDD search format (e.g., '2026-08-14' -> '260814')."""
    if not raw_date_str or raw_date_str == "No_Date":
        return "No_Date"

    if re.match(r'^\d{6}$', raw_date_str):
        return raw_date_str

    match_iso = re.search(r'\b(20\d{2})-(\d{2})-(\d{2})\b', raw_date_str)
    if match_iso:
        yy, mm, dd = match_iso.group(1)[2:], match_iso.group(2), match_iso.group(3)
        return f"{yy}{mm}{dd}"

    match_alt = re.search(r'\b(\d{1,2})[/\.-](\d{1,2})[/\.-](20\d{2}|\d{2})\b', raw_date_str)
    if match_alt:
        mm, dd, yy = match_alt.groups()
        mm = mm.zfill(2)
        dd = dd.zfill(2)
        yy = yy[2:] if len(yy) == 4 else yy
        return f"{yy}{mm}{dd}"

    return raw_date_str


def get_year_subfolder(log_date_yymmdd):
    """Determines full 4-digit year folder path from YYMMDD prefix (e.g. '260814' -> '/Daily Job Logs/2026')."""
    if log_date_yymmdd and len(log_date_yymmdd) == 6 and log_date_yymmdd.isdigit():
        full_year = f"20{log_date_yymmdd[:2]}"
        return f"{DROPBOX_BASE_FOLDER}/{full_year}"
    return DROPBOX_BASE_FOLDER


def get_existing_dropbox_files(dbx, folder_path):
    """Retrieves list of existing files in Dropbox to avoid duplicate processing."""
    existing_files = set()
    try:
        res = dbx.files_list_folder(folder_path)
        for entry in res.entries:
            existing_files.add(entry.name)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            for entry in res.entries:
                existing_files.add(entry.name)
    except dropbox.exceptions.ApiError:
        pass
    return existing_files


def parse_metadata_from_email(subject, body_text):
    """Extracts Date (formatted as YYMMDD), Name, and Job/WO/Location identifier."""
    combined = f"{subject}\n{body_text}"

    date_match = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', combined)
    if not date_match:
        alt_date = re.search(
            r'(?:Today\'s\s*date|Date)\s*[:\n\r]*\s*(\d{1,2})[/\.-](\d{1,2})[/\.-](20\d{2}|\d{2})',
            body_text, re.IGNORECASE
        )
        if alt_date:
            mm, dd, yy = alt_date.groups()
            raw_date = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
        else:
            raw_date = "No_Date"
    else:
        raw_date = date_match.group(1)

    log_date = format_date_for_jacque(raw_date)

    name_match = re.search(r'Daily Job Log\s*\([^)]*\)\s*([A-Za-z\s\.]+?)\s*\d{4}-\d{2}-\d{2}', subject)
    if not name_match:
        name_match = re.search(r'(?:Your\s*name|Name)\s*[:\n\r]*\s*([A-Za-z\s\.]+)', body_text, re.IGNORECASE)
    log_name = name_match.group(1).strip() if name_match else "Unknown"

    job_match = re.search(r'\bJ\s*(\d{3,5})\b', combined, re.IGNORECASE) or re.search(r'Job number\s*[:\n\r]*\s*(\d+)', combined, re.IGNORECASE)
    wo_match = re.search(r'\b(WO|S)\s*(\d{3,5})\b', combined, re.IGNORECASE) or re.search(r'(?:Service work order|Work order)\s*[:\n\r]*\s*(\d+)', combined, re.IGNORECASE)

    job_wo = ""
    if job_match and job_match.group(1):
        job_wo = f"J{job_match.group(1)}"
    elif wo_match:
        prefix = wo_match.group(1).upper() if len(wo_match.groups()) > 1 else "WO"
        job_wo = f"{prefix}{wo_match.group(2) if len(wo_match.groups()) > 1 else wo_match.group(1)}"
    elif re.search(r'how\s*long\s*did\s*that\s*take', combined, re.IGNORECASE) and re.search(r'next\s*action', combined, re.IGNORECASE):
        # Repeating project-slot form (project / what you did / how long / next action)
        job_wo = "Daily Management Log"
    elif re.search(r'what\s*did\s*your\s*team\s*work\s*on\s*today', combined, re.IGNORECASE):
        # Fab Shop's own team log — no job number, shop-wide daily entry
        job_wo = "Fab Shop Daily Log"
    elif re.search(r'\boffice\b', combined, re.IGNORECASE):
        job_wo = "Office"
    elif re.search(r'\bshop\b', combined, re.IGNORECASE):
        job_wo = "Shop"
    elif re.search(r'Equipment\s*Repair', combined, re.IGNORECASE):
        job_wo = "Equipment Repair"
    elif re.search(r'Daily\s*Management\s*Log', combined, re.IGNORECASE):
        job_wo = "Daily Management Log"

    return log_date, log_name, job_wo


def clean_email_body_for_pdf(body_text):
    """Strips signature footer, forward headers, and top metadata."""
    clean = body_text
    clean = re.sub(r'---------- Forwarded message --------[\s\S]*?To:.*?\n', '', clean)
    clean = re.sub(r'Typeform sent you this email on behalf of a typeform creator[\s\S]*$', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'Thanks for completing this typeform[\s\S]*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def generate_pdf_from_text(title, text_content):
    """Generates clean PDF byte stream from formatted text content."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontSize=16, leading=20, spaceAfter=12)
    body_style = ParagraphStyle('DocBody', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6)

    story = [Paragraph(title, title_style), Spacer(1, 12)]

    lines = text_content.split('\n')
    for line in lines:
        cleaned_line = line.strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if cleaned_line:
            story.append(Paragraph(cleaned_line, body_style))
        else:
            story.append(Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def extract_photo_urls_from_html(html_content):
    """Finds image URLs embedded inside Typeform email HTML."""
    soup = BeautifulSoup(html_content, 'html.parser')
    image_urls = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        if re.search(r'\.(jpg|jpeg|png|heic|webp)', href, re.IGNORECASE) or ("typeform" in href.lower() and "files" in href.lower()):
            image_urls.append(href)
    return list(dict.fromkeys(image_urls))


# ==========================================
# MAIN EXECUTION
# ==========================================

def run_gmail_djl_uploader():
    email_user = _require_env("EMAIL_USER")
    email_pass = _require_env("EMAIL_PASS")

    search_since_date = _get_search_since_date()
    print(f"\n--- Scanning Gmail Inbox for Logs and Attached Photos since {search_since_date} ---")

    dbx = get_dropbox_client()

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
    mail.login(email_user, email_pass)
    mail.select("inbox")

    search_query = f'(SINCE "{search_since_date}")'
    status, messages = mail.search(None, search_query)
    email_ids = messages[0].split()

    summary = {"scanned": len(email_ids), "pdfs_uploaded": 0, "photos_uploaded": 0, "skipped": 0}

    if not email_ids:
        print("No emails found.")
        mail.logout()
        return summary

    for e_id in email_ids:
        _, msg_data = mail.fetch(e_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                subject_header = msg["Subject"]
                subject = ""
                if subject_header:
                    decoded = decode_header(subject_header)[0]
                    if isinstance(decoded[0], bytes):
                        subject = decoded[0].decode(decoded[1] or "utf-8")
                    else:
                        subject = str(decoded[0])

                if "Typeform" not in subject and "Daily" not in subject and "Log" not in subject:
                    continue

                body_text = ""
                body_html = ""
                email_attachments = []

                if msg.is_multipart():
                    for part in msg.get_payload():
                        content_type = part.get_content_type()
                        content_disp = str(part.get("Content-Disposition"))

                        if content_type == "text/plain":
                            body_text += part.get_payload(decode=True).decode(errors="ignore")
                        elif content_type == "text/html":
                            body_html += part.get_payload(decode=True).decode(errors="ignore")
                        elif "attachment" in content_disp or content_type.startswith("image/"):
                            filename = part.get_filename()
                            if filename:
                                email_attachments.append((filename, part.get_payload(decode=True)))
                else:
                    body_text = msg.get_payload(decode=True).decode(errors="ignore")

                log_date, log_name, job_wo = parse_metadata_from_email(subject, body_text or body_html)

                target_folder_path = get_year_subfolder(log_date)
                existing_dropbox_files = get_existing_dropbox_files(dbx, target_folder_path)

                # 1. PDF
                parts = [log_date, log_name]
                if job_wo:
                    parts.append(job_wo)
                pdf_filename = " | ".join(parts) + ".pdf"

                if pdf_filename not in existing_dropbox_files:
                    print(f"\n📄 Generating PDF for Log: {pdf_filename}")
                    clean_text = clean_email_body_for_pdf(body_text or body_html)
                    pdf_bytes = generate_pdf_from_text(pdf_filename.replace('.pdf', ''), clean_text)

                    dest_pdf_path = f"{target_folder_path}/{pdf_filename}"
                    try:
                        dbx.files_upload(pdf_bytes, dest_pdf_path, mode=dropbox.files.WriteMode.overwrite)
                        print(f"✅ Uploaded PDF Log: {dest_pdf_path}")
                        existing_dropbox_files.add(pdf_filename)
                        summary["pdfs_uploaded"] += 1
                    except Exception as pdf_err:
                        print(f"⚠️ PDF upload error: {pdf_err}")
                else:
                    print(f"Skipping PDF Log (already in Dropbox): {pdf_filename}")
                    summary["skipped"] += 1

                # 2. Photos
                photo_items = []
                for att_name, att_bytes in email_attachments:
                    ext = os.path.splitext(att_name)[1].lower()
                    if ext in ['.jpg', '.jpeg', '.png', '.heic', '.webp']:
                        photo_items.append(('bytes', ext, att_bytes))

                if body_html:
                    html_urls = extract_photo_urls_from_html(body_html)
                    for url in html_urls:
                        ext = os.path.splitext(url)[1].lower() or ".jpeg"
                        photo_items.append(('url', ext, url))

                for idx, (p_type, ext, content) in enumerate(photo_items, start=1):
                    photo_ext = ".jpeg" if ext in ['.jpg', '.jpeg', '.png'] else ext

                    photo_parts = [log_date, log_name]
                    if job_wo:
                        photo_parts.append(job_wo)
                    photo_parts.append(f"Image{idx}")

                    photo_filename = " | ".join(photo_parts) + photo_ext

                    if photo_filename in existing_dropbox_files:
                        continue

                    print(f"🖼️ Uploading site photo: {photo_filename}")
                    try:
                        if p_type == 'bytes':
                            img_bytes = content
                        else:
                            resp = requests.get(content, timeout=20)
                            img_bytes = resp.content if resp.status_code == 200 else None

                        if img_bytes:
                            dest_photo_path = f"{target_folder_path}/{photo_filename}"
                            dbx.files_upload(img_bytes, dest_photo_path, mode=dropbox.files.WriteMode.overwrite)
                            print(f"✅ Photo uploaded: {dest_photo_path}")
                            existing_dropbox_files.add(photo_filename)
                            summary["photos_uploaded"] += 1
                    except Exception as img_err:
                        print(f"⚠️ Photo upload error: {img_err}")

    mail.logout()
    print("\nGmail scan and upload complete!")
    return summary


if __name__ == "__main__":
    result = run_gmail_djl_uploader()
    print(result)
