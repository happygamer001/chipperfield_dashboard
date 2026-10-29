# Daily Job Logs: PC script

`daily_job_logs.py` is the only thing that writes Daily Job Logs to Dropbox. It runs on the office PC once a day. The Chipperfield Dashboard only reads what it writes:

| Where | What | Who uses it |
|---|---|---|
| `Daily Job Logs/<year>/YYMMDD \| Name \| Job.pdf` | The complete log | Jacque and billing |
| `Daily Job Logs/<year>/YYMMDD \| Name \| Job \| ImageN.jpeg` | Site photos | Jacque, billing and the dashboard |
| `Dashboard/Daily Log Summaries/<year>/YYMMDD \| Name \| Job.txt` | Short summary | Calvin's dashboard |

The summaries are kept out of the job log folders on purpose, so those folders hold only PDFs and photos.

It reads Gmail **All Mail**, so archived or filtered emails are included. The mailbox is opened read-only, so nothing gets marked as read. Only emails with "log" in the subject are processed: Daily Job Log, Daily Management Log and the Fab Shop daily log. Service requests, job applications and screenings are skipped.

## Equipment Repair forms

Nic's **Equipment Repair** Typeform is picked up too (any email with "repair" in the subject). It's saved with the job logs:

- `YYMMDD | Nic | Equipment Repair.pdf`, plus its photos as `... | Equipment Repair | Image1.jpeg`
- A second repair on the same day is `Equipment Repair 2`, then `3`, and so on.
- The date comes from the form's date question, or the day the email arrived if there isn't one.
- The name comes from the form's name question, or **Nic** if there isn't one. To change that, add `REPAIR_DEFAULT_NAME=Name` to `djl_settings.txt`.

## One-time setup on the PC

1. **Python 3.10 or newer.** In a Command Prompt, `python --version` should print a version. If it doesn't, install it from python.org and tick "Add python.exe to PATH".
2. **Tesseract OCR**, used only for scanned or photographed logs. Install it to the default `C:\Program Files\Tesseract-OCR\`.
3. **Python packages:**
   ```
   pip install dropbox requests beautifulsoup4 pillow pytesseract pypdf reportlab
   ```
4. **Copy this `local` folder** somewhere permanent, for example `C:\DailyJobLogs\`.
5. **Settings:** copy `djl_settings.example.txt` to `djl_settings.txt` in the same folder and fill in the new, rotated Gmail app password, the Dropbox values and the Typeform token.
   - **Typeform token:** in Typeform, go to Account > Personal tokens, create one with the `responses:read` scope, and paste it after `TYPEFORM_API_TOKEN=`. Without it, Typeform usually refuses the photo downloads and the photos land on the review list instead.
6. **Test** by opening a Command Prompt in that folder:
   ```
   python daily_job_logs.py preview 5
   ```
   This writes PDFs to `preview_output` and uploads nothing. Check that they look right.

## One-time repair: rebuild the last 90 days

```
run_daily_job_logs.bat rebuild 90
```
- **Overwrites PDFs:** every log from the last 90 days is replaced with the full log, and its summary is rewritten in `Dashboard/Daily Log Summaries`.
- **Archives old copies:** an old copy saved under a different name, such as `261001 | Craig.pdf`, is moved to `Original Unformatted Files`.
- **Photos** are not re-uploaded.
- **Results:** see `logs\djl_<today>.log` and the review list in `Daily Job Logs/_Needs Review`.

There's no time limit on the PC, so a long run is fine.

## One-time move: take the old summaries out of the job log folders

```
python daily_job_logs.py move-summaries
```
- **Moves** every `.txt` file in `Daily Job Logs` and its year folders into `Dashboard/Daily Log Summaries/<year>/`. No PDFs or photos are touched.
- **Duplicates:** if a summary with the same name is already there, the newer one is kept and the older copy goes to `Daily Log Summaries/_replaced`.
- **Merge the dashboard pull request first, then run this.** The live dashboard needs to know where to find the summaries.

## One-time repair: recover the missing photos

```
python daily_job_logs.py photos 30
```
- **Uploads only missing photos** from the last 30 days (use a bigger number to go further back). PDFs and summaries aren't touched, and photos already in Dropbox are skipped.
- **Why they were missing:** Typeform wraps every link in its emails in a tracking link, and the script didn't recognise those as photos, so it skipped them. The daily run now saves photos too.
- **Photos that can't be saved** are listed in `Daily Job Logs/_Needs Review` as "PHOTO NOT SAVED" with the reason, such as a missing token or an expired link.

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
| `python daily_job_logs.py apply backfill_2026.csv` | Applies a reviewed rename list. Summaries are renamed to match |
| `python daily_job_logs.py move-summaries` | One-time: moves the summary `.txt` files out of the job log folders |
| `python daily_job_logs.py photos 30` | Uploads any missing site photos from the last 30 days. Leaves PDFs alone |
| `python daily_job_logs.py other` | Lists logs where "Other" was picked for the name |
