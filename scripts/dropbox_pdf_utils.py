"""
Shared helpers for turning a Daily Job Log submission into a PDF and
filing it (plus any photos) in Dropbox using the consistent naming
convention: "YYMMDD | Name | JobOrWO.ext"

Used by:
  - api/typeform-webhook.py   (primary path, real-time)
  - scripts/daily_log_uploader.py  (legacy/backup path, email-based)
"""

import os
import re
import io
import dropbox
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DROPBOX_BASE_FOLDER = os.environ.get(
    "DROPBOX_BASE_FOLDER", "/Chipperfield Ag/Chipperfield/Daily Job Logs"
)


def get_dropbox_client():
    dbx_team = dropbox.DropboxTeam(
        oauth2_refresh_token=require_env("DROPBOX_REFRESH_TOKEN"),
        app_key=require_env("DROPBOX_APP_KEY"),
        app_secret=require_env("DROPBOX_APP_SECRET"),
        timeout=300.0,
    )
    team_members = dbx_team.team_members_list().members
    my_member_id = team_members[0].profile.team_member_id
    dbx_user = dbx_team.as_user(my_member_id)

    account_info = dbx_user.users_get_current_account()
    root_namespace_id = account_info.root_info.root_namespace_id

    return dbx_user.with_path_root(dropbox.common.PathRoot.root(root_namespace_id))


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
        mm, dd = mm.zfill(2), dd.zfill(2)
        yy = yy[2:] if len(yy) == 4 else yy
        return f"{yy}{mm}{dd}"

    return raw_date_str


def get_year_subfolder(log_date_yymmdd):
    if log_date_yymmdd and len(log_date_yymmdd) == 6 and log_date_yymmdd.isdigit():
        full_year = f"20{log_date_yymmdd[:2]}"
        return f"{DROPBOX_BASE_FOLDER}/{full_year}"
    return DROPBOX_BASE_FOLDER


def get_existing_dropbox_files(dbx, folder_path):
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


def generate_pdf_from_text(title, text_content):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontSize=16, leading=20, spaceAfter=12)
    body_style = ParagraphStyle('DocBody', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6)

    story = [Paragraph(title, title_style), Spacer(1, 12)]
    for line in text_content.split('\n'):
        cleaned_line = line.strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        story.append(Paragraph(cleaned_line, body_style) if cleaned_line else Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def build_filenames(log_date_yymmdd, log_name, job_wo):
    """Returns (pdf_filename, photo_filename_fn) following the shared naming convention."""
    parts = [log_date_yymmdd, log_name]
    if job_wo:
        parts.append(job_wo)
    pdf_filename = " | ".join(parts) + ".pdf"

    def photo_filename(idx, ext=".jpeg"):
        photo_parts = [log_date_yymmdd, log_name]
        if job_wo:
            photo_parts.append(job_wo)
        photo_parts.append(f"Image{idx}")
        return " | ".join(photo_parts) + ext

    return pdf_filename, photo_filename


def upload_pdf_and_photos(dbx, log_date_yymmdd, log_name, job_wo, pdf_text_content, pdf_title, photo_bytes_list):
    """
    photo_bytes_list: list of raw bytes for each photo (already downloaded).
    Returns a summary dict.
    """
    target_folder_path = get_year_subfolder(log_date_yymmdd)
    existing_files = get_existing_dropbox_files(dbx, target_folder_path)
    pdf_filename, photo_filename_fn = build_filenames(log_date_yymmdd, log_name, job_wo)

    summary = {"pdf_uploaded": False, "photos_uploaded": 0, "pdf_path": None}

    if pdf_filename not in existing_files:
        pdf_bytes = generate_pdf_from_text(pdf_title, pdf_text_content)
        dest_pdf_path = f"{target_folder_path}/{pdf_filename}"
        dbx.files_upload(pdf_bytes, dest_pdf_path, mode=dropbox.files.WriteMode.overwrite)
        summary["pdf_uploaded"] = True
        summary["pdf_path"] = dest_pdf_path
        existing_files.add(pdf_filename)

    for idx, photo_bytes in enumerate(photo_bytes_list, start=1):
        filename = photo_filename_fn(idx)
        if filename in existing_files:
            continue
        dest_path = f"{target_folder_path}/{filename}"
        dbx.files_upload(photo_bytes, dest_path, mode=dropbox.files.WriteMode.overwrite)
        summary["photos_uploaded"] += 1
        existing_files.add(filename)

    return summary
