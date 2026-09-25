"""
Run this from your own terminal to retroactively fix Daily Job Log PDF
filenames — re-derives each file's real Name/Date/Job straight from its
own text content, the same logic the admin page's "Fix Filenames" button
uses. The only difference is WHERE it runs: this has no 60-second
function limit, so it can work through the entire archive in one go
instead of needing to be batched to survive Vercel's timeout.

SETUP (one time):
  1. Make sure you're in the project's scripts/ folder, or that
     scripts/ is on your Python path.
  2. Install dependencies if you haven't already:
       pip install dropbox pypdf reportlab requests beautifulsoup4
  3. Set the same three Dropbox environment variables the deployed app
     uses (get these from Vercel's project settings — Environment
     Variables — or wherever they're stored):
       export DROPBOX_APP_KEY="..."
       export DROPBOX_APP_SECRET="..."
       export DROPBOX_REFRESH_TOKEN="..."

RUN:
    python3 fix_filenames_local.py

    Add --dry-run to see what WOULD be renamed without actually
    renaming anything:
    python3 fix_filenames_local.py --dry-run
"""

import sys
import argparse
import dropbox

from dropbox_pdf_utils import get_dropbox_client, extract_text_from_pdf_bytes, format_date_for_jacque, DROPBOX_BASE_FOLDER
import form_parsers


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
    parser = argparse.ArgumentParser(description="Retroactively fix Daily Job Log PDF filenames")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be renamed without renaming")
    args = parser.parse_args()

    print("Connecting to Dropbox...")
    dbx = get_dropbox_client()

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
