# Mark Orders Shipped Routine - Stage 2 Automation

Python + Playwright automation for the first stage of the "mark orders shipped" workflow.

Manual process reference:

https://scribehow.com/viewer/How_To_Process_And_Filter_Shipping_Report_Export_Data__yzCBrf8XQ1mNQiWUOE8N8g

This repo is being built step by step from the process guide. The current script focuses on the Helm/DC side:

1. Download Rithum/ChannelAdvisor orders using the API.
2. Log in to Helm.
3. Open Reports.
4. Open the Shipping report.
5. Click Download Report.
6. Save the downloaded report locally.

## What Rithum/ChannelAdvisor Does

Rithum, formerly ChannelAdvisor, is the ecommerce platform that provides the order export file. That export tells us which orders need to be shipped.

In the full workflow there are two input files:

- Rithum/ChannelAdvisor order export from email, FTP, or API.
- Helm/DC shipping report containing the real tracking numbers.

The current `automation.py` downloads the Rithum order data first, then requests the Helm/DC shipping report.

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
MATCH_KEY_COLUMN=Order ID
SHIPPING_COUNTRY_COLUMN=Shipping Country
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
- `MATCH_KEY_COLUMN`: column name that exists in both files, for example `Order ID`.
- `SHIPPING_COUNTRY_COLUMN`: country column used to reproduce the Excel non-GB filter.
- `RITHUM_ORDERS_QUERY_STRING`: OData query string passed to `/v1/orders`.
- `RITHUM_MAX_PAGES`: maximum number of paginated Rithum API pages to read.
- `HEADLESS`: set to `true` to run without opening the browser.
- `DEBUG`: set to `true` for extra logging.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python automation.py
```

The script prints completed steps as it runs. Rithum orders are saved to `downloads/rithum_orders.csv` by default. If Helm emits a direct browser download, that file is saved to `downloads/` by default.

## Current Script Map

- `automation.py`: active script for downloading Rithum orders and requesting the Helm Shipping Report.
- `clf_temp.py`: previous Helm automation containing the reusable login flow and helper functions.

## Next Stages

Planned next steps:

1. Confirm the correct match column between the Rithum order export and DC report.
2. Confirm the final output columns needed to mark orders as shipped.
