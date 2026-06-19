# Mark Orders Shipped Routine - Stage 2 Automation

Python + Playwright automation for the "mark orders shipped" workflow.

Manual process reference:

https://scribehow.com/viewer/How_To_Process_And_Filter_Shipping_Report_Export_Data__yzCBrf8XQ1mNQiWUOE8N8g

The workflow runs three scripts in sequence. Stage 0 must complete cleanly before Stage 1 and Stage 2 are allowed to run.

## Pipeline Overview

```text
Stage 0 (automation_stage00.py)
    ├─ 0 PreGen Failure orders → exit 0 → Stage 1 → Stage 2
    ├─ All failures resolved by passes → exit 0 → Stage 1 → Stage 2
    └─ Failures remain after all 4 passes → exit 1 → email sent → STOP
```

Current Stage 0 reporting is scoped to today's Ship By Date. Non-today PreGen Failure orders can remain without blocking Stage 1 or Stage 2.

## What Rithum/ChannelAdvisor Does

Rithum, formerly ChannelAdvisor, is the ecommerce platform that provides the order export file. That export tells us which orders need to be shipped.

In the full workflow there are two input files:

- Rithum/ChannelAdvisor order export fetched via the CA API.
- Helm/DC shipping report containing the real tracking numbers.

`automation_stage01.py` fetches Rithum order data via the CA API first, then requests the Helm/DC shipping report. The CA API response is converted to Basic Layout column format (`DD/MM/YYYY HH:MM` dates, human-readable column names matching the Rithum UI export) before saving to `downloads/rithum_orders.csv`.

## Stage 0

### Current Stage 0 Logic

`automation_stage00.py` clears PreGen Failure orders in Helm before Stage 1 and Stage 2 are allowed to run. It uses four progressively targeted passes. Each pass opens the relevant PreGen Failure orders one by one, changes the courier service on the order detail page, clicks the shipping lock toggle, and sets the order back to PreGen status (3003) so Helm re-generates the label.

Passes 1 and 2 do not use a Ship By Date filter. Passes 3 and 4 only process orders with today's Ship By Date.

**Pass 1: No Ship By Date filter - Evri 24 Non POD**

1. Click the PreGen Failure dashboard tile.
2. Collect the visible PreGen Failure order links.
3. Open each order one by one.
4. Verify the Pregenerated Labels Plugin error is visible.
5. Select `Evri 24 Non POD`.
6. Click the shipping lock toggle.
7. Set status to PreGen (3003).
8. Poll the dashboard until the PreGen count reaches 0.
9. Check the remaining PreGen Failure count.

**Pass 2: No Ship By Date filter - Royal Mail 48 No Signature**

Only runs if Pass 1 leaves failures:

1. Click the PreGen Failure dashboard tile.
2. Collect the visible PreGen Failure order links.
3. Open each order one by one.
4. Verify the Pregenerated Labels Plugin error is visible.
5. Select `Royal Mail 48 No Signature`.
6. Click the shipping lock toggle.
7. Set status to PreGen (3003).
8. Poll the dashboard until the PreGen count reaches 0.
9. Check the remaining PreGen Failure count.

**Pass 3: Today's Ship By Date - Royal Mail 48 No Signature**

Only runs if Pass 2 leaves failures:

1. Click the PreGen Failure dashboard tile.
2. Open Filters.
3. Apply today's Ship By Date filter.
4. If the filtered count is 0, Stage 1 runs even if non-today PreGen Failures remain.
5. Open each filtered order one by one.
6. Verify the Pregenerated Labels Plugin error is visible.
7. Select `Royal Mail 48 No Signature`.
8. Click the shipping lock toggle.
9. Set status to PreGen (3003).
10. Poll the dashboard until the PreGen count reaches 0.

**Pass 4: Today's Ship By Date - Royal Mail 48 With Signature**

Only runs if Pass 3 leaves failures:

1. Click the PreGen Failure dashboard tile.
2. Apply today's Ship By Date filter.
3. If the filtered count is 0, Stage 1 runs even if non-today PreGen Failures remain.
4. Open each filtered order one by one.
5. Verify the Pregenerated Labels Plugin error is visible.
6. Select `Royal Mail 48 With Signature`.
7. Click the shipping lock toggle.
8. Set status to PreGen (3003).
9. Poll the dashboard until the PreGen count reaches 0.
10. Run the final reporting check for today's Ship By Date only.

### Reporting Rule

Stage 0 only reports and blocks when PreGen Failure orders remain for today's Ship By Date.

After Pass 4, the script opens the PreGen Failure queue, applies today's Ship By Date filter, counts the filtered rows, and sends the manual intervention email only if that filtered count is greater than 0. If PreGen Failure orders remain on the dashboard but none match today's Ship By Date, Stage 0 exits with code 0 so Stage 1 and Stage 2 can continue.

**Per-order error emails are only sent during Pass 4.** Passes 1, 2, and 3 suppress individual order failure emails to avoid noise for orders that may not be due today. System-level errors (login failure, cannot collect orders page, PreGen queue timeout) are always emailed regardless of pass.

### Key Helm Status IDs

| ID                | Status                     |
| ----------------- | -------------------------- |
| `#status_id_3009` | PreGen Failure (problem)   |
| `#status_id_3003` | PreGen (re-trigger target) |

## Stage 1

`automation_stage01.py` handles the first spreadsheet-cleaning section:

1. Log in to Helm.
2. Fetch Rithum/ChannelAdvisor orders via the CA API and save as Basic Layout format.
3. Download the Helm/DC Shipping Report.
4. Match order rows to DC shipping rows using the configured order ID columns.
5. Add DC output fields:
   - `DC Date`
   - `DC Ship M`
   - `DC Track`
6. Filter non-GB rows for review.
7. Fill missing non-GB dispatch values with `Airmail`.
8. Save the rows that still need Stage 2 review to `NON_GB_UNMATCHED_OUTPUT_PATH`.

Stage 2 uses two Stage 1 outputs:

```text
downloads/matched_orders.csv
downloads/non_gb_unmatched_orders_review.csv
```

`matched_orders.csv` is the full Stage 1 enriched order export. This is the base for the final upload template, matching the screenshot workflow where the upload template is populated from the main prepared order file.

`non_gb_unmatched_orders_review.csv` is the smaller review file containing the rows that still need the Full Orders Report lookup.

## Generated Files And Flow

The `downloads` folder contains both source exports and generated review/output files. The important files are:

```text
downloads/rithum_orders.csv
downloads/dc_shipping_report.csv
downloads/matched_orders.csv
downloads/non_gb_orders_review.csv
downloads/non_gb_orders_airmail.csv
downloads/non_gb_orders_with_dc_date_review.csv
downloads/non_gb_unmatched_orders_review.csv
downloads/dc_full_orders_export.csv
downloads/stage2_full_orders_matched.csv
downloads/unmapped_courier_services.csv
downloads/tracking_upload_template_{timestamp}.txt
```

Stage 1 starts with the Rithum order export and the Helm/DC Shipping Report. It produces `matched_orders.csv`, which is the full enriched order file. It also produces review files for non-GB rows, including `non_gb_unmatched_orders_review.csv`, which is the smaller set of rows that still need Stage 2 correction.

Stage 2 downloads the Helm Full Orders Report as `dc_full_orders_export.csv`, matches it against `non_gb_unmatched_orders_review.csv`, and writes the detailed correction review to `stage2_full_orders_matched.csv`. That review file is then merged back into the full Stage 1 `matched_orders.csv` data in memory.

The final output is written to the working folder and then copied to the shared output location:

```text
downloads/tracking_upload_template_{timestamp}.txt          ← working copy
M:\Final Automations\Mark Orders Shipped\Output Files\      ← final export copy
```

That file is the tab-delimited tracking upload handoff. It is built from the full Stage 1 matched export plus any Stage 2 corrections.

The script currently stops here. It does not upload the file to Rithum/ChannelAdvisor or FTP. The final upload step is intentionally paused until the reporting manager has been informed and has approved the upload.

The interaction between files is:

```text
rithum_orders.csv
        +
dc_shipping_report.csv
        |
        v
matched_orders.csv
        |
        +---- non_gb_unmatched_orders_review.csv
                         +
              dc_full_orders_export.csv
                         |
                         v
              stage2_full_orders_matched.csv
                         |
                         v
matched_orders.csv + Stage 2 corrections
                         |
                         v
tracking_upload_template.txt
```

## Stage 2

`automation_stage02.py` continues from the Stage 1 outputs:

1. Log in to Helm.
2. Open Reports.
3. Open the Orders reports section.
4. Request and download the Helm Full Orders Report.
5. Match Stage 1 unmatched rows against the Full Orders Report.
6. Pull Full Orders status and supporting order fields into the Stage 2 matched rows.
7. If an order is `Cancelled`, set `DC Date`, `DC Ship M`, and `DC Track` to `Cancelled`.
8. If an order is `Despatch Ready` and still missing tracking, generate tracking numbers by incrementing the last three digits of a usable tracking seed.
9. Copy the seed shipping method and dispatch date into generated rows where needed.
10. Save the detailed Stage 2 matched output to `FULL_ORDERS_MATCHED_OUTPUT_PATH`.
11. Merge the corrected Stage 2 review rows back into the full Stage 1 `MATCHED_OUTPUT_PATH` rows.
12. Build the final tracking upload rows from the corrected full Stage 1 matched export.
13. Save the final tracking upload handoff as a tab-delimited text file.

The Stage 2 matched output is a detailed review file. It is not the final upload template.

## Tracking Upload Template Logic

The manual process opens a separate upload template and copy/pastes values into it. The automation does not create a separate Excel template workbook. Instead, `automation_stage02.py` memorizes the template layout in code, builds the equivalent rows, and writes the final upload file as tab-delimited text.

The memorized upload-template columns are:

```text
Invoice Number
Tracking Number
Date Shipped
Shipping Carrier Source
Shipping Carrier Code
Shipping Class Code
Prevent Site Processing
```

The script maps values like this:

- `Invoice Number`: from `SiteOrderID`
- `Tracking Number`: from `DC Track`
- `Date Shipped`: from `DC Date`
- `Shipping Carrier Source`: from `DC Ship M`
- `Shipping Carrier Code`: converted from `DC Ship M`
- `Shipping Class Code`: converted from `DC Ship M`
- `Prevent Site Processing`: `TRUE` for cancelled rows, `FALSE` for normal rows

The manual spreadsheet uses formulas and paste-as-values. In Python, those are represented as direct transformations:

- The carrier/class lookup is handled by `courier_conversions.json`.
- International orders are detected from the order country/status before domestic generated-tracking logic runs.
- If an international order has a real tracking number from the DC/Helm lookup, the script keeps that tracking number and uses the courier/class conversion for the matched `DC Ship M` service.
- If an international order has no real tracking number, the script outputs `Royal Mail` / `Airmail` and leaves `Tracking Number` blank.
- Domestic Evri 24 rows with no real tracking number can receive a generated Evri tracking number from the first usable Evri 24 tracking seed in the run.
- The raw helper column is removed in memory after conversion.
- Cancelled rows are flagged with `Prevent Site Processing = TRUE`.
- Non-cancelled rows are flagged with `Prevent Site Processing = FALSE`.
- Unknown courier services are written as `#N/A` and listed in `UNMAPPED_COURIER_SERVICES_OUTPUT_PATH`.
- The final upload rows are reviewed with filters cleared and `Prevent Site Processing` filled for all rows.
- The manual save step is represented as a tab-delimited upload handoff only.
- The CA/Rithum FTP or API upload is intentionally paused until manager approval is confirmed.

The final manual steps save the completed template into the `CA Tracking Update\Out` folder as `Text (Tab delimited)`. The script now writes the same tab-delimited upload handoff to `TRACKING_UPLOAD_OUTPUT_PATH`, but does not upload it. The file is written to the working `downloads/` folder first, then automatically copied to the final output location:

```text
M:\Final Automations\Mark Orders Shipped\Output Files\tracking_upload_template_{timestamp}.txt
```

The `M:\` drive must be mounted and accessible on the machine running the automation. If the drive is unavailable, the copy step fails with a `[WARN]` log but does not stop the automation — the file remains accessible in `downloads/`.

## Courier Conversions

Known courier service mappings live in:

```text
courier_conversions.json
```

The key format is:

```json
{
  "Raw DC Ship M value": {
    "carrier_code": "Shipping Carrier Code",
    "class_code": "Shipping Class Code"
  }
}
```

If the script sees a `DC Ship M` value that is not in `courier_conversions.json`, it writes `#N/A` into the carrier/class upload columns and prints a warning. It also writes the raw unmapped values to:

```text
downloads/unmapped_courier_services.csv
```

That file is only a review list. The confirmed mapping should be added to `courier_conversions.json` before the upload file is used.

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
python -m playwright install
```

If `requirements.txt` does not exist yet, install the current script dependencies directly:

```powershell
pip install playwright python-dotenv
python -m playwright install
```

## Environment Variables

Create a `.env` file in the repo root. Use `.env.example` as the template.

| Variable                    | Required | Used by       | Description                                                    |
| --------------------------- | -------- | ------------- | -------------------------------------------------------------- |
| `HELM_EMAIL`                | Yes      | Stage 0, 1, 2 | Helm login email                                               |
| `HELM_PASSWORD`             | Yes      | Stage 0, 1, 2 | Helm login password                                            |
| `CA_APPLICATION_ID`         | Yes      | Stage 1       | ChannelAdvisor API app ID                                      |
| `CA_SHARED_SECRET`          | Yes      | Stage 1       | ChannelAdvisor API shared secret                               |
| `CA_REFRESH_TOKEN`          | Yes      | Stage 1       | ChannelAdvisor OAuth2 refresh token                            |
| `CA_PROFILE_ID`             | Yes      | Stage 1       | ChannelAdvisor profile ID                                      |
| `AMAZON_EMAIL`              | Yes      | Stage 2       | Amazon Seller Central login email                              |
| `AMAZON_PASSWORD`           | Yes      | Stage 2       | Amazon Seller Central login password                           |
| `HEADLESS`                  | No       | Stage 0, 1, 2 | Set `true` to run browsers without a window (default: `false`) |
| `DEBUG`                     | No       | Stage 0, 1, 2 | Set `true` for verbose `[INFO]` logging (default: `false`)     |
| `NOTIFY_EMAIL_FROM`         | No       | Stage 0       | Gmail address used as the sender for all Stage 0 alerts        |
| `NOTIFY_EMAIL_APP_PASSWORD` | No       | Stage 0       | Gmail App Password for SMTP auth                               |

`NOTIFY_EMAIL_FROM` and `NOTIFY_EMAIL_APP_PASSWORD` are optional. If absent, Stage 0 skips all email sending silently. All Stage 0 emails — both mid-run operational errors and the manual intervention alert — are sent to `deelaka@gudz.com`, `veer@gudz.com`, and `supply@gudz.com`. There is no separate `NOTIFY_EMAIL_TO` variable; the recipients are hardcoded.

Current hardcoded Stage 0 recipients are `supply@gudz.com`, `veer@gudz.com`, `deelaka@gudz.com`, `chamike@gudz.com`, and `lavanga@gudz.com`.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python automation_stage00.py
python automation_stage01.py
python automation_stage02.py
```

If Stage 0 exits with a non-zero code, today's Ship By Date still has unresolved PreGen Failure orders. Do not run Stage 1 or Stage 2 until those failures are manually resolved in Helm.

The scripts print completed steps as they run. Rithum orders are saved to `downloads/rithum_orders.csv` by default. Helm report downloads are saved to `downloads/` by default.

## Streamlit Dashboard

The scripts can also be run from a local Streamlit dashboard:

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py --server.headless true --server.port 8201
```

The dashboard runs Stage 0, Stage 1, and Stage 2 in sequence with a single button. If Stage 0 exits with a non-zero code, Stage 1 and Stage 2 are automatically skipped. The dashboard streams all script logs, shows the current stage, current step, uptime, generated files, and download buttons for the final tab-delimited tracking upload handoff and unmapped courier review file.

Runs launched from the dashboard force `AUTOMATION_HEADLESS=true`, so Playwright runs without opening the browser window even if `.env` has `HEADLESS=false`.

The dashboard still respects the upload pause. It generates the tracking upload handoff in `downloads/` and copies it to `M:\Final Automations\Mark Orders Shipped\Output Files\`; it does not upload to Rithum/ChannelAdvisor or FTP.

## Exposing the Dashboard Publicly with ngrok

ngrok creates a public HTTPS tunnel to the local Streamlit server so team members can access the dashboard remotely without VPN.

### One-time setup

Install ngrok (already available if winget is installed):

```powershell
winget install ngrok.ngrok
```

After installing, open a new terminal so the PATH is refreshed, then add your auth token from [dashboard.ngrok.com/get-started/your-authtoken](https://dashboard.ngrok.com/get-started/your-authtoken):

```powershell
ngrok config add-authtoken <your-authtoken>
```

The token is saved to `%LOCALAPPDATA%\ngrok\ngrok.yml` and only needs to be set once.

### Starting the tunnel

Keep Streamlit running in one terminal, then open a second terminal and run:

```powershell
ngrok http 8501
```

ngrok prints a public URL:

```
Forwarding  https://xxxx-xxxx.ngrok-free.app -> http://localhost:8501
```

Share the `https://...ngrok-free.app` URL with anyone who needs access. The tunnel stays live as long as both processes are running.

### Notes

- The free tier gives a different random URL every time ngrok restarts.
- First-time visitors see a brief ngrok warning page — they click "Visit Site" to proceed.
- If `ngrok` is not recognised in the terminal after install, add it to your user PATH permanently:
  ```powershell
  $ngrokDir = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe"
  [Environment]::SetEnvironmentVariable("PATH", "$env:PATH;$ngrokDir", "User")
  ```
  Open a new terminal after running this — existing sessions won't pick up the change.
  Until then, use the full path directly:
  ```powershell
  & "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 8501
  ```
- Keep your auth token private. If it is ever shared accidentally, regenerate it at [dashboard.ngrok.com/authtokens](https://dashboard.ngrok.com/authtokens) and re-run `ngrok config add-authtoken <new-token>`.
- If you get `ERR_NGROK_334` (endpoint already online), a previous ngrok session is still running. Stop it first:
  ```powershell
  Stop-Process -Name ngrok -Force
  ```
  Then start the tunnel again normally.

## Current Script Map

- `automation_stage00.py`: Helm login, PreGen Failure detection, 4-pass per-order courier/lock fix, and manual intervention email only when today's Ship By Date failures persist.
- `automation_stage01.py`: Helm login, CA API order fetch (Basic Layout format), Shipping Report download, Rithum/order matching, non-GB review outputs.
- `automation_stage02.py`: Full Orders Report download, Stage 2 matching, cancelled/despatch-ready handling, full Stage 1 merge, and tab-delimited tracking upload preparation.
- `app.py`: local operator dashboard for running all three automation stages and downloading the generated handoff file.

## Next Stages

Planned next steps:

1. Add FTP upload or Rithum API upload only after manager approval and the handoff method are confirmed.
2. Add any missing courier mappings to `courier_conversions.json`.
