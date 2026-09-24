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
