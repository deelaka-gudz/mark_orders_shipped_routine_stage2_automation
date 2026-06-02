# Mark Orders Shipped Routine - Stage 1 Automation

Python + Playwright automation for the first stage of the "mark orders shipped" workflow.

Manual process reference:

https://scribehow.com/viewer/How_To_Process_And_Filter_Shipping_Report_Export_Data__yzCBrf8XQ1mNQiWUOE8N8g

This project replaces the repetitive parts of the manual Helm, Rithum/ChannelAdvisor, and Excel workflow with one Python script.

## The Manual Process

The manual process starts with two reports:

- A Rithum/ChannelAdvisor order export containing the orders that need shipment data.
- A Helm/DC Shipping Report containing dispatch dates, shipping services, and tracking numbers.

In Excel, the order export is then updated with VLOOKUP formulas against the DC Shipping Report. The manual guide fills three output columns:

- `DC Date`
- `DC Ship M`
- `DC Track`

After the formulas calculate, the manual process copies those formula results and pastes them back as values. It then filters the shipping country column to exclude `GB`, reviews the non-GB orders, fills missing non-GB DC values with `Airmail`, and creates smaller review groups for rows that still need attention.

## What This Script Does Instead

`automation.py` keeps the same business logic, but performs it as API, browser, and CSV processing instead of manual Excel work.

Current flow:

1. Log in to Helm.
2. Open the Helm Reports page.
3. Open the Shipping reports section.
4. Request a new Helm Shipping Report export.
5. Go to Helm Export History.
6. Wait while the newest `Shipping Report` row is `Pending`, `Queued`, or `Processing`.
7. Download only that same row once its status becomes `Completed`.
8. Download recent Rithum/ChannelAdvisor orders from the API.
9. Match Rithum orders to the DC Shipping Report.
10. Add `DC Date`, `DC Ship M`, and `DC Track` directly as CSV values.
11. Create the same non-GB review outputs that the Excel filtering steps produce.

The key replacement is that Python does the VLOOKUP work directly:

- Rithum order key: `SiteOrderID`
- DC Shipping Report key: `Order ID`
- `DC Date`: from `Date Despatched`
- `DC Ship M`: from `Shipping Service Booked`
- `DC Track`: from `Consignment Number`

Because the output is CSV, there are no Excel formulas to calculate or convert to values. The generated files already contain final values.

## Step 4/5 Reliability

The manual guide says the Helm Shipping Report can be delivered from the History section, by email, or potentially by API. This automation uses the History section as the primary path because it is the closest match to the manual process and it keeps the run inside the active Helm session.

The script protects this step in two ways:

1. It does not download an older completed report. It watches the newest row whose type is exactly `Shipping Report`.
2. If the newest Shipping Report becomes `Cancelled` or `Failed`, it requests a fresh Shipping Report export and starts watching History again.
3. Once that row is `Completed`, it reads the row's `dc_shipping_report.csv` link and downloads it directly through Playwright's authenticated browser request context. That means it uses the same Helm login cookies/session, without depending on Outlook or a visible browser download prompt.

If the direct authenticated History download is not available, the script falls back to clicking the row's download button. Outlook/email should be treated as a later fallback only if Helm stops exposing a usable History download link.

## Output Files

By default the workflow writes these files:

- `downloads/rithum_orders.csv`: recent Rithum/ChannelAdvisor orders from the API.
- `downloads/dc_shipping_report.csv`: Helm/DC Shipping Report.
- `downloads/matched_orders.csv`: Rithum orders enriched with DC values.
- `downloads/non_gb_orders_review.csv`: matched rows where shipping country is not `GB`.
- `downloads/non_gb_orders_airmail.csv`: non-GB rows after filling missing `DC Date`, `DC Ship M`, and `DC Track` with `Airmail`.
- `downloads/non_gb_orders_with_dc_date_review.csv`: non-GB rows that still have a real DC date after Airmail defaults.
- `downloads/non_gb_unmatched_orders_review.csv`: non-GB rows that had blank or `#N/A` DC Date before Airmail defaults.

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

## Environment Variables

Create a `.env` file in the repo root. Use `.env.example` as the template.

```env
HELM_URL=https://mybeautyandcareltd1.myhelm.app/login.php?type=standard
HELM_EMAIL=your_helm_email@example.com
HELM_PASSWORD=your_helm_password

HELM_REPORT_DOWNLOAD_DIR=downloads
HELM_REPORT_READY_TIMEOUT_SECONDS=2400
HELM_MANUAL_LOGIN_FALLBACK=true
HELM_MANUAL_LOGIN_TIMEOUT_SECONDS=300

RITHUM_ENABLED=true
RITHUM_BASE_URL=https://api.channeladvisor.com
RITHUM_APPLICATION_ID=
RITHUM_SHARED_SECRET=
RITHUM_REFRESH_TOKEN=
RITHUM_ACCESS_TOKEN=
RITHUM_ORDERS_OUTPUT_PATH=downloads/rithum_orders.csv
RITHUM_ORDERS_QUERY_STRING=$top=100&$orderby=CreatedDateUtc desc
RITHUM_MAX_PAGES=20

ORDER_FILE_PATH=downloads/rithum_orders.csv
DC_SHIPPING_REPORT_PATH=downloads/dc_shipping_report.csv
MATCHED_OUTPUT_PATH=downloads/matched_orders.csv
NON_GB_OUTPUT_PATH=downloads/non_gb_orders_review.csv
NON_GB_AIRMAIL_OUTPUT_PATH=downloads/non_gb_orders_airmail.csv
NON_GB_WITH_DC_DATE_OUTPUT_PATH=downloads/non_gb_orders_with_dc_date_review.csv
NON_GB_UNMATCHED_OUTPUT_PATH=downloads/non_gb_unmatched_orders_review.csv

MATCH_KEY_COLUMN=Order ID
ORDER_MATCH_KEY_COLUMN=SiteOrderID
DC_MATCH_KEY_COLUMN=Order ID
SHIPPING_COUNTRY_COLUMN=ShippingCountry

HEADLESS=false
DEBUG=false
```

Important notes:

- `HELM_EMAIL` and `HELM_PASSWORD` are for Helm, not the Rithum developer account.
- `RITHUM_APPLICATION_ID`, `RITHUM_SHARED_SECRET`, and `RITHUM_REFRESH_TOKEN` come from the Rithum/ChannelAdvisor Developer Console.
- `RITHUM_ACCESS_TOKEN` is optional. The script currently uses the refresh token to generate fresh access tokens automatically.
- `RITHUM_ORDERS_QUERY_STRING` sorts by `CreatedDateUtc desc` so the API export contains recent orders first.
- `HELM_REPORT_READY_TIMEOUT_SECONDS=2400` allows up to 40 minutes for slow Helm export jobs.
- `HELM_MANUAL_LOGIN_FALLBACK=true` keeps the browser open if Helm rejects automated login, so you can log in manually and let the automation continue.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python automation.py
```

The script prints `[DONE]` messages as it completes each stage. During the Helm Shipping Report export it also prints `[WAIT]` messages showing the current History status, elapsed time, and remaining timeout. If Helm rejects the automated login, manually log in inside the open browser window. The script will continue after it detects that Helm is logged in.

## Running Only Part of the Workflow

Refresh only the Rithum order export:

```powershell
python -c "from automation import Config, download_rithum_orders; download_rithum_orders(Config.load())"
```

Run only the CSV matching/review output steps:

```powershell
python -c "from automation import Config, match_order_file_to_dc_shipping_report; match_order_file_to_dc_shipping_report(Config.load())"
```

## Step Mapping

Manual guide steps 6-10:

- Manual: open the order file and enter VLOOKUP formulas against the DC Shipping Report.
- Script: reads `downloads/rithum_orders.csv` and `downloads/dc_shipping_report.csv`, then matches `SiteOrderID` to `Order ID`.

Manual guide steps 11-26:

- Manual: fill VLOOKUP results down into `DC Date`, `DC Ship M`, and `DC Track`, calculate formulas, copy, and paste as values.
- Script: writes `DC Date`, `DC Ship M`, and `DC Track` directly as plain CSV values in `matched_orders.csv`.

Manual guide steps 27-33:

- Manual: filter shipping country and exclude `GB`.
- Script: writes non-GB rows to `non_gb_orders_review.csv`.

Manual guide steps 34-43:

- Manual: fill missing non-GB DC values with `Airmail`.
- Script: writes the Airmail-filled version to `non_gb_orders_airmail.csv`.

Manual guide steps 44-53:

- Manual: filter by `DC Date` to review real DC dates and `#N/A` rows.
- Script: writes those groups to `non_gb_orders_with_dc_date_review.csv` and `non_gb_unmatched_orders_review.csv`.

## Safety

Do not commit `.env`. It contains Helm credentials and Rithum API secrets. If a shared secret, refresh token, or access token is exposed in chat or screenshots, rotate the Rithum application credentials before production use.
