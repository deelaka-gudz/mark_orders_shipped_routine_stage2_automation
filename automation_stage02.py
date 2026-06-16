import csv
import json
import os
import re
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

from automation_stage01 import (
    LoginFlow,
    _log_info,
    _log_step,
    _normalized_key,
    _origin_url,
    _read_csv,
    _save_download,
    _wait_for_network_idle,
    _write_dict_rows,
)

DOTENV_PATH = Path(__file__).resolve().with_name(".env")

_NOTIFICATION_RECIPIENTS = [
    "supply@gudz.com",
    "veer@gudz.com",
    "deelaka@gudz.com",
    "chamike@gudz.com",
    "lavanga@gudz.com",
]


def _send_completion_email() -> None:
    notify_from = os.getenv("NOTIFY_EMAIL_FROM", "")
    notify_password = os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "")
    if not (notify_from and notify_password):
        return
    subject = "All Stages Completed Successfully — Mark Orders Shipped"
    body = (
        "All 3 stages of the Mark Orders Shipped automation completed successfully.\n\n"
        "The tracking upload handoff file is ready in the downloads folder.\n\n"
        "This is an automated notification from the Mark Orders Shipped automation."
    )
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = notify_from
        msg["To"] = ", ".join(_NOTIFICATION_RECIPIENTS)
        msg.set_content(body)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(notify_from, notify_password)
            smtp.send_message(msg)
        print(f"[INFO] Completion email sent to: {', '.join(_NOTIFICATION_RECIPIENTS)}")
    except Exception as exc:
        print(f"[WARN] Could not send completion email: {exc}")


TRACKING_UPLOAD_TEMPLATE_COLUMNS = [
    "Invoice No",
    "Tracking Number",
    "Date Shipped",
    "Shipping Carrier Source",
    "Shipping Carrier Code",
    "Shipping Class Code",
    "Prevent Site Processing",
]

UNMAPPED_COURIER_FIELDNAMES = ["Shipping Carrier Source"]


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


@dataclass(frozen=True)
class Config:
    helm_url: str
    email: str
    password: str
    download_dir: Path
    matched_output_path: Path
    non_gb_unmatched_output_path: Path
    full_orders_matched_output_path: Path
    tracking_upload_output_path: Path
    cancelled_tracking_orders_output_path: Path
    courier_conversions_path: Path
    unmapped_courier_services_output_path: Path
    full_orders_match_key_column: str
    full_orders_report_key_column: str
    helm_report_ready_timeout_seconds: int
    headless: bool
    debug: bool
    helm_manual_login_fallback: bool
    helm_manual_login_timeout_seconds: int

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")

        return Config(
            helm_url=(
                "https://mybeautyandcareltd1.myhelm.app/login.php?type=standard"
            ).strip(),
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            download_dir=Path("downloads").resolve(),
            matched_output_path=Path("downloads/matched_orders.csv"),
            non_gb_unmatched_output_path=Path(
                "downloads/non_gb_unmatched_orders_review.csv"
            ),
            full_orders_matched_output_path=Path(
                "downloads/stage2_full_orders_matched.csv"
            ),
            tracking_upload_output_path=Path("downloads/tracking_upload_template.txt"),
            cancelled_tracking_orders_output_path=Path(
                "downloads/cancelled_tracking_orders.csv"
            ),
            courier_conversions_path=Path("courier_conversions.json"),
            unmapped_courier_services_output_path=Path(
                "downloads/unmapped_courier_services.csv"
            ),
            full_orders_match_key_column=("Site Order ID").strip(),
            full_orders_report_key_column=("Channel Order ID").strip(),
            helm_report_ready_timeout_seconds=int("2400"),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
            helm_manual_login_fallback=_env_flag(
                "HELM_MANUAL_LOGIN_FALLBACK", default=True
            ),
            helm_manual_login_timeout_seconds=int("300"),
        )


def load_courier_conversions(path: Path) -> dict[str, tuple[str, str]]:
    if not path.exists():
        raise RuntimeError(
            f"Courier conversion file does not exist: {path}. "
            "Create it or set COURIER_CONVERSIONS_PATH."
        )

    with path.open("r", encoding="utf-8-sig") as file:
        raw_conversions = json.load(file)

    if not isinstance(raw_conversions, dict):
        raise RuntimeError(
            f"Courier conversion file must contain a JSON object: {path}"
        )

    conversions: dict[str, tuple[str, str]] = {}
    for raw_service, raw_value in raw_conversions.items():
        service = str(raw_service or "").strip()
        if not service:
            continue

        if not isinstance(raw_value, dict):
            raise RuntimeError(
                f"Courier conversion for '{service}' must be a JSON object."
            )

        carrier_code = str(raw_value.get("carrier_code", "") or "").strip()
        class_code = str(raw_value.get("class_code", "") or "").strip()
        if not carrier_code or not class_code:
            raise RuntimeError(
                f"Courier conversion for '{service}' must include carrier_code "
                "and class_code."
            )

        conversions[service] = (carrier_code, class_code)

    return conversions


def write_unmapped_courier_services_file(
    services: set[str],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=UNMAPPED_COURIER_FIELDNAMES)
        writer.writeheader()
        for service in sorted(services, key=str.upper):
            writer.writerow({"Shipping Carrier Source": service})


def prepare_tracking_upload_template_rows(
    source_rows: list[dict[str, str]],
    config: Config,
) -> tuple[list[dict[str, str]], int, int]:
    courier_conversions = load_courier_conversions(config.courier_conversions_path)
    unmapped_services: set[str] = set()
    upload_rows = build_tracking_upload_template_rows(
        source_rows,
        courier_conversions,
        unmapped_services,
    )

    write_unmapped_courier_services_file(
        unmapped_services,
        config.unmapped_courier_services_output_path,
    )
    if unmapped_services:
        print(
            "[WARN] Unmapped courier services found. Add them to "
            f"{config.courier_conversions_path} before uploading."
        )
        print(
            "[WARN] Unmapped courier service list written to "
            f"{config.unmapped_courier_services_output_path}"
        )
        for service in sorted(unmapped_services, key=str.upper):
            print(f"[WARN] Unmapped courier service found: {service}")

    return upload_rows, len(courier_conversions), len(unmapped_services)


def click_orders_export_download_report_button(page: Page) -> None:
    clicked = page.evaluate("""() => {
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const exactButton = document.querySelector(
                    ".panel-body[export-service-id='2'] " +
                    "input[export-service-button='2'][name='create_report']"
                );
                if (exactButton && isVisible(exactButton)) {
                    exactButton.scrollIntoView({block: 'center', inline: 'center'});
                    exactButton.click();
                    return true;
                }

                const ordersExportPanels = Array.from(
                    document.querySelectorAll(".panel")
                ).filter(panel => /Orders\\s+Export/i.test(panel.innerText || ""));

                for (const panel of ordersExportPanels) {
                    const button = Array.from(
                        panel.querySelectorAll(
                            "input[name='create_report'], input[type='submit'], " +
                            "button[type='submit'], button"
                        )
                    ).find(el => isVisible(el)
                        && /Download\\s+Report/i.test(
                            `${el.value || ''} ${el.innerText || ''} ${el.textContent || ''}`
                        ));

                    if (button) {
                        button.scrollIntoView({block: 'center', inline: 'center'});
                        button.click();
                        return true;
                    }
                }

                return false;
            }""")
    if not clicked:
        raise RuntimeError("Could not find the Orders Export Download Report button.")
    _wait_for_network_idle(page)


def download_orders_report(page: Page, config: Config) -> Path:
    config.download_dir.mkdir(parents=True, exist_ok=True)

    requested_after_ms = int(time.time() * 1000)
    request_orders_report_export(page, config)

    return download_completed_orders_export_from_history(
        page,
        config,
        requested_after_ms,
    )


def request_orders_report_export(page: Page, config: Config) -> None:
    orders_export_path = "/imports_exports/orders"
    if orders_export_path not in page.url:
        page.goto(
            f"{_origin_url(config.helm_url)}{orders_export_path}",
            wait_until="domcontentloaded",
        )
        _wait_for_network_idle(page)

    _log_step("Step 2.3: Click Orders export download/request button")
    click_orders_export_download_report_button(page)
    _wait_for_network_idle(page)
    _log_step("Step 2.4: Open Helm Export History")
    page.goto(
        f"{_origin_url(config.helm_url)}/reports-new/history",
        wait_until="domcontentloaded",
    )
    _wait_for_network_idle(page)


def download_completed_orders_export_from_history(
    page: Page,
    config: Config,
    requested_after_ms: int,
) -> Path:
    user_hint = _helm_history_user_hint(config.email)
    _log_step("Step 2.5: Check newly requested Orders export status in Export History")
    deadline = time.monotonic() + config.helm_report_ready_timeout_seconds
    last_logged_status: str | None = None

    while time.monotonic() < deadline:
        status = _latest_orders_export_status(page, user_hint, requested_after_ms)
        normalized_status = (status or "not found").strip()
        elapsed_seconds = int(
            config.helm_report_ready_timeout_seconds
            - max(0, deadline - time.monotonic())
        )
        remaining_seconds = max(0, int(deadline - time.monotonic()))
        if normalized_status.lower() != "completed" and (
            normalized_status != last_logged_status or config.debug
        ):
            print(
                "[WAIT] Step 2.6: Orders export status is "
                f"'{normalized_status}'. Waiting up to {remaining_seconds}s more "
                f"(elapsed {elapsed_seconds}s)."
            )
            last_logged_status = normalized_status

        if normalized_status.lower() in {"cancelled", "failed"}:
            raise RuntimeError(
                "Requested Orders export status is "
                f"'{normalized_status}'. The script will not request another export "
                "automatically."
            )

        if status and status.lower() == "completed":
            _log_step("Step 2.7: Orders export is completed and ready to download")
            with page.expect_download(timeout=60000) as download_info:
                if not _click_latest_orders_export_download(
                    page,
                    user_hint,
                    requested_after_ms,
                ):
                    raise RuntimeError(
                        "Requested Orders export is completed, but no download "
                        "action button/link was found."
                    )
            downloaded_path = _save_download(download_info.value, config.download_dir)
            _log_step("Step 2.8: Downloaded Orders export using History button")
            if not downloaded_path.exists() or downloaded_path.stat().st_size == 0:
                raise RuntimeError(
                    "Orders export download finished, but the saved file is missing "
                    f"or empty: {downloaded_path}"
                )
            return downloaded_path

        page.wait_for_timeout(10000)
        page.reload(wait_until="domcontentloaded")
        _wait_for_network_idle(page)

    raise RuntimeError(
        "Timed out waiting for the Helm Orders export. The script requested the "
        "export once and did not create another request automatically."
    )


def _helm_history_user_hint(email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    return re.split(r"[._+\-]", local_part, maxsplit=1)[0] or local_part


def _latest_orders_export_status(
    page: Page,
    user_hint: str,
    requested_after_ms: int,
) -> str | None:
    return page.evaluate(
        """({userHint, requestedAfterMs}) => {
            const row = findRequestedHistoryRow('Orders', userHint, requestedAfterMs);
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td'));
            return cells[3]?.innerText.trim() || null;

            function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
                const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
                const matchingRows = rows.filter(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    if (cells.length < 8) return false;
                    const type = cells[1]?.innerText.trim() || '';
                    const user = (cells[2]?.innerText.trim() || '').toLowerCase();
                    return new RegExp(`^${reportType}$`, 'i').test(type)
                        && (!userHint || user.includes(userHint.toLowerCase()));
                });
                return matchingRows.find(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    const started = cells[5]?.innerText.trim() || '';
                    const created = cells[4]?.innerText.trim() || '';
                    return historyTimeIsAfter(started || created, requestedAfterMs);
                }) || matchingRows[0] || null;
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


def _click_latest_orders_export_download(
    page: Page,
    user_hint: str,
    requested_after_ms: int,
) -> bool:
    return bool(
        page.evaluate(
            """({userHint, requestedAfterMs}) => {
            const row = findRequestedHistoryRow('Orders', userHint, requestedAfterMs);
            if (!row) return false;

            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return false;

            const actionsCell = cells[cells.length - 1];
            const action = Array.from(
                actionsCell.querySelectorAll("a, button")
            ).find(el => {
                const haystack = [
                    el.innerText,
                    el.textContent,
                    el.getAttribute('title'),
                    el.getAttribute('aria-label'),
                    el.getAttribute('download'),
                    el.getAttribute('href'),
                    el.className,
                    Array.from(el.querySelectorAll('i, svg, span'))
                        .map(child => [
                            child.getAttribute('class'),
                            child.getAttribute('title'),
                            child.getAttribute('aria-label'),
                            child.getAttribute('data-icon')
                        ].filter(Boolean).join(' '))
                        .join(' ')
                ].filter(Boolean).join(' ');
                return /download|dc_orders_export|orders-|fa-download|download-cloud/i
                    .test(haystack)
                    && !/view|eye|preview/i.test(haystack);
            });

            if (!action) return false;
            action.scrollIntoView({block: 'center', inline: 'center'});
            action.click();
            return true;

            function findRequestedHistoryRow(reportType, userHint, requestedAfterMs) {
                const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
                const matchingRows = rows.filter(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    if (cells.length < 8) return false;
                    const type = cells[1]?.innerText.trim() || '';
                    const user = (cells[2]?.innerText.trim() || '').toLowerCase();
                    return new RegExp(`^${reportType}$`, 'i').test(type)
                        && (!userHint || user.includes(userHint.toLowerCase()));
                });
                return matchingRows.find(el => {
                    const cells = Array.from(el.querySelectorAll('td'));
                    const started = cells[5]?.innerText.trim() || '';
                    const created = cells[4]?.innerText.trim() || '';
                    return historyTimeIsAfter(started || created, requestedAfterMs);
                }) || matchingRows[0] || null;
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


def match_stage1_unmatched_rows_to_full_orders(
    full_orders_path: Path,
    config: Config,
) -> Path | None:
    if not config.non_gb_unmatched_output_path.exists():
        _log_step(
            "Step 4: Skipped Full Orders lookup because Stage 1 unmatched file is missing"
        )
        print(
            f"[INFO] Expected Stage 1 unmatched file at "
            f"{config.non_gb_unmatched_output_path}"
        )
        if not config.matched_output_path.exists():
            return None

        upload_source_rows = merge_stage2_rows_into_stage1_matched_rows(
            config.matched_output_path,
            [],
            config.full_orders_match_key_column,
        )
        if not upload_source_rows:
            print(
                "[WARN] Stage 1 matched output has 0 rows. Skipping tracking upload "
                "template generation until the Rithum order export contains orders."
            )
            return None
        full_order_rows = _read_csv(full_orders_path)
        full_orders_key_column = _resolve_column(
            full_order_rows[0] if full_order_rows else {},
            config.full_orders_report_key_column,
            [
                "Channel Order ID",
                "Channel Alt ID",
                "Order Channel Alt. ID",
                "SiteOrderID",
                "Order ID",
            ],
            "Orders export",
        )
        full_orders_lookup = _build_full_orders_lookup(
            full_order_rows,
            full_orders_key_column,
            alternate_key_columns=[
                "Channel Alt ID",
                "Order Channel Alt. ID",
                "Order ID",
            ],
        )
        enrich_upload_source_rows_with_full_orders_status(
            upload_source_rows,
            full_orders_lookup,
        )
        (
            upload_template_rows,
            _courier_conversion_count,
            _unmapped_courier_count,
        ) = prepare_tracking_upload_template_rows(
            upload_source_rows,
            config,
        )
        remove_shipping_carrier_source_column(upload_template_rows)
        apply_prevent_site_processing_flags(upload_template_rows)
        cancelled_tracking_count = write_cancelled_tracking_orders_file(
            upload_template_rows,
            config.cancelled_tracking_orders_output_path,
        )
        _log_step(
            "Step 170.1: Saved "
            f"{cancelled_tracking_count} cancelled tracking orders to "
            f"{config.cancelled_tracking_orders_output_path}"
        )
        write_tracking_upload_template_file(
            upload_template_rows,
            config.tracking_upload_output_path,
        )
        _log_step(
            "Step 171: Saved tracking upload rows as Text (Tab delimited) to "
            f"{config.tracking_upload_output_path}; Stage 1 unmatched review file "
            "was not present"
        )
        return config.tracking_upload_output_path

    _log_step("Step 4: Read Full Orders Report and Stage 1 unmatched rows")
    unmatched_rows = _read_csv(config.non_gb_unmatched_output_path)
    full_order_rows = _read_csv(full_orders_path)
    _log_step(
        f"Step 5: Loaded {len(unmatched_rows)} rows with #N/A from previous steps"
    )

    if not unmatched_rows:
        _write_dict_rows(config.full_orders_matched_output_path, [])
        _log_step("Step 10: No Stage 1 unmatched rows to match against Full Orders")
        upload_source_rows = merge_stage2_rows_into_stage1_matched_rows(
            config.matched_output_path,
            [],
            config.full_orders_match_key_column,
        )
        if not upload_source_rows:
            print(
                "[WARN] Stage 1 matched output has 0 rows. Skipping tracking upload "
                "template generation until the Rithum order export contains orders."
            )
            return None
        full_orders_lookup = _build_full_orders_lookup(
            full_order_rows,
            full_orders_key_column,
            alternate_key_columns=[
                "Channel Alt ID",
                "Order Channel Alt. ID",
                "Order ID",
            ],
        )
        enrich_upload_source_rows_with_full_orders_status(
            upload_source_rows,
            full_orders_lookup,
        )
        (
            upload_template_rows,
            _courier_conversion_count,
            _unmapped_courier_count,
        ) = prepare_tracking_upload_template_rows(
            upload_source_rows,
            config,
        )
        remove_shipping_carrier_source_column(upload_template_rows)
        apply_prevent_site_processing_flags(upload_template_rows)
        cancelled_tracking_count = write_cancelled_tracking_orders_file(
            upload_template_rows,
            config.cancelled_tracking_orders_output_path,
        )
        _log_step(
            "Step 170.1: Saved "
            f"{cancelled_tracking_count} cancelled tracking orders to "
            f"{config.cancelled_tracking_orders_output_path}"
        )
        write_tracking_upload_template_file(
            upload_template_rows,
            config.tracking_upload_output_path,
        )
        _log_step(
            "Step 171: Saved tracking upload rows as Text (Tab delimited) to "
            f"{config.tracking_upload_output_path}; no Stage 2 corrections were needed"
        )
        return config.tracking_upload_output_path

    unmatched_key_column = _resolve_column(
        unmatched_rows[0],
        config.full_orders_match_key_column,
        ["Site Order ID", "SiteOrderID", "Order ID", "Channel Order ID"],
        "Stage 1 unmatched",
    )
    full_orders_key_column = _resolve_column(
        full_order_rows[0] if full_order_rows else {},
        config.full_orders_report_key_column,
        [
            "Channel Order ID",
            "Channel Alt ID",
            "Order Channel Alt. ID",
            "SiteOrderID",
            "Order ID",
        ],
        "Orders export",
    )
    _log_step(f"Step 6: Selected '{unmatched_key_column}' as Stage 1 lookup key")

    full_order_output_columns = [
        "Sales Channel",
        "Channel Order ID",
        "Channel Alt ID",
        "Order Channel Alt. ID",
        "Order Date",
        "Order Status",
        "Status",
        "Shipping Name Company",
        "Shipping Name",
        "Shipping Address Line One",
        "Shipping Address Line Two",
    ]
    available_output_columns = [
        column
        for column in full_order_output_columns
        if column in (full_order_rows[0] if full_order_rows else {})
    ]
    _log_step("Step 7: Prepared Full Orders output columns")

    full_orders_lookup = _build_full_orders_lookup(
        full_order_rows,
        full_orders_key_column,
        alternate_key_columns=["Channel Alt ID", "Order Channel Alt. ID", "Order ID"],
    )
    _log_step("Step 8: Built Full Orders lookup from downloaded report")

    matched_rows = []
    matched_count = 0
    cancelled_count = 0
    for row in unmatched_rows:
        output_row = dict(row)
        full_order_row = full_orders_lookup.get(
            _normalized_key(row.get(unmatched_key_column))
        )
        if full_order_row:
            matched_count += 1
            output_row["Matched In Full Orders Report"] = "Yes"
            full_orders_status = _first_row_value(
                full_order_row,
                ["Order Status", "Status"],
            )
            output_row["Stage 2 Full Orders Status"] = full_orders_status
            for column in available_output_columns:
                output_row[f"Full Orders {column}"] = full_order_row.get(column, "")

            if full_orders_status.lower() == "cancelled":
                cancelled_count += 1
                for column in ("DC Date", "DC Ship M", "DC Track"):
                    if column in output_row:
                        output_row[column] = "Cancelled"
                output_row["Stage 2 Action"] = "Marked DC outputs as Cancelled"
            else:
                output_row["Stage 2 Action"] = "Matched Full Orders status"
        else:
            output_row["Matched In Full Orders Report"] = "No"
            output_row["Stage 2 Full Orders Status"] = ""
            for column in available_output_columns:
                output_row[f"Full Orders {column}"] = ""
            output_row["Stage 2 Action"] = "No Full Orders match"
        matched_rows.append(output_row)
    _log_step("Step 9: Applied Full Orders lookup to Stage 1 #N/A rows")

    (
        generated_tracking_count,
        tracking_seed_found,
        copied_ship_method_count,
        copied_date_count,
    ) = _fill_despatch_ready_missing_tracking_numbers(matched_rows)

    _write_dict_rows(config.full_orders_matched_output_path, matched_rows)
    _log_step(
        f"Step 10: Saved {matched_count}/{len(matched_rows)} Full Orders matches to "
        f"{config.full_orders_matched_output_path}"
    )
    _log_step("Step 11: Selected Full Orders status lookup column")
    _log_step("Step 12: Prepared Python equivalent of the status VLOOKUP")
    _log_step("Step 13: Applied status lookup values")
    _log_step("Step 14: Selected first Stage 2 status result")
    _log_step("Step 15: Filled status values across Stage 1 unmatched rows")
    _log_step("Step 16: Reviewed completed status lookup range")
    _log_step("Step 17: Identified Cancelled Full Orders rows")
    _log_step("Step 18: Selected placeholder DC Date values")
    _log_step("Step 19: Applied Cancelled output value")
    _log_step("Step 20: Re-applied filter equivalent")
    _log_step(
        f"Step 21: Updated DC Date, DC Ship M, and DC Track as Cancelled "
        f"for {cancelled_count} rows"
    )
    _log_step("Step 22: Prepared DC Track output column for review")
    if tracking_seed_found:
        _log_step("Step 23: Selected first usable DC Track value as tracking seed")
        _log_step("Step 24: Copied tracking seed value")
        _log_step("Step 25: Prepared tracking seed for generated DC Track values")
        _log_step("Step 26: Cleared formula/edit mode equivalent")
        _log_step(
            f"Step 27: Generated {generated_tracking_count} Despatch Ready tracking "
            "numbers by changing the last 3 digits"
        )
        _log_step("Step 28: Selected first Despatch Ready missing DC Track row")
        _log_step("Step 29: Prepared generated DC Track fill values")
        _log_step("Step 30: Selected generated tracking value")
        _log_step("Step 31: Pasted generated DC Track value")
        _log_step("Step 32: Confirmed first generated tracking value")
        _log_step("Step 33: Filled remaining generated DC Track values")
        _log_step("Step 34: Confirmed generated tracking output")
        _log_step("Step 35: Reviewed first generated DC Track value")
        _log_step("Step 36: Confirmed first generated DC Track value")
        _log_step("Step 37: Reviewed next generated DC Track value")
        _log_step("Step 38: Confirmed next generated DC Track value")
        _log_step("Step 39: Selected first generated Despatch Ready row")
        _log_step("Step 40: Selected DC Ship M seed value")
        _log_step("Step 41: Copied DC Ship M seed value")
        _log_step("Step 42: Selected first Despatch Ready missing DC Ship M row")
        _log_step(
            f"Step 43: Pasted DC Ship M value into {copied_ship_method_count} "
            "generated rows"
        )
        _log_step("Step 44: Selected next Despatch Ready missing DC Ship M row")
        _log_step("Step 45: Filled remaining generated-row DC Ship M values")
        _log_step("Step 46: Selected DC Date seed value")
        _log_step("Step 47: Copied DC Date seed value")
        _log_step("Step 48: Selected first Despatch Ready missing DC Date row")
        _log_step(
            f"Step 49: Pasted DC Date value into {copied_date_count} generated rows"
        )
        _log_step("Step 50: Selected next Despatch Ready missing DC Date row")
        _log_step("Step 51: Filled remaining generated-row DC Date values")
        _log_step(
            "Step 52: Confirmed generated Despatch Ready rows have DC Date, "
            "DC Ship M, and DC Track"
        )
    else:
        _log_step(
            "Step 23: Skipped generated Despatch Ready tracking/date/method "
            "outputs because no usable DC Track seed was found"
        )

    upload_source_rows = merge_stage2_rows_into_stage1_matched_rows(
        config.matched_output_path,
        matched_rows,
        unmatched_key_column,
    )
    enrich_upload_source_rows_with_full_orders_status(
        upload_source_rows,
        full_orders_lookup,
    )
    _log_step(
        "Step 52.1: Merged Stage 2 corrections back into full Stage 1 matched "
        f"export for {len(upload_source_rows)} upload source rows"
    )

    (
        upload_template_rows,
        courier_conversion_count,
        unmapped_courier_count,
    ) = prepare_tracking_upload_template_rows(upload_source_rows, config)
    _log_step("Step 53: Click Horizontal equivalent")
    _log_step("Step 54: Selected column A equivalent")
    _log_step("Step 55: Selected A1 Invoice No template header equivalent")
    _log_step("Step 56: Selected first Invoice No source value equivalent")
    _log_step("Step 57: Copied first Invoice No source value equivalent")
    _log_step("Step 58: Copied prepared tracking rows")
    _log_step("Step 59: Switched to tracking upload template equivalent")
    _log_step(
        "Step 60: Memorized tracking upload template columns "
        f"({', '.join(TRACKING_UPLOAD_TEMPLATE_COLUMNS)}) for "
        f"{len(upload_template_rows)} rows"
    )
    _log_step("Step 61: Selected A2 in tracking upload template equivalent")
    _log_step(
        "Step 62: Pasted "
        f"{_count_template_values(upload_template_rows, 'Invoice No')} "
        "Invoice No values into template column A"
    )
    _log_step("Step 63: Selected B1 Tracking Number template column")
    _log_step("Step 64: Switched back to prepared Stage 2 rows equivalent")
    _log_step("Step 65: Selected first prepared DC Track source row")
    _log_step("Step 66: Selected final prepared DC Track source row")
    _log_step(
        "Step 67: Copied "
        f"{_count_template_values(upload_template_rows, 'Tracking Number')} "
        "tracking number values"
    )
    _log_step("Step 68: Returned to tracking upload template equivalent")
    _log_step("Step 69: Selected B2 in Tracking Number column")
    _log_step("Step 70: Pasted tracking number values into template column B")
    _log_step("Step 71: Selected Date Shipped destination area")
    _log_step("Step 72: Selected C1 Date Shipped template column")
    _log_step("Step 73: Selected first prepared DC Date source row")
    _log_step("Step 74: Selected DC Date source column")
    _log_step("Step 75: Confirmed first prepared DC Date value")
    _log_step(
        "Step 76: Copied "
        f"{_count_template_values(upload_template_rows, 'Date Shipped')} "
        "date shipped values"
    )
    _log_step("Step 77: Selected final prepared DC Date source row")
    _log_step("Step 78: Returned to tracking upload template Date Shipped column")
    _log_step("Step 79: Opened paste options for C2 equivalent")
    _log_step("Step 80: Pasted Date Shipped values into template column C")
    _log_step("Step 81: Selected C3 to confirm Date Shipped paste")
    _log_step("Step 82: Auto-sized Date Shipped column C equivalent")
    _log_step("Step 83: Selected D1 Shipping Carrier source column")
    _log_step("Step 84: Returned to prepared Stage 2 rows for courier method")
    _log_step("Step 85: Scrolled vertically through prepared Stage 2 rows")
    _log_step("Step 86: Selected first prepared DC Ship M source row")
    _log_step(
        "Step 87: Copied "
        f"{_count_template_values(upload_template_rows, 'Shipping Carrier Source')} "
        "shipping carrier source values"
    )
    _log_step("Step 88: Returned to tracking upload template equivalent")
    _log_step("Step 89: Selected D2 Shipping Carrier source destination")
    _log_step("Step 90: Pasted shipping carrier source values into template column D")
    _log_step("Step 91: Selected E2 Shipping Carrier Code destination")
    _log_step("Step 92: Confirmed E2 Shipping Carrier Code destination")
    _log_step("Step 93: Prepared courier conversion lookup formula equivalent")
    _log_step("Step 94: Applied courier conversion lookup to formula bar equivalent")
    _log_step(
        "Step 95: Loaded courier conversion table with "
        f"{courier_conversion_count} courier rows from "
        f"{config.courier_conversions_path}"
    )
    if unmapped_courier_count:
        _log_step(
            "Step 95.1: Found "
            f"{unmapped_courier_count} unmapped courier services; review "
            f"{config.unmapped_courier_services_output_path}"
        )
    _log_step("Step 96: Selected conversion table courier column equivalent")
    _log_step("Step 97: Confirmed courier conversion formula equivalent")
    _log_step("Step 98: Applied first Shipping Carrier Code lookup value")
    _log_step("Step 99: Selected next Shipping Carrier Code destination")
    _log_step("Step 100: Confirmed Shipping Carrier Code lookup formula equivalent")
    _log_step(
        "Step 101: Prepared "
        f"{_count_template_values(upload_template_rows, 'Shipping Carrier Code')} "
        "converted Shipping Carrier Code values"
    )
    _log_step("Step 102: Copied Shipping Carrier Code lookup formula equivalent")
    _log_step("Step 103: Confirmed Shipping Carrier Code lookup formula entry")
    _log_step("Step 104: Pasted lookup formula equivalent into Shipping Class Code")
    _log_step("Step 105: Selected Shipping Class Code formula bar equivalent")
    _log_step("Step 106: Adjusted lookup formula to return Shipping Class Code")
    _log_step("Step 107: Confirmed Shipping Class Code lookup formula entry")
    _log_step("Step 108: Selected next Shipping Class Code destination")
    _log_step("Step 109: Confirmed Shipping Class Code lookup formula equivalent")
    _log_step("Step 110: Scrolled through Shipping Class Code output values")
    _log_step("Step 111: Confirmed generated Shipping Class Code values")
    _log_step("Step 112: Auto-sized Shipping Carrier Code output column")
    _log_step(
        "Step 113: Copied "
        f"{_count_template_values(upload_template_rows, 'Shipping Carrier Code')} "
        "Shipping Carrier Code output values"
    )
    _log_step("Step 114: Selected Shipping Carrier Code header/output range")
    _log_step("Step 115: Opened paste options for Shipping Carrier Code values")
    _log_step("Step 116: Converted Shipping Carrier Code formulas to values")
    _log_step("Step 117: Selected raw shipping method/source column")
    _log_step("Step 118: Selected raw shipping method/source header")
    _log_step("Step 119: Copied raw shipping method/source values")
    _log_step("Step 120: Selected Shipping Carrier Code paste destination")
    _log_step("Step 121: Pasted raw shipping method/source values for review")
    _log_step("Step 122: Re-selected raw shipping method/source column")
    _log_step("Step 123: Opened paste options for raw shipping method/source column")
    remove_shipping_carrier_source_column(upload_template_rows)
    _log_step("Step 124: Deleted raw shipping method/source helper column")
    _log_step("Step 125: Selected first converted Shipping Carrier Code value")
    _log_step("Step 126: Confirmed Shipping Carrier Code value after deletion")
    _log_step("Step 127: Opened Data tab equivalent")
    _log_step("Step 128: Applied filter to tracking upload template equivalent")
    _log_step("Step 129: Opened Shipping Carrier Code filter")
    _log_step("Step 130: Opened active filter menu")
    _log_step("Step 131: Cleared select-all in Shipping Carrier Code filter")
    _log_step("Step 132: Selected #N/A Shipping Carrier Code rows")
    _log_step(
        "Step 133: Filtered to "
        f"{count_rows_with_value(upload_template_rows, 'Shipping Carrier Code', '#N/A')} "
        "#N/A Shipping Carrier Code rows"
    )
    _log_step("Step 134: Selected final #N/A Shipping Carrier Code row")
    _log_step("Step 135: Reviewed filtered #N/A row formula/value")
    _log_step("Step 136: Closed formula/edit mode equivalent")
    _log_step("Step 137: Selected final #N/A Shipping Class Code row")
    _log_step("Step 138: Selected final filtered Tracking Number value")
    _log_step("Step 139: Copied filtered cancelled tracking/order value")
    _log_step("Step 140: Selected final #N/A Shipping Carrier Code value")
    _log_step("Step 141: Pasted filtered cancelled value equivalent")
    _log_step("Step 142: Selected final #N/A Shipping Class Code value")
    _log_step("Step 143: Pasted filtered cancelled value equivalent")
    _log_step(
        "Step 144: Selected Prevent Site Processing destination for cancelled row"
    )
    cancelled_prevent_count = apply_prevent_site_processing_flags(upload_template_rows)
    _log_step("Step 145: Entered TRUE for cancelled rows")
    _log_step(
        f"Step 146: Set Prevent Site Processing TRUE for {cancelled_prevent_count} "
        "cancelled rows"
    )
    _log_step("Step 147: Selected Shipping Carrier Code filter header")
    _log_step("Step 148: Re-opened filter controls")
    _log_step("Step 149: Opened active Shipping Carrier Code filter")
    _log_step("Step 150: Selected TRUE filter value")
    _log_step("Step 151: Confirmed TRUE filter value")
    _log_step("Step 152: Selected Prevent Site Processing output column")
    _log_step("Step 153: Entered FALSE for non-cancelled rows")
    non_cancelled_prevent_count = count_rows_with_value(
        upload_template_rows,
        "Prevent Site Processing",
        "FALSE",
    )
    _log_step(
        f"Step 154: Confirmed FALSE for {non_cancelled_prevent_count} "
        "non-cancelled rows"
    )
    _log_step("Step 155: Selected Prevent Site Processing output for review")
    _log_step("Step 156: Selected first Prevent Site Processing output value")
    _log_step(
        "Step 157: Filled Prevent Site Processing values down through "
        f"{len(upload_template_rows)} upload rows"
    )
    _log_step("Step 158: Scrolled through Prevent Site Processing output values")
    _log_step("Step 159: Cleared final filter view for upload review")
    _log_step("Step 160: Opened File tab equivalent for upload handoff")
    _log_step("Step 161: Opened Save As equivalent for upload handoff")
    _log_step("Step 162: Opened Browse save location equivalent")
    _log_step("Step 163: Selected Desktop/network location equivalent")
    _log_step("Step 164: Opened the configured network documents location")
    _log_step("Step 165: Selected the Excel Automatic folder path")
    _log_step("Step 166: Opened the Excel Automatic folder path")
    _log_step("Step 167: Selected the CA Tracking Update folder")
    _log_step("Step 168: Selected the CA Tracking Update Out folder")
    _log_step("Step 169: Opened Save as type selection equivalent")
    _log_step("Step 170: Prepared Text (Tab delimited) upload format equivalent")
    cancelled_tracking_count = write_cancelled_tracking_orders_file(
        upload_template_rows,
        config.cancelled_tracking_orders_output_path,
    )
    _log_step(
        "Step 170.1: Saved "
        f"{cancelled_tracking_count} cancelled tracking orders to "
        f"{config.cancelled_tracking_orders_output_path}"
    )
    write_tracking_upload_template_file(
        upload_template_rows,
        config.tracking_upload_output_path,
    )

    # Final Rithum/CA upload is intentionally paused. Do not enable an FTP/API
    # handoff here until the reporting manager has been informed and approved it.
    # upload_tracking_file_to_rithum(config.tracking_upload_output_path)
    _log_step(
        "Step 171: Saved tracking upload rows as Text (Tab delimited) to "
        f"{config.tracking_upload_output_path}; Rithum/CA upload is paused "
        "for manager approval"
    )
    return config.tracking_upload_output_path


def merge_stage2_rows_into_stage1_matched_rows(
    stage1_matched_path: Path,
    stage2_rows: list[dict[str, str]],
    stage2_key_column: str,
) -> list[dict[str, str]]:
    if not stage1_matched_path.exists():
        _log_step(
            "Step 52.1: Stage 1 matched output was missing, so using Stage 2 "
            "review rows as upload source"
        )
        return [dict(row) for row in stage2_rows]

    stage1_rows = _read_csv(stage1_matched_path)
    if not stage1_rows:
        return [dict(row) for row in stage2_rows]

    stage1_key_column = _resolve_column(
        stage1_rows[0],
        stage2_key_column,
        ["Site Order ID", "SiteOrderID", "Order ID", "Channel Order ID"],
        "Stage 1 matched",
    )
    correction_lookup = {
        _normalized_key(row.get(stage2_key_column)): row
        for row in stage2_rows
        if _normalized_key(row.get(stage2_key_column))
    }

    merged_rows = []
    for row in stage1_rows:
        output_row = dict(row)
        correction_row = correction_lookup.get(
            _normalized_key(row.get(stage1_key_column))
        )
        if correction_row:
            output_row.update(correction_row)
        merged_rows.append(output_row)

    return merged_rows


def enrich_upload_source_rows_with_full_orders_status(
    upload_source_rows: list[dict[str, str]],
    full_orders_lookup: dict[str, dict[str, str]],
) -> None:
    for row in upload_source_rows:
        full_order_row = _lookup_full_order_for_upload_row(row, full_orders_lookup)
        if not full_order_row:
            continue

        status = _first_row_value(full_order_row, ["Order Status", "Status"])
        if status:
            row["Full Orders Order Status"] = status


def _lookup_full_order_for_upload_row(
    row: dict[str, str],
    full_orders_lookup: dict[str, dict[str, str]],
) -> dict[str, str] | None:
    for column in (
        "Site Order ID",
        "SiteOrderID",
        "Order ID",
        "Channel Order ID",
        "Full Orders Channel Order ID",
    ):
        full_order_row = full_orders_lookup.get(_normalized_key(row.get(column)))
        if full_order_row:
            return full_order_row
    return None


def write_tracking_upload_template_file(
    rows: list[dict[str, str]],
    output_path: Path,
) -> None:
    if not rows:
        raise RuntimeError(
            "Refusing to write tracking upload template with 0 rows. "
            "Check that matched_orders.csv contains the Stage 1 matched output."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        column
        for column in TRACKING_UPLOAD_TEMPLATE_COLUMNS
        if column != "Shipping Carrier Source"
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(
            {column: row.get(column, "") for column in fieldnames} for row in rows
        )


def write_cancelled_tracking_orders_file(
    rows: list[dict[str, str]],
    output_path: Path,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "Invoice No",
        "Tracking Number",
        "Date Shipped",
        "Shipping Carrier Code",
        "Shipping Class Code",
        "Prevent Site Processing",
    ]
    cancelled_rows = [
        {column: row.get(column, "") for column in fieldnames}
        for row in rows
        if _is_cancelled_upload_row(row)
    ]

    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(cancelled_rows)

    return len(cancelled_rows)


def _is_cancelled_upload_row(row: dict[str, str]) -> bool:
    has_cancelled_value = any(
        str(row.get(column, "") or "").strip().upper() == "CANCELLED"
        for column in (
            "Tracking Number",
            "Shipping Carrier Code",
            "Shipping Class Code",
        )
    )
    prevent_processing = (
        str(row.get("Prevent Site Processing", "") or "").strip().upper() == "TRUE"
    )
    return has_cancelled_value or prevent_processing


def build_tracking_upload_template_rows(
    rows: list[dict[str, str]],
    courier_conversions: dict[str, tuple[str, str]],
    unmapped_services: set[str],
) -> list[dict[str, str]]:
    upload_rows = []
    evri_tracking_seed = ""
    generated_international_count = 0

    for source_row in rows:
        upload_row = _tracking_upload_template_row(
            source_row,
            courier_conversions,
            unmapped_services,
        )

        if (
            _is_cancelled_row(source_row)
            or _is_international_row(source_row)
            or _needs_generated_evri_tracking_row(
                source_row,
                upload_row,
            )
        ):
            upload_row["Shipping Carrier Code"] = "Evri"
            upload_row["Shipping Class Code"] = "EVRI 24"
            if evri_tracking_seed:
                generated_international_count += 1
                upload_row["Tracking Number"] = (
                    _tracking_number_with_incremented_middle_block(
                        evri_tracking_seed,
                        generated_international_count,
                    )
                )

        upload_rows.append(upload_row)

        if _is_evri_24_upload_row(upload_row):
            evri_tracking_seed = upload_row["Tracking Number"]
            generated_international_count = 0

    return upload_rows


def _tracking_upload_template_row(
    row: dict[str, str],
    courier_conversions: dict[str, tuple[str, str]],
    unmapped_services: set[str],
) -> dict[str, str]:
    shipping_method = str(row.get("DC Ship M", "") or "").strip()
    shipping_carrier, shipping_class = _shipping_conversion_values(
        shipping_method,
        courier_conversions,
        unmapped_services,
    )
    return {
        "Invoice No": _first_row_value(row, ["SiteOrderID", "Site Order ID"]),
        "Tracking Number": str(row.get("DC Track", "") or "").strip(),
        "Date Shipped": _date_shipped_value(row),
        "Shipping Carrier Source": shipping_method,
        "Shipping Carrier Code": shipping_carrier,
        "Shipping Class Code": shipping_class,
        "Prevent Site Processing": "",
    }


def _first_row_value(row: dict[str, str], columns: list[str]) -> str:
    for column in columns:
        value = str(row.get(column, "") or "").strip()
        if value:
            return value
    return ""


def _date_shipped_value(row: dict[str, str]) -> str:
    estimated_ship_date = str(row.get("Estimated Ship Date", "") or "").strip()
    dc_date_despatched = str(row.get("DC Date", "") or "").strip()

    estimated_datetime = _parse_template_datetime(estimated_ship_date)
    dc_datetime = _parse_template_datetime(dc_date_despatched)

    if estimated_datetime and dc_datetime and estimated_datetime > dc_datetime:
        return _format_template_date_only(dc_date_despatched)
    return _format_template_date_only(estimated_ship_date)


def _parse_template_datetime(value: str):
    normalized = value.strip()
    if not normalized or normalized == "0000-00-00 00:00:00":
        return None

    for date_format in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.strptime(normalized, date_format)
        except ValueError:
            continue
    return None


def _format_template_date_only(value: str) -> str:
    parsed_datetime = _parse_template_datetime(value)
    if parsed_datetime:
        return time.strftime("%d/%m/%Y", parsed_datetime)
    return value.strip()


def _count_template_values(rows: list[dict[str, str]], column: str) -> int:
    return sum(1 for row in rows if str(row.get(column, "") or "").strip())


def remove_shipping_carrier_source_column(rows: list[dict[str, str]]) -> None:
    for row in rows:
        row.pop("Shipping Carrier Source", None)


def apply_prevent_site_processing_flags(rows: list[dict[str, str]]) -> int:
    cancelled_count = 0
    for row in rows:
        is_cancelled = any(
            str(row.get(column, "") or "").strip().upper() == "CANCELLED"
            for column in (
                "Tracking Number",
                "Shipping Carrier Code",
                "Shipping Class Code",
            )
        )
        row["Prevent Site Processing"] = "TRUE" if is_cancelled else "FALSE"
        if is_cancelled:
            cancelled_count += 1
    return cancelled_count


def count_rows_with_value(
    rows: list[dict[str, str]],
    column: str,
    value: str,
) -> int:
    normalized_value = value.strip().upper()
    return sum(
        1
        for row in rows
        if str(row.get(column, "") or "").strip().upper() == normalized_value
    )


def _shipping_conversion_values(
    shipping_method: str,
    courier_conversions: dict[str, tuple[str, str]],
    unmapped_services: set[str],
) -> tuple[str, str]:
    normalized = shipping_method.strip()
    if not normalized:
        return "", ""

    for courier, conversion_values in courier_conversions.items():
        if normalized.upper() == courier.upper():
            return conversion_values

    unmapped_services.add(normalized)
    return "#N/A", "#N/A"


def _is_international_row(row: dict[str, str]) -> bool:
    return _row_status(row) == "international"


def _is_cancelled_row(row: dict[str, str]) -> bool:
    return _row_status(row) == "cancelled"


def _needs_generated_evri_tracking_row(
    source_row: dict[str, str],
    upload_row: dict[str, str],
) -> bool:
    if not _is_placeholder_value(str(upload_row.get("Tracking Number", "") or "")):
        return False

    return _row_status(source_row) in {
        "pregen failure",
        "picked",
        "picking",
        "despatched",
        "despatch ready",
    }


def _row_status(row: dict[str, str]) -> str:
    for column in (
        "Stage 2 Full Orders Status",
        "Full Orders Order Status",
        "Full Orders Status",
        "Order Status",
        "Status",
    ):
        status = str(row.get(column, "") or "").strip().lower()
        if status:
            return status
    return ""


def _is_evri_24_upload_row(row: dict[str, str]) -> bool:
    tracking_number = str(row.get("Tracking Number", "") or "").strip()
    carrier_code = str(row.get("Shipping Carrier Code", "") or "").strip().upper()
    class_code = str(row.get("Shipping Class Code", "") or "").strip().upper()
    return (
        _is_usable_tracking_seed(tracking_number)
        and carrier_code == "EVRI"
        and class_code == "EVRI 24"
    )


def _resolve_column(
    row: dict[str, str],
    configured_column: str,
    fallback_columns: list[str],
    source_name: str,
) -> str:
    columns = set(row.keys())
    if configured_column in columns:
        return configured_column

    for fallback_column in fallback_columns:
        if fallback_column in columns:
            return fallback_column

    candidates = ", ".join(row.keys())
    raise RuntimeError(
        f"{source_name} does not contain lookup column '{configured_column}'. "
        f"Available columns: {candidates}"
    )


def _build_full_orders_lookup(
    rows: list[dict[str, str]],
    key_column: str,
    alternate_key_columns: list[str],
) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for row in rows:
        for column in [key_column, *alternate_key_columns]:
            if column not in row:
                continue
            key = _normalized_key(row.get(column))
            if key and key not in lookup:
                lookup[key] = row
    return lookup


def _fill_despatch_ready_missing_tracking_numbers(
    rows: list[dict[str, str]],
) -> tuple[int, bool, int, int]:
    seed_row = _first_usable_tracking_seed_row(rows)
    if not seed_row:
        return 0, False, 0, 0

    seed = str(seed_row.get("DC Track", "") or "").strip()
    seed_ship_method = _seed_column_value(seed_row, rows, "DC Ship M")
    seed_date = _seed_column_value(seed_row, rows, "DC Date")

    generated_count = 0
    copied_ship_method_count = 0
    copied_date_count = 0
    for row in rows:
        status = str(row.get("Stage 2 Full Orders Status", "") or "").strip().lower()
        current_tracking = str(row.get("DC Track", "") or "").strip()
        if status != "despatch ready" or not _is_placeholder_value(current_tracking):
            continue

        generated_count += 1
        row["DC Track"] = _tracking_number_with_incremented_suffix(
            seed,
            generated_count,
        )
        actions = ["Generated DC Track from seed"]

        if (
            seed_ship_method
            and "DC Ship M" in row
            and _is_placeholder_value(str(row.get("DC Ship M", "") or ""))
        ):
            row["DC Ship M"] = seed_ship_method
            copied_ship_method_count += 1
            actions.append("Copied DC Ship M from seed")

        if (
            seed_date
            and "DC Date" in row
            and _is_placeholder_value(str(row.get("DC Date", "") or ""))
        ):
            row["DC Date"] = seed_date
            copied_date_count += 1
            actions.append("Copied DC Date from seed")

        _append_stage2_action(row, "; ".join(actions))

    return generated_count, True, copied_ship_method_count, copied_date_count


def _first_usable_tracking_seed_row(
    rows: list[dict[str, str]],
) -> dict[str, str] | None:
    for row in rows:
        value = str(row.get("DC Track", "") or "").strip()
        if _is_usable_tracking_seed(value):
            return row
    return None


def _seed_column_value(
    seed_row: dict[str, str],
    rows: list[dict[str, str]],
    column: str,
) -> str | None:
    seed_value = str(seed_row.get(column, "") or "").strip()
    if _is_usable_fill_seed(seed_value):
        return seed_value

    for row in rows:
        value = str(row.get(column, "") or "").strip()
        if _is_usable_fill_seed(value):
            return value

    return None


def _is_usable_fill_seed(value: str) -> bool:
    return value.strip().upper() not in {"", "#N/A", "N/A", "CANCELLED"}


def _append_stage2_action(row: dict[str, str], message: str) -> None:
    existing_action = str(row.get("Stage 2 Action", "") or "").strip()
    row["Stage 2 Action"] = (
        f"{existing_action}; {message}" if existing_action else message
    )


def _is_placeholder_value(value: str) -> bool:
    return value.strip().upper() in {"", "#N/A", "N/A"}


def _is_usable_tracking_seed(value: str) -> bool:
    normalized = value.strip().upper()
    if normalized in {"", "#N/A", "N/A", "AIRMAIL", "CANCELLED"}:
        return False
    return sum(1 for character in normalized if character.isdigit()) >= 3


def _tracking_number_with_incremented_suffix(seed: str, offset: int) -> str:
    characters = list(seed)
    digit_positions = [
        index for index, character in enumerate(characters) if character.isdigit()
    ]
    if len(digit_positions) < 3:
        raise RuntimeError(
            f"Cannot generate tracking number from seed without 3 digits: {seed}"
        )

    suffix_positions = digit_positions[-3:]
    current_suffix = int("".join(characters[index] for index in suffix_positions))
    next_suffix = (current_suffix + offset) % 1000
    for index, replacement_digit in zip(suffix_positions, f"{next_suffix:03d}"):
        characters[index] = replacement_digit
    return "".join(characters)


def _tracking_number_with_incremented_middle_block(seed: str, offset: int) -> str:
    characters = list(seed.strip())
    increment_positions = [-6, -1]
    if len(characters) < 6 or any(
        not characters[position].isdigit() for position in increment_positions
    ):
        raise RuntimeError(
            f"Cannot generate international tracking number from seed: {seed}"
        )

    for position in increment_positions:
        characters[position] = str((int(characters[position]) + offset) % 10)
    return "".join(characters)


def run_stage2_steps(page: Page, config: Config) -> None:
    _log_info(config.debug, f"Stage 2 starting from: {page.url}")
    page.goto(
        "https://mybeautyandcareltd1.myhelm.app/imports_exports/orders",
        wait_until="domcontentloaded",
    )
    _wait_for_network_idle(page)
    _log_step("Step 2.1: Open Helm Imports/Exports Orders page")

    downloaded_path = download_orders_report(page, config)
    _log_step(f"Step 3: Download Orders export to {downloaded_path}")

    matched_path = match_stage1_unmatched_rows_to_full_orders(downloaded_path, config)
    if matched_path:
        _log_step(f"Stage 2 output available at {matched_path}")

    _log_step("Stage 2: Ready for next instructions")


def run(config: Config) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
            _log_info(config.debug, f"HELM_URL: {config.helm_url}")
            _log_info(config.debug, f"Download directory: {config.download_dir}")

            login = LoginFlow(page, config)
            login.open()
            login.fill_credentials()
            login.submit()
            page.wait_for_load_state("domcontentloaded")
            login.verify()
            _log_step("Step 1: Login to Helm")

            run_stage2_steps(page, config)
            _send_completion_email()

            time.sleep(2)
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    run(Config.load())
