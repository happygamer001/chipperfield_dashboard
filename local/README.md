# Daily Job Logs: PC script

`daily_job_logs.py` is the only thing that writes Daily Job Logs to Dropbox. It runs on the office PC once a day. The Chipperfield Dashboard only reads what it writes:

- **`YYMMDD | Name | Job.pdf`** is the complete log, the record Jacque and billing use.
- **`YYMMDD | Name | Job.txt`** is the short summary that Calvin's dashboard shows.
- **Photos** are saved as `YYMMDD | Name | Job | ImageN.jpeg`.

It reads Gmail **All Mail**, so archived or filtered emails are included. The mailbox is opened read-only, so nothing gets marked as read. Only emails with "log" in the subject are processed: Daily Job Log, Daily Management Log and the Fab Shop daily log. Service requests, job applications and screenings are skipped.

## One-time setup on the PC

1. **Python 3.10 or newer.** In a Command Prompt, `python --version` should print a version. If it doesn't, install it from python.org and tick "Add python.exe to PATH".
2. **Tesseract OCR**, used only for scanned or photographed logs. Install it to the default `C:\Program Files\Tesseract-OCR\`.
3. **Python packages:**
   ```
   pip install dropbox requests beautifulsoup4 pillow pytesseract pypdf reportlab
   ```
4. **Copy this `local` folder** somewhere permanent, for example `C:\DailyJobLogs\`.
5. **Settings:** copy `djl_settings.example.txt` to `djl_settings.txt` in the same folder and fill in the new, rotated Gmail app password and Dropbox values.
6. **Test** by opening a Command Prompt in that folder:
   ```
   python daily_job_logs.py preview 5
   ```
   This writes PDFs to `preview_output` and uploads nothing. Check that they look right.

## One-time repair: rebuild the last 90 days

```
run_daily_job_logs.bat rebuild 90
```
- **Overwrites PDFs:** every log from the last 90 days is replaced with the full log, and its `.txt` with the summary.
- **Archives old copies:** an old copy saved under a different name, such as `261001 | Craig.pdf`, is moved to `Original Unformatted Files`.
- **Photos** are not re-uploaded.
- **Results:** see `logs\djl_<today>.log` and the review list in `Daily Job Logs/_Needs Review`.

There's no time limit on the PC, so a long run is fine.

## Schedule the daily run

Open a Command Prompt **as the user who will own the task**, change `C:\DailyJobLogs` below if you used a different folder, and run:

```
schtasks /Create /TN "Daily Job Logs" /TR "\"C:\DailyJobLogs\run_daily_job_logs.bat\"" /SC DAILY /ST 06:45 /F
```

- **Runs daily at 6:45 AM.** Change `/ST` for a different time.
- **The PC has to be on and signed in.** If it was off, the next run still catches up, because each run looks back 7 days.
- **Missed runs:** in Task Scheduler, under Settings, tick "Run task as soon as possible after a scheduled start is missed" so a run happens when the PC comes back on.
- **Run it now** to check it: `schtasks /Run /TN "Daily Job Logs"`, then look in `logs\`.

## Other commands

| Command | What it does |
|---|---|
| `python daily_job_logs.py` | Daily run, the same thing the scheduled task does |
| `python daily_job_logs.py preview 10` | Builds the last 10 logs locally. Uploads nothing |
| `python daily_job_logs.py backfill 2026` | Lists the title fixes for a year folder. Renames nothing |
| `python daily_job_logs.py apply backfill_2026.csv` | Applies a reviewed rename list |
| `python daily_job_logs.py other` | Lists logs where "Other" was picked for the name |
