from http.server import BaseHTTPRequestHandler
import json
import re
import datetime
import sys
import os
import urllib.parse

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from daily_log_uploader import get_dropbox_client, DROPBOX_BASE_FOLDER  # noqa: E402

DAYS_TO_SHOW = 5

# Matches "YYMMDD | Name | JobOrWO[ | ImageN].ext"
FILENAME_PATTERN = re.compile(
    r'^(?P<date>\d{6}|No_Date)\s*\|\s*(?P<name>[^|]+?)\s*(?:\|\s*(?P<jobwo>[^|]+?))?(?:\s*\|\s*Image\d+)?\.(?P<ext>\w+)$'
)


def _yymmdd_to_date(yymmdd):
    if yymmdd == "No_Date" or len(yymmdd) != 6:
        return None
    try:
        return datetime.date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None


def _years_to_check(window_start, window_end):
    return sorted({window_start.year, window_end.year})


def get_recent_daily_logs(days=DAYS_TO_SHOW):
    dbx = get_dropbox_client()

    today = datetime.date.today()
    window_start = today - datetime.timedelta(days=days - 1)

    entries_by_key = {}  # (date, name, jobwo) -> {date, name, job_wo, pdf_path, photo_paths: []}

    for year in _years_to_check(window_start, today):
        folder_path = f"{DROPBOX_BASE_FOLDER}/{year}"
        try:
            res = dbx.files_list_folder(folder_path)
            all_entries = list(res.entries)
            while res.has_more:
                res = dbx.files_list_folder_continue(res.cursor)
                all_entries.extend(res.entries)
        except Exception:
            continue  # folder may not exist for this year yet

        for entry in all_entries:
            match = FILENAME_PATTERN.match(entry.name)
            if not match:
                continue

            log_date = _yymmdd_to_date(match.group("date"))
            if not log_date or log_date < window_start or log_date > today:
                continue

            name = match.group("name").strip()
            job_wo = (match.group("jobwo") or "").strip()
            ext = match.group("ext").lower()

            key = (match.group("date"), name, job_wo)
            if key not in entries_by_key:
                entries_by_key[key] = {
                    "date": log_date.isoformat(),
                    "name": name,
                    "job_or_wo": job_wo or None,
                    "dropbox_pdf_path": None,
                    "photo_count": 0,
                }

            if ext == "pdf":
                entries_by_key[key]["dropbox_pdf_path"] = entry.path_display
            elif ext in ("jpeg", "jpg", "png", "heic", "webp"):
                entries_by_key[key]["photo_count"] += 1

    results = list(entries_by_key.values())
    results.sort(key=lambda r: r["date"], reverse=True)
    return results


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            days = int(qs.get("days", [DAYS_TO_SHOW])[0])

            logs = get_recent_daily_logs(days=days)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"days": days, "count": len(logs), "logs": logs}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
