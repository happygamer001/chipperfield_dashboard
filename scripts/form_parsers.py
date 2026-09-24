"""
Shared structured-parsing logic for the three known Typeform forms (Daily
Job Log, Management Log, Fab Shop Log). Used by BOTH:
  - api/index.py's Typeform webhook (real-time, JSON payload)
  - scripts/daily_log_uploader.py's Gmail scan (backup path, email body)

Both paths convert their raw input into the same list-of-answers shape
({"field": {"title": ..., "id": ...}, "type": ..., "text"/"number"/"date"/
"choice": ...}) and then call the same functions here — so a submission
gets identically good structured output regardless of which path it came
in through, instead of the email path silently being lower quality.
"""

import re

# Every field label we've seen across the three known forms (Daily Job
# Log, Management Log, Fab Shop Log) plus real historical PDFs — used so a
# line can be recognized as a field marker even with no bullet character
# at all, or an unfamiliar one. Real files are inconsistent: some lines
# use '*', some use '•', some use nothing.
KNOWN_FIELD_LABELS = [
    "Your Name", "Name", "Date", "Today's weather", "Crew",
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
    elif re.search(r'\boffice\b', combined_source, re.IGNORECASE):
        job_wo = "Office"
    elif re.search(r'\bshop\b', combined_source, re.IGNORECASE):
        job_wo = "Shop"
    elif re.search(r'Daily\s*Management\s*Log', combined_source, re.IGNORECASE):
        job_wo = "Daily Management Log"
    elif re.search(r'Equipment\s*Repair', combined_source, re.IGNORECASE):
        job_wo = "Equipment Repair"

    name_match = re.search(r'(?:Your\s*name|Name)\s*[:\n\r]*\s*([A-Za-z\s\.]+)', text, re.IGNORECASE)
    name = None
    if name_match:
        candidate = name_match.group(1).split('\n')[0].strip()
        if candidate and candidate.lower() not in ("j", "unknown", "none"):
            name = candidate

    return raw_date, name, job_wo
