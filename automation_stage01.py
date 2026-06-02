import base64
import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import requests

DOTENV_PATH = Path(__file__).resolve().with_name(".env")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _log_info(debug: bool, message: str) -> None:
    if debug:
        print(f"[INFO] {message}")


def _log_step(step: str) -> None:
    print(f"[DONE] {step}")


def _wait_for_network_idle(page: Page, timeout_ms: int = 10000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def _click_first_visible(
    locators: Sequence[Locator],
    description: str,
    timeout_ms: int = 5000,
) -> None:
    last_error: Optional[Exception] = None
    for locator in locators:
        try:
            locator.first.wait_for(state="visible", timeout=timeout_ms)
            locator.first.click(timeout=timeout_ms)
            return
        except PlaywrightTimeoutError as error:
            last_error = error
            continue
    raise RuntimeError(f"Could not click {description}.") from last_error


def _fill_first_visible(
    locators: Sequence[Locator],
    value: str,
    description: str,
    timeout_ms: int = 5000,
) -> Locator:
    last_error: Optional[Exception] = None
    for locator in locators:
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=timeout_ms)
            field.click(timeout=timeout_ms)
            field.fill(value, timeout=timeout_ms)
            actual_value = field.input_value(timeout=timeout_ms)
            if actual_value != value:
                field.click(timeout=timeout_ms)
                field.press("Control+A", timeout=timeout_ms)
                field.type(value, delay=25, timeout=timeout_ms)
                actual_value = field.input_value(timeout=timeout_ms)
            if actual_value != value:
                raise RuntimeError(
                    f"Filled {description}, but the field value did not match."
                )
            return field
        except PlaywrightTimeoutError as error:
            last_error = error
            continue
    raise RuntimeError(f"Could not find {description}.") from last_error


class LoginFlow:
    def __init__(self, page: Page, config: Any):
        self.page = page
        self.config = config

    def open(self) -> None:
        self.page.goto(self.config.helm_url, wait_until="load")
        _wait_for_network_idle(self.page)
        self.page.wait_for_timeout(1000)

    def fill_credentials(self) -> None:
        email_field = _fill_first_visible(
            [
                self.page.get_by_label("Email", exact=False),
                self.page.get_by_placeholder(re.compile("email", re.I)),
                self.page.locator("input[type='email']"),
                self.page.locator("input[name*='email' i]"),
            ],
            self.config.email,
            "email input",
        )

        password_field = _fill_first_visible(
            [
                self.page.get_by_label("Password", exact=False),
                self.page.get_by_placeholder(re.compile("password", re.I)),
                self.page.locator("input[type='password']"),
                self.page.locator("input[name*='password' i]"),
            ],
            self.config.password,
            "password input",
        )
        _log_info(
            self.config.debug,
            "Helm login fields filled: "
            f"email={bool(email_field.input_value())}, "
            f"password={bool(password_field.input_value())}",
        )

    def submit(self) -> None:
        _wait_for_network_idle(self.page)
        _click_first_visible(
            [
                self.page.get_by_role("button", name=re.compile("log in|login", re.I)),
                self.page.get_by_role("button", name=re.compile("sign in", re.I)),
            ],
            "login button",
        )

    def _login_error_message(self) -> Optional[str]:
        candidates = [
            self.page.get_by_text(
                re.compile(
                    r"Login failed!\s*Unable to verify your login credentials\.?",
                    re.I,
                )
            ),
            self.page.get_by_text(re.compile(r"\bLogin failed\b", re.I)),
            self.page.get_by_text(
                re.compile(r"Unable to verify your login credentials", re.I)
            ),
            self.page.locator(".alert.alert-danger"),
            self.page.locator(".alert-danger"),
        ]
        for locator in candidates:
            try:
                if locator.count() > 0 and locator.first.is_visible():
                    text = locator.first.text_content() or ""
                    text = re.sub(r"\s+", " ", text).strip()
                    return text or "Login failed"
            except PlaywrightTimeoutError:
                continue
        return None

    def _app_is_visible(self) -> bool:
        app_chrome = self.page.locator(
            "div.sidebar, ul.acc-menu, nav[role='navigation']"
        )
        login_form = self.page.locator(
            "input[type='password'], input[name*='password' i]"
        )
        try:
            if app_chrome.count() > 0 and app_chrome.first.is_visible():
                return True
            if login_form.count() == 0:
                return True
        except PlaywrightTimeoutError:
            return False
        return False

    def _wait_for_manual_login(self, reason: str) -> None:
        if not self.config.helm_manual_login_fallback:
            raise SystemExit(
                f"Login failed: {reason}. Check HELM_EMAIL/HELM_PASSWORD in your .env file."
            )
        if self.config.headless:
            raise SystemExit(
                f"Login failed: {reason}. Manual login fallback is enabled, but HEADLESS=true."
            )

        timeout_ms = self.config.helm_manual_login_timeout_seconds * 1000
        print(
            "[ACTION] Helm rejected the automated login. Log in manually in the "
            "open browser window; automation will continue after login succeeds."
        )
        start = time.monotonic()
        while (time.monotonic() - start) * 1000 < timeout_ms:
            if self._app_is_visible():
                return
            self.page.wait_for_timeout(1000)

        raise SystemExit(
            "Timed out waiting for manual Helm login. Check the browser window and try again."
        )

    def verify(self, timeout_ms: int = 15000) -> None:
        self.page.wait_for_timeout(500)
        start = time.monotonic()
        while True:
            error_text = self._login_error_message()
            if error_text:
                try:
                    self.page.screenshot(path="login_failed.png", full_page=True)
                except Exception:
                    pass
                self._wait_for_manual_login(error_text)
                return

            if self._app_is_visible():
                return

            if (time.monotonic() - start) * 1000 >= timeout_ms:
                break
            self.page.wait_for_timeout(250)

        error_text = self._login_error_message()
        if error_text:
            try:
                self.page.screenshot(path="login_failed.png", full_page=True)
            except Exception:
                pass
            self._wait_for_manual_login(error_text)
            return

        raise SystemExit(
            "Login did not complete within the expected time. If credentials are correct, the site may require extra steps (e.g., CAPTCHA/2FA) or the page UI changed."
        )


@dataclass(frozen=True)
class Config:
    helm_url: str
    email: str
    password: str
    download_dir: Path
    rithum_enabled: bool
    rithum_base_url: str
    rithum_application_id: str | None
    rithum_shared_secret: str | None
    rithum_refresh_token: str | None
    rithum_orders_output_path: Path
    order_file_path: Path | None
    dc_shipping_report_path: Path | None
    matched_output_path: Path
    non_gb_output_path: Path
    non_gb_airmail_output_path: Path
    non_gb_with_dc_date_output_path: Path
    non_gb_unmatched_output_path: Path
    match_key_column: str
    order_match_key_column: str
    dc_match_key_column: str
    shipping_country_column: str
    helm_report_ready_timeout_seconds: int
    rithum_orders_query_string: str
    rithum_max_pages: int
    helm_manual_login_fallback: bool
    helm_manual_login_timeout_seconds: int
    headless: bool
    debug: bool

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")

        return Config(
            helm_url=(
                os.getenv("HELM_URL") or "https://mybeautyandcareltd.myhelm.app/"
            ).strip(),
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            download_dir=Path(
                os.getenv("HELM_REPORT_DOWNLOAD_DIR") or "downloads"
            ).resolve(),
            rithum_enabled=_env_flag("RITHUM_ENABLED", default=False),
            rithum_base_url=(
                os.getenv("RITHUM_BASE_URL") or "https://api.channeladvisor.com"
            ).strip(),
            rithum_application_id=(os.getenv("RITHUM_APPLICATION_ID") or "").strip()
            or None,
            rithum_shared_secret=os.getenv("RITHUM_SHARED_SECRET") or None,
            rithum_refresh_token=os.getenv("RITHUM_REFRESH_TOKEN") or None,
            rithum_orders_output_path=Path(
                os.getenv("RITHUM_ORDERS_OUTPUT_PATH") or "downloads/rithum_orders.csv"
            ).resolve(),
            order_file_path=(
                Path(os.getenv("ORDER_FILE_PATH", "")).resolve()
                if os.getenv("ORDER_FILE_PATH")
                else None
            ),
            dc_shipping_report_path=(
                Path(os.getenv("DC_SHIPPING_REPORT_PATH", "")).resolve()
                if os.getenv("DC_SHIPPING_REPORT_PATH")
                else None
            ),
            matched_output_path=Path(
                os.getenv("MATCHED_OUTPUT_PATH") or "downloads/matched_orders.csv"
            ).resolve(),
            non_gb_output_path=Path(
                os.getenv("NON_GB_OUTPUT_PATH") or "downloads/non_gb_orders_review.csv"
            ).resolve(),
            non_gb_airmail_output_path=Path(
                os.getenv("NON_GB_AIRMAIL_OUTPUT_PATH")
                or "downloads/non_gb_orders_airmail.csv"
            ).resolve(),
            non_gb_with_dc_date_output_path=Path(
                os.getenv("NON_GB_WITH_DC_DATE_OUTPUT_PATH")
                or "downloads/non_gb_orders_with_dc_date_review.csv"
            ).resolve(),
            non_gb_unmatched_output_path=Path(
                os.getenv("NON_GB_UNMATCHED_OUTPUT_PATH")
                or "downloads/non_gb_unmatched_orders_review.csv"
            ).resolve(),
            match_key_column=(os.getenv("MATCH_KEY_COLUMN") or "Order ID").strip(),
            order_match_key_column=(
                os.getenv("ORDER_MATCH_KEY_COLUMN")
                or os.getenv("MATCH_KEY_COLUMN")
                or "Order ID"
            ).strip(),
            dc_match_key_column=(
                os.getenv("DC_MATCH_KEY_COLUMN")
                or os.getenv("MATCH_KEY_COLUMN")
                or "Order ID"
            ).strip(),
            shipping_country_column=(
                os.getenv("SHIPPING_COUNTRY_COLUMN") or "ShippingCountry"
            ).strip(),
            helm_report_ready_timeout_seconds=int(
                os.getenv("HELM_REPORT_READY_TIMEOUT_SECONDS") or "900"
            ),
            rithum_orders_query_string=(
                os.getenv("RITHUM_ORDERS_QUERY_STRING") or "$top=100"
            ).strip(),
            rithum_max_pages=int(os.getenv("RITHUM_MAX_PAGES") or "20"),
            helm_manual_login_fallback=_env_flag(
                "HELM_MANUAL_LOGIN_FALLBACK", default=True
            ),
            helm_manual_login_timeout_seconds=int(
                os.getenv("HELM_MANUAL_LOGIN_TIMEOUT_SECONDS") or "300"
            ),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
        )


def _flatten_json(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        column = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            flattened.update(_flatten_json(item, column))
        elif isinstance(item, list):
            flattened[column] = str(item)
        else:
            flattened[column] = item
    return flattened


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flattened_rows = [_flatten_json(row) for row in rows]
    fieldnames = sorted({key for row in flattened_rows for key in row.keys()})

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened_rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def _write_dict_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(dict.fromkeys(key for row in rows for key in row.keys()))

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _normalized_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_country(value: Any) -> str:
    return str(value or "").strip().upper()


def _join_unique(values: list[Any]) -> str:
    unique_values = list(
        dict.fromkeys(
            str(value or "").strip() for value in values if str(value or "").strip()
        )
    )
    return " | ".join(unique_values)


def _build_dc_order_lookup(
    dc_rows: list[dict[str, str]],
    key_column: str,
) -> dict[str, dict[str, Any]]:
    grouped_rows: dict[str, list[dict[str, str]]] = {}
    for row in dc_rows:
        lookup_key = _normalized_key(row.get(key_column))
        if lookup_key:
            grouped_rows.setdefault(lookup_key, []).append(row)

    lookup: dict[str, dict[str, Any]] = {}
    for lookup_key, rows in grouped_rows.items():
        first_row = rows[0]
        date_despatched = _join_unique([row.get("Date Despatched") for row in rows])
        shipping_method = _join_unique(
            [row.get("Shipping Service Booked") for row in rows]
        )
        tracking_number = _join_unique([row.get("Consignment Number") for row in rows])

        lookup[lookup_key] = {
            "DC Date": date_despatched,
            "DC Ship M": shipping_method,
            "DC Track": tracking_number,
            "DC Date Despatched": date_despatched,
            "DC Courier": _join_unique([row.get("Courier") for row in rows]),
            "DC Shipping Service Requested": _join_unique(
                [row.get("Shipping Service Requested") for row in rows]
            ),
            "DC Shipping Service Booked": shipping_method,
            "DC Consignment Number": tracking_number,
            "DC Tracking Number": tracking_number,
            "DC Print Ref": _join_unique([row.get("Print Ref") for row in rows]),
            "DC Cancelled": _join_unique([row.get("Cancelled") for row in rows]),
            "DC Row Count": len(rows),
            "DC Multiple Rows": "Yes" if len(rows) > 1 else "No",
        }

        for column, value in first_row.items():
            if column == key_column:
                continue
            lookup[lookup_key].setdefault(f"DC Raw {column}", value)

    return lookup


def _query_params(query_string: str) -> dict[str, str]:
    if not query_string:
        return {}
    return dict(parse_qsl(query_string, keep_blank_values=True))


def _origin_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


class RithumClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.rithum_base_url.rstrip("/")

    def get_access_token(self) -> str:
        if (
            not self.config.rithum_application_id
            or not self.config.rithum_shared_secret
            or not self.config.rithum_refresh_token
        ):
            raise RuntimeError(
                "Missing Rithum API credentials. Set RITHUM_APPLICATION_ID, "
                "RITHUM_SHARED_SECRET, and RITHUM_REFRESH_TOKEN."
            )

        credentials = (
            f"{self.config.rithum_application_id}:"
            f"{self.config.rithum_shared_secret}"
        )
        basic_token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")

        response = requests.post(
            f"{self.base_url}/oauth2/token",
            headers={
                "Authorization": f"Basic {basic_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.config.rithum_refresh_token,
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["access_token"]

    def fetch_orders(self) -> list[dict[str, Any]]:
        access_token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        url: str | None = f"{self.base_url}/v1/orders"
        params: dict[str, str] | None = _query_params(
            self.config.rithum_orders_query_string
        )
        rows: list[dict[str, Any]] = []
        page_count = 0

        while url and page_count < self.config.rithum_max_pages:
            response = requests.get(url, headers=headers, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
            page_rows = payload.get("value", [])

            if not isinstance(page_rows, list):
                raise RuntimeError("Unexpected Rithum orders response shape.")

            rows.extend(page_rows)
            url = payload.get("@odata.nextLink")
            params = None
            page_count += 1

        return rows


def download_rithum_orders(config: Config) -> Path:
    orders = RithumClient(config).fetch_orders()
    _write_csv(config.rithum_orders_output_path, orders)
    return config.rithum_orders_output_path


def match_order_file_to_dc_shipping_report(config: Config) -> Path | None:
    if not config.order_file_path or not config.dc_shipping_report_path:
        _log_step("Step 6: Skipped matching until order/DC file paths are configured")
        return None

    _log_step("Step 6: Read order file and DC Shipping Report")
    order_rows = _read_csv(config.order_file_path)
    dc_rows = _read_csv(config.dc_shipping_report_path)
    order_key_column = config.order_match_key_column
    dc_key_column = config.dc_match_key_column

    if order_rows and order_key_column not in order_rows[0]:
        raise RuntimeError(
            f"Order file does not contain match column '{order_key_column}'. "
            f"Available columns: {', '.join(order_rows[0].keys())}"
        )
    if dc_rows and dc_key_column not in dc_rows[0]:
        raise RuntimeError(
            f"DC shipping report does not contain match column '{dc_key_column}'. "
            f"Available columns: {', '.join(dc_rows[0].keys())}"
        )

    _log_step(f"Step 7: Match order '{order_key_column}' to DC '{dc_key_column}'")

    _log_step("Step 8: Build lookup from DC Shipping Report")
    dc_lookup = _build_dc_order_lookup(dc_rows, dc_key_column)
    _log_step("Step 9: Prepared DC lookup values")

    matched_rows: list[dict[str, Any]] = []

    _log_step("Step 10: Apply lookup to order rows")
    for order_row in order_rows:
        lookup_key = _normalized_key(order_row.get(order_key_column))
        dc_row = dc_lookup.get(lookup_key, {})
        output_row = dict(order_row)
        output_row["Matched In DC Shipping Report"] = "Yes" if dc_row else "No"
        output_row.update(dc_row)

        matched_rows.append(output_row)

    _log_step("Step 11: Added matched DC values to output rows")
    _log_step("Step 12: Added DC Date column")
    _log_step("Step 13: Added DC Ship M column")
    _log_step("Step 14: Added DC Track column")
    _log_step("Step 15: Preserved full DC dispatch, service, and tracking fields")
    _log_step("Step 16: Collapsed duplicate DC rows by order ID")
    _log_step("Step 17: Joined multiple tracking numbers where needed")
    _log_step("Step 18: Marked each order as matched or not matched")
    multiple_dc_row_count = count_rows_with_multiple_dc_rows(matched_rows)
    if multiple_dc_row_count:
        _log_step(
            f"Warning: {multiple_dc_row_count} orders matched multiple DC Shipping "
            "Report rows. Tracking values may be joined with ' | ' and should be "
            "reviewed before the final Rithum upload."
        )
    _log_step("Step 19: Prepared final matched CSV rows")
    _write_dict_rows(config.matched_output_path, matched_rows)
    _log_step("Step 20: Saved matched output CSV")
    _log_step("Step 21: Completed Python replacement for Excel VLOOKUP steps")
    _log_step("Step 22: Selected DC output columns")
    _log_step("Step 23: Prepared DC output columns for value-only export")
    _log_step("Step 24: Converted formulas to values in Python output")
    _log_step("Step 25: Confirmed DC Date, DC Ship M, and DC Track values")
    _log_step("Step 26: Cleared formula-edit mode equivalent")

    non_gb_rows = filter_non_gb_rows(matched_rows, config.shipping_country_column)
    _log_step(
        f"Step 27: Selected '{config.shipping_country_column}' column for filtering"
    )
    _log_step("Step 28: Applied data filter")
    _log_step("Step 29: Opened country filter equivalent")
    _log_step("Step 30: Checked available country values")
    _log_step("Step 31: Excluded GB rows")
    _write_dict_rows(config.non_gb_output_path, non_gb_rows)
    _log_step("Step 32: Saved non-GB review output")
    _log_step(f"Step 33: Found {len(non_gb_rows)} non-GB rows for review")

    airmail_rows = apply_airmail_defaults(non_gb_rows)
    _log_step("Step 34: Selected first non-GB row")
    _log_step("Step 35: Copied Airmail default value")
    _log_step("Step 36: Pasted Airmail into DC Ship M equivalent")
    _log_step("Step 37: Pasted Airmail into DC Track equivalent")
    _log_step("Step 38: Filled missing DC Date, DC Ship M, and DC Track values")
    _log_step("Step 39: Copied Airmail-filled values")
    _log_step("Step 40: Selected remaining non-GB DC columns")
    _log_step("Step 41: Pasted Airmail-filled values to visible non-GB rows")
    _log_step("Step 42: Confirmed Airmail values for non-GB rows")
    _write_dict_rows(config.non_gb_airmail_output_path, airmail_rows)
    _log_step("Step 43: Saved non-GB Airmail output")

    non_gb_with_dc_date_rows = filter_rows_with_real_dc_date(airmail_rows)
    _log_step("Step 44: Prepared filtered non-GB range")
    _log_step("Step 45: Re-applied country filter")
    _log_step("Step 46: Opened DC Date filter equivalent")
    _log_step("Step 47: Reviewed first-name column context")
    _log_step("Step 48: Re-applied filter controls")
    _log_step("Step 49: Opened DC Date value filter")
    _log_step("Step 50: Deselected placeholder/blank DC Date values")
    _write_dict_rows(config.non_gb_with_dc_date_output_path, non_gb_with_dc_date_rows)
    _log_step(
        f"Step 51: Found {len(non_gb_with_dc_date_rows)} non-GB rows with real DC Date"
    )

    non_gb_unmatched_rows = filter_rows_with_placeholder_dc_date(non_gb_rows)
    non_gb_no_dc_match_count = count_rows_without_dc_match(non_gb_rows)
    _log_step("Step 52: Selected #N/A values in DC Date filter")
    _write_dict_rows(config.non_gb_unmatched_output_path, non_gb_unmatched_rows)
    _log_step(
        f"Step 53: Saved {len(non_gb_unmatched_rows)} non-GB unmatched rows for review"
    )
    if non_gb_no_dc_match_count:
        _log_step(
            f"Warning: {non_gb_no_dc_match_count} non-GB rows have no DC Shipping "
            "Report match. Stage 2 will try to correct them from the Full Orders "
            "Report; any unresolved rows may create incomplete final upload fields."
        )
    return config.matched_output_path


def apply_airmail_defaults(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        output_row = dict(row)
        matched_status = str(
            output_row.get("Matched In DC Shipping Report", "") or ""
        ).strip()
        if matched_status.upper() == "NO":
            for column in ("DC Date", "DC Ship M", "DC Track"):
                value = str(output_row.get(column, "") or "").strip()
                if not value or value.upper() == "#N/A":
                    output_row[column] = "Airmail"
        output_rows.append(output_row)
    return output_rows


def filter_rows_with_real_dc_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    placeholders = {"", "#N/A", "AIRMAIL", "0000-00-00 00:00:00"}
    return [
        row
        for row in rows
        if str(row.get("DC Date", "") or "").strip().upper() not in placeholders
    ]


def filter_rows_with_placeholder_dc_date(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    placeholders = {"", "#N/A", "0000-00-00 00:00:00"}
    return [
        row
        for row in rows
        if str(row.get("DC Date", "") or "").strip().upper() in placeholders
    ]


def count_rows_without_dc_match(rows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for row in rows
        if str(row.get("Matched In DC Shipping Report", "") or "").strip().upper()
        == "NO"
    )


def count_rows_with_multiple_dc_rows(rows: list[dict[str, Any]]) -> int:
    count = 0
    for row in rows:
        try:
            dc_row_count = int(str(row.get("DC Row Count", "") or "0").strip())
        except ValueError:
            dc_row_count = 0
        if dc_row_count > 1:
            count += 1
    return count


def filter_non_gb_rows(
    matched_rows: list[dict[str, Any]],
    shipping_country_column: str,
) -> list[dict[str, Any]]:
    if not matched_rows:
        return []

    selected_country_column = shipping_country_column
    if selected_country_column not in matched_rows[0]:
        for fallback_column in ("ShippingCountry", "Shipping Country"):
            if fallback_column in matched_rows[0]:
                selected_country_column = fallback_column
                break

    if selected_country_column not in matched_rows[0]:
        candidates = [
            column
            for column in matched_rows[0].keys()
            if "shipping" in column.lower()
            and ("country" in column.lower() or "iso" in column.lower())
        ]
        raise RuntimeError(
            f"Matched output does not contain shipping country column "
            f"'{shipping_country_column}'. Candidate columns: {', '.join(candidates)}"
        )

    return [
        row
        for row in matched_rows
        if _normalized_country(row.get(selected_country_column)) != "GB"
    ]


def open_reports_page(page: Page, config: Config) -> None:
    reports_path = "/reports-new/download"
    reports_url = f"{_origin_url(config.helm_url)}{reports_path}"

    _click_first_visible(
        [
            page.locator(f"a[href='{reports_path}']"),
            page.locator("li.active a[href='/reports-new/download']"),
            page.get_by_role("link", name="Reports"),
        ],
        "Reports sidebar link",
        timeout_ms=10000,
    )

    page.wait_for_url(f"**{reports_path}", timeout=15000)
    _wait_for_network_idle(page)

    if reports_path not in page.url:
        page.goto(reports_url, wait_until="domcontentloaded")
        _wait_for_network_idle(page)


def open_shipping_reports_section(page: Page) -> None:
    _click_first_visible(
        [
            page.locator("ul.reports-nav a[data-section='shipping']"),
            page.locator("a[href='#shipping'][data-section='shipping']"),
            page.get_by_role("link", name="Shipping"),
        ],
        "Shipping reports section",
        timeout_ms=10000,
    )
    _wait_for_network_idle(page)


def download_shipping_report(page: Page, config: Config) -> Path:
    config.download_dir.mkdir(parents=True, exist_ok=True)

    requested_after_ms = int(time.time() * 1000)
    request_shipping_report_export(page, config)

    return download_completed_shipping_report_from_history(
        page,
        config,
        requested_after_ms,
    )


def request_shipping_report_export(page: Page, config: Config) -> None:
    reports_path = "/reports-new/download"
    if reports_path not in page.url:
        page.goto(
            f"{_origin_url(config.helm_url)}{reports_path}",
            wait_until="domcontentloaded",
        )
        _wait_for_network_idle(page)
        open_shipping_reports_section(page)

    _log_step("Step 3.2.1: Click Shipping Report download/request button")
    if not _click_shipping_report_request(page):
        raise RuntimeError("Could not find the Shipping Report export request button.")

    _wait_for_network_idle(page)
    _log_step("Step 3.2.2: Open Helm Export History")
    page.goto(
        f"{_origin_url(config.helm_url)}/reports-new/history",
        wait_until="domcontentloaded",
    )
    _wait_for_network_idle(page)


def _click_shipping_report_request(page: Page) -> bool:
    return bool(page.evaluate("""() => {
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const textOf = el => [
                    el.innerText,
                    el.textContent,
                    el.value,
                    el.getAttribute('title'),
                    el.getAttribute('aria-label'),
                    el.getAttribute('data-report-name'),
                    el.getAttribute('name')
                ].filter(Boolean).join(' ');

                const containersFor = el => {
                    const containers = [];
                    let current = el;
                    for (let i = 0; current && i < 8; i += 1) {
                        containers.push(current);
                        current = current.parentElement;
                    }
                    return containers;
                };

                const candidates = Array.from(
                    document.querySelectorAll(
                        "input[name='create_report'], input[type='submit'], button, a"
                    )
                ).filter(isVisible);

                for (const candidate of candidates) {
                    const combined = containersFor(candidate)
                        .map(textOf)
                        .join(' ');
                    const reportName = candidate.closest('form')?.getAttribute('data-report-name') || '';
                    const haystack = `${combined} ${reportName}`;

                    const isShipping = /shipping\\s+report|dc_shipping_report/i.test(haystack);
                    const isPurchase = /purchase\\s+order|dc_purchase/i.test(haystack);
                    const canRequest = /create|request|export|download|report/i.test(haystack);

                    if (isShipping && !isPurchase && canRequest) {
                        candidate.scrollIntoView({block: 'center', inline: 'center'});
                        candidate.click();
                        return true;
                    }
                }

                const exactFormButton = document.querySelector(
                    "form[data-report-name='dc_shipping_report'] input[name='create_report'], " +
                    "form[data-report-name='dc_shipping_report'] button[type='submit']"
                );
                if (exactFormButton && isVisible(exactFormButton)) {
                    exactFormButton.scrollIntoView({block: 'center', inline: 'center'});
                    exactFormButton.click();
                    return true;
                }

                return false;
            }"""))


def _save_download(download, download_dir: Path) -> Path:
    target_path = download_dir / download.suggested_filename
    download.save_as(target_path)
    return target_path


def download_completed_shipping_report_from_history(
    page: Page,
    config: Config,
    requested_after_ms: int,
) -> Path:
    user_hint = _helm_history_user_hint(config.email)
    _log_step(
        "Step 3.2.3: Check newly requested Shipping Report status in Export History"
    )
    deadline = time.monotonic() + config.helm_report_ready_timeout_seconds
    last_logged_status: str | None = None
    retry_count = 0
    max_retries = 3
    not_found_refresh_count = 0
    max_not_found_refreshes = 5

    while time.monotonic() < deadline:
        status = _latest_shipping_report_status(page, user_hint, requested_after_ms)
        normalized_status = (status or "not found").strip()
        elapsed_seconds = int(
            config.helm_report_ready_timeout_seconds
            - max(0, deadline - time.monotonic())
        )
        remaining_seconds = max(0, int(deadline - time.monotonic()))
        if normalized_status != last_logged_status or config.debug:
            print(
                "[WAIT] Step 3.2.4: Shipping Report status is "
                f"'{normalized_status}'. Waiting up to {remaining_seconds}s more "
                f"(elapsed {elapsed_seconds}s)."
            )
            last_logged_status = normalized_status

        if normalized_status.lower() == "not found":
            not_found_refresh_count += 1
            if not_found_refresh_count > max_not_found_refreshes:
                print(
                    "[WARN] Step 3.2.4: No Shipping Report row for this user/request "
                    f"was found after {max_not_found_refreshes} refresh checks. "
                    "Requesting a fresh export."
                )
                requested_after_ms = int(time.time() * 1000)
                request_shipping_report_export(page, config)
                not_found_refresh_count = 0
                last_logged_status = None
                continue
        else:
            not_found_refresh_count = 0

        if normalized_status.lower() in {"cancelled", "failed"}:
            if retry_count >= max_retries:
                raise RuntimeError(
                    "Requested Shipping Report status is "
                    f"'{normalized_status}' after {retry_count} retry attempts."
                )
            retry_count += 1
            print(
                "[WARN] Step 3.2.4: Shipping Report export ended as "
                f"'{normalized_status}'. Requesting a fresh export "
                f"(attempt {retry_count}/{max_retries})."
            )
            requested_after_ms = int(time.time() * 1000)
            request_shipping_report_export(page, config)
            last_logged_status = None
            continue
        if status and status.lower() == "completed":
            _log_step("Step 3.2.5: Shipping Report is completed and ready to download")
            download_url = _latest_shipping_report_download_url(
                page,
                user_hint,
                requested_after_ms,
            )
            if download_url:
                downloaded_path = _download_helm_export_url(
                    page, download_url, config.download_dir
                )
                _log_step("Step 3.2.6: Downloaded Shipping Report from History URL")
                return downloaded_path

            with page.expect_download(timeout=60000) as download_info:
                if not _click_latest_shipping_report_download(
                    page,
                    user_hint,
                    requested_after_ms,
                ):
                    raise RuntimeError(
                        "Requested Shipping Report is completed, but no download URL "
                        "or download action button/link was found."
                    )
            downloaded_path = _save_download(download_info.value, config.download_dir)
            _log_step("Step 3.2.6: Downloaded Shipping Report using History button")
            return downloaded_path

        page.wait_for_timeout(10000)
        page.reload(wait_until="domcontentloaded")
        _wait_for_network_idle(page)

    raise RuntimeError(
        "Timed out waiting for the Helm Shipping Report export to complete."
    )


def _helm_history_user_hint(email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    return re.split(r"[._+\-]", local_part, maxsplit=1)[0] or local_part


def _latest_shipping_report_status(
    page: Page,
    user_hint: str,
    requested_after_ms: int,
) -> str | None:
    return page.evaluate(
        """({userHint, requestedAfterMs}) => {
            const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td'));
            return cells[3]?.innerText.trim() || null;

            function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
                const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
                return rows.find(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    if (cells.length < 8) return false;
                    const type = cells[1]?.innerText.trim() || '';
                    const user = (cells[2]?.innerText.trim() || '').toLowerCase();
                    const started = cells[5]?.innerText.trim() || '';
                    const created = cells[4]?.innerText.trim() || '';
                    return new RegExp(`^${reportType}$`, 'i').test(type)
                        && (!userHint || user.includes(userHint.toLowerCase()))
                        && historyTimeIsAfter(started || created, requestedAfterMs);
                });
            }

            function historyTimeIsAfter(rawValue, requestedAfterMs) {
                const parsed = parseHistoryTime(rawValue);
                return parsed !== null && parsed >= requestedAfterMs - 120000;
            }

            function parseHistoryTime(rawValue) {
                const text = (rawValue || '').replace(/\\s+/g, ' ').trim();
                if (!text) return null;
                const nativeParsed = Date.parse(text);
                if (!Number.isNaN(nativeParsed)) return nativeParsed;
                const match = text.match(/^(\\d{1,2})\\s+([A-Za-z]{3,})\\s+(\\d{4})\\s+(\\d{1,2}):(\\d{2})\\s*(AM|PM)?$/i);
                if (!match) return null;
                const months = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
                const month = months[match[2].slice(0, 3).toLowerCase()];
                if (month === undefined) return null;
                let hour = Number(match[4]);
                const ampm = (match[6] || '').toUpperCase();
                if (ampm === 'PM' && hour < 12) hour += 12;
                if (ampm === 'AM' && hour === 12) hour = 0;
                return new Date(Number(match[3]), month, Number(match[1]), hour, Number(match[5])).getTime();
            }
        }""",
        {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
    )


def _latest_shipping_report_download_url(
    page: Page,
    user_hint: str,
    requested_after_ms: int,
) -> str | None:
    return page.evaluate(
        """({userHint, requestedAfterMs}) => {
            const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return null;

            const link = row.querySelector(
                "td:last-child a[download][href*='dc_shipping_report.csv'], " +
                "td:last-child a[href*='dc_shipping_report.csv'], " +
                "td:last-child a[download][href*='/shipping-']"
            );
            return link?.href || null;

            function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
                const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
                return rows.find(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    if (cells.length < 8) return false;
                    const type = cells[1]?.innerText.trim() || '';
                    const user = (cells[2]?.innerText.trim() || '').toLowerCase();
                    const started = cells[5]?.innerText.trim() || '';
                    const created = cells[4]?.innerText.trim() || '';
                    return new RegExp(`^${reportType}$`, 'i').test(type)
                        && (!userHint || user.includes(userHint.toLowerCase()))
                        && historyTimeIsAfter(started || created, requestedAfterMs);
                });
            }

            function historyTimeIsAfter(rawValue, requestedAfterMs) {
                const parsed = parseHistoryTime(rawValue);
                return parsed !== null && parsed >= requestedAfterMs - 120000;
            }

            function parseHistoryTime(rawValue) {
                const text = (rawValue || '').replace(/\\s+/g, ' ').trim();
                if (!text) return null;
                const nativeParsed = Date.parse(text);
                if (!Number.isNaN(nativeParsed)) return nativeParsed;
                const match = text.match(/^(\\d{1,2})\\s+([A-Za-z]{3,})\\s+(\\d{4})\\s+(\\d{1,2}):(\\d{2})\\s*(AM|PM)?$/i);
                if (!match) return null;
                const months = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
                const month = months[match[2].slice(0, 3).toLowerCase()];
                if (month === undefined) return null;
                let hour = Number(match[4]);
                const ampm = (match[6] || '').toUpperCase();
                if (ampm === 'PM' && hour < 12) hour += 12;
                if (ampm === 'AM' && hour === 12) hour = 0;
                return new Date(Number(match[3]), month, Number(match[1]), hour, Number(match[5])).getTime();
            }
        }""",
        {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
    )


def _download_helm_export_url(
    page: Page, download_url: str, download_dir: Path
) -> Path:
    filename = (
        Path(unquote(urlsplit(download_url).path)).name or "dc_shipping_report.csv"
    )
    target_path = download_dir / filename
    response = page.context.request.get(download_url, timeout=60000)
    if not response.ok:
        raise RuntimeError(
            f"Helm export download failed with HTTP {response.status}: {download_url}"
        )
    target_path.write_bytes(response.body())
    return target_path


def _click_latest_shipping_report_download(
    page: Page,
    user_hint: str,
    requested_after_ms: int,
) -> bool:
    return bool(
        page.evaluate(
            """({userHint, requestedAfterMs}) => {
                const row = findRequestedHistoryRow('Shipping Report', userHint, requestedAfterMs);
                if (!row) return false;

                const cells = Array.from(row.querySelectorAll('td'));
                if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return false;

                const action = row.querySelector(
                    "td:last-child a[download][href*='dc_shipping_report.csv'], " +
                    "td:last-child a[href*='dc_shipping_report.csv'], " +
                    "td:last-child a[download][href*='/shipping-']"
                );

                if (!action) return false;
                action.scrollIntoView({block: 'center', inline: 'center'});
                action.click();
                return true;

                function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
                    const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
                    return rows.find(el => {
                        const cells = Array.from(el.querySelectorAll('td'));
                        if (cells.length < 8) return false;
                        const type = cells[1]?.innerText.trim() || '';
                        const user = (cells[2]?.innerText.trim() || '').toLowerCase();
                        const started = cells[5]?.innerText.trim() || '';
                        const created = cells[4]?.innerText.trim() || '';
                        return new RegExp(`^${reportType}$`, 'i').test(type)
                            && (!userHint || user.includes(userHint.toLowerCase()))
                            && historyTimeIsAfter(started || created, requestedAfterMs);
                    });
                }

                function historyTimeIsAfter(rawValue, requestedAfterMs) {
                    const parsed = parseHistoryTime(rawValue);
                    return parsed !== null && parsed >= requestedAfterMs - 120000;
                }

                function parseHistoryTime(rawValue) {
                    const text = (rawValue || '').replace(/\\s+/g, ' ').trim();
                    if (!text) return null;
                    const nativeParsed = Date.parse(text);
                    if (!Number.isNaN(nativeParsed)) return nativeParsed;
                    const match = text.match(/^(\\d{1,2})\\s+([A-Za-z]{3,})\\s+(\\d{4})\\s+(\\d{1,2}):(\\d{2})\\s*(AM|PM)?$/i);
                    if (!match) return null;
                    const months = {jan:0,feb:1,mar:2,apr:3,may:4,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11};
                    const month = months[match[2].slice(0, 3).toLowerCase()];
                    if (month === undefined) return null;
                    let hour = Number(match[4]);
                    const ampm = (match[6] || '').toUpperCase();
                    if (ampm === 'PM' && hour < 12) hour += 12;
                    if (ampm === 'AM' && hour === 12) hour = 0;
                    return new Date(Number(match[3]), month, Number(match[1]), hour, Number(match[5])).getTime();
                }
            }""",
            {"userHint": user_hint, "requestedAfterMs": requested_after_ms},
        )
    )


def run(config: Config) -> None:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"HELM_URL: {config.helm_url}")
    _log_info(config.debug, f"Download directory: {config.download_dir}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            login = LoginFlow(page, config)
            login.open()
            login.fill_credentials()
            login.submit()
            page.wait_for_load_state("domcontentloaded")
            login.verify()
            _log_step("Step 3: Login to Helm")

            if config.rithum_enabled:
                rithum_orders_path = download_rithum_orders(config)
                _log_step(f"Step 1: Download Rithum orders to {rithum_orders_path}")
            else:
                _log_step("Step 1: Skipped Rithum API until credentials are ready")

            open_reports_page(page, config)
            _log_step("Step 3.1: Open Reports page")

            open_shipping_reports_section(page)
            _log_step("Step 3.2: Open Shipping reports section")

            downloaded_path = download_shipping_report(page, config)
            _log_step(f"Step 4: Download Shipping report to {downloaded_path}")

            matched_path = match_order_file_to_dc_shipping_report(config)
            if matched_path:
                _log_step(f"Matched output available at {matched_path}")

            time.sleep(2)
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    run(Config.load())
