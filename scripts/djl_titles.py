"""
Daily Job Log title rules - copied verbatim from Chicken's daily_job_logs.py
(the local pipeline script), which is the authority for naming. Keeping the
rules in one dependency-free module lets the dashboard server, the Gmail scan
and the webhook all produce exactly the same titles as the local script.

TITLE RULES
    YYMMDD | Name | Job or WO numbers      e.g.  261007 | Jon | WO21787, WO21774
    4-digit number -> J####     5-digit number -> WO#####     several -> all listed
    J516 is the default work order placeholder: dropped when a real WO is
    present, flagged when it is alone.
    Numbers are read ONLY from the "Job number" / work order answers.
    If "Other" is picked for Your Name, the typed-in name is the name.

DO NOT edit the copied functions here without also changing the local script.
"""

import re
import os
import unicodedata

from bs4 import BeautifulSoup

# Logs dated this year or later get flagged when something is missing or unusual.
FLAG_FROM_YEAR = 2024

# J516 is the default work order placeholder.
PLACEHOLDER_NUMBER = "516"

# Question labels on the Daily Job Log form (used to read answers by field).
KNOWN_LABELS = [
    "Was any work completed today not included in the original scope?",
    "What did your team work on today?",
    "Today's temperature", "Service work order?", "Productivity Issues",
    "Today's weather", "Incident Report?", "Others on site?", "Equipment Used",
    "Today's date", "Work order #", "Workorder #", "Site Photos?", "Equipment #",
    "Job number", "Start Time", "Stop Time", "Your Name", "Crew", "Date", "Name",
]
KNOWN_LABELS.sort(key=len, reverse=True)

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
    """If the line starts with a known question, returns (question, rest of line)."""
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
    return bool(re.fullmatch(r'(NO JOB NUMBER|Office|Shop|Equipment Repair|Daily Management Log)', part.strip(), re.IGNORECASE))


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


def extract_log_metadata(body_text, subject=""):
    """Reads date, name and job/WO numbers from a log. Returns a dict."""
    clean_text, recognized = clean_log_body(body_text)
    fields = parse_typeform_fields(clean_text)
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
    flags.extend(id_flags)

    if not recognized:
        flags.append("LOG ANSWERS NOT RECOGNIZED")

    return {
        "date": log_date, "year": year, "name": name, "identifier": identifier,
        "flags": flags, "clean_text": clean_text, "job_raw": f"{job_raw} {wo_raw}".strip(),
        "has_log_fields": has_field(fields, "Job number", "Your Name"),
        "has_job_field": has_field(fields, "Job number"),
        "other_selected": other_selected,
    }


def title_parts_for(meta):
    """Name and job part as they should appear in a title, with flags shown in place of gaps."""
    name = meta["name"] or ("NO NAME" if is_flaggable(meta["year"]) else "")
    return name, meta["identifier"]



# ---------------------------------------------------------------------------
# Dashboard-only helpers (not in the local script)
# ---------------------------------------------------------------------------

def id_tags(number_text):
    """Just the J####/WO#####/S### tags the title rules would produce for
    some free text - used by the form parsers. Labels like 'Office' or
    'NO JOB NUMBER' and 'CHECK ...' items are not tags."""
    ident, _ = build_identifier(number_text or "", "", None)
    tags = []
    for tok in [t.strip() for t in ident.split(",")]:
        if re.fullmatch(r'(J|WO|S)\d+', tok) and tok not in tags:
            tags.append(tok)
    return tags


def normalize_identifier(text):
    """Display-time fix for titles written under the old rule (any non-516
    five-digit number was labeled J). Applies the current rule: 4 digits ->
    J, 5 digits -> WO. Leaves everything else (names, Office, Shop, CHECK...)
    untouched. Does not rename anything in Dropbox."""
    if not text:
        return text

    def fix(m):
        prefix, digits = m.group(1).upper(), m.group(2)
        if prefix == "J" and len(digits) == 5:
            return f"WO{digits}"
        if prefix == "WO" and len(digits) == 4:
            return f"J{digits}"
        return m.group(0)

    return re.sub(r'\b(J|WO)(\d{4,5})\b', fix, text, flags=re.IGNORECASE)
