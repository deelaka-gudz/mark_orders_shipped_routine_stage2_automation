import csv
import datetime
import os
import re
import time

import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import dotenv_values, load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

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


def _dotenv_or_env(dotenv_values_map: dict[str, Any], name: str) -> str | None:
    value = dotenv_values_map.get(name)
    if value is None:
        value = os.getenv(name)
    return str(value) if value is not None else None


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
        except (PlaywrightTimeoutError, PlaywrightError) as error:
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
            field.dispatch_event("input", timeout=timeout_ms)
            field.dispatch_event("change", timeout=timeout_ms)
            actual_value = field.input_value(timeout=timeout_ms)
            if actual_value != value:
                field.click(timeout=timeout_ms)
                field.press("Control+A", timeout=timeout_ms)
                field.type(value, delay=25, timeout=timeout_ms)
                field.dispatch_event("input", timeout=timeout_ms)
                field.dispatch_event("change", timeout=timeout_ms)
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
    rithum_orders_output_path: Path
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
    helm_manual_login_fallback: bool
    helm_manual_login_timeout_seconds: int
    headless: bool
    debug: bool
    ca_application_id: str | None = None
    ca_shared_secret: str | None = None
    ca_refresh_token: str | None = None
    ca_profile_id: str | None = None
    ca_order_filter: str = (
        "ShippingStatus eq 'Unshipped'"
        " and (SiteName eq 'Amazon UK' or SiteName eq 'eBay Fixed Price UK' or SiteName eq 'Temu UK')"
        " and (RequestedShippingClass eq null"
        " or (RequestedShippingClass ne 'NextDay'"
        " and RequestedShippingClass ne 'SecondDay'"
        " and RequestedShippingClass ne 'Prime NextDay'"
        " and RequestedShippingClass ne 'Prime SecondDay'"
        " and RequestedShippingClass ne 'Prime Standard'"
        " and RequestedShippingClass ne 'Premium NextDay'"
        " and RequestedShippingClass ne 'Premium SecondDay'))"
    )

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")
        dotenv_values_map = dotenv_values(dotenv_path, encoding="utf-8-sig")

        return Config(
            helm_url=(
                "https://mybeautyandcareltd1.myhelm.app/login.php?type=standard"
            ).strip(),
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            download_dir=Path("downloads").resolve(),
            rithum_orders_output_path=Path("downloads/rithum_orders.csv").resolve(),
            dc_shipping_report_path=Path("downloads/dc_shipping_report.csv").resolve(),
            matched_output_path=Path("downloads/matched_orders.csv").resolve(),
            non_gb_output_path=Path("downloads/non_gb_orders_review.csv").resolve(),
            non_gb_airmail_output_path=Path(
                "downloads/non_gb_orders_airmail.csv"
            ).resolve(),
            non_gb_with_dc_date_output_path=Path(
                "downloads/non_gb_orders_with_dc_date_review.csv"
            ).resolve(),
            non_gb_unmatched_output_path=Path(
                "downloads/non_gb_unmatched_orders_review.csv"
            ).resolve(),
            match_key_column=("Order ID").strip(),
            order_match_key_column=("Site Order ID").strip(),
            dc_match_key_column=("Order ID").strip(),
            shipping_country_column=("ShippingCountry").strip(),
            helm_report_ready_timeout_seconds=int("2400"),
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
            ca_application_id=_dotenv_or_env(dotenv_values_map, "CA_APPLICATION_ID")
            or None,
            ca_shared_secret=_dotenv_or_env(dotenv_values_map, "CA_SHARED_SECRET")
            or None,
            ca_refresh_token=_dotenv_or_env(dotenv_values_map, "CA_REFRESH_TOKEN")
            or None,
            ca_profile_id=_dotenv_or_env(dotenv_values_map, "CA_PROFILE_ID") or None,
            ca_order_filter=(
                _dotenv_or_env(dotenv_values_map, "CA_ORDER_FILTER")
                or (
                    "ShippingStatus eq 'Unshipped'"
                    " and (SiteName eq 'Amazon UK' or SiteName eq 'eBay Fixed Price UK' or SiteName eq 'Temu UK')"
                    " and (RequestedShippingClass eq null"
                    " or (RequestedShippingClass ne 'NextDay'"
                    " and RequestedShippingClass ne 'SecondDay'"
                    " and RequestedShippingClass ne 'Prime NextDay'"
                    " and RequestedShippingClass ne 'Prime SecondDay'"
                    " and RequestedShippingClass ne 'Prime Standard'"
                    " and RequestedShippingClass ne 'Premium NextDay'"
                    " and RequestedShippingClass ne 'Premium SecondDay'))"
                )
            ).strip(),
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


def _origin_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def match_order_file_to_dc_shipping_report(config: Config) -> Path | None:
    if not config.rithum_orders_output_path or not config.dc_shipping_report_path:
        _log_step("Step 6: Skipped matching until order/DC file paths are configured")
        return None

    _log_step("Step 6: Read order file and DC Shipping Report")
    order_rows = _read_csv(config.rithum_orders_output_path)
    dc_rows = _read_csv(config.dc_shipping_report_path)
    order_key_column = config.order_match_key_column
    dc_key_column = config.dc_match_key_column

    if not order_rows:
        print(
            "[WARN] Rithum order export has 0 order rows. The downloaded "
            f"Basic Layout file only contains headers: {config.rithum_orders_output_path}. "
            "Check the selected Rithum saved filter/date range, then rerun Stage 1. "
            "No matched output or tracking upload file will be generated for this run."
        )
        return None
    if not dc_rows:
        raise RuntimeError(
            "DC shipping report has 0 rows. Check the Helm Shipping Report export, "
            f"then rerun Stage 1: {config.dc_shipping_report_path}."
        )

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

    requested_after_ms = request_shipping_report_export(page, config)
    open_helm_export_history(page, config)

    return download_completed_shipping_report_from_history(
        page,
        config,
        requested_after_ms,
    )


def request_shipping_report_export(page: Page, config: Config) -> int:
    reports_path = "/reports-new/download"
    if reports_path not in page.url:
        page.goto(
            f"{_origin_url(config.helm_url)}{reports_path}",
            wait_until="domcontentloaded",
        )
        _wait_for_network_idle(page)

    open_shipping_reports_section(page)
    _log_step("Step 3.2.1: Open Shipping reports section")

    try:
        page.locator("form[data-report-name='dc_shipping_report']").first.wait_for(
            state="visible",
            timeout=15000,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Could not find the Helm Shipping Report form after opening the "
            "Shipping reports section."
        ) from exc

    requested_after_ms = int(time.time() * 1000)
    if not _click_shipping_report_request(page):
        raise RuntimeError("Could not find the Shipping Report export request button.")

    _log_step("Step 3.2.2: Click Shipping Report Download Report button")
    page.wait_for_timeout(1500)
    _wait_for_network_idle(page)
    return requested_after_ms


def open_helm_export_history(page: Page, config: Config) -> None:
    _log_step("Step 3.2.3: Open Helm Export History after requesting Shipping Report")
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

                const requestSelector =
                    "input[name='create_report'], input[type='submit'], " +
                    "button[type='submit'], button";

                const isRequestControl = el => {
                    return el.getAttribute('name') === 'create_report'
                        || /download\\s+report|create|request|export/i.test(textOf(el));
                };

                const click = el => {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    el.click();
                    return true;
                };

                const exactForm = document.querySelector(
                    "form[data-report-name='dc_shipping_report']"
                );
                if (exactForm) {
                    const exactButton = Array.from(exactForm.querySelectorAll(requestSelector))
                        .find(el => isVisible(el) && isRequestControl(el));
                    if (exactButton) {
                        return click(exactButton);
                    }
                }

                const candidates = Array.from(document.querySelectorAll(requestSelector))
                    .filter(el => isVisible(el) && isRequestControl(el));

                for (const candidate of candidates) {
                    let current = candidate;
                    let combined = '';
                    for (let i = 0; current && i < 8; i += 1) {
                        combined += ' ' + textOf(current);
                        current = current.parentElement;
                    }

                    const reportName = candidate.closest('form')?.getAttribute('data-report-name') || '';
                    const haystack = `${combined} ${reportName}`;

                    const isShipping = /shipping\\s+report|dc_shipping_report/i.test(haystack);
                    const isOtherReport = /purchase\\s+order|dc_purchase|ioss\\s+report/i.test(haystack);

                    if (isShipping && !isOtherReport) {
                        return click(candidate);
                    }
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

        if normalized_status.lower() in {"cancelled", "failed"}:
            raise RuntimeError(
                "Requested Helm Shipping Report export ended as "
                f"'{normalized_status}'. The script will not request another "
                "Shipping Report during the same run. Check Helm Export History "
                "and start a new run when you are ready to request it again."
            )
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
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            print(
                "[WAIT] Step 3.2.4: Helm Export History refresh timed out; "
                "checking the current table again."
            )
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


_CA_TOKEN_URL = "https://api.channeladvisor.com/oauth2/token"
_CA_ORDERS_URL = "https://api.channeladvisor.com/v1/Orders"


def _ca_get_access_token(
    application_id: str, shared_secret: str, refresh_token: str
) -> str:
    response = requests.post(
        _CA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": application_id,
            "client_secret": shared_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"CA API authentication failed ({response.status_code}): {response.text[:500]}"
        )
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError(
            "CA API returned a token response without an access_token field."
        )
    return token


def _ca_fetch_orders_page(
    access_token: str, profile_id: str, filter_expr: str, skip: int, top: int = 100
) -> list[dict[str, Any]]:
    response = requests.get(
        _CA_ORDERS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "$filter": filter_expr,
            "$top": top,
            "$skip": skip,
            "profileid": profile_id,
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"CA API orders request failed ({response.status_code}): {response.text[:500]}"
        )
    return response.json().get("value", [])


def _utc_to_basic_layout_datetime(value: Any) -> str:
    """Convert a CA API UTC timestamp to 'DD/MM/YYYY HH:MM' (Basic Layout format)."""
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return text


def _ca_order_to_csv_row(order: dict[str, Any]) -> dict[str, Any]:
    order_id = str(order.get("SiteOrderID") or "").strip()
    candidates: dict[str, Any] = {
        # Basic Layout column names — matches what Stage 2 reads
        "Site Order ID": order_id,
        "Site Name": order.get("SiteName"),
        "Buyer": order.get("BuyerEmailAddress"),
        "Order Date": _utc_to_basic_layout_datetime(order.get("CheckoutDateUtc")),
        "Estimated Ship Date": _utc_to_basic_layout_datetime(
            order.get("EstimatedShipDateUtc")
        ),
        "Shipping Status": order.get("ShippingStatus"),
        "Site Shipping Status": order.get("ShippingStatus"),
        "Order Total": order.get("TotalPrice"),
        "Payment Type": order.get("PaymentStatus"),
        "Shipping First Name": order.get("ShippingFirstName"),
        "Shipping Last Name": order.get("ShippingLastName"),
        "Shipping Company Name": order.get("ShippingTitle"),
        "Shipping Address Line 1": order.get("ShippingAddressLine1"),
        "Shipping Address Line 2": order.get("ShippingAddressLine2"),
        "Shipping City": order.get("ShippingCity"),
        "Shipping State or Province": order.get("ShippingStateOrProvince"),
        "Shipping Postal Code": order.get("ShippingPostalCode"),
        "Shipping Country": order.get("ShippingCountry"),
        "Shipping Day Phone": order.get("ShippingPhoneNumber"),
        # Technical fields kept for downstream processing
        "SiteOrderID": order_id,
        "RequestedShippingCarrier": order.get("RequestedShippingCarrier"),
        "RequestedShippingClass": order.get("RequestedShippingClass"),
        "SecondarySiteOrderID": order.get("SecondarySiteOrderID"),
    }
    return {k: v for k, v in candidates.items() if v is not None and v != ""}


def fetch_rithum_orders_via_api(config: Config) -> Path:
    if not all(
        [
            config.ca_application_id,
            config.ca_shared_secret,
            config.ca_refresh_token,
            config.ca_profile_id,
        ]
    ):
        raise RuntimeError(
            "CA API credentials missing. Set CA_APPLICATION_ID, CA_SHARED_SECRET, "
            "CA_REFRESH_TOKEN, and CA_PROFILE_ID in .env."
        )
    _log_step("Step 1.1: Authenticating with CA REST API")
    access_token = _ca_get_access_token(
        config.ca_application_id,  # type: ignore[arg-type]
        config.ca_shared_secret,  # type: ignore[arg-type]
        config.ca_refresh_token,  # type: ignore[arg-type]
    )
    _log_step("Step 1.2: CA API access token obtained")

    today = datetime.date.today()
    day_after_tomorrow = today + datetime.timedelta(days=2)
    filter_expr = (
        config.ca_order_filter
        + f" and EstimatedShipDateUtc lt {day_after_tomorrow.isoformat()}T00:00:00Z"
    )
    _log_info(config.debug, f"CA API order filter: {filter_expr}")

    all_rows: list[dict[str, Any]] = []
    page_size = 100
    skip = 0

    while True:
        page = _ca_fetch_orders_page(
            access_token,
            config.ca_profile_id,  # type: ignore[arg-type]
            filter_expr,
            skip,
            page_size,
        )
        if not page:
            break
        all_rows.extend(_ca_order_to_csv_row(order) for order in page)
        _log_info(
            config.debug,
            f"CA API: fetched {len(all_rows)} orders (page size {len(page)})",
        )
        skip += len(page)
        if len(page) < page_size:
            break

    _log_step(f"Step 1.3: Fetched {len(all_rows)} orders from CA REST API")
    output_path = config.rithum_orders_output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_dict_rows(output_path, all_rows)
    _log_step(f"Step 1.4: Saved CA API orders to {output_path}")
    return output_path


def run(config: Config) -> None:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"HELM_URL: {config.helm_url}")
    _log_info(config.debug, f"Download directory: {config.download_dir}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            rithum_orders_path = fetch_rithum_orders_via_api(config)
            _log_step(f"Step 1: Downloaded Rithum orders to {rithum_orders_path}")

            rithum_row_count = (
                sum(1 for _ in open(rithum_orders_path, encoding="utf-8-sig")) - 1
            )
            if rithum_row_count <= 0:
                _log_step("Step 1 result: 0 orders to process — nothing to do today.")
                return
            login = LoginFlow(page, config)
            login.open()
            login.fill_credentials()
            login.submit()
            page.wait_for_load_state("domcontentloaded")
            login.verify()
            _log_step("Step 3: Login to Helm")

            open_reports_page(page, config)
            _log_step("Step 3.1: Open Reports page")

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
