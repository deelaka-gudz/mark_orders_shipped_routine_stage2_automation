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
- The manual save step is represented as a tab-delimited upload handoff, ready for the CA/Rithum FTP process or a future API upload.

The final manual steps save the completed template into the `CA Tracking Update\Out` folder as `Text (Tab delimited)`. The script now writes the same tab-delimited upload handoff to `TRACKING_UPLOAD_OUTPUT_PATH`. By default this is:

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

Create a `.env` file in the repo root:

```env
HELM_URL=https://mybeautyandcareltd.myhelm.app/
HELM_EMAIL=your_email@example.com
HELM_PASSWORD=your_password

RITHUM_BASE_URL=https://api.channeladvisor.com

# Optional
HELM_REPORT_DOWNLOAD_DIR=downloads
HELM_REPORT_READY_TIMEOUT_SECONDS=900
RITHUM_ENABLED=false
RITHUM_APPLICATION_ID=
RITHUM_SHARED_SECRET=
RITHUM_REFRESH_TOKEN=
RITHUM_ORDERS_OUTPUT_PATH=downloads/rithum_orders.csv
ORDER_FILE_PATH=
DC_SHIPPING_REPORT_PATH=
MATCHED_OUTPUT_PATH=downloads/matched_orders.csv
NON_GB_OUTPUT_PATH=downloads/non_gb_orders_review.csv
NON_GB_AIRMAIL_OUTPUT_PATH=downloads/non_gb_orders_airmail.csv
NON_GB_WITH_DC_DATE_OUTPUT_PATH=downloads/non_gb_orders_with_dc_date_review.csv
NON_GB_UNMATCHED_OUTPUT_PATH=downloads/non_gb_unmatched_orders_review.csv
FULL_ORDERS_MATCHED_OUTPUT_PATH=downloads/stage2_full_orders_matched.csv
TRACKING_UPLOAD_OUTPUT_PATH=downloads/tracking_upload_template.txt
COURIER_CONVERSIONS_PATH=courier_conversions.json
UNMAPPED_COURIER_SERVICES_OUTPUT_PATH=downloads/unmapped_courier_services.csv
MATCH_KEY_COLUMN=Order ID
ORDER_MATCH_KEY_COLUMN=SiteOrderID
DC_MATCH_KEY_COLUMN=Order ID
SHIPPING_COUNTRY_COLUMN=Shipping Country
FULL_ORDERS_MATCH_KEY_COLUMN=SiteOrderID
FULL_ORDERS_REPORT_KEY_COLUMN=Channel Order ID
RITHUM_ORDERS_QUERY_STRING=$top=100
RITHUM_MAX_PAGES=20
HEADLESS=false
DEBUG=false
```

Optional values:

- `HELM_REPORT_DOWNLOAD_DIR`: where downloaded reports are saved.
- `HELM_REPORT_READY_TIMEOUT_SECONDS`: how long to wait for Helm's History export job to become `Completed`.
- `RITHUM_ENABLED`: set to `true` after admin provides Rithum API credentials.
- `RITHUM_APPLICATION_ID`: Rithum/ChannelAdvisor API application ID.
- `RITHUM_SHARED_SECRET`: Rithum/ChannelAdvisor API shared secret.
- `RITHUM_REFRESH_TOKEN`: Rithum/ChannelAdvisor API refresh token.
- `RITHUM_ORDERS_OUTPUT_PATH`: where the Rithum orders CSV is saved.
- `ORDER_FILE_PATH`: local order export CSV to match when the Rithum API step is skipped.
- `DC_SHIPPING_REPORT_PATH`: detailed Helm `dc_shipping_report` CSV to match against.
- `MATCHED_OUTPUT_PATH`: output CSV created by the Python VLOOKUP-style match.
- `NON_GB_OUTPUT_PATH`: review CSV containing rows whose shipping country is not `GB`.
- `NON_GB_AIRMAIL_OUTPUT_PATH`: non-GB CSV after filling missing DC values with `Airmail`.
- `NON_GB_WITH_DC_DATE_OUTPUT_PATH`: non-GB review CSV containing rows that still have a real DC date after Airmail defaults.
- `NON_GB_UNMATCHED_OUTPUT_PATH`: non-GB review CSV containing rows that had blank or `#N/A` DC Date before Airmail defaults.
- `FULL_ORDERS_MATCHED_OUTPUT_PATH`: detailed Stage 2 output after matching Stage 1 unmatched rows to the Full Orders Report.
- `TRACKING_UPLOAD_OUTPUT_PATH`: final tab-delimited tracking upload file shaped like the memorized upload template.
- `COURIER_CONVERSIONS_PATH`: JSON file containing known courier service to Rithum carrier/class mappings.
- `UNMAPPED_COURIER_SERVICES_OUTPUT_PATH`: review CSV listing raw courier services that did not exist in the conversion JSON.
- `MATCH_KEY_COLUMN`: column name that exists in both files, for example `Order ID`.
- `ORDER_MATCH_KEY_COLUMN`: order export column used for the Stage 1 lookup.
- `DC_MATCH_KEY_COLUMN`: DC shipping report column used for the Stage 1 lookup.
- `SHIPPING_COUNTRY_COLUMN`: country column used to reproduce the Excel non-GB filter.
- `FULL_ORDERS_MATCH_KEY_COLUMN`: Stage 1 unmatched column used to match against the Full Orders Report.
- `FULL_ORDERS_REPORT_KEY_COLUMN`: Full Orders Report column used for the Stage 2 lookup.
- `RITHUM_ORDERS_QUERY_STRING`: OData query string passed to `/v1/orders`.
- `RITHUM_MAX_PAGES`: maximum number of paginated Rithum API pages to read.
- `HEADLESS`: set to `true` to run without opening the browser.
- `DEBUG`: set to `true` for extra logging.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python automation_stage01.py
python automation_stage02.py
```

The scripts print completed steps as they run. Rithum orders are saved to `downloads/rithum_orders.csv` by default. Helm report downloads are saved to `downloads/` by default.

## Current Script Map

- `automation_stage01.py`: Helm login, Shipping Report download, Rithum/order matching, non-GB review outputs.
- `automation_stage02.py`: Full Orders Report download, Stage 2 matching, cancelled/despatch-ready handling, full Stage 1 merge, and tab-delimited tracking upload preparation.

## Next Stages

Planned next steps:

1. Add FTP upload or Rithum API upload once the handoff method is confirmed.
2. Add any missing courier mappings to `courier_conversions.json`.
