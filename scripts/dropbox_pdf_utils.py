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
import datetime
import dropbox
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    parts = [log_date_yymmdd, log_name]
    if job_wo:
        parts.append(job_wo)
    pdf_filename = " | ".join(parts) + ".pdf"

    def photo_filename(idx, ext=".jpeg"):
        # Job/WO comes BEFORE name for photos (opposite of the report
        # filename) — lets Calvin scan filenames to see which job a photo
        # belongs to at a glance without opening it.
        photo_parts = [log_date_yymmdd]
        if job_wo:
            photo_parts.append(job_wo)
        photo_parts.append(log_name)
        photo_parts.append(f"Image{idx}")
        return " | ".join(photo_parts) + ext

    return pdf_filename, photo_filename


def upload_pdf_and_photos(dbx, log_date_yymmdd, log_name, job_wo, pdf_text_content, pdf_title, photo_bytes_list):
    """
    photo_bytes_list: list of raw bytes for each photo (already downloaded).
    Returns a summary dict.

    Also writes a plain-text sidecar next to the PDF (same name, .txt) with
    the same content used to generate the PDF — this is what lets the
    dashboard show "work completed" text inline without anyone having to
    open the PDF. Older entries uploaded before this existed won't have one.
    """
    target_folder_path = get_year_subfolder(log_date_yymmdd)
    existing_files = get_existing_dropbox_files(dbx, target_folder_path)
    pdf_filename, photo_filename_fn = build_filenames(log_date_yymmdd, log_name, job_wo)
    txt_filename = pdf_filename[:-4] + ".txt"  # same base name as the PDF

    summary = {"pdf_uploaded": False, "photos_uploaded": 0, "pdf_path": None}

    if pdf_filename not in existing_files:
        pdf_bytes = generate_pdf_from_text(pdf_title, pdf_text_content)
        dest_pdf_path = f"{target_folder_path}/{pdf_filename}"
        dbx.files_upload(pdf_bytes, dest_pdf_path, mode=dropbox.files.WriteMode.overwrite)
        summary["pdf_uploaded"] = True
        summary["pdf_path"] = dest_pdf_path
        existing_files.add(pdf_filename)

        # Sidecar text file, same content as the PDF
        try:
            dest_txt_path = f"{target_folder_path}/{txt_filename}"
            dbx.files_upload(pdf_text_content.encode("utf-8"), dest_txt_path, mode=dropbox.files.WriteMode.overwrite)
            existing_files.add(txt_filename)
        except Exception:
            pass  # non-critical — the PDF is still there even if this fails

    for idx, photo_bytes in enumerate(photo_bytes_list, start=1):
        filename = photo_filename_fn(idx)
        if filename in existing_files:
            continue
        dest_path = f"{target_folder_path}/{filename}"
        dbx.files_upload(photo_bytes, dest_path, mode=dropbox.files.WriteMode.overwrite)
        summary["photos_uploaded"] += 1
        existing_files.add(filename)

    return summary


# ==========================================
# RECENT LOGS LOOKUP (for the admin dashboard's "last N days" view)
# ==========================================

DAYS_TO_SHOW_DEFAULT = 5

# Filenames look like "YYMMDD | Name | JobOrWO.ext" for reports, and
# "YYMMDD | JobOrWO | Name | ImageN.ext" for photos (job/WO comes first
# for photos specifically, per the naming convention). This is parsed by
# segment position + an Image-suffix check, not one fragile do-everything
# regex — the old regex misread a photo's "| Image1" suffix as the
# job/WO field whenever job_wo was empty, splintering one person's
# report and photos into several bogus separate entries.
_IMAGE_SUFFIX_PATTERN = re.compile(r'^Image(\d+)$', re.IGNORECASE)
_DATE_SEGMENT_PATTERN = re.compile(r'^(\d{6}|No_Date)$')
_VALID_EXTENSIONS = {"pdf", "txt", "jpeg", "jpg", "png", "heic", "webp"}


def _yymmdd_to_date(yymmdd):
    if yymmdd == "No_Date" or len(yymmdd) != 6:
        return None
    try:
        return datetime.date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None


def _parse_dropbox_filename(filename):
    """Returns None if this isn't one of our files. Otherwise a dict with
    date/segments/is_image/image_index/ext. 'segments' holds the 1-2
    plain-text parts (name and/or job/WO) with NO assumption about which
    order they're in — callers decide that based on file type, since PDFs
    and photos use different conventions and old/new photo files may
    coexist during the transition."""
    if "." not in filename:
        return None
    stem, _, ext = filename.rpartition(".")
    ext = ext.lower()
    if ext not in _VALID_EXTENSIONS:
        return None

    segments = [s.strip() for s in stem.split("|")]
    if len(segments) < 2:
        return None

    date_seg = segments[0]
    if not _DATE_SEGMENT_PATTERN.match(date_seg):
        return None

    rest = segments[1:]
    is_image = False
    image_index = None
    if rest:
        m = _IMAGE_SUFFIX_PATTERN.match(rest[-1])
        if m:
            is_image = True
            image_index = int(m.group(1))
            rest = rest[:-1]

    if not rest or len(rest) > 2:
        return None  # malformed — not one of our filenames

    return {
        "date": date_seg,
        "segments": rest,
        "is_image": is_image,
        "image_index": image_index,
        "ext": ext,
    }


def get_recent_daily_logs(days=DAYS_TO_SHOW_DEFAULT):
    dbx = get_dropbox_client()

    today = datetime.date.today()
    window_start = today - datetime.timedelta(days=days - 1)
    years_to_check = sorted({window_start.year, today.year})

    entries_by_key = {}
    photo_files = []  # collected in a second pass, once all reports are known

    for year in years_to_check:
        folder_path = f"{DROPBOX_BASE_FOLDER}/{year}"
        try:
            res = dbx.files_list_folder(folder_path)
            all_entries = list(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                all_entries.extend(res.entries)
        except Exception:
            continue

        for entry in all_entries:
            parsed = _parse_dropbox_filename(entry.name)
            if not parsed:
                continue

            log_date = _yymmdd_to_date(parsed["date"])
            if not log_date or log_date < window_start or log_date > today:
                continue

            if parsed["is_image"]:
                # Held for a second pass — needs every report's key known
                # first so it can be matched regardless of segment order.
                photo_files.append((entry, parsed, log_date))
                continue

            # PDF/TXT: this convention has always been "name, then job/WO"
            segs = parsed["segments"]
            name = segs[0]
            job_wo = segs[1] if len(segs) > 1 else ""
            ext = parsed["ext"]

            key = (parsed["date"], name, job_wo)
            if key not in entries_by_key:
                entries_by_key[key] = {
                    "date": log_date.isoformat(),
                    "name": name,
                    "job_or_wo": job_wo or None,
                    "dropbox_pdf_path": None,
                    "dropbox_txt_path": None,
                    "photo_count": 0,
                    "photo_paths": [],
                    "uploaded_at": None,
                }

            if ext == "pdf":
                entries_by_key[key]["dropbox_pdf_path"] = entry.path_display
                # Dropbox's own upload timestamp for this file — used to
                # match "Unknown"-named entries to the right CSV row by
                # closest submit time when a title correction is imported.
                if getattr(entry, "client_modified", None):
                    entries_by_key[key]["uploaded_at"] = entry.client_modified.isoformat() + "Z"
            elif ext == "txt":
                entries_by_key[key]["dropbox_txt_path"] = entry.path_display

    # Second pass: match each photo to its report by comparing segments as
    # an unordered set — correctly handles both the old photo convention
    # (Name, then Job/WO) and the new one (Job/WO, then Name) without
    # needing to know which is which, and without ever misreading the
    # "ImageN" suffix as a job/WO value the way the old single regex did.
    for entry, parsed, log_date in photo_files:
        segs = parsed["segments"]
        seg_set = frozenset(segs)
        date_str = parsed["date"]

        matched_key = None
        for key in entries_by_key:
            key_date, key_name, key_job = key
            if key_date != date_str:
                continue
            key_set = frozenset(s for s in (key_name, key_job) if s)
            if key_set == seg_set:
                matched_key = key
                break

        if matched_key is None:
            # No matching report (yet) — file it as its own entry rather
            # than dropping the photo. Best-effort naming since we can't
            # be sure which segment is the name vs the job/WO here.
            name = segs[0]
            job_wo = segs[1] if len(segs) > 1 else ""
            matched_key = (date_str, name, job_wo)
            if matched_key not in entries_by_key:
                entries_by_key[matched_key] = {
                    "date": log_date.isoformat(),
                    "name": name,
                    "job_or_wo": job_wo or None,
                    "dropbox_pdf_path": None,
                    "dropbox_txt_path": None,
                    "photo_count": 0,
                    "photo_paths": [],
                    "uploaded_at": None,
                }

        entries_by_key[matched_key]["photo_count"] += 1
        entries_by_key[matched_key]["photo_paths"].append(entry.path_display)

    results = list(entries_by_key.values())
    results.sort(key=lambda r: r["date"], reverse=True)

    # All of this is independent I/O (one Dropbox API call each) that was
    # previously done one at a time — with several entries each having
    # several photos, that serialized into 16-32+ SECOND page loads and
    # was likely starving other concurrent requests on the same server
    # process. Run everything in parallel instead.
    def _fetch_temp_link(path):
        try:
            return dbx.files_get_temporary_link(path).link
        except Exception:
            return None

    def _fetch_txt_content(path):
        try:
            _, res = dbx.files_download(path)
            return res.content.decode("utf-8", errors="ignore").strip()
        except Exception:
            return None

    jobs = []  # (result_dict, kind, extra) — extra is a photo index for photo jobs
    for r in results:
        if r["dropbox_pdf_path"]:
            jobs.append((r, "pdf", None))
        if r["dropbox_txt_path"]:
            jobs.append((r, "txt", None))
        for i, path in enumerate(r.get("photo_paths", [])):
            jobs.append((r, "photo", i))
        r["dropbox_view_url"] = None
        r["summary"] = None
        r["photo_urls"] = [None] * len(r.get("photo_paths", []))

    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_job = {}
        for r, kind, extra in jobs:
            if kind == "pdf":
                future = executor.submit(_fetch_temp_link, r["dropbox_pdf_path"])
            elif kind == "txt":
                future = executor.submit(_fetch_txt_content, r["dropbox_txt_path"])
            else:
                future = executor.submit(_fetch_temp_link, r["photo_paths"][extra])
            future_to_job[future] = (r, kind, extra)

        for future in as_completed(future_to_job):
            r, kind, extra = future_to_job[future]
            value = future.result()
            if kind == "pdf":
                r["dropbox_view_url"] = value
            elif kind == "txt":
                r["summary"] = value
            else:
                r["photo_urls"][extra] = value

    for r in results:
        r["photo_urls"] = [u for u in r["photo_urls"] if u]  # drop any that failed
        del r["photo_paths"]  # internal only — URLs are what the frontend needs

    return results


def extract_text_from_pdf_bytes(pdf_bytes):
    """Extracts raw text from a PDF's pages using pypdf — used by the
    self-correction tool to re-derive a file's real name/date/job from its
    own already-written content, no external CSV needed."""
    from pypdf import PdfReader
    text = ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
    except Exception:
        pass
    return text
