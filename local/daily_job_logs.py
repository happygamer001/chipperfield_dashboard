"""
Daily Job Logs pipeline  (Chipperfield Ag)

THIS SCRIPT IS THE ONLY THING THAT WRITES DAILY JOB LOGS TO DROPBOX.
    It runs on the office PC (Windows Task Scheduler, see local/README.md). The Chipperfield
    Dashboard only READS the logs:
      Daily Job Logs/<year>/            PDF (full log) + photos   - what Jacque and billing use
      Dashboard/Daily Log Summaries/<year>/   .txt (short summary)  - what Calvin's dashboard shows
    The summaries are kept out of the job log folders on purpose.

HOW TO RUN
    python daily_job_logs.py                     Daily run: log emails from the last DAILY_WINDOW_DAYS days
                                                 (Gmail -> Dropbox, then clean-up steps). Existing PDFs are left alone.
    python daily_job_logs.py rebuild 90          ONE-TIME repair: every log email from the last 90 days is rebuilt,
                                                 overwriting existing PDFs/.txt files with the full log. Old copies
                                                 saved under a different name ("261001 | Craig.pdf") are moved to
                                                 Original Unformatted Files. Photos are not touched.
    python daily_job_logs.py photos 30           Re-reads the last 30 days of log emails and uploads only the photos
                                                 missing from Dropbox. PDFs are not touched.
    python daily_job_logs.py move-summaries      ONE-TIME: moves the old summary .txt files out of the Daily Job Logs
                                                 folders into Dashboard/Daily Log Summaries. Changes no PDFs.
    python daily_job_logs.py preview 10          Look at the last 10 log emails. Uploads NOTHING.
                                                 Saves the PDFs and a title list into the local "preview_output" folder.
    python daily_job_logs.py backfill 2026       List what the 2026 folder WOULD be renamed to. Renames NOTHING.
                                                 Saves backfill_2026.csv for you to review (and edit if needed).
    python daily_job_logs.py apply backfill_2026.csv
                                                 Carries out the renames in a reviewed list.
    python daily_job_logs.py other               List logs where "Other" was picked for the name (2026, then 2025, 2024)
                                                 and the write-in name that should go in the title. Renames NOTHING.
                                                 Saves other_names.csv. Then:  python daily_job_logs.py apply other_names.csv

CREDENTIALS
    No passwords or tokens are stored in this file. Put them in djl_settings.txt next to the script
    (one KEY=value per line) or in environment variables with the same names:
        EMAIL_USER, EMAIL_PASS, (optional) DAILY_WINDOW_DAYS, (optional) REPAIR_DEFAULT_NAME,
        DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
    Keep djl_settings.txt out of any git repo / shared folder.

WHICH EMAILS ARE READ
    Gmail "All Mail" (so archived / filtered emails are found too), opened READ-ONLY - nothing is marked read.
    Only emails whose subject contains the word "log" (Daily Job Log, Daily Management Log, Fab Shop log),
    plus Nic's Equipment Repair form (subject contains "repair").
    Other Typeform forms (job applications, screenings, service requests) are skipped and never uploaded.

TITLE RULES
    YYMMDD | Name | Job or WO numbers          e.g.  261007 | Jon | WO21787, WO21774
    4-digit number  -> J####      5-digit number -> WO#####      several numbers -> all listed, comma separated
    J516 is the default work order placeholder: dropped when a real WO is present, flagged when it is alone.
    Numbers are read ONLY from the "Job number" / work order answers, never from the rest of the log.
    If "Other" is picked for Your Name, whatever was typed in as the name is the name in the title.
    From 2024 onward, anything missing or unusual is marked in the title (NO NAME, NO JOB NUMBER, CHECK ...)
    and listed in a review file in Dropbox:  Daily Job Logs/_Needs Review
    Equipment Repair form:  YYMMDD | Nic | Equipment Repair   (a second repair the same day: Equipment Repair 2)
"""

import imaplib
import email
from email.header import decode_header
import email.utils
import csv
import re
import os
import sys
import tempfile
import io
import datetime
import unicodedata
import base64
import zlib
from urllib.parse import urlparse, parse_qs, unquote

import dropbox
import requests
from bs4 import BeautifulSoup
from PIL import Image
import pytesseract
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ==========================================
# CONFIGURATION
# ==========================================

# Path to Tesseract executable on Windows
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Credentials are NOT stored here. They come from djl_settings.txt or environment variables
# (see CREDENTIALS in the notes at the top). Leave these blank.
EMAIL_USER = ""
EMAIL_PASS = ""
# Daily run looks back this many days (a week of slack in case the PC was off).
# Not a secret; can be overridden with DAILY_WINDOW_DAYS in djl_settings.txt.
DAILY_WINDOW_DAYS = 7

DROPBOX_APP_KEY = ""
DROPBOX_APP_SECRET = ""
DROPBOX_REFRESH_TOKEN = ""

SETTINGS_FILE = os.path.join(SCRIPT_DIR, "djl_settings.txt")

IMAP_SERVER = "imap.gmail.com"

DROPBOX_BASE_FOLDER = "/Chipperfield Ag/Chipperfield/Daily Job Logs"
ARCHIVE_FOLDER_PATH = "/Chipperfield Ag/Chipperfield/Daily Job Logs/Original Unformatted Files"
REVIEW_FOLDER_PATH = "/Chipperfield Ag/Chipperfield/Daily Job Logs/_Needs Review"

# Short summaries for Calvin's dashboard live here (same file name as the PDF, .txt),
# NOT next to the PDFs, so the job log folders hold only PDFs and photos.
SUMMARY_FOLDER_PATH = "/Chipperfield Ag/Chipperfield/Dashboard/Daily Log Summaries"

# Which emails count as logs: the subject must contain the word "log" (Daily Job Log,
# Daily Management Log, the Fab Shop daily log...). Service requests, job applications
# and screenings don't, so they're skipped and never uploaded.
LOG_SUBJECT = re.compile(r'\blogs?\b', re.IGNORECASE)

# Nic's Equipment Repair Typeform. Its subject has "repair" instead of "log". These are
# saved with the job logs as "YYMMDD | Nic | Equipment Repair"; a second repair on the
# same day becomes "Equipment Repair 2", and so on.
REPAIR_SUBJECT = re.compile(r'\brepairs?\b', re.IGNORECASE)
REPAIR_LABEL = "Equipment Repair"
# Name used when the repair form has no name question (change with REPAIR_DEFAULT_NAME).
REPAIR_DEFAULT_NAME = "Nic"

# Jerron's Fab Shop team log has no Job number question; the dashboard's Fab Shop section
# looks for this exact label in the title.
FAB_SHOP_LABEL = "Fab Shop Daily Log"

# Logs dated this year or later get flagged when something is missing or unusual.
FLAG_FROM_YEAR = 2024

# J516 is the default work order placeholder.
PLACEHOLDER_NUMBER = "516"

# Gmail folders tried in order. All Mail includes archived / labelled emails; Inbox is the fallback.
MAILBOXES = ('"[Gmail]/All Mail"', '"[Google Mail]/All Mail"', "INBOX")

# Naming validation (name may contain letters, spaces, . ' -   job part may contain commas)
STRICT_PDF_REGEX = r"^\d{6}\s\|\s[A-Za-z\s\.'\-]+\s\|\s[A-Za-z0-9\s,]+\.pdf$"
STRICT_JPEG_REGEX = r"^\d{6}\s\|\s[A-Za-z\s\.'\-]+(\s\|\s[A-Za-z0-9\s,]+)?\s\|\sImage\d+\.jpeg$"

# Same shape, but also accepts "No_Date" as the date. These are files this script already titled
# itself; the repair steps leave them alone so they are not re-converted on every run.
# (They are already listed in a review file the first time they are made.)
PROCESSED_PDF_REGEX = r"^(\d{6}|No_Date)\s\|\s[A-Za-z\s\.'\-]+\s\|\s[A-Za-z0-9\s,]+\.pdf$"
PROCESSED_JPEG_REGEX = r"^(\d{6}|No_Date)\s\|\s[A-Za-z\s\.'\-]+(\s\|\s[A-Za-z0-9\s,]+)?\s\|\sImage\d+\.jpeg$"

# Question labels on the Daily Job Log form (used to read answers by field).
KNOWN_LABELS = [
    "Was any work completed today not included in the original scope?",
    "What did your team work on today?",
    "Today's temperature", "Service work order?", "Productivity Issues",
    "Today's weather", "Incident Report?", "Others on site?", "Equipment Used",
    "Today's date", "Work order #", "Workorder #", "Site Photos?", "Equipment #",
    "Job number", "Start Time", "Stop Time", "Your Name", "Crew", "Date", "Name",
    "What project did you work on today?", "How long did that take?", "Did you work on any more projects?",
    "Describe additional work", "Who was working on additional work?", "Materials Used", "To do:",
]
KNOWN_LABELS.sort(key=len, reverse=True)

SETTINGS = {}
dbx = None
REVIEW_ROWS = []
SKIPPED_FORMS = []


# ==========================================
# SETTINGS & DROPBOX CONNECTION
# ==========================================

def load_settings():
    values = {
        "EMAIL_USER": EMAIL_USER, "EMAIL_PASS": EMAIL_PASS, "DAILY_WINDOW_DAYS": str(DAILY_WINDOW_DAYS),
        "DROPBOX_APP_KEY": DROPBOX_APP_KEY, "DROPBOX_APP_SECRET": DROPBOX_APP_SECRET,
        "DROPBOX_REFRESH_TOKEN": DROPBOX_REFRESH_TOKEN,
    }
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("EMAIL_USER", "EMAIL_PASS", "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET",
                "DROPBOX_REFRESH_TOKEN", "DAILY_WINDOW_DAYS", "TYPEFORM_API_TOKEN",
                "REPAIR_DEFAULT_NAME"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def require_settings(*keys):
    missing = [k for k in keys if not SETTINGS.get(k)]
    if missing:
        print(f"\n❌ Missing settings: {', '.join(missing)}")
        print(f"   Add them to {SETTINGS_FILE} (one KEY=value per line) or set them as environment variables.")
        sys.exit(1)


def connect_dropbox():
    global dbx
    require_settings("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
    print("Connecting to Dropbox Shared Team Space...")
    dbx_team = dropbox.DropboxTeam(
        oauth2_refresh_token=SETTINGS["DROPBOX_REFRESH_TOKEN"],
        app_key=SETTINGS["DROPBOX_APP_KEY"],
        app_secret=SETTINGS["DROPBOX_APP_SECRET"],
        timeout=300.0
    )
    team_members = dbx_team.team_members_list().members
    my_member_id = team_members[0].profile.team_member_id
    dbx_user = dbx_team.as_user(my_member_id)
    account_info = dbx_user.users_get_current_account()
    root_namespace_id = account_info.root_info.root_namespace_id
    dbx = dbx_user.with_path_root(dropbox.common.PathRoot.root(root_namespace_id))


# ==========================================
# REVIEW LIST
# ==========================================

def add_review(step, file_name, flags, detail=""):
    """Records anything a person needs to look at."""
    if isinstance(flags, (list, tuple)):
        flags = "; ".join(flags)
    REVIEW_ROWS.append({"step": step, "file": file_name, "needs_review": flags, "detail": detail})


def write_csv_bytes(rows, columns):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8-sig")


def save_review_list():
    if not REVIEW_ROWS:
        print("\n✅ Nothing needs review.")
        return
    stamp = datetime.datetime.now().strftime("%y%m%d_%H%M")
    name = f"review_{stamp}.csv"
    data = write_csv_bytes(REVIEW_ROWS, ["step", "file", "needs_review", "detail"])
    with open(os.path.join(SCRIPT_DIR, name), "wb") as f:
        f.write(data)
    where = f"local file {name}"
    try:
        dbx.files_upload(data, f"{REVIEW_FOLDER_PATH}/{name}", mode=dropbox.files.WriteMode.overwrite)
        where = f"Dropbox: Daily Job Logs/_Needs Review/{name}"
    except Exception as err:
        print(f"⚠️ Could not upload the review list to Dropbox ({err}). It is saved next to the script.")
    print(f"\n⚠️ {len(REVIEW_ROWS)} item(s) need review -> {where}")


def report_skipped_forms():
    if not SKIPPED_FORMS:
        return
    print(f"\n⏭️  Skipped {len(SKIPPED_FORMS)} Typeform email(s) that are not job logs (not uploaded):")
    for subject in dedupe(SKIPPED_FORMS)[:10]:
        print(f"     - {subject}")


# ==========================================
# SHARED HELPER FUNCTIONS
# ==========================================

def format_date_for_jacque(raw_date_str):
    """Converts standard dates into Jacque's YYMMDD search format (e.g., '2026-08-14' -> '260814')."""
    if not raw_date_str or raw_date_str == "No_Date":
        return "No_Date"
    raw_date_str = raw_date_str.strip()

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

    return "No_Date"


def year_of(log_date):
    if log_date and re.fullmatch(r'\d{6}', log_date):
        return 2000 + int(log_date[:2])
    return None


def is_flaggable(year):
    """Flags apply from FLAG_FROM_YEAR onward, and whenever the date is unknown."""
    return year is None or year >= FLAG_FROM_YEAR


def dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def already_processed(filename):
    """True for files this script already titled (including No_Date ones)."""
    return bool(re.match(PROCESSED_PDF_REGEX, filename) or re.match(PROCESSED_JPEG_REGEX, filename))


# ---------- reading the log body ----------

def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup.get_text("\n")


FOOTER_PATTERNS = [
    r'Log in to view or download your responses',
    r'^\s*Team Typeform\s*$',
    r'Typeform sent you this email',
]


def clean_log_body(text):
    """
    Keeps only the form answers.
    Removes everything above the first answer (email subject, From/To lines, forwarded-message
    block, "has a new response" line) and everything from the Typeform footer down.
    Returns (clean_text, recognized) - recognized is False when no log answers were found.
    """
    text = (text or "").replace('\r\n', '\n').replace('\r', '\n')
    text = "\n".join(re.sub(r'^\s*(?:>\s?)+', '', line) for line in text.split('\n'))

    cut = len(text)
    for pattern in FOOTER_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            cut = min(cut, m.start())
    text = text[:cut]

    recognized = True
    m = re.search(r'^.*has a new response:?[ \t]*$', text, re.IGNORECASE | re.MULTILINE)
    if m:
        text = text[m.end():]
    else:
        m = re.search(r'^[ \t]*\*?[ \t]*Your\s*Name\b', text, re.IGNORECASE | re.MULTILINE)
        if m:
            text = text[m.start():]
        else:
            recognized = False
            kept = []
            for i, line in enumerate(text.split('\n')):
                if i < 20 and re.match(r'^\s*(From|To|Cc|Bcc|Sent|Subject|Reply-To|Date)\s*:', line, re.IGNORECASE):
                    continue
                if re.match(r'^\s*-{3,}\s*Forwarded message\s*-{3,}', line, re.IGNORECASE):
                    continue
                kept.append(line)
            text = "\n".join(kept)

    return text.strip(), recognized


def _split_known_label(line):
    """If the line starts with a known question, returns (question, rest of line).
    A trailing colon on the question ("Your name:", "Today's date:") is ignored."""
    if line.rstrip().endswith(":") and not line.rstrip().endswith("::"):
        line = line.rstrip()[:-1].rstrip()
    low = line.lower()
    for label in KNOWN_LABELS:
        l = label.lower()
        if low == l:
            return label, ""
        if low.startswith(l) and line[len(l)].isspace():
            return label, line[len(l):].strip()
    return None, ""


def parse_typeform_fields(clean_text):
    """Turns the cleaned log into a list of (question, answer) pairs."""
    lines = clean_text.split('\n')
    bullet_mode = sum(1 for l in lines if re.match(r'^\s*\*\s*\S', l)) >= 3
    fields = []
    current = None

    for raw in lines:
        line = raw.strip()
        if bullet_mode:
            m = re.match(r'^\*(.*)$', line)
            if m:
                label_text = m.group(1).strip().strip('*').strip()
                if not label_text:
                    current = None
                    continue
                label, rest = _split_known_label(label_text)
                current = [label or label_text, [rest] if rest else []]
                fields.append(current)
                continue
        else:
            label, rest = _split_known_label(line)
            if label:
                current = [label, [rest] if rest else []]
                fields.append(current)
                continue
        if current is not None and line:
            current[1].append(line)

    return [(label, "\n".join(answer).strip()) for label, answer in fields]


def get_field(fields, *names):
    wanted = [n.lower() for n in names]
    for label, answer in fields:
        if label.lower() in wanted and answer:
            return answer
    return ""


def has_field(fields, *names):
    wanted = [n.lower() for n in names]
    return any(label.lower() in wanted for label, _ in fields)


OTHER_CHOICE = re.compile(r'^\s*other\b[\s:;,\-\u2013\u2014()]*(.*)$', re.IGNORECASE | re.DOTALL)


def read_name(fields):
    """
    Returns (name, other_selected).
    Normal pick:   the Your Name answer.
    "Other" pick:  whatever was typed in - either right after "Other" in the same answer,
                   or in the separate "name" question further down the form.
    """
    your_name = get_field(fields, "Your Name")
    write_in = get_field(fields, "Name")
    m = OTHER_CHOICE.match(your_name or "")
    if m:
        return (clean_name(m.group(1)) or clean_name(write_in)), True
    return (clean_name(your_name) or clean_name(write_in)), False


def clean_name(raw):
    """Returns a tidy employee name, or '' when there isn't a usable one."""
    if not raw:
        return ""
    first_line = raw.strip().split('\n')[0]
    ascii_name = unicodedata.normalize('NFKD', first_line).encode('ascii', 'ignore').decode()
    name = re.sub(r"[^A-Za-z .'\-]", " ", ascii_name)
    name = re.sub(r'\s+', ' ', name).strip(" -'")
    if len(re.sub(r'[^A-Za-z]', '', name)) < 2:
        return ""
    if name.lower() in ("unknown", "no name", "none", "na", "n a", "other"):
        return ""
    return name


# ---------- job / work order numbers ----------

NUMBER_TOKEN = re.compile(r'(?<![A-Za-z0-9])(?:(WO|J)\s*[#.\-]?\s*|(S))?(\d+)(?![A-Za-z0-9])', re.IGNORECASE)


def build_identifier(number_text, context_text="", year=None):
    """
    Builds the job / work order part of the title from the Job number and work order answers.
      4 digits -> J####      5 digits -> WO#####      several -> comma separated
      J516 (default work order) is dropped when a real WO is present.
    Returns (identifier, flags).
    """
    flaggable = is_flaggable(year)
    flags = []
    text = number_text or ""

    # Dates and clock times typed into the field are not job numbers.
    text = re.sub(r'\b20\d{2}-\d{2}-\d{2}\b', ' ', text)
    text = re.sub(r'\b\d{1,2}[/\.-]\d{1,2}[/\.-](?:20\d{2}|\d{2})\b', ' ', text)
    text = re.sub(r'\b\d{1,2}:\d{2}\b', ' ', text)

    jobs, work_orders, others = [], [], []
    placeholder = False

    for m in NUMBER_TOKEN.finditer(text):
        prefix = (m.group(1) or m.group(2) or "").upper()
        digits = m.group(3)
        if len(digits) < 3:
            continue
        if prefix == "S":
            others.append(f"S{digits}")
            flags.append(f"LEGACY LABEL S{digits}")
            continue
        if digits == PLACEHOLDER_NUMBER and prefix in ("", "J"):
            placeholder = True
            continue
        if len(digits) == 4:
            jobs.append(f"J{digits}")
            if prefix == "WO":
                flags.append(f"WRITTEN AS WO{digits} BUT 4 DIGITS - LABELED J{digits}")
        elif len(digits) == 5:
            work_orders.append(f"WO{digits}")
            if prefix == "J":
                flags.append(f"WRITTEN AS J{digits} BUT 5 DIGITS - LABELED WO{digits}")
        else:
            if prefix == "WO":
                label = f"WO{digits}"
            elif len(digits) == 3:
                label = f"J{digits}"
            else:
                label = f"CHECK {digits}"
            others.append(label)
            flags.append(f"CHECK NUMBER {digits} ({len(digits)} digits)")

    ids = dedupe(jobs) + dedupe(work_orders) + dedupe(others)

    if placeholder and not work_orders:
        ids.insert(0, f"J{PLACEHOLDER_NUMBER}")
        flags.append(f"NO WO - J{PLACEHOLDER_NUMBER} ONLY")

    if ids:
        return ", ".join(ids), (dedupe(flags) if flaggable else [])

    # No numbers at all: fall back to the non-job labels.
    hint = f"{number_text or ''}\n{context_text or ''}"
    if re.search(r'Daily\s*Management\s*Log', hint, re.IGNORECASE):
        return "Daily Management Log", []
    if re.search(r'Equipment\s*Repair', hint, re.IGNORECASE):
        return "Equipment Repair", []
    if re.search(r'\boffice\b', number_text or "", re.IGNORECASE):
        return "Office", []
    if re.search(r'\bshop\b', number_text or "", re.IGNORECASE):
        return "Shop", []

    if flaggable:
        return "NO JOB NUMBER", ["NO JOB NUMBER"]
    return "", []


def looks_like_identifier(part):
    if re.search(r'\d{3,}', part):
        return True
    return bool(re.fullmatch(r'(NO JOB NUMBER|Office|Shop|Equipment Repair(?: \d{1,2})?|Daily Management Log|Fab Shop Daily Log)', part.strip(), re.IGNORECASE))


def split_log_filename(filename):
    """Splits 'YYMMDD | Name | Job | ImageN.ext' into its pieces (any may be '')."""
    stem, ext = os.path.splitext(filename)
    parts = [p.strip() for p in stem.split("|") if p.strip()]
    image_tag = ""
    if parts and re.fullmatch(r'Image\d+', parts[-1], re.IGNORECASE):
        image_tag = parts.pop()

    date = ""
    if parts:
        first = parts[0]
        if re.fullmatch(r'\d{6}', first) or re.fullmatch(r'20\d{2}-\d{2}-\d{2}', first):
            date = format_date_for_jacque(first)
            parts = parts[1:]
        elif first == "No_Date":
            date = "No_Date"
            parts = parts[1:]

    name = ""
    if parts and not looks_like_identifier(parts[0]):
        name = clean_name(parts.pop(0))

    return date, name, " ".join(parts), image_tag, ext


def build_filename(date, name, identifier, image_tag="", ext=".pdf"):
    parts = [p for p in (date, name, identifier, image_tag) if p]
    return " | ".join(parts) + ext


def is_repair_form(subject):
    """Nic's Equipment Repair form: "repair" in the subject and not a log."""
    return bool(REPAIR_SUBJECT.search(subject or "")) and not LOG_SUBJECT.search(subject or "")


def extract_repair_metadata(clean_text, fields, subject, received):
    """Equipment Repair form: the title is always 'YYMMDD | Name | Equipment Repair'."""
    flags = []
    raw_date = get_field(fields, "Date", "Today's date") or next(
        (answer for label, answer in fields if "date" in label.lower() and answer), "")
    log_date = format_date_for_jacque(raw_date)
    if log_date == "No_Date" and received:
        log_date = received.strftime("%y%m%d")          # the day the form was sent
    if log_date == "No_Date":
        flags.append("NO DATE")
    name, _ = read_name(fields)
    if not name:
        name = clean_name(_find(fields, "mechanic", "your name", "name")) or \
            clean_name(SETTINGS.get("REPAIR_DEFAULT_NAME") or REPAIR_DEFAULT_NAME)
    return {
        "date": log_date, "year": year_of(log_date), "name": name, "identifier": REPAIR_LABEL,
        "flags": flags, "clean_text": clean_text, "job_raw": "",
        "has_log_fields": True, "has_job_field": False, "other_selected": False, "form": "repair",
    }


def assign_repair_number(meta, seen):
    """A second Equipment Repair from the same person on the same day gets ' 2', then ' 3'...
    Emails are read oldest first, so the numbers stay the same from one run to the next."""
    if meta.get("form") != "repair":
        return
    key = (meta["date"], meta["name"])
    seen[key] = seen.get(key, 0) + 1
    if seen[key] > 1:
        meta["identifier"] = f"{REPAIR_LABEL} {seen[key]}"


def extract_log_metadata(body_text, subject="", received=None):
    """Reads date, name and job/WO numbers from a log. Returns a dict."""
    clean_text, recognized = clean_log_body(body_text)
    fields = parse_typeform_fields(clean_text)
    if is_repair_form(subject):
        return extract_repair_metadata(clean_text, fields, subject, received)
    flags = []

    # Date
    log_date = format_date_for_jacque(get_field(fields, "Date", "Today's date"))
    if log_date == "No_Date":
        m = re.search(r'(20\d{2}-\d{2}-\d{2})', subject) or re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', clean_text)
        if m:
            log_date = format_date_for_jacque(m.group(1))
    year = year_of(log_date)
    flaggable = is_flaggable(year)
    if log_date == "No_Date":
        flags.append("NO DATE")

    # Name. If "Other" was picked, the typed-in name is the name in the title.
    name, other_selected = read_name(fields)
    if other_selected and not name and flaggable:
        flags.append("OTHER PICKED BUT NO NAME TYPED IN")
    if not name and not other_selected:
        m = re.search(r'Daily Job Log\s*\([^)]*\)\s*([A-Za-z\s\.]+?)\s*\d{4}-\d{2}-\d{2}', subject)
        if m:
            name = clean_name(m.group(1))
    if not name and flaggable and not other_selected:
        flags.append("NO NAME")

    # Job / work order numbers: ONLY from the Job number and work order answers
    job_raw = get_field(fields, "Job number")
    wo_raw = " ".join(answer for label, answer in fields
                      if re.search(r'work\s*order', label, re.IGNORECASE) and re.search(r'\d', answer))
    labels_text = " ".join(label for label, _ in fields)
    identifier, id_flags = build_identifier(f"{job_raw} {wo_raw}", f"{subject}\n{labels_text}", year)
    is_fab_shop = (not has_field(fields, "Job number")
                   and has_field(fields, "What did your team work on today?"))
    if is_fab_shop and identifier in ("NO JOB NUMBER", ""):
        identifier, id_flags = FAB_SHOP_LABEL, []
    flags.extend(id_flags)

    if not recognized:
        flags.append("LOG ANSWERS NOT RECOGNIZED")

    return {
        "date": log_date, "year": year, "name": name, "identifier": identifier,
        "flags": flags, "clean_text": clean_text, "job_raw": f"{job_raw} {wo_raw}".strip(),
        "has_log_fields": has_field(fields, "Job number", "Your Name"),
        "has_job_field": has_field(fields, "Job number"),
        "other_selected": other_selected, "form": "log",
    }


def title_parts_for(meta):
    """Name and job part as they should appear in a title, with flags shown in place of gaps."""
    name = meta["name"] or ("NO NAME" if is_flaggable(meta["year"]) else "")
    return name, meta["identifier"]


# ---------- Dropbox helpers ----------

def get_year_subfolder(log_date_yymmdd):
    if log_date_yymmdd and len(log_date_yymmdd) == 6 and log_date_yymmdd.isdigit():
        full_year = f"20{log_date_yymmdd[:2]}"
        return f"{DROPBOX_BASE_FOLDER}/{full_year}"
    return DROPBOX_BASE_FOLDER


def list_folder_files(folder_path):
    """All files (not subfolders) in a Dropbox folder."""
    res = dbx.files_list_folder(folder_path)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)
    return [e for e in entries if isinstance(e, dropbox.files.FileMetadata)]


_FOLDER_CACHE = {}


def get_existing_dropbox_files(folder_path):
    if folder_path not in _FOLDER_CACHE:
        try:
            _FOLDER_CACHE[folder_path] = {e.name for e in list_folder_files(folder_path)}
        except dropbox.exceptions.ApiError:
            _FOLDER_CACHE[folder_path] = set()
    return _FOLDER_CACHE[folder_path]


def safe_dropbox_upload(file_bytes, destination_path):
    file_size = len(file_bytes)
    CHUNK_SIZE = 4 * 1024 * 1024

    if file_size <= CHUNK_SIZE:
        dbx.files_upload(file_bytes, destination_path, mode=dropbox.files.WriteMode.overwrite)
    else:
        upload_session_start_result = dbx.files_upload_session_start(file_bytes[:CHUNK_SIZE])
        cursor = dropbox.files.UploadSessionCursor(
            session_id=upload_session_start_result.session_id,
            offset=CHUNK_SIZE
        )
        commit = dropbox.files.CommitInfo(path=destination_path, mode=dropbox.files.WriteMode.overwrite)

        offset = CHUNK_SIZE
        while offset < file_size:
            chunk = file_bytes[offset:offset + CHUNK_SIZE]
            if (file_size - offset) <= CHUNK_SIZE:
                dbx.files_upload_session_finish(chunk, cursor, commit)
            else:
                dbx.files_upload_session_append_v2(chunk, cursor)
                cursor.offset += len(chunk)
            offset += len(chunk)


# ==========================================
# MODULE 1: GMAIL INGESTION
# ==========================================

def generate_pdf_from_text(title, text_content):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('DocTitle', parent=styles['Heading1'], fontSize=16, leading=20, spaceAfter=12)
    body_style = ParagraphStyle('DocBody', parent=styles['Normal'], fontSize=10, leading=14, spaceAfter=6)

    safe_title = title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    story = [Paragraph(safe_title, title_style), Spacer(1, 12)]

    for line in text_content.split('\n'):
        cleaned_line = line.strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        if cleaned_line:
            story.append(Paragraph(cleaned_line, body_style))
        else:
            story.append(Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def decode_subject(msg):
    pieces = []
    for value, charset in decode_header(msg["Subject"] or ""):
        if isinstance(value, bytes):
            pieces.append(value.decode(charset or "utf-8", errors="ignore"))
        else:
            pieces.append(str(value))
    return "".join(pieces)


def is_log_subject(subject):
    """True for log notifications (forwards included) - the subject must contain the word "log" -
    and for the Equipment Repair form ("repair" in the subject)."""
    return bool(LOG_SUBJECT.search(subject or "") or REPAIR_SUBJECT.search(subject or ""))


def received_date(msg):
    """The day the email arrived, in local time (None if the header is unreadable)."""
    try:
        return email.utils.parsedate_to_datetime(msg["Date"]).astimezone().date()
    except Exception:
        return None


def read_email(msg):
    """Returns (subject, body_text, body_html, attachments, received) or None if it is not a log email."""
    subject = decode_subject(msg)
    if not is_log_subject(subject):
        # Other Typeform forms (job applications, screenings...) are noted but never uploaded.
        if "typeform" in subject.lower():
            SKIPPED_FORMS.append(subject)
        return None

    body_text, body_html, attachments = "", "", []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        content_disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        is_attachment = "attachment" in content_disp.lower()

        if content_type == "text/plain" and not is_attachment:
            body_text += payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        elif content_type == "text/html" and not is_attachment:
            body_html += payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        elif (is_attachment or content_type.startswith("image/")) and filename:
            attachments.append((filename, payload))

    return subject, body_text, body_html, attachments, received_date(msg)


PHOTO_EXT = re.compile(r'\.(jpe?g|png|heic|heif|webp)$', re.IGNORECASE)


def _unwrap_tracking_link(href):
    """
    Typeform's notification emails wrap every link in a tracking redirect
    (email.followups.typeform.io/c/<compressed data>). The real address is the
    "l" value inside that compressed data - for a photo, something like
    https://api.typeform.com/responses/files/<id>/IMG_1159.jpeg
    Returns the real address, or the href unchanged if it isn't a tracking link.
    """
    if "/c/" not in href or "followups.typeform.io" not in href:
        return href
    try:
        payload = href.split("/c/", 1)[1].split("?", 1)[0]
        raw = zlib.decompress(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).decode("utf-8", "ignore")
        real = parse_qs(raw).get("l", [""])[0]
        return unquote(real) or href
    except Exception:
        return href


def collect_photo_items(body_html, attachments):
    """
    Photos for one log: image attachments, plus every photo link in the email.
    A link counts as a photo when its real address (after unwrapping Typeform's
    tracking redirect) or its link text ends in an image extension, or it points
    at Typeform's file storage. Returns (kind, ext, content) with kind 'bytes'/'url'.
    """
    photo_items, seen = [], set()
    for att_name, att_bytes in attachments:
        ext = os.path.splitext(att_name)[1].lower()
        if PHOTO_EXT.search(att_name or ""):
            photo_items.append(('bytes', ext, att_bytes))

    if body_html:
        soup = BeautifulSoup(body_html, 'html.parser')
        for a_tag in soup.find_all('a', href=True):
            real = _unwrap_tracking_link(a_tag['href'].strip())
            text = a_tag.get_text(" ", strip=True)
            path = urlparse(real).path
            is_photo = (PHOTO_EXT.search(path) or PHOTO_EXT.search(text)
                        or ("typeform" in real.lower() and "/files/" in real.lower()))
            if not is_photo or real in seen:
                continue
            seen.add(real)
            name = os.path.basename(path) if PHOTO_EXT.search(path) else text
            ext = os.path.splitext(name)[1].lower() or ".jpeg"
            photo_items.append(('url', ext, real))
    return photo_items


def _looks_like_image(data):
    return bool(data) and (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
                           or data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc")
                           or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def download_photo(url):
    """
    Downloads one photo. Typeform's file storage normally needs an access token
    (TYPEFORM_API_TOKEN in djl_settings.txt); without one, or if the link has
    expired, this raises with the reason so it lands on the review list instead
    of being skipped silently. Only real image data is accepted.
    """
    token = SETTINGS.get("TYPEFORM_API_TOKEN")
    headers = {"User-Agent": "Chipperfield-DailyJobLogs/1.0"}
    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    if resp.status_code in (401, 403) and token:
        resp = requests.get(url, headers={**headers, "Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Typeform refused the download ({resp.status_code})"
                           + ("" if token else " - add TYPEFORM_API_TOKEN to djl_settings.txt"))
    if resp.status_code != 200:
        raise RuntimeError(f"download failed ({resp.status_code})")
    if not _looks_like_image(resp.content):
        raise RuntimeError(f"not an image (got {resp.headers.get('Content-Type', 'unknown type')}) - link may have expired")
    return resp.content


def open_log_mailbox():
    """Logs in and opens All Mail (fallback: Inbox) READ-ONLY. Returns (mail, mailbox name)."""
    require_settings("EMAIL_USER", "EMAIL_PASS")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
    mail.login(SETTINGS["EMAIL_USER"], SETTINGS["EMAIL_PASS"])
    for box in MAILBOXES:
        try:
            typ, _ = mail.select(box, readonly=True)
        except imaplib.IMAP4.error:
            continue
        if typ == "OK":
            return mail, box.strip('"')
    mail.logout()
    raise RuntimeError("Could not open Gmail (All Mail or Inbox).")


def search_log_email_ids(mail, days, repairs_only=False):
    """Ids of log emails from the last `days` days, oldest first."""
    since = datetime.date.today() - datetime.timedelta(days=days)
    # Gmail narrows it to subjects containing "log" or "repair"; is_log_subject() re-checks each one.
    subjects = "subject:repair OR subject:repairs" if repairs_only else "subject:log OR subject:repair OR subject:repairs"
    gmail_query = f"after:{since:%Y/%m/%d} ({subjects})"
    try:
        # Gmail's own search: only log emails are downloaded, which keeps a 90-day rebuild quick.
        typ, messages = mail.search(None, "X-GM-RAW", f'"{gmail_query}"')
        if typ == "OK":
            return messages[0].split()
    except imaplib.IMAP4.error:
        pass
    typ, messages = mail.search(None, f'(SINCE "{since:%d-%b-%Y}")')
    return messages[0].split()


def fetch_log_emails(limit=None, days=None, repairs_only=False):
    """Yields parsed log emails, oldest first (so repair numbers stay stable).
    With limit (preview), only the most recent ones, newest first."""
    days = days or int(SETTINGS.get("DAILY_WINDOW_DAYS") or DAILY_WINDOW_DAYS)
    mail, mailbox = open_log_mailbox()
    email_ids = search_log_email_ids(mail, days, repairs_only)
    if limit:
        email_ids = list(reversed(email_ids))
    print(f"Searching {mailbox}, last {days} day(s): {len(email_ids)} candidate email(s).")

    found = 0
    try:
        for e_id in email_ids:
            _, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    parsed = read_email(email.message_from_bytes(response_part[1]))
                    if parsed and repairs_only and not is_repair_form(parsed[0]):
                        parsed = None
                    if parsed:
                        found += 1
                        yield parsed
            if limit and found >= limit:
                break
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    if found == 0:
        print("No log emails found.")


def _find(fields, *needles):
    """First non-empty answer whose question contains any of the given words."""
    for label, answer in fields:
        low = label.lower()
        if answer and any(n in low for n in needles):
            return answer.strip()
    return ""


def _meaningful(text):
    return text if text and text.strip().lower() not in ("no", "n/a", "na", "none", "-", "0") else ""


def build_dashboard_summary(clean_text):
    """
    The short text the dashboard shows Calvin (saved as a .txt in Dashboard/Daily Log Summaries).
    The PDF itself always holds the complete log - this is only the at-a-glance view.
    """
    fields = parse_typeform_fields(clean_text)
    lines = []

    work = _find(fields, "what did your team work on today")
    if work:
        if not any(label.lower() == "job number" for label, _ in fields):   # Fab Shop log
            start, stop = _find(fields, "start time"), _find(fields, "stop time")
            if start or stop:
                lines.append(f"Hours: {start or '?'} - {stop or '?'}")
        lines.append(f"Work: {work}")
        materials = _meaningful(_find(fields, "materials used"))
        if materials:
            lines.append(f"Materials used: {materials}")
        for label, needles in (("Productivity issues", ("productivity issues",)),
                               ("Incident", ("incident report", "safety incident")),
                               ("To do", ("to do",)),
                               ("Additional work", ("describe additional work",))):
            value = _meaningful(_find(fields, *needles))
            if value:
                lines.append(f"{label}: {value}")
        return "\n".join(lines)

    # Daily Management Log: project / what was done / how long / next action, repeated.
    current = None
    for label, answer in fields:
        low = label.lower()
        if "project" in low and "any more" not in low and "did you do" not in low:
            current = {"project": answer, "did": "", "how_long": "", "next": ""}
            lines.append(current)
        elif current is not None and "did you do" in low:
            current["did"] = answer
        elif current is not None and "how long" in low:
            current["how_long"] = answer
        elif current is not None and "next" in low:
            current["next"] = _meaningful(answer)
    projects = [p for p in lines if isinstance(p, dict) and (p["project"] or p["did"])]
    if projects:
        out = []
        for p in projects:
            line = f"{p['project'] or 'Project'}: {p['did']}"
            if p["how_long"]:
                line += f" ({p['how_long']})"
            if p["next"]:
                line += f" - Next: {p['next']}"
            out.append(line)
        return "\n".join(out)

    return clean_text


def build_repair_summary(clean_text):
    """Dashboard summary for an Equipment Repair form: one 'Question: answer' line per answer."""
    lines = []
    for label, answer in parse_typeform_fields(clean_text):
        if label.lower() in ("your name", "name", "date", "today's date") or not _meaningful(answer):
            continue
        lines.append(f"{label.rstrip(':?').strip()}: {' '.join(answer.split())}")
    return "\n".join(lines) or clean_text


def get_summary_folder(log_date_yymmdd):
    """Dashboard/Daily Log Summaries/<year> (or the folder itself for No_Date logs)."""
    if log_date_yymmdd and len(log_date_yymmdd) == 6 and log_date_yymmdd.isdigit():
        return f"{SUMMARY_FOLDER_PATH}/20{log_date_yymmdd[:2]}"
    return SUMMARY_FOLDER_PATH


def ensure_folder(path):
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError:
        pass  # already exists


def rename_summary(old_pdf_name, new_pdf_name):
    """Keeps a log's dashboard summary matched to its PDF after the PDF is renamed."""
    old_date = split_log_filename(old_pdf_name)[0]
    new_date = split_log_filename(new_pdf_name)[0]
    src = f"{get_summary_folder(old_date)}/{old_pdf_name[:-4]}.txt"
    dst = f"{get_summary_folder(new_date)}/{new_pdf_name[:-4]}.txt"
    if src == dst:
        return
    try:
        dbx.files_move_v2(src, dst, autorename=False)
    except dropbox.exceptions.ApiError:
        pass  # no summary for this log (older logs) - the dashboard falls back to the PDF text


def archive_old_copy(folder, old_pdf_name, existing_files):
    """Moves an old copy (and its .txt) into Original Unformatted Files. Returns True if moved."""
    moved = False
    for old in (old_pdf_name, old_pdf_name[:-4] + ".txt"):
        if old in existing_files:
            try:
                dbx.files_move_v2(f"{folder}/{old}", f"{ARCHIVE_FOLDER_PATH}/{old}", autorename=True)
                existing_files.discard(old)
                moved = True
            except Exception as err:
                add_review("Rebuild", old, "OLD COPY NOT ARCHIVED", str(err))
    return moved


def upload_photos(meta, name_part, job_part, folder, existing_files, photo_items, counts):
    """Uploads a log's photos that aren't in Dropbox yet; every failure goes on the review list."""
    for idx, (p_type, ext, content) in enumerate(photo_items, start=1):
        ext = ".jpeg" if ext in (".jpg", ".jpeg", "") else ext
        names = [build_filename(meta["date"], name_part, job_part, f"Image{idx}", e) for e in (ext, ".jpeg")]
        if any(n in existing_files for n in names):
            continue
        photo_filename = names[0]
        try:
            img_bytes = content if p_type == 'bytes' else download_photo(content)
            safe_dropbox_upload(img_bytes, f"{folder}/{photo_filename}")
            existing_files.add(photo_filename)
            counts["photos"] = counts.get("photos", 0) + 1
            print(f"🖼️ Uploaded Site Photo: {photo_filename}")
        except Exception as err:
            counts["photo failures"] = counts.get("photo failures", 0) + 1
            print(f"⚠️ Photo not saved: {photo_filename} ({err})")
            add_review("Photos", photo_filename, "PHOTO NOT SAVED", str(err))


def run_photo_backfill(days):
    """Re-reads the last `days` days of log emails and uploads only the photos that are
    missing from Dropbox. PDFs and summaries are not touched."""
    print(f"\n==========================================")
    print(f"PHOTO BACKFILL: last {days} days (PDFs untouched)")
    print(f"==========================================")
    counts = {"logs checked": 0, "photos": 0, "photo failures": 0}
    repairs_seen = {}
    for subject, body_text, body_html, attachments, received in fetch_log_emails(days=days):
        try:
            source_text = body_text if body_text.strip() else html_to_text(body_html)
            meta = extract_log_metadata(source_text, subject, received)
            assign_repair_number(meta, repairs_seen)
            name_part, job_part = title_parts_for(meta)
            folder = get_year_subfolder(meta["date"])
            counts["logs checked"] += 1
            upload_photos(meta, name_part, job_part, folder, get_existing_dropbox_files(folder),
                          collect_photo_items(body_html, attachments), counts)
        except Exception as err:
            print(f"❌ Could not process email: {subject} ({err})")
            add_review("Photos", subject, "EMAIL FAILED TO PROCESS", str(err))
    print("\nPhotos: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def run_gmail_djl_uploader(days=None, rebuild=False, repairs_only=False):
    print(f"\n==========================================")
    if repairs_only:
        print(f"EQUIPMENT REPAIR FORMS: last {days} days (logs untouched)")
    else:
        print(f"STEP 1: INGESTING GMAIL LOGS & PHOTOS" + (f"  (REBUILD: last {days} days)" if rebuild else ""))
    print(f"==========================================")
    counts = {"new": 0, "rebuilt": 0, "already there": 0, "archived": 0, "failed": 0, "photos": 0, "photo failures": 0}

    repairs_seen = {}
    for subject, body_text, body_html, attachments, received in fetch_log_emails(days=days, repairs_only=repairs_only):
        try:
            source_text = body_text if body_text.strip() else html_to_text(body_html)
            meta = extract_log_metadata(source_text, subject, received)
            assign_repair_number(meta, repairs_seen)
            name_part, job_part = title_parts_for(meta)

            target_folder_path = get_year_subfolder(meta["date"])
            existing_files = get_existing_dropbox_files(target_folder_path)

            # PDF (full log) + .txt (dashboard summary)
            pdf_filename = build_filename(meta["date"], name_part, job_part)
            txt_filename = pdf_filename[:-4] + ".txt"
            is_new = pdf_filename not in existing_files

            if is_new or rebuild:
                pdf_bytes = generate_pdf_from_text(pdf_filename.replace('.pdf', ''), meta["clean_text"])
                safe_dropbox_upload(pdf_bytes, f"{target_folder_path}/{pdf_filename}")
                existing_files.add(pdf_filename)
                try:
                    summary = (build_repair_summary(meta["clean_text"]) if meta.get("form") == "repair"
                               else build_dashboard_summary(meta["clean_text"]))
                    safe_dropbox_upload(summary.encode("utf-8"), f"{get_summary_folder(meta['date'])}/{txt_filename}")
                except Exception as err:
                    add_review("Gmail", txt_filename, "DASHBOARD SUMMARY NOT SAVED", str(err))
                counts["new" if is_new else "rebuilt"] += 1
                print(f"📄 {'Uploaded' if is_new else 'Rebuilt'} Log PDF: {pdf_filename}")
                if meta["flags"] and is_new:
                    add_review("Gmail", pdf_filename, meta["flags"], f"Job number as entered: {meta['job_raw'] or '(blank)'}")

                # The old dashboard saved logs it couldn't classify with no job part
                # ("261001 | Craig.pdf"). On a rebuild, archive that copy so it doesn't
                # sit next to the correctly named full version.
                old_name = build_filename(meta["date"], name_part, "")
                if rebuild and old_name != pdf_filename and archive_old_copy(target_folder_path, old_name, existing_files):
                    counts["archived"] += 1
                    print(f"🗄️ Archived old copy: {old_name}")
            else:
                counts["already there"] += 1

            # Site Photos Upload (ImageN labels unchanged; job part follows the PDF title). Never re-uploaded.
            upload_photos(meta, name_part, job_part, target_folder_path, existing_files,
                          collect_photo_items(body_html, attachments), counts)
        except Exception as err:
            counts["failed"] += 1
            print(f"❌ Could not process email: {subject} ({err})")
            add_review("Gmail", subject, "EMAIL FAILED TO PROCESS", str(err))

    print("\nLogs: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


def run_preview(limit=10):
    """Shows what the newest log emails would produce. Uploads nothing."""
    print(f"\nPREVIEW: last {limit} log email(s). Nothing is uploaded.")
    out_dir = os.path.join(SCRIPT_DIR, "preview_output")
    os.makedirs(out_dir, exist_ok=True)
    rows = []

    repairs_seen = {}
    for subject, body_text, body_html, attachments, received in fetch_log_emails(limit=limit):
        source_text = body_text if body_text.strip() else html_to_text(body_html)
        meta = extract_log_metadata(source_text, subject, received)
        assign_repair_number(meta, repairs_seen)
        name_part, job_part = title_parts_for(meta)
        title = build_filename(meta["date"], name_part, job_part)
        photos = len(collect_photo_items(body_html, attachments))

        local_name = re.sub(r'[\\/:*?"<>]', '', title.replace(" | ", " - "))
        with open(os.path.join(out_dir, local_name), "wb") as f:
            f.write(generate_pdf_from_text(title.replace('.pdf', ''), meta["clean_text"]))

        rows.append({"title": title, "needs_review": "; ".join(meta["flags"]),
                     "job_number_as_entered": meta["job_raw"], "photos": photos})
        print(f"  {title}" + (f"   <-- {'; '.join(meta['flags'])}" if meta["flags"] else ""))

    with open(os.path.join(out_dir, "preview_titles.csv"), "wb") as f:
        f.write(write_csv_bytes(rows, ["title", "needs_review", "job_number_as_entered", "photos"]))
    print(f"\nSaved {len(rows)} PDF(s) and preview_titles.csv in: {out_dir}")
    report_skipped_forms()


# ==========================================
# MODULE 2: OCR & IMAGE REPAIR
# ==========================================

def extract_image_from_pdf_or_file(local_path):
    ocr_text = ""
    jpeg_bytes = None
    pdf_bytes = None

    file_ext = os.path.splitext(local_path)[1].lower()

    if file_ext in ['.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.webp']:
        try:
            with Image.open(local_path) as img:
                img_rgb = img.convert('RGB')
                buffer = io.BytesIO()
                img_rgb.save(buffer, format="JPEG", quality=95)
                jpeg_bytes = buffer.getvalue()

                pdf_temp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                pdf_temp_path = pdf_temp.name
                pdf_temp.close()
                img_rgb.save(pdf_temp_path, "PDF", resolution=100.0)

                with open(pdf_temp_path, "rb") as f:
                    pdf_bytes = f.read()

                if os.path.exists(pdf_temp_path):
                    os.remove(pdf_temp_path)

                try:
                    ocr_text = pytesseract.image_to_string(img_rgb)
                except Exception:
                    pass
        except Exception:
            pass

    elif file_ext == '.pdf':
        try:
            reader = PdfReader(local_path)
            for page in reader.pages:
                ocr_text += (page.extract_text() or "") + "\n"
                for img_file_obj in page.images:
                    with Image.open(io.BytesIO(img_file_obj.data)) as img:
                        img_rgb = img.convert('RGB')
                        buffer = io.BytesIO()
                        img_rgb.save(buffer, format="JPEG", quality=95)
                        jpeg_bytes = buffer.getvalue()
                        break

            with open(local_path, "rb") as f:
                pdf_bytes = f.read()

        except Exception:
            pass

    return ocr_text, jpeg_bytes, pdf_bytes


NOT_NAME_WORDS = {"job", "wo", "log", "logs", "daily", "scan", "scanned", "img", "image", "photo", "pdf",
                  "no", "date", "unknown", "name", "number", "copy", "doc", "document", "workorder", "work", "order",
                  "management", "equipment", "repair", "office", "shop"}


def guess_name_from_filename(filename):
    """Best guess at a name from a loosely named file. Returns '' when nothing looks like a name."""
    stem = re.sub(r'\s*\.(jpeg|jpg|png|pdf|tiff|webp)$', '', filename, flags=re.IGNORECASE)
    words = []
    for w in re.split(r'[-–_\|\s]+', stem):
        w = w.strip()
        if not w or re.search(r'\d', w) or w.lower() in NOT_NAME_WORDS:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z.']*", w):
            words.append(w)
    return clean_name(" ".join(dedupe(words)[:2]).title())


def identifier_from_loose_text(text, filename, year):
    """For scans and loosely named files: read job/WO numbers from labelled lines, else the file name."""
    lines = []
    for m in re.finditer(r'(?:Job\s*(?:number|no\.?|#)|Work\s*order\s*#?|Workorder\s*#?)\s*[:#]?\s*([^\n]*)', text or "", re.IGNORECASE):
        lines.append(m.group(1))
    if lines:
        return build_identifier(" ".join(lines), text, year)

    stem = os.path.splitext(filename)[0]
    stem = re.sub(r'\bImage\d+\b', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'^\s*\d{6}\b', ' ', stem)
    return build_identifier(stem, stem, year)


def run_ocr_pdf_conversion():
    print(f"\n==========================================")
    print(f"STEP 2: OCR & IMAGE CONVERSION REPAIR")
    print(f"==========================================")

    for entry in newest_first(list_folder_files(DROPBOX_BASE_FOLDER)):
        filename = entry.name
        file_ext = os.path.splitext(filename)[1].lower()

        # Skip anything this script already titled - including No_Date files, which would
        # otherwise be re-converted and re-uploaded on every run.
        if already_processed(filename):
            continue

        if file_ext in ['.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.webp']:
            from_path = f"{DROPBOX_BASE_FOLDER}/{filename}"
            tmp_file = tempfile.NamedTemporaryFile(suffix=file_ext, delete=False)
            tmp_path = tmp_file.name
            tmp_file.close()

            try:
                _, response = dbx.files_download(from_path)
                with open(tmp_path, "wb") as f:
                    f.write(response.content)

                ocr_text, jpeg_bytes, pdf_bytes = extract_image_from_pdf_or_file(tmp_path)

                image_tag_match = re.search(r'\b(Image\d+)\b', filename, re.IGNORECASE)
                image_tag = image_tag_match.group(1) if image_tag_match else ""

                flags = []
                meta = extract_log_metadata(ocr_text) if ocr_text.strip() else None

                # Date
                raw_date = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', ocr_text) or re.search(r'^\s*(\d{6})\b', filename)
                log_date = format_date_for_jacque(raw_date.group(1) if raw_date else "No_Date")
                year = year_of(log_date)
                flaggable = is_flaggable(year)
                if log_date == "No_Date":
                    flags.append("NO DATE")

                # Name: from the log text if it has one, otherwise a guess from the file name
                log_name = (meta["name"] if meta and meta["has_log_fields"] else "") or guess_name_from_filename(filename)
                if not log_name and flaggable:
                    log_name = "NO NAME"
                    flags.append("NO NAME")

                # Job / work order numbers
                if meta and meta["has_job_field"]:
                    job_wo, id_flags = build_identifier(meta["job_raw"], "", year)
                else:
                    job_wo, id_flags = identifier_from_loose_text(ocr_text, filename, year)
                flags.extend(id_flags)

                out_ext = ".jpeg" if image_tag else ".pdf"
                new_filename = build_filename(log_date, log_name, job_wo, image_tag, out_ext)
                to_path = f"{DROPBOX_BASE_FOLDER}/{new_filename}"

                upload_bytes = jpeg_bytes if (image_tag and jpeg_bytes) else pdf_bytes

                if upload_bytes:
                    safe_dropbox_upload(upload_bytes, to_path)
                    print(f"🔄 Converted & Uploaded: {new_filename}")
                    if flags:
                        add_review("OCR repair", new_filename, flags, f"Original file: {filename}")

                    if from_path != to_path:
                        try:
                            dbx.files_move_v2(from_path, f"{ARCHIVE_FOLDER_PATH}/{filename}", autorename=True)
                        except Exception as err:
                            add_review("OCR repair", filename, "ORIGINAL NOT ARCHIVED", str(err))
                else:
                    add_review("OCR repair", filename, "FILE COULD NOT BE READ", "")
            except Exception as err:
                print(f"❌ Could not convert: {filename} ({err})")
                add_review("OCR repair", filename, "FILE FAILED TO CONVERT", str(err))
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass


# ==========================================
# MODULE 3: METADATA & RENAMING FIXER
# ==========================================

def fix_no_date_files():
    print(f"\n==========================================")
    print(f"STEP 3: FIXING UNFORMATTED METADATA & PIPES")
    print(f"==========================================")

    main_files = [e.name for e in newest_first(list_folder_files(DROPBOX_BASE_FOLDER))]

    for filename in main_files:
        if already_processed(filename):
            continue

        if filename.startswith("No_Date") or "Unknown" in filename or "||" in filename or "| |" in filename or "J516" in filename or re.match(r'^\d{4}-\d{2}-\d{2}', filename):
            main_pdf_path = f"{DROPBOX_BASE_FOLDER}/{filename}"

            date, name, old_id, img_tag, ext = split_log_filename(filename)
            date = date or "No_Date"
            year = year_of(date)
            flaggable = is_flaggable(year)
            flags = ["NO DATE"] if date == "No_Date" else []

            if not name and flaggable:
                name = "NO NAME"
                flags.append("NO NAME")

            identifier, id_flags = build_identifier(old_id, old_id, year)
            flags.extend(id_flags)

            new_filename = build_filename(date, name, identifier, img_tag, ext.lower())

            if new_filename != filename:
                try:
                    dbx.files_move_v2(main_pdf_path, f"{DROPBOX_BASE_FOLDER}/{new_filename}", autorename=True)
                    print(f"🔄 Renamed Metadata: '{filename}' ➔ '{new_filename}'")
                    if flags:
                        add_review("Rename fixer", new_filename, flags, f"Was: {filename}")
                except Exception as err:
                    add_review("Rename fixer", filename, "RENAME FAILED", str(err))


# ==========================================
# MODULE 4: YEAR SUBFOLDER SORTING
# ==========================================

def newest_first(file_entries):
    """Orders Dropbox files so the latest dated names come first."""
    return sorted(file_entries, key=lambda e: e.name, reverse=True)


def sort_files_by_year():
    print(f"\n==========================================")
    print(f"STEP 4: SORTING ROOT FILES INTO YEAR SUBFOLDERS")
    print(f"==========================================")

    for file_meta in newest_first(list_folder_files(DROPBOX_BASE_FOLDER)):
        filename = file_meta.name
        match = re.search(r'^\s*(\d{2})(\d{2})(\d{2})\b', filename)
        if match:
            year_folder = f"20{match.group(1)}"
            to_path = f"{DROPBOX_BASE_FOLDER}/{year_folder}/{filename}"
            try:
                dbx.files_move_v2(file_meta.path_lower, to_path, autorename=True)
                print(f"📁 Moved: '{filename}' ➔ '/Daily Job Logs/{year_folder}/'")
            except Exception as err:
                add_review("Year sort", filename, "MOVE FAILED", str(err))


# ==========================================
# BACKFILL: RETITLE FILES ALREADY IN DROPBOX (one year at a time)
# ==========================================

BACKFILL_COLUMNS = ["folder", "current_name", "new_name", "action", "needs_review", "read_from", "job_number_as_entered"]


def _digits_in(text):
    return re.findall(r'\d{3,}', text or "")


def plan_pdf_title(folder, filename):
    """Works out the corrected title for one existing PDF. Changes nothing."""
    old_date, old_name, old_id, _, ext = split_log_filename(filename)
    flags, read_from, job_raw = [], "file name", ""
    meta = None

    try:
        _, response = dbx.files_download(f"{folder}/{filename}")
        reader = PdfReader(io.BytesIO(response.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if text.strip():
            candidate = extract_log_metadata(text)
            if candidate["has_log_fields"]:
                meta = candidate
    except Exception as err:
        flags.append(f"COULD NOT READ PDF ({type(err).__name__})")

    # Date: the date already in the title is kept; a different date inside the log is flagged
    date = old_date if re.fullmatch(r'\d{6}', old_date or "") else ""
    if meta and meta["date"] != "No_Date":
        if date and meta["date"] != date:
            flags.append(f"DATE IN LOG IS {meta['date']}")
        date = date or meta["date"]
    date = date or "No_Date"
    year = year_of(date)
    flaggable = is_flaggable(year)
    if date == "No_Date":
        flags.append("NO DATE")

    # Name ("Other" picks use the typed-in name)
    name = (meta["name"] if meta else "") or old_name
    other_selected = bool(meta and meta["other_selected"])
    if other_selected and meta["name"]:
        read_from = "Other write-in name"
    if not name and flaggable:
        name = "NO NAME"
        flags.append("OTHER PICKED BUT NO NAME TYPED IN" if other_selected else "NO NAME")

    # Job / work order numbers: from the log's Job number answer when the PDF has one
    if meta and meta["has_job_field"] and meta["job_raw"]:
        read_from = "; ".join(filter(None, [read_from if read_from != "file name" else "", "log text"]))
        job_raw = meta["job_raw"]
        identifier, id_flags = build_identifier(job_raw, "", year)
        old_digits, new_digits = _digits_in(old_id), _digits_in(identifier)
        unexplained = [d for d in old_digits
                       if d != PLACEHOLDER_NUMBER and not any(n.startswith(d) for n in new_digits)]
        if unexplained:
            flags.append(f"TITLE HAD {old_id} BUT LOG SAYS {job_raw}")
    else:
        identifier, id_flags = build_identifier(old_id, old_id, year)
    flags.extend(id_flags)

    if not meta and not old_date:
        return {"folder": folder, "current_name": filename, "new_name": filename, "action": "SKIPPED",
                "needs_review": "TITLE FORMAT NOT RECOGNIZED", "read_from": read_from, "job_number_as_entered": "",
                "_other": False, "_old_name": old_name}

    new_name = build_filename(date, name, identifier, "", ext.lower() or ".pdf")
    return {"folder": folder, "current_name": filename, "new_name": new_name,
            "action": "RENAME" if new_name != filename else "NO CHANGE",
            "needs_review": "; ".join(dedupe(flags)), "read_from": read_from, "job_number_as_entered": job_raw,
            "_other": other_selected, "_old_name": old_name}


def is_other_title(filename):
    """True when the title's name slot says Other (or is missing / NO NAME)."""
    stem = os.path.splitext(filename)[0]
    parts = [p.strip() for p in stem.split("|") if p.strip()]
    if parts and re.fullmatch(r'Image\d+', parts[-1], re.IGNORECASE):
        parts.pop()
    if len(parts) < 2:
        return True
    return parts[1].strip().lower() in ("other", "no name", "unknown") or looks_like_identifier(parts[1])


def plan_folder(folder, only_other=False):
    """Plans renames for one year folder. Changes nothing. Returns the rows."""
    entries = list_folder_files(folder)
    all_names = {e.name for e in entries}
    # Newest first: file names start with YYMMDD, so reverse order runs from the latest date backwards.
    pdfs = sorted((e.name for e in entries if e.name.lower().endswith(".pdf")), reverse=True)
    photos = sorted((e.name for e in entries if re.search(r'\|\s*Image\d+\.(jpe?g|png)$', e.name, re.IGNORECASE)), reverse=True)

    rows, stem_map = [], {}
    for i, filename in enumerate(pdfs, start=1):
        row = plan_pdf_title(folder, filename)
        if i % 25 == 0:
            print(f"  {folder.rsplit('/', 1)[-1]}: checked {i} of {len(pdfs)} PDFs...")
        if only_other and not (row["_other"] or is_other_title(filename)):
            continue
        rows.append(row)
        stem_map[os.path.splitext(filename)[0]] = os.path.splitext(row["new_name"])[0]

    # Photos follow their PDF. The ImageN label is never changed.
    for filename in photos:
        m = re.match(r'^(.*?)\s*\|\s*(Image\d+)(\.\w+)$', filename, re.IGNORECASE)
        new_stem = stem_map.get(m.group(1).strip()) if m else None
        if not new_stem:
            continue
        new_name = f"{new_stem} | {m.group(2)}{m.group(3)}"
        rows.append({"folder": folder, "current_name": filename, "new_name": new_name,
                     "action": "RENAME" if new_name != filename else "NO CHANGE",
                     "needs_review": "", "read_from": "follows its PDF", "job_number_as_entered": ""})

    # Never plan two files onto the same name, or onto a name that already exists.
    planned = {r["current_name"] for r in rows}
    taken = {r["current_name"] for r in rows if r["action"] != "RENAME"} | (all_names - planned)
    for row in rows:
        if row["action"] != "RENAME":
            continue
        if row["new_name"] in taken:
            row["action"] = "CONFLICT"
            row["needs_review"] = "; ".join(filter(None, [row["needs_review"], "A FILE WITH THE NEW NAME ALREADY EXISTS - POSSIBLE DUPLICATE"]))
        else:
            taken.add(row["new_name"])
    return rows


def save_rename_list(rows, csv_name):
    data = write_csv_bytes(rows, BACKFILL_COLUMNS)
    with open(os.path.join(SCRIPT_DIR, csv_name), "wb") as f:
        f.write(data)
    try:
        dbx.files_upload(data, f"{REVIEW_FOLDER_PATH}/{csv_name}", mode=dropbox.files.WriteMode.overwrite)
    except Exception as err:
        print(f"⚠️ Could not copy the list to Dropbox ({err}).")

    counts = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    flagged = sum(1 for r in rows if r["needs_review"])
    print(f"\n{len(rows)} file(s) listed: " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"))
    print(f"{flagged} need review.")
    print(f"List saved as {csv_name} (next to the script and in Daily Job Logs/_Needs Review).")
    print(f"When you are happy with it:  python daily_job_logs.py apply {csv_name}")


def run_backfill_preview(year):
    print(f"\nBACKFILL LIST for {year}. Nothing is renamed.")
    rows = plan_folder(f"{DROPBOX_BASE_FOLDER}/{year}")
    save_rename_list(rows, f"backfill_{year}.csv")


def run_other_names_preview(years=None):
    """Finds logs where "Other" was picked for the name and lists the typed-in name for the title."""
    years = years or [str(y) for y in range(datetime.date.today().year, FLAG_FROM_YEAR - 1, -1)]
    print(f"\nOTHER NAME CHECK for {', '.join(years)}. Nothing is renamed.")
    rows = []
    for year in years:
        folder = f"{DROPBOX_BASE_FOLDER}/{year}"
        try:
            rows.extend(plan_folder(folder, only_other=True))
        except dropbox.exceptions.ApiError:
            print(f"  No {year} folder found - skipped.")
    save_rename_list(rows, "other_names.csv")


def run_move_summaries():
    """One-time: moves summary .txt files out of the Daily Job Logs folders into the summary folder."""
    print(f"\nMOVING SUMMARIES out of Daily Job Logs into {SUMMARY_FOLDER_PATH}")
    folders = [DROPBOX_BASE_FOLDER]
    res = dbx.files_list_folder(DROPBOX_BASE_FOLDER)
    entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        entries.extend(res.entries)
    folders += [e.path_display for e in entries
                if isinstance(e, dropbox.files.FolderMetadata) and re.fullmatch(r'20\d{2}', e.name)]

    moved = replaced = failed = 0
    for folder in folders:
        txts = [e for e in list_folder_files(folder) if e.name.lower().endswith(".txt")]
        if not txts:
            continue
        year = folder.rsplit("/", 1)[-1]
        dest_folder = f"{SUMMARY_FOLDER_PATH}/{year}" if re.fullmatch(r'20\d{2}', year) else SUMMARY_FOLDER_PATH
        ensure_folder(dest_folder)
        print(f"  {folder.rsplit('/', 1)[-1]}: {len(txts)} summary file(s)")
        for e in txts:
            src, dst = f"{folder}/{e.name}", f"{dest_folder}/{e.name}"
            try:
                dbx.files_move_v2(src, dst, autorename=False)
                moved += 1
            except dropbox.exceptions.ApiError:
                # A newer summary with this name is already in the summary folder (written by a
                # recent run) - keep that one and set the old copy aside instead of overwriting.
                try:
                    ensure_folder(f"{SUMMARY_FOLDER_PATH}/_replaced")
                    dbx.files_move_v2(src, f"{SUMMARY_FOLDER_PATH}/_replaced/{e.name}", autorename=True)
                    replaced += 1
                except Exception as err:
                    failed += 1
                    add_review("Move summaries", e.name, "SUMMARY NOT MOVED", str(err))
    print(f"\n{moved} moved, {replaced} older duplicate(s) set aside in _replaced, {failed} failed.")


def run_backfill_apply(csv_path):
    if not os.path.isabs(csv_path):
        csv_path = os.path.join(SCRIPT_DIR, csv_path)
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    todo = [r for r in rows if (r.get("action") or "").strip().upper() == "RENAME"
            and r.get("new_name") and r["new_name"] != r["current_name"]]
    print(f"\nAPPLYING {len(todo)} rename(s) from {os.path.basename(csv_path)}")

    done, results = 0, []
    for row in todo:
        new_name = row["new_name"].strip()
        if re.search(r'[\\/]', new_name):
            row["result"] = "NOT RENAMED - name contains a slash"
        else:
            try:
                dbx.files_move_v2(f"{row['folder']}/{row['current_name']}", f"{row['folder']}/{new_name}", autorename=False)
                if new_name.lower().endswith(".pdf"):
                    rename_summary(row["current_name"], new_name)
                row["result"] = "RENAMED"
                done += 1
                print(f"🔄 '{row['current_name']}' ➔ '{new_name}'")
            except Exception as err:
                row["result"] = f"NOT RENAMED - {type(err).__name__}"
                print(f"❌ '{row['current_name']}' was not renamed ({type(err).__name__})")
        results.append(row)

    out_name = os.path.basename(csv_path).replace(".csv", "_results.csv")
    with open(os.path.join(SCRIPT_DIR, out_name), "wb") as f:
        f.write(write_csv_bytes(results, BACKFILL_COLUMNS + ["result"]))
    print(f"\n{done} of {len(todo)} renamed. Details saved as {out_name}.")


# ==========================================
# MASTER EXECUTION PIPELINE
# ==========================================

def main():
    global SETTINGS
    SETTINGS = load_settings()
    args = sys.argv[1:]
    command = args[0].lower() if args else "run"

    if command == "preview":
        run_preview(int(args[1]) if len(args) > 1 else 10)
        return

    if command == "backfill":
        if len(args) < 2 or not re.fullmatch(r'20\d{2}', args[1]):
            print("Usage: python daily_job_logs.py backfill 2026")
            return
        connect_dropbox()
        run_backfill_preview(args[1])
        return

    if command == "other":
        years = [a for a in args[1:] if re.fullmatch(r'20\d{2}', a)] or None
        connect_dropbox()
        run_other_names_preview(years)
        return

    if command == "photos":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        connect_dropbox()
        run_photo_backfill(days)
        save_review_list()
        return

    if command == "repairs":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        connect_dropbox()
        run_gmail_djl_uploader(days=days, repairs_only=True)
        save_review_list()
        return

    if command == "move-summaries":
        connect_dropbox()
        run_move_summaries()
        save_review_list()
        return

    if command == "apply":
        if len(args) < 2:
            print("Usage: python daily_job_logs.py apply backfill_2026.csv")
            return
        connect_dropbox()
        run_backfill_apply(args[1])
        return

    rebuild, days = False, None
    if command == "rebuild":
        rebuild = True
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 90
        print(f"\n♻️  REBUILD MODE: every log email from the last {days} days will be rebuilt with the full log.")
    elif command != "run":
        print(f"Unknown command '{command}'. See the notes at the top of the script.")
        return

    connect_dropbox()
    print("\n🚀 STARTING DAILY JOB LOGS AUTOMATION PIPELINE...")
    run_gmail_djl_uploader(days=days, rebuild=rebuild)
    run_ocr_pdf_conversion()
    fix_no_date_files()
    sort_files_by_year()
    report_skipped_forms()
    save_review_list()
    print("\n🏁 PIPELINE FINISHED." + (" Check the review list above." if REVIEW_ROWS else ""))


if __name__ == "__main__":
    main()
