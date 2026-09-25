"""
SINGLE-FILE, SELF-CONTAINED VERSION — everything in one script, no other
project files needed. Run this from your own terminal to:
  1. Retroactively fix Daily Job Log PDF filenames — re-derives each
     file's real Name/Date/Job straight from its own text content.
  2. Sync Kevin's Daily Management Form entries from Notion into Dropbox
     as PDFs, matching everyone else's format.

Your terminal has no 60-second function limit, so this can work through
the entire archive in one go instead of needing to be batched to survive
Vercel's timeout the way the website's version does.

SETUP (one time):
  1. Install dependencies:
       pip3 install dropbox pypdf reportlab requests beautifulsoup4
  2. Set these environment variables (get the values from Vercel's
     project settings -> Environment Variables). Watch for stray spaces
     inside the quotes when you paste -- that alone will cause an
     "invalid_client" error from Dropbox:
       export DROPBOX_APP_KEY="..."
       export DROPBOX_APP_SECRET="..."
       export DROPBOX_REFRESH_TOKEN="..."
       export NOTION_TOKEN="..."

RUN:
    python3 fix_filenames_standalone.py              # both passes
    python3 fix_filenames_standalone.py --dry-run    # preview only
    python3 fix_filenames_standalone.py --skip-kevin       # filename fix only
    python3 fix_filenames_standalone.py --skip-filenames   # Kevin sync only
"""

import os
import re
import io
import sys
import argparse
import requests
import dropbox
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ============================================================
# ---- form_parsers.py content ----
# ============================================================


# Every field label we've seen across the three known forms (Daily Job
# Log, Management Log, Fab Shop Log) plus real historical PDFs — used so a
# line can be recognized as a field marker even with no bullet character
# at all, or an unfamiliar one. Real files are inconsistent: some lines
# use '*', some use '•', some use nothing.
KNOWN_FIELD_LABELS = [
    "Your Name", "Name", "Date", "Today's date", "Today's weather", "Crew",
    "Today's temperature", "Start Time", "Job number", "Workorder #",
    "Workorder Name", "Service work order?", "Equipment Used", "Equipment #",
    "Mileage to work site", "Workorder Completed", "To do:",
    "What did your team work on today?", "Materials Used",
    "Productivity Issues", "Incident Report?", "Incident Report",
    "Safety Incident", "Others on site?", "On Site", "Stop Time",
    "Was any work completed today not included in the original scope?",
    "Was any work completed today not included in original scope?",
    "Who was working on additional work?", "Describe additional work",
    "How long did it take?", "Site Photos?", "Upload up to 6 photos",
    "What project did you work on today?", "How long did that take?",
    "Did you work on any more projects?",
]
_KNOWN_LABELS_LOWER = {lbl.lower().rstrip("?: ") for lbl in KNOWN_FIELD_LABELS}


def extract_starred_fields(text):
    """
    Extracts '<marker> Title' / value pairs from text that lists fields the
    way Typeform's email notification (and PDFs built from it) do. Handles
    three real-world marker styles seen in actual files: a leading '*', a
    leading '•', or no marker at all (recognized by matching a known field
    label directly) — real historical PDFs mix all three inconsistently.
    Returns the same {'field': {'title': ..., 'id': None}, 'type': 'text',
    'text': ...} shape the shared structured parsers expect.
    """
    # Skip anything before Typeform's own marker text, if present — more
    # reliable than pattern-matching every email client's forward-header
    # style, and this same literal line also appears in PDFs built from
    # these emails, so it works for both input sources.
    start_marker = re.search(r'has a new response:', text, re.IGNORECASE)
    if start_marker:
        text = text[start_marker.end():]

    # Trim trailing footer text so it doesn't get glued onto the last
    # field's value (there's no marker after the last field, so without
    # this the footer would just become part of it).
    footer_marker = re.search(
        r'(Thanks for completing this typeform|Typeform sent you this email|Log in to view or download your responses)',
        text, re.IGNORECASE
    )
    if footer_marker:
        text = text[:footer_marker.start()]

    lines = text.split("\n")
    marker_indices = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        bulleted = re.match(r'^[\s>]*[\*•]\s*(.+)$', stripped)
        if bulleted:
            candidate = bulleted.group(1).strip()
        else:
            candidate = stripped
        if candidate.lower().rstrip("?: ") in _KNOWN_LABELS_LOWER:
            marker_indices.append((i, candidate))

    answers = []
    for idx, (line_i, title) in enumerate(marker_indices):
        start = line_i + 1
        end = marker_indices[idx + 1][0] if idx + 1 < len(marker_indices) else len(lines)
        value = "\n".join(lines[start:end]).strip()
        # Strip a leading '> ' quote-prefix from every line, in case the
        # source got quote-wrapped by an email forward.
        value = "\n".join(re.sub(r'^\s*>+\s?', '', ln) for ln in value.split("\n")).strip()
        if title:
            answers.append({
                "field": {"title": title, "id": None},
                "type": "text",
                "text": value,
            })
    return answers


def find_answer(answers, keywords):
    for ans in answers:
        title = (ans.get("field", {}).get("title") or "").lower().replace("*", "")
        if any(kw in title for kw in keywords):
            return ans
    return None


def find_answer_with_learning(answers, keywords, role_key, learned_map):
    """
    Same as find_answer, but self-healing: Typeform's webhook payload only
    guarantees a field's 'id', not its 'title' — title is genuinely absent
    on some submissions. The first time a field is matched by its title
    text, we remember that field id -> role permanently. On a later
    submission where title happens to be missing, we can still recognize
    the same field by its id and classify it correctly. (The email path
    has no field id at all, so learned_map is effectively a no-op there —
    that's fine, since email reliably includes titles anyway.)
    """
    found = find_answer(answers, keywords)
    if found:
        field_id = found.get("field", {}).get("id")
        if field_id:
            learned_map[field_id] = role_key
        return found

    for ans in answers:
        field_id = ans.get("field", {}).get("id")
        if field_id and learned_map.get(field_id) == role_key:
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


def _classify_field(field_title):
    """
    This form repeats a group of questions per project: project name, what
    was done, how long, next action, then a yes/no "any more projects?"
    check. The exact wording drifts slightly slot to slot (and Typeform
    embeds a {{field:UUID}} reference in some titles), so this matches on
    the stable keyword rather than the exact title. Also handles a real
    typo in the live form — "Next Projet:" (missing the 'c') on one slot.
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
    if "project" in t or "projet" in t:
        return "project"
    return None


_NO_VALUE_PLACEHOLDERS = {"n/a", "na", "none", "no", "-", "", "0"}


def _clean_or_none(text):
    """Treats placeholder non-answers ('N/A', 'None', '0' for a no/false checkbox, etc.) as empty."""
    t = (text or "").strip()
    return t if t and t.lower() not in _NO_VALUE_PLACEHOLDERS else ""


def _extract_job_numbers(text):
    """
    Pulls ALL Job/Work Order numbers out of free text (not just the first),
    so a field like '21764, 21761, shop' produces two tags, not one.
    A 5-digit number starting with '516' is Chipperfield's Work Order
    numbering convention — tagged WO instead of J.
    """
    if not text:
        return []
    tags = []
    seen = set()

    for m in re.finditer(r'\bJ\s*(\d{3,5})\b', text, re.IGNORECASE):
        tag = f"J{m.group(1)}"
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    for m in re.finditer(r'\b(WO|S)\s*(\d{3,5})\b', text, re.IGNORECASE):
        tag = f"{m.group(1).upper()}{m.group(2)}"
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)
    for m in re.finditer(r'\b(\d{3,5})\b', text):
        num = m.group(1)
        tag = f"WO{num}" if (len(num) == 5 and num.startswith("516")) else f"J{num}"
        if tag not in seen:
            tags.append(tag)
            seen.add(tag)

    return tags


def _extract_job_number(text):
    """Backward-compatible single-result version — first tag found, or None."""
    tags = _extract_job_numbers(text)
    return tags[0] if tags else None


def detect_form_type(answers, learned_map=None):
    """
    Multiple Typeform forms feed into this pipeline now, so the first step
    is figuring out which one this submission is from, based on which
    fields are present. Checked in order from most to least specific.
    Falls back to the learned field-id map if no title text matched
    anything (title is genuinely optional in Typeform's webhook payload —
    though not in email, which always includes it).
    """
    titles = [(a.get("field", {}).get("title") or "").lower().replace("*", "") for a in answers]
    joined = " | ".join(titles)

    if "job number" in joined:
        return "daily_job_log"
    if "how long did that take" in joined and "next action" in joined:
        return "management_log"
    if "what did your team work on today" in joined:
        return "fab_shop_log"

    if learned_map:
        field_ids = {a.get("field", {}).get("id") for a in answers if a.get("field", {}).get("id")}
        for fid in field_ids:
            role = learned_map.get(fid)
            if role == "djl_job_number":
                return "daily_job_log"
            if role in ("mgmt_duration", "mgmt_next_action"):
                return "management_log"
            if role == "fab_work_desc":
                return "fab_shop_log"

    return "unknown"


def parse_management_log(answers, log_name, log_date, learned_map=None):
    """The repeating project-slot form (project / did / how long / next action, up to 7x)."""
    learned_map = learned_map if learned_map is not None else {}
    projects = []
    current_project = None

    for ans in answers[2:]:
        field_title = ans.get("field", {}).get("title", "")
        if ans.get("type") == "file_url":
            continue

        kind = _classify_field(field_title)
        field_id = ans.get("field", {}).get("id")
        if kind is None and field_id:
            # Title was missing on this submission — fall back to what
            # we've learned this exact field means from a past submission.
            learned_role = learned_map.get(field_id)
            if learned_role in ("mgmt_duration", "mgmt_next_action", "mgmt_did", "mgmt_project"):
                kind = learned_role.replace("mgmt_", "")
        elif kind and field_id:
            learned_map[field_id] = f"mgmt_{kind}"

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


def parse_daily_job_log(answers, log_name, log_date, learned_map=None):
    """The real per-job crew log: has an actual Job # field, work order fields, photos, etc."""
    learned_map = learned_map if learned_map is not None else {}
    job_ans = find_answer_with_learning(answers, ["job number"], "djl_job_number", learned_map)
    wo_num_ans = find_answer_with_learning(answers, ["workorder #", "work order #"], "djl_workorder_num", learned_map)
    wo_name_ans = find_answer_with_learning(answers, ["workorder name"], "djl_workorder_name", learned_map)
    work_desc_ans = find_answer_with_learning(answers, ["what did your team work on today"], "djl_work_desc", learned_map)
    productivity_ans = find_answer_with_learning(answers, ["productivity issues"], "djl_productivity", learned_map)
    incident_ans = find_answer_with_learning(answers, ["incident report", "safety incident"], "djl_incident", learned_map)
    todo_ans = find_answer_with_learning(answers, ["to do"], "djl_todo", learned_map)
    scope_desc_ans = find_answer_with_learning(answers, ["describe additional work"], "djl_additional_work", learned_map)

    raw_job = answer_text(job_ans) or ""
    job_tags = _extract_job_numbers(raw_job)
    if not job_tags:
        raw_wo = answer_text(wo_num_ans) or ""
        job_tags = _extract_job_numbers(raw_wo)
    job_wo = ", ".join(job_tags) if job_tags else _clean_or_none(raw_job)
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


def parse_fab_shop_log(answers, log_name, log_date, learned_map=None):
    """Fab Shop's own simple team log — no job number, shop-wide."""
    learned_map = learned_map if learned_map is not None else {}
    work_desc_ans = find_answer_with_learning(answers, ["what did your team work on today"], "fab_work_desc", learned_map)
    materials_ans = find_answer_with_learning(answers, ["materials used"], "fab_materials", learned_map)
    productivity_ans = find_answer_with_learning(answers, ["productivity issues"], "fab_productivity", learned_map)
    incident_ans = find_answer_with_learning(answers, ["incident report"], "fab_incident", learned_map)
    start_ans = find_answer_with_learning(answers, ["start time"], "fab_start_time", learned_map)
    stop_ans = find_answer_with_learning(answers, ["stop time"], "fab_stop_time", learned_map)

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


def parse_structured_answers(answers, log_name, log_date, learned_map=None):
    """
    One entry point: detects the form type, dispatches to the right
    parser, and falls back to a labeled field-by-field dump (not a raw
    blob) if the form isn't recognized. Returns (job_wo, pdf_text, extra).
    """
    form_type = detect_form_type(answers, learned_map)
    if form_type == "management_log":
        job_wo, pdf_text, extra = parse_management_log(answers, log_name, log_date, learned_map)
    elif form_type == "daily_job_log":
        job_wo, pdf_text, extra = parse_daily_job_log(answers, log_name, log_date, learned_map)
    elif form_type == "fab_shop_log":
        job_wo, pdf_text, extra = parse_fab_shop_log(answers, log_name, log_date, learned_map)
    else:
        job_wo = ""
        lines = []
        for ans in answers:
            title = ans.get("field", {}).get("title") or "Field (untitled)"
            val = answer_text(ans)
            if val:
                lines.append(f"{title}: {val}")
        pdf_text = "\n".join(lines)
        extra = {"form_type": "unrecognized"}

    extra["form_type"] = form_type
    return job_wo, pdf_text, extra


def legacy_regex_extract(text, original_filename=""):
    """
    Fallback for files where extract_starred_fields() finds no recognizable
    field markers at all — genuinely old or differently-formatted content.
    Adapted directly from the team's original fix_no_date_files.py script,
    which was proven against real historical files, rather than
    re-deriving these patterns from scratch.
    Returns (date_iso_or_None, name, job_wo) — NOT yet YYMMDD-formatted;
    caller applies its own date formatter.
    """
    combined_source = f"{original_filename}\n{text}"
    raw_date = None

    label_match = re.search(
        r'(?:Today\'s\s*date|Date)\s*[:\n\r]*\s*(\d{1,2}[/\.-]\d{1,2}[/\.-]\d{2,4}|\d{4}-\d{2}-\d{2})',
        text, re.IGNORECASE
    )
    if label_match:
        found_val = label_match.group(1)
        alt_m = re.match(r'^(\d{1,2})[/\.-](\d{1,2})[/\.-](\d{2,4})$', found_val)
        if alt_m:
            mm, dd, yy = alt_m.groups()
            yy = f"20{yy}" if len(yy) == 2 else yy
            raw_date = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', found_val):
            raw_date = found_val

    if not raw_date:
        date_iso = re.search(r'\b(20\d{2}-\d{2}-\d{2})\b', combined_source)
        if date_iso:
            raw_date = date_iso.group(1)

    if not raw_date:
        date_8digit = re.search(r'\b(20\d{2})(\d{2})(\d{2})\b', combined_source)
        if date_8digit:
            raw_date = f"{date_8digit.group(1)}-{date_8digit.group(2)}-{date_8digit.group(3)}"

    if not raw_date:
        prefix_6digit = re.search(r'^\s*(\d{2})(\d{2})(\d{2})\b', original_filename)
        if prefix_6digit:
            yy, mm, dd = prefix_6digit.groups()
            raw_date = f"20{yy}-{mm}-{dd}"

    if not raw_date:
        alt_date = re.search(r'\b(\d{1,2})[/\.-](\d{1,2})[/\.-](20\d{2}|\d{2})\b', combined_source)
        if alt_date:
            mm, dd, yy = alt_date.groups()
            yy = f"20{yy}" if len(yy) == 2 else yy
            raw_date = f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"

    job_wo = ""
    job_match = re.search(r'(?:^|[_\s])J(\d{2,5})(?:[_\s]|$)', combined_source, re.IGNORECASE)
    wo_match = re.search(r'(?:^|[_\s])(WO|S)(\d{2,5})(?:[_\s]|$)', combined_source, re.IGNORECASE)
    if job_match:
        job_wo = f"J{job_match.group(1)}"
    elif wo_match:
        job_wo = f"{wo_match.group(1).upper()}{wo_match.group(2)}"
    elif re.search(r'\boffice\b', text, re.IGNORECASE):
        job_wo = "Office"
    elif re.search(r'\bshop\b', text, re.IGNORECASE):
        job_wo = "Shop"
    elif re.search(r'Daily\s*Management\s*Log', text, re.IGNORECASE):
        job_wo = "Daily Management Log"
    elif re.search(r'Equipment\s*Repair', text, re.IGNORECASE):
        job_wo = "Equipment Repair"

    name_match = re.search(r'(?:Your\s*name|Name)\s*[:\n\r]*\s*([A-Za-z\s\.]+)', text, re.IGNORECASE)
    name = None
    if name_match:
        candidate = name_match.group(1).split('\n')[0].strip()
        if candidate and candidate.lower() not in ("j", "unknown", "none"):
            name = candidate

    return raw_date, name, job_wo


# ============================================================
# ---- dropbox_pdf_utils.py content (trimmed to what's used here) ----
# ============================================================

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


def extract_text_from_pdf_bytes(pdf_bytes):
    from pypdf import PdfReader
    text = ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
    except Exception:
        pass
    return text



# ============================================================
# ---- notion_utils.py content (trimmed to what's used here) ----
# ============================================================

NOTION_API_VERSION_DS = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"


def _notion_headers():
    token = require_env("NOTION_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_API_VERSION_DS,
        "Content-Type": "application/json",
    }


def query_data_source(data_source_id, page_size=50, filter_obj=None):
    headers = _notion_headers()
    url = f"{NOTION_BASE_URL}/data_sources/{data_source_id}/query"
    body = {"page_size": page_size}
    if filter_obj:
        body["filter"] = filter_obj
    resp = requests.post(url, headers=headers, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


def prop_text(props, name):
    p = props.get(name)
    if not p:
        return None
    if p["type"] == "title":
        parts = p.get("title", [])
    elif p["type"] == "rich_text":
        parts = p.get("rich_text", [])
    else:
        return None
    return "".join(t.get("plain_text", "") for t in parts).strip() or None


def prop_people(props, name):
    p = props.get(name)
    if not p or p["type"] != "people":
        return None
    people = p.get("people", [])
    names = [person.get("name") for person in people if person.get("name")]
    return ", ".join(names) if names else None


def prop_date(props, name):
    p = props.get(name)
    if not p or p["type"] != "date":
        return None
    d = p.get("date")
    return d.get("start") if d else None



# ============================================================
# ---- main script logic ----
# ============================================================

NOTION_KEVIN_FORM_DATASOURCE_ID = "3ab4562c-e56f-8029-b87a-000b05352beb"

_KEVIN_FORM_SLOT_FIELDS = [
    ("1 What project did you work on today? ", "1 What did you do for this project? (1)", "1 How long did that take? (2) 1", "1 What is the next action for this project? (2) 1"),
    ("2 What project did you work on today? (3)", "2 What did you do for this project? (2)", "2 How long did that take? (2)", "2 What is the next action for this project? (2)"),
    ("3 What project did you work on today? (4)", "3 What did you do for this project? (3)", "3 How long did that take? (4) 1", "3 What is the next action for this project? (4) 1"),
    ("4 What project did you work on today? (5)", "4 What did you do for this project? (4)", "4 How long did that take? (4)", "4 What is the next action for this project? (4)"),
    ("5 What project did you work on today? (6)", "5 What did you do for this project? (5)", "5 How long did that take? (5)", "5 What is the next action for this project? (6)"),
    ("6 What project did you work on today? (7)", "6 What did you do for this project? (6)", "6 How long did that take? (7)", "6 What is the next action for this project? (7)"),
    ("7 What project did you work on today? (8)", "7 What did you do for this project? (7)", "7 How long did that take? (8)", "7 What is the next action for this project? (8)"),
]


def sync_kevin_notion_logs(dbx, dry_run=False):
    """
    Pulls Kevin's Daily Management Form entries straight from Notion (the
    same data source the dashboard already reads) and files each one as a
    PDF + .txt sidecar in Dropbox, matching the same naming convention as
    every other Daily Management Log — so Kevin's entries live alongside
    everyone else's instead of only being reachable through the separate
    Notion-specific endpoint.
    """
    print("\n--- Syncing Kevin's Notion Daily Management Form entries ---")
    pages = query_data_source(NOTION_KEVIN_FORM_DATASOURCE_ID, page_size=100)
    print(f"Found {len(pages)} entries in Notion.\n")

    synced = 0
    skipped = 0
    errors = 0

    for page in pages:
        props = page.get("properties", {})
        raw_date = prop_date(props, "Date")
        name = prop_people(props, "Name") or "Kevin"

        if not raw_date:
            print(f"  skip (no date on this entry)")
            skipped += 1
            continue

        log_date = format_date_for_jacque(raw_date)

        lines = []
        for project_f, did_f, how_long_f, next_f in _KEVIN_FORM_SLOT_FIELDS:
            project = prop_text(props, project_f)
            did = prop_text(props, did_f)
            how_long = prop_text(props, how_long_f)
            next_action = prop_text(props, next_f)

            if not (project or did):
                continue

            line = f"{project or 'Project'}: {did or ''}"
            if how_long:
                line += f" ({how_long})"
            if next_action:
                line += f" — Next: {next_action}"
            lines.append(line)

        pdf_text = "\n".join(lines)
        if not pdf_text:
            print(f"  [{log_date}] {name} ... skip (no project content on this entry)")
            skipped += 1
            continue

        target_folder_path = get_year_subfolder(log_date)
        pdf_filename = f"{log_date} | {name} | Daily Management Log.pdf"
        dest_pdf_path = f"{target_folder_path}/{pdf_filename}"

        # Skip if already synced (same de-dupe approach as everywhere else)
        try:
            dbx.files_get_metadata(dest_pdf_path)
            print(f"  [{log_date}] {name} ... skip (already synced)")
            skipped += 1
            continue
        except dropbox.exceptions.ApiError:
            pass  # doesn't exist yet, proceed

        if dry_run:
            print(f"  [{log_date}] {name} ... WOULD SYNC -> {pdf_filename}")
            synced += 1
            continue

        try:
            pdf_bytes = generate_pdf_from_text(pdf_filename.replace(".pdf", ""), pdf_text)
            dbx.files_upload(pdf_bytes, dest_pdf_path, mode=dropbox.files.WriteMode.overwrite)

            txt_filename = pdf_filename[:-4] + ".txt"
            dest_txt_path = f"{target_folder_path}/{txt_filename}"
            dbx.files_upload(pdf_text.encode("utf-8"), dest_txt_path, mode=dropbox.files.WriteMode.overwrite)

            print(f"  [{log_date}] {name} ... synced -> {pdf_filename}")
            synced += 1
        except Exception as e:
            print(f"  [{log_date}] {name} ... FAILED ({e})")
            errors += 1

    print(f"\nKevin sync done: {synced} {'would be synced' if dry_run else 'synced'}, {skipped} skipped, {errors} error(s).")


def collect_all_pdf_entries(dbx):
    """Flat list of (folder_path, entry) for every PDF across all year folders."""
    entries_flat = []
    print("Listing year folders...")
    res = dbx.files_list_folder(DROPBOX_BASE_FOLDER)
    base_entries = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        base_entries.extend(res.entries)

    year_folders = sorted(
        e.name for e in base_entries
        if isinstance(e, dropbox.files.FolderMetadata) and e.name.isdigit() and len(e.name) == 4
    )
    print(f"Found year folders: {year_folders}")

    for year in year_folders:
        folder_path = f"{DROPBOX_BASE_FOLDER}/{year}"
        print(f"Listing {folder_path}...")
        res = dbx.files_list_folder(folder_path)
        year_entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            year_entries.extend(res.entries)
        for e in year_entries:
            if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".pdf"):
                entries_flat.append((folder_path, e))

    return entries_flat


def _parse_existing_filename(filename):
    """
    Best-effort parse of an ALREADY-formatted filename's own segments —
    used ONLY as a last resort when content-based extraction finds
    nothing at all. Returns (date_yymmdd, name, job_wo) or (None, None,
    None) if the filename doesn't look like a valid parseable date/name.
    """
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    parts = [p.strip() for p in stem.split("|") if p.strip()]
    if len(parts) < 2:
        return None, None, None

    date_part = parts[0]
    name_part = parts[1]
    job_part = parts[2] if len(parts) > 2 else ""

    if not (len(date_part) == 6 and date_part.isdigit()):
        return None, None, None
    if not name_part or name_part.lower() in ("unknown", "no_date"):
        return None, None, None

    return date_part, name_part, job_part


def derive_correct_filename(dbx, folder_path, entry):
    """Downloads one PDF, re-derives its correct name from its own content.
    Returns (new_filename_or_None, reason_if_skipped)."""
    try:
        _, res_dl = dbx.files_download(entry.path_display)
        pdf_bytes = res_dl.content
    except Exception as e:
        return None, f"download failed ({e})"

    text = extract_text_from_pdf_bytes(pdf_bytes)

    answers = extract_starred_fields(text)
    has_real_titles = any(a["field"]["title"] for a in answers)

    new_name = None
    new_date = None
    new_job_wo = None

    if answers and has_real_titles:
        name_ans = find_answer(answers, ["your name", "name"])
        date_ans = find_answer(answers, ["date"])
        new_name = _clean_or_none(answer_text(name_ans))
        raw_date_text = answer_text(date_ans) or ""
        new_date = format_date_for_jacque(raw_date_text) if raw_date_text else None
        new_job_wo, _pdf_text, _extra = parse_structured_answers(
            answers, new_name or "Unknown", new_date or "No_Date"
        )

    if not new_name or not new_date or new_date == "No_Date":
        raw_date, fallback_name, fallback_job_wo = legacy_regex_extract(text, entry.name)
        if not new_name:
            new_name = fallback_name
        if not new_date or new_date == "No_Date":
            new_date = format_date_for_jacque(raw_date) if raw_date else None
        if not new_job_wo:
            new_job_wo = fallback_job_wo

    if not new_date or new_date == "No_Date" or not new_name:
        # Last resort: content gave us nothing usable at all — check
        # whether the EXISTING filename already has a valid-looking
        # name/date sitting in it (adapted from the team's original
        # fix_no_date_files.py, which had this same safety net). This
        # only fires when content extraction found nothing, so it can't
        # override or contaminate a real content-based result — it just
        # stops a perfectly fine existing filename from being skipped
        # with an alarming "couldn't determine" message.
        fb_date, fb_name, fb_job = _parse_existing_filename(entry.name)
        if fb_date and fb_name:
            if not new_date or new_date == "No_Date":
                new_date = fb_date
            if not new_name:
                new_name = fb_name
            if not new_job_wo:
                new_job_wo = fb_job

    if not new_date or new_date == "No_Date" or not new_name:
        return None, "couldn't determine a name/date from this file"

    parts = [new_date, new_name]
    if new_job_wo:
        parts.append(new_job_wo)
    new_filename = " | ".join(parts) + ".pdf"

    if new_filename == entry.name:
        return None, "already correct"

    return new_filename, None


def main():
    parser = argparse.ArgumentParser(description="Retroactively fix Daily Job Log PDF filenames and/or sync Kevin's Notion logs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without actually changing anything")
    parser.add_argument("--skip-filenames", action="store_true", help="Skip the filename-fixing pass")
    parser.add_argument("--skip-kevin", action="store_true", help="Skip syncing Kevin's Notion entries")
    args = parser.parse_args()

    print("Connecting to Dropbox...")
    dbx = get_dropbox_client()

    if not args.skip_kevin:
        sync_kevin_notion_logs(dbx, dry_run=args.dry_run)

    if args.skip_filenames:
        return

    all_entries = collect_all_pdf_entries(dbx)
    total = len(all_entries)
    print(f"\nFound {total} PDF(s) to check.\n")

    renamed_count = 0
    skipped_count = 0
    error_count = 0

    for i, (folder_path, entry) in enumerate(all_entries, start=1):
        print(f"[{i}/{total}] {entry.name} ... ", end="", flush=True)

        new_filename, reason = derive_correct_filename(dbx, folder_path, entry)

        if new_filename is None:
            print(f"skip ({reason})")
            if reason != "already correct":
                error_count += 1
            else:
                skipped_count += 1
            continue

        if args.dry_run:
            print(f"WOULD RENAME -> {new_filename}")
            renamed_count += 1
        else:
            to_path = f"{folder_path}/{new_filename}"
            try:
                dbx.files_move_v2(entry.path_display, to_path, autorename=True)
                print(f"renamed -> {new_filename}")
                renamed_count += 1
            except Exception as e:
                print(f"RENAME FAILED ({e})")
                error_count += 1

    print(f"\n{'=' * 50}")
    print(f"Done. {renamed_count} {'would be renamed' if args.dry_run else 'renamed'}, "
          f"{skipped_count} already correct, {error_count} error(s)/skipped.")


if __name__ == "__main__":
    sys.exit(main())
