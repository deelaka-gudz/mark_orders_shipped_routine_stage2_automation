# Mark Orders Shipped Routine - Stage 2 Automation

Python + Playwright automation for the "mark orders shipped" workflow.

Manual process reference:

https://scribehow.com/viewer/How_To_Process_And_Filter_Shipping_Report_Export_Data__yzCBrf8XQ1mNQiWUOE8N8g

This repo is being built step by step from the process guide. The current flow is split into two scripts:

1. `automation_stage01.py`: downloads/matches the Rithum order export against the Helm/DC Shipping Report.
2. `automation_stage02.py`: downloads the Helm Full Orders Report and prepares the final tracking-upload data in memory.

## What Rithum/ChannelAdvisor Does

Rithum, formerly ChannelAdvisor, is the ecommerce platform that provides the order export file. That export tells us which orders need to be shipped.

In the full workflow there are two input files:

- Rithum/ChannelAdvisor order export from email, FTP, or API.
- Helm/DC shipping report containing the real tracking numbers.

`automation_stage01.py` downloads or reads the Rithum order data first, then requests the Helm/DC shipping report.

## Stage 1

`automation_stage01.py` handles the first spreadsheet-cleaning section:

1. Log in to Helm.
2. Optionally download Rithum/ChannelAdvisor orders through the API.
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
downloads/tracking_upload_template.txt
```

Stage 1 starts with the Rithum order export and the Helm/DC Shipping Report. It produces `matched_orders.csv`, which is the full enriched order file. It also produces review files for non-GB rows, including `non_gb_unmatched_orders_review.csv`, which is the smaller set of rows that still need Stage 2 correction.

Stage 2 downloads the Helm Full Orders Report as `dc_full_orders_export.csv`, matches it against `non_gb_unmatched_orders_review.csv`, and writes the detailed correction review to `stage2_full_orders_matched.csv`. That review file is then merged back into the full Stage 1 `matched_orders.csv` data in memory.

The final output is:

```text
downloads/tracking_upload_template.txt
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
Invoice No
Tracking Number
Date Shipped
Shipping Carrier Source
Shipping Carrier Code
Shipping Class Code
Prevent Site Processing
```

The script maps values like this:

- `Invoice No`: from `SiteOrderID`
- `Tracking Number`: from `DC Track`
- `Date Shipped`: from `DC Date`
- `Shipping Carrier Source`: from `DC Ship M`
- `Shipping Carrier Code`: converted from `DC Ship M`
- `Shipping Class Code`: converted from `DC Ship M`
- `Prevent Site Processing`: `TRUE` for cancelled rows, `FALSE` for normal rows

The manual spreadsheet uses formulas and paste-as-values. In Python, those are represented as direct transformations:

- The carrier/class lookup is handled by `courier_conversions.json`.
- The raw helper column is removed in memory after conversion.
- Cancelled rows are flagged with `Prevent Site Processing = TRUE`.
- Non-cancelled rows are flagged with `Prevent Site Processing = FALSE`.
- Unknown courier services are written as `#N/A` and listed in `UNMAPPED_COURIER_SERVICES_OUTPUT_PATH`.
- The final upload rows are reviewed with filters cleared and `Prevent Site Processing` filled for all rows.
- The manual save step is represented as a tab-delimited upload handoff only.
- The CA/Rithum FTP or API upload is intentionally paused until manager approval is confirmed.

The final manual steps save the completed template into the `CA Tracking Update\Out` folder as `Text (Tab delimited)`. The script now writes the same tab-delimited upload handoff to `TRACKING_UPLOAD_OUTPUT_PATH`, but does not upload it. By default this is:

```text
downloads/tracking_upload_template.txt
```

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

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python automation_stage01.py
python automation_stage02.py
```

The scripts print completed steps as they run. Rithum orders are saved to `downloads/rithum_orders.csv` by default. Helm report downloads are saved to `downloads/` by default.

## Streamlit Dashboard

The scripts can also be run from a local Streamlit dashboard:

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py --server.headless true
```

The dashboard provides one button to run Stage 1 and then Stage 2 with the same Python environment. It streams the script logs, shows the current stage, current step, uptime, generated files, and download buttons for the final tab-delimited tracking upload handoff and unmapped courier review file.

Runs launched from the dashboard force `AUTOMATION_HEADLESS=true`, so Playwright runs without opening the browser window even if `.env` has `HEADLESS=false`.

The dashboard still respects the upload pause. It only generates `downloads/tracking_upload_template.txt`; it does not upload to Rithum/ChannelAdvisor or FTP.

## Current Script Map

- `automation_stage01.py`: Helm login, Shipping Report download, Rithum/order matching, non-GB review outputs.
- `automation_stage02.py`: Full Orders Report download, Stage 2 matching, cancelled/despatch-ready handling, full Stage 1 merge, and tab-delimited tracking upload preparation.
- `app.py`: local operator dashboard for running both automation stages and downloading the generated handoff file.

## Next Stages

Planned next steps:

1. Add FTP upload or Rithum API upload only after manager approval and the handoff method are confirmed.
2. Add any missing courier mappings to `courier_conversions.json`.
