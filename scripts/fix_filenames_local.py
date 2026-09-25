"""
Run this from your own terminal to:
  1. Retroactively fix Daily Job Log PDF filenames — re-derives each
     file's real Name/Date/Job straight from its own text content, the
     same logic the admin page's "Fix Filenames" button uses.
  2. Sync Kevin's Daily Management Form entries from Notion into Dropbox
     as PDFs, matching everyone else's format, so his entries live
     alongside the rest instead of only being reachable through the
     separate Notion-specific dashboard endpoint.

The only difference from running these via the website is WHERE this
runs: your terminal has no 60-second function limit, so it can work
through the entire archive in one go instead of needing to be batched to
survive Vercel's timeout.

SETUP (one time):
  1. Make sure you're in the project's scripts/ folder, or that
     scripts/ is on your Python path.
  2. Install dependencies if you haven't already:
       pip install dropbox pypdf reportlab requests beautifulsoup4
  3. Set these environment variables (get the values from Vercel's
     project settings — Environment Variables):
       export DROPBOX_APP_KEY="..."
       export DROPBOX_APP_SECRET="..."
       export DROPBOX_REFRESH_TOKEN="..."
       export NOTION_TOKEN="..."

RUN:
    python3 fix_filenames_local.py              # both passes
    python3 fix_filenames_local.py --dry-run    # preview only, nothing changes
    python3 fix_filenames_local.py --skip-kevin       # filename fix only
    python3 fix_filenames_local.py --skip-filenames   # Kevin sync only
"""

import sys
import argparse
import dropbox

from dropbox_pdf_utils import (
    get_dropbox_client, extract_text_from_pdf_bytes, format_date_for_jacque,
    DROPBOX_BASE_FOLDER, get_year_subfolder, generate_pdf_from_text,
)
import form_parsers
import notion_utils

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
    pages = notion_utils.query_data_source(NOTION_KEVIN_FORM_DATASOURCE_ID, page_size=100)
    print(f"Found {len(pages)} entries in Notion.\n")

    synced = 0
    skipped = 0
    errors = 0

    for page in pages:
        props = page.get("properties", {})
        raw_date = notion_utils.prop_date(props, "Date")
        name = notion_utils.prop_people(props, "Name") or "Kevin"

        if not raw_date:
            print(f"  skip (no date on this entry)")
            skipped += 1
            continue

        log_date = format_date_for_jacque(raw_date)

        lines = []
        for project_f, did_f, how_long_f, next_f in _KEVIN_FORM_SLOT_FIELDS:
            project = notion_utils.prop_text(props, project_f)
            did = notion_utils.prop_text(props, did_f)
            how_long = notion_utils.prop_text(props, how_long_f)
            next_action = notion_utils.prop_text(props, next_f)

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


def derive_correct_filename(dbx, folder_path, entry):
    """Downloads one PDF, re-derives its correct name from its own content.
    Returns (new_filename_or_None, reason_if_skipped)."""
    try:
        _, res_dl = dbx.files_download(entry.path_display)
        pdf_bytes = res_dl.content
    except Exception as e:
        return None, f"download failed ({e})"

    text = extract_text_from_pdf_bytes(pdf_bytes)

    answers = form_parsers.extract_starred_fields(text)
    has_real_titles = any(a["field"]["title"] for a in answers)

    new_name = None
    new_date = None
    new_job_wo = None

    if answers and has_real_titles:
        name_ans = form_parsers.find_answer(answers, ["your name", "name"])
        date_ans = form_parsers.find_answer(answers, ["date"])
        new_name = form_parsers._clean_or_none(form_parsers.answer_text(name_ans))
        raw_date_text = form_parsers.answer_text(date_ans) or ""
        new_date = format_date_for_jacque(raw_date_text) if raw_date_text else None
        new_job_wo, _pdf_text, _extra = form_parsers.parse_structured_answers(
            answers, new_name or "Unknown", new_date or "No_Date"
        )

    if not new_name or not new_date or new_date == "No_Date":
        raw_date, fallback_name, fallback_job_wo = form_parsers.legacy_regex_extract(text, entry.name)
        if not new_name:
            new_name = fallback_name
        if not new_date or new_date == "No_Date":
            new_date = format_date_for_jacque(raw_date) if raw_date else None
        if not new_job_wo:
            new_job_wo = fallback_job_wo

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
