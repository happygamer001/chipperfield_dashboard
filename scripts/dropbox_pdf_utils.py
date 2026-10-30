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
import json
import time
import datetime
import dropbox
from concurrent.futures import ThreadPoolExecutor, as_completed
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import djl_titles


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
        # Same order as the local pipeline script: date | name | job | ImageN
        photo_parts = [log_date_yymmdd, log_name]
        if job_wo:
            photo_parts.append(job_wo)
        photo_parts.append(f"Image{idx}")
        return " | ".join(photo_parts) + ext

    return pdf_filename, photo_filename


def upload_pdf_and_photos(dbx, log_date_yymmdd, log_name, job_wo, pdf_text_content, pdf_title, photo_bytes_list,
                          full_text=None, rebuild=False):
    """
    photo_bytes_list: list of raw bytes for each photo (already downloaded).
    Returns a summary dict.

    TWO DIFFERENT TEXTS:
      full_text        -> the PDF. The complete log (crew, date, job #, weather,
                          times, equipment, everything). This is the record
                          Jacque and billing use - it must never be the summary.
      pdf_text_content -> the .txt sidecar next to the PDF. The short
                          Work: / Productivity issues: summary the dashboard
                          shows inline for Calvin. (Name kept for backward
                          compatibility with existing callers.)
    If full_text isn't given, the PDF falls back to pdf_text_content.
    rebuild=True overwrites an existing PDF/.txt with the same name.
    """
    full_text = full_text or pdf_text_content
    target_folder_path = get_year_subfolder(log_date_yymmdd)
    existing_files = get_existing_dropbox_files(dbx, target_folder_path)
    pdf_filename, photo_filename_fn = build_filenames(log_date_yymmdd, log_name, job_wo)
    txt_filename = pdf_filename[:-4] + ".txt"  # same base name as the PDF

    summary = {"pdf_uploaded": False, "photos_uploaded": 0, "pdf_path": None}

    if pdf_filename not in existing_files or rebuild:
        pdf_bytes = generate_pdf_from_text(pdf_title, full_text)
        dest_pdf_path = f"{target_folder_path}/{pdf_filename}"
        dbx.files_upload(pdf_bytes, dest_pdf_path, mode=dropbox.files.WriteMode.overwrite)
        summary["pdf_uploaded"] = True
        summary["pdf_path"] = dest_pdf_path
        existing_files.add(pdf_filename)

        # Dashboard summary (not the full log) - in the summary folder, not next to the PDF
        try:
            summary_year = target_folder_path.rsplit("/", 1)[-1]
            summary_dir = f"{SUMMARY_FOLDER}/{summary_year}" if summary_year.isdigit() else SUMMARY_FOLDER
            dest_txt_path = f"{summary_dir}/{txt_filename}"
            dbx.files_upload((pdf_text_content or full_text).encode("utf-8"), dest_txt_path, mode=dropbox.files.WriteMode.overwrite)
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

DAYS_TO_SHOW_DEFAULT = 7

# Short summaries the dashboard shows (written by the office PC script, same file
# name as the PDF with .txt). Kept out of the Daily Job Logs folders on purpose -
# those hold only the full PDFs and photos.
SUMMARY_FOLDER = os.environ.get(
    "DJL_SUMMARY_FOLDER", "/Chipperfield Ag/Chipperfield/Dashboard/Daily Log Summaries"
)

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


MAX_RANGE_DAYS = 93  # longest date range one request may ask for (about 3 months)


def get_recent_daily_logs(days=DAYS_TO_SHOW_DEFAULT, start=None, end=None, keep=None, max_days=None):
    """Logs from the last `days` days, or - when start/end (datetime.date) are
    given - from start through end inclusive. Calvin's date filter uses the
    latter. A range longer than MAX_RANGE_DAYS (or max_days) is cut to the
    last that many days.
    keep: optional test on each log ({"name", "job_or_wo", ...}); logs it
    rejects are dropped BEFORE summaries are read, so a narrow filter (e.g.
    only Fab Shop logs) stays fast over long ranges."""
    dbx = get_dropbox_client()

    today = datetime.date.today()
    if start or end:
        end_date = min(end or today, today)
        window_start = start or (end_date - datetime.timedelta(days=days - 1))
        if window_start > end_date:
            window_start, end_date = end_date, window_start
        window_start = max(window_start, end_date - datetime.timedelta(days=(max_days or MAX_RANGE_DAYS) - 1))
    else:
        end_date = today
        window_start = today - datetime.timedelta(days=days - 1)
    years_to_check = list(range(window_start.year, end_date.year + 1))

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
            if not log_date or log_date < window_start or log_date > end_date:
                continue

            if parsed["is_image"]:
                # Held for a second pass — needs every report's key known
                # first so it can be matched regardless of segment order.
                photo_files.append((entry, parsed, log_date))
                continue

            # PDF/TXT: this convention has always been "name, then job/WO"
            segs = parsed["segments"]
            name = segs[0]
            raw_job_wo = segs[1] if len(segs) > 1 else ""
            # Titles written under the old rule (any non-516 5-digit number
            # labeled J) are shown under the current rule (5 digits = WO).
            job_wo = djl_titles.normalize_identifier(raw_job_wo)
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
                existing_pdf = entries_by_key[key].get("dropbox_pdf_path")
                # Same log under both an old-rule and a new-rule title (e.g.
                # J21764 and WO21764): show one card, keep the new-rule file.
                if existing_pdf and entries_by_key[key].get("_raw_job") == job_wo:
                    continue
                if existing_pdf and raw_job_wo != job_wo:
                    continue
                entries_by_key[key]["_raw_job"] = raw_job_wo
                entries_by_key[key]["pdf_id"] = getattr(entry, "id", None)
                entries_by_key[key]["pdf_rev"] = getattr(entry, "rev", None)
                entries_by_key[key]["dropbox_pdf_path"] = entry.path_display
                # Dropbox's own upload timestamp for this file — used to
                # match "Unknown"-named entries to the right CSV row by
                # closest submit time when a title correction is imported.
                if getattr(entry, "client_modified", None):
                    entries_by_key[key]["uploaded_at"] = entry.client_modified.isoformat() + "Z"
            elif ext == "txt":
                if not entries_by_key[key].get("dropbox_txt_path") or raw_job_wo == job_wo:
                    entries_by_key[key]["dropbox_txt_path"] = entry.path_display

    # Second pass: match each photo to its report by comparing segments as
    # an unordered set — correctly handles both the old photo convention
    # (Name, then Job/WO) and the new one (Job/WO, then Name) without
    # needing to know which is which, and without ever misreading the
    # "ImageN" suffix as a job/WO value the way the old single regex did.
    for entry, parsed, log_date in photo_files:
        segs = [djl_titles.normalize_identifier(x) for x in parsed["segments"]]
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

    # Summaries: Dashboard/Daily Log Summaries/<year>. Matched to a log by the
    # same date / name / job key as its PDF; preferred over an old .txt that
    # may still sit next to the PDF. A summary with no matching PDF is ignored.
    for year in years_to_check:
        try:
            res = dbx.files_list_folder(f"{SUMMARY_FOLDER}/{year}")
            summary_entries = list(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                summary_entries.extend(res.entries)
        except Exception:
            continue
        for entry in summary_entries:
            parsed = _parse_dropbox_filename(entry.name)
            if not parsed or parsed["is_image"] or parsed["ext"] != "txt":
                continue
            segs = parsed["segments"]
            key = (parsed["date"], segs[0],
                   djl_titles.normalize_identifier(segs[1] if len(segs) > 1 else ""))
            if key in entries_by_key:
                entries_by_key[key]["dropbox_txt_path"] = entry.path_display

    results = list(entries_by_key.values())
    if keep:
        results = [r for r in results if keep(r)]
    results.sort(key=lambda r: r["date"], reverse=True)

    # No Dropbox links are made here any more. PDFs and photos are opened on
    # click through /api/log-links, so loading the dashboard is one folder
    # listing plus a cached-text lookup instead of one API call per file.
    _attach_summaries(dbx, results)

    for r in results:
        r.pop("_raw_job", None)
        r["has_pdf"] = bool(r.get("dropbox_pdf_path"))
        r["photo_paths"] = sorted(r.get("photo_paths", []))
        r["photo_count"] = len(r["photo_paths"])
        r.pop("pdf_id", None)
        r.pop("pdf_rev", None)

    return results


def list_log_index(years):
    """
    Every Daily Job Log PDF in the given year folders, read from the FILE NAMES
    only (no downloads): [{"date": "YYYY-MM-DD", "name", "job_or_wo", "path"}].
    Cheap enough to scan all years - used by the calendar to place job numbers
    on the days they were logged and to count how long each job has run.
    """
    dbx = get_dropbox_client()
    out = []
    for year in years:
        try:
            res = dbx.files_list_folder(f"{DROPBOX_BASE_FOLDER}/{year}")
            entries = list(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                entries.extend(res.entries)
        except Exception:
            continue
        for entry in entries:
            parsed = _parse_dropbox_filename(entry.name)
            if not parsed or parsed["is_image"] or parsed["ext"] != "pdf":
                continue
            log_date = _yymmdd_to_date(parsed["date"])
            if not log_date:
                continue
            segs = parsed["segments"]
            out.append({
                "date": log_date.isoformat(),
                "name": segs[0],
                "job_or_wo": djl_titles.normalize_identifier(segs[1] if len(segs) > 1 else ""),
                "path": entry.path_display,
            })
    return out


# ==========================================
# SAVED LOG TEXT (read each PDF once, remember it)
# ==========================================

LOG_TEXT_CACHE_PATH = os.environ.get(
    "LOG_TEXT_CACHE_PATH", "/Chipperfield Ag/Chipperfield/Dashboard/log_text_cache.json"
)
MAX_NEW_EXTRACTIONS = int(os.environ.get("MAX_NEW_EXTRACTIONS", "12"))
MAX_SUMMARY_READS = int(os.environ.get("MAX_SUMMARY_READS", "250"))
EXTRACTION_BUDGET_SECONDS = 20
MAX_CACHE_ENTRIES = 3000


def _read_cache(dbx):
    try:
        _, res = dbx.files_download(LOG_TEXT_CACHE_PATH)
        data = json.loads(res.content.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(dbx, cache):
    try:
        if len(cache) > MAX_CACHE_ENTRIES:
            newest = sorted(cache.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[:MAX_CACHE_ENTRIES]
            cache = dict(newest)
        dbx.files_upload(json.dumps(cache).encode("utf-8"), LOG_TEXT_CACHE_PATH,
                         mode=dropbox.files.WriteMode.overwrite)
    except Exception:
        pass  # cache is an optimization only


def _strip_title_line(text):
    """The PDF's first line repeats the filename ('261007 | Jon | WO21787')."""
    lines = (text or "").strip().split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r'^\s*(\d{6}|No_Date)\s*\|', lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


def _extract_log_text(dbx, r):
    """Text for one log: sidecar .txt if present, else the PDF's own text."""
    if r.get("dropbox_txt_path"):
        try:
            _, res = dbx.files_download(r["dropbox_txt_path"])
            text = res.content.decode("utf-8", errors="ignore").strip()
            if text:
                return text
        except Exception:
            pass
    if r.get("dropbox_pdf_path"):
        try:
            _, res = dbx.files_download(r["dropbox_pdf_path"])
            return _strip_title_line(extract_text_from_pdf_bytes(res.content))
        except Exception:
            return None
    return None


def _attach_summaries(dbx, results):
    """Sets r['summary'] from the saved-text cache (keyed by Dropbox file id,
    so a rename doesn't lose it). Anything not cached yet is read once, in
    parallel, within a time budget; the rest fill in on later loads/cron."""
    cache = _read_cache(dbx)
    missing = []
    for r in results:
        r["summary"] = None
        fid = r.get("pdf_id")
        if not fid:
            if r.get("dropbox_txt_path"):
                missing.append(r)  # sidecar-only: read, don't cache
            continue
        c = cache.get(fid)
        if c and c.get("rev") == r.get("pdf_rev") and c.get("text"):
            r["summary"] = c["text"]
        else:
            missing.append(r)

    if not missing:
        return

    changed = False
    ex = ThreadPoolExecutor(max_workers=8)
    # Reading a small summary .txt is cheap, so up to MAX_SUMMARY_READS of those
    # per load (enough for a long date range). Pulling text out of a PDF is
    # slow, so those stay capped at MAX_NEW_EXTRACTIONS; the rest fill in later.
    from_txt = [r for r in missing if r.get("dropbox_txt_path")][:MAX_SUMMARY_READS]
    from_pdf = [r for r in missing if not r.get("dropbox_txt_path")][:MAX_NEW_EXTRACTIONS]
    futures = {ex.submit(_extract_log_text, dbx, r): r for r in from_txt + from_pdf}
    deadline = time.time() + EXTRACTION_BUDGET_SECONDS
    try:
        for fut in as_completed(futures, timeout=max(1, deadline - time.time())):
            r = futures[fut]
            try:
                text = fut.result()
            except Exception:
                text = None
            if text:
                r["summary"] = text
                if r.get("pdf_id"):
                    cache[r["pdf_id"]] = {"rev": r.get("pdf_rev"), "text": text, "date": r.get("date", "")}
                    changed = True
    except Exception:
        pass  # time budget hit - remaining logs are picked up next time
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    if changed:
        _write_cache(dbx, cache)


# ==========================================
# OPENING FILES ON CLICK
# ==========================================

def _safe_log_path(path):
    """Only files inside the Daily Job Logs folder may be linked."""
    if not path or ".." in path:
        return False
    return path.lower().startswith(DROPBOX_BASE_FOLDER.lower() + "/")


def get_file_link(dbx, path, mode="view"):
    """
    mode 'view'     permanent link that opens the file in the browser
    mode 'download' permanent link that downloads it
    mode 'temp'     4-hour direct link (used for photos shown inline)
    Permanent links are created once and reused. If Dropbox won't make a
    public one (team policy), falls back to a 4-hour temporary link, which
    needs no Dropbox sign-in. Returns {"url", "kind"}.
    """
    if not _safe_log_path(path):
        raise ValueError("Path not allowed")

    if mode != "temp":
        try:
            link = None
            existing = dbx.sharing_list_shared_links(path=path, direct_only=True).links
            for l in existing:
                if _link_is_public(l):
                    link = l
                    break
            if link is None:
                try:
                    settings = dropbox.sharing.SharedLinkSettings(
                        requested_visibility=dropbox.sharing.RequestedVisibility.public)
                    link = dbx.sharing_create_shared_link_with_settings(path, settings)
                except dropbox.exceptions.ApiError:
                    link = dbx.sharing_create_shared_link_with_settings(path)
            if link is not None and _link_is_public(link):
                return {"url": _with_param(link.url, "dl", "1" if mode == "download" else "0",
                                           raw=(mode == "view")), "kind": "permanent"}
        except Exception:
            pass  # fall through to a temporary link

    url = dbx.files_get_temporary_link(path).link
    return {"url": url, "kind": "temporary"}


def _link_is_public(link):
    try:
        return link.link_permissions.resolved_visibility.is_public()
    except Exception:
        return False


def _with_param(url, key, value, raw=False):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("dl", "raw")]
    if raw:
        q.append(("raw", "1"))
    else:
        q.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def get_file_links(dbx, paths, mode="view"):
    """Several links at once, in parallel. Failed ones come back as None."""
    out = {}

    def one(p):
        try:
            return get_file_link(dbx, p, mode)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for p, res in zip(paths, ex.map(one, paths)):
            out[p] = res
    return out


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
