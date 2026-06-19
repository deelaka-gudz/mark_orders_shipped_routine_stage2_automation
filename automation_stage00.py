import datetime
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Optional, Sequence

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DOTENV_PATH = Path(__file__).resolve().with_name(".env")

_MANUAL_INTERVENTION_RECIPIENTS = [
    "supply@gudz.com",
    "veer@gudz.com",
    "deelaka@gudz.com",
    "chamike@gudz.com",
    "lavanga@gudz.com",
]


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


def _send_manual_intervention_alert(config: Any, remaining_count: int) -> None:
    if not (config.notify_from and config.notify_app_password):
        print(
            "[WARN] Cannot send manual intervention alert — SMTP credentials not configured."
        )
        return
    subject = f"ACTION REQUIRED: {remaining_count} PreGen Failure order(s) need manual attention"
    body = (
        f"{remaining_count} PreGen Failure order(s) could not be resolved automatically "
        "after all 4 passes.\n\n"
        "Please log in to Helm and manually fix the remaining PreGen Failure orders before "
        "running the Stage 1 and Stage 2 automation.\n\n"
        "This is an automated alert from the Mark Orders Shipped automation."
    )
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.notify_from
        msg["To"] = ", ".join(_MANUAL_INTERVENTION_RECIPIENTS)
        msg.set_content(body)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.notify_from, config.notify_app_password)
            smtp.send_message(msg)
        print(
            f"[INFO] Manual intervention alert sent to: "
            f"{', '.join(_MANUAL_INTERVENTION_RECIPIENTS)}"
        )
    except Exception as exc:
        print(f"[WARN] Could not send manual intervention alert: {exc}")


def _send_failure_email(config: Any, subject: str, body: str) -> None:
    if not (config.notify_from and config.notify_app_password):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.notify_from
        msg["To"] = ", ".join(_MANUAL_INTERVENTION_RECIPIENTS)
        msg.set_content(body)
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.notify_from, config.notify_app_password)
            smtp.send_message(msg)
        print(
            f"[INFO] Failure notification sent to: "
            f"{', '.join(_MANUAL_INTERVENTION_RECIPIENTS)}"
        )
    except Exception as exc:
        print(f"[WARN] Could not send notification email: {exc}")


def _is_abort_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "err_aborted",
            "net::err_",
            "target closed",
            "execution context was destroyed",
            "page closed",
        )
    )


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


def _click_pregen_failure(page: Page, allow_empty: bool = False) -> int:
    pregen_failure_count = _status_count(page, "#status_id_3009")
    print(f"[INFO] Initial PreGen Failure count: {pregen_failure_count}")
    if pregen_failure_count <= 0:
        return pregen_failure_count

    tile = page.locator(
        ".status.loadthis",
        has=page.locator("#status_id_3009"),
    ).filter(has_text=re.compile(r"\bPreGen Failure\b", re.I))

    _click_first_visible(
        [
            tile,
            page.locator("#status_id_3009").locator("..").locator(".."),
            page.get_by_text(re.compile(r"\bPreGen Failure\b", re.I)).locator(".."),
        ],
        "PreGen Failure status",
    )
    page.wait_for_load_state("domcontentloaded")
    _wait_for_network_idle(page)
    return pregen_failure_count


def _click_pregen_failure_if_count_greater_than_zero(page: Page) -> bool:
    pregen_failure_count = _status_count(page, "#status_id_3009")
    print(f"[INFO] PreGen Failure count after dashboard return: {pregen_failure_count}")
    if pregen_failure_count <= 0:
        print("[INFO] PreGen Failure count is 0 — all failures resolved")
        return False

    _click_pregen_failure(page)
    return True


def _click_first_order_id(page: Page) -> None:
    order_link = page.locator(
        "tbody tr.has-second-row a[href^='/orders/edit?id='], "
        "tbody tr.has-second-row a[href*='/orders/edit?id='], "
        "a[href^='/orders/edit?id='], "
        "a[href*='/orders/edit?id=']"
    ).first
    order_link.wait_for(state="visible", timeout=5000)
    order_id = re.sub(r"\s+", " ", order_link.text_content(timeout=5000) or "").strip()
    print(f"[INFO] Opening order ID: {order_id}")
    order_link.evaluate("element => element.removeAttribute('target')")
    order_link.click(timeout=5000)
    page.wait_for_load_state("domcontentloaded")
    _wait_for_network_idle(page)


def _collect_order_links(page: Page) -> list[dict[str, str]]:
    links = page.evaluate("""
        () => {
            const anchors = Array.from(document.querySelectorAll(
                "tbody tr.has-second-row a[href*='/orders/edit?id=']"
            ));
            const seen = new Set();
            return anchors
                .map(anchor => ({
                    href: anchor.getAttribute("href") || "",
                    order_id: (anchor.textContent || "").replace(/\\s+/g, " ").trim(),
                }))
                .filter(order => {
                    if (!order.href || seen.has(order.href)) {
                        return false;
                    }
                    seen.add(order.href);
                    return true;
                });
        }
        """)
    if not links:
        raise RuntimeError("Could not find any order ID links on the orders page.")
    print(f"[INFO] Found {len(links)} PreGen Failure order row(s).")
    return links


def _open_order_link(page: Page, order: dict[str, str]) -> None:
    href = order["href"]
    url = (
        href
        if href.startswith("http")
        else f"https://mybeautyandcareltd1.myhelm.app{href}"
    )
    print(f"[INFO] Opening order ID: {order['order_id']}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _wait_for_network_idle(page)


def _process_pregen_failure_order(
    page: Page,
    order: dict[str, str],
    index: int,
    config: Any,
    select_shipping,
    shipping_description: str,
    step_prefix: str,
    notify_on_order_failure: bool = True,
) -> None:
    _open_order_link(page, order)
    _log_step(f"{step_prefix}[{index}]: Open order ID {order['order_id']}")

    if not _verify_pregen_label_error_exists(page):
        skip_msg = (
            f"Order {order['order_id']} does not show a Pregenerated Labels "
            "Plugin error. Skipping this order."
        )
        print(f"[WARN] {skip_msg}")
        if notify_on_order_failure:
            _send_failure_email(
                config,
                f"PreGen Failure: Order {order['order_id']} skipped — no label error found",
                f"{skip_msg}\n\nThis order may be in an unexpected state. Please check it manually in Helm.",
            )
        _log_step(
            f"{step_prefix}.1[{index}]: Pregenerated Labels Plugin error not found"
        )
        return
    _log_step(f"{step_prefix}.1[{index}]: Verify Pregenerated Labels Plugin error")

    select_shipping(page)
    _log_step(f"{step_prefix}.2[{index}]: Select {shipping_description}")

    _click_visible_toggle_or_retry_shipping(page, select_shipping)
    _log_step(f"{step_prefix}.3[{index}]: Click lock toggle button")

    _select_order_status_pregen(page)
    _log_step(f"{step_prefix}.4[{index}]: Select PreGen status")


def _process_current_pregen_failure_orders(
    page: Page,
    config: Any,
    select_shipping,
    shipping_description: str,
    pass_label: str,
    step_prefix: str,
    notify_on_order_failure: bool = True,
) -> int:
    try:
        orders = _collect_order_links(page)
    except RuntimeError as exc:
        msg = f"{pass_label}: Could not collect order links from the page: {exc}"
        print(f"[WARN] {msg}")
        _send_failure_email(
            config,
            f"PreGen Failure: Cannot Collect Orders ({pass_label})",
            f"{msg}\n\nPlease open Helm and process the remaining PreGen Failure orders manually.",
        )
        return 0

    for index, order in enumerate(orders, start=1):
        try:
            _process_pregen_failure_order(
                page,
                order,
                index,
                config,
                select_shipping,
                shipping_description,
                step_prefix,
                notify_on_order_failure,
            )
        except Exception as order_exc:
            if _is_abort_error(order_exc):
                print(
                    f"[WARN] Abort error on order {order['order_id']} — retrying once: {order_exc}"
                )
                page.wait_for_timeout(2000)
                try:
                    _process_pregen_failure_order(
                        page,
                        order,
                        index,
                        config,
                        select_shipping,
                        shipping_description,
                        step_prefix,
                        notify_on_order_failure,
                    )
                except Exception as retry_exc:
                    retry_msg = (
                        f"{pass_label} ({step_prefix}[{index}]): "
                        f"Order {order['order_id']} failed after abort-error retry: {retry_exc}"
                    )
                    print(f"[WARN] {retry_msg}")
                    raise RuntimeError(retry_msg) from retry_exc
            else:
                msg = (
                    f"{pass_label} ({step_prefix}[{index}]): "
                    f"Order {order['order_id']} failed: {order_exc}"
                )
                print(f"[WARN] {msg}")
                if notify_on_order_failure:
                    _send_failure_email(
                        config,
                        f"PreGen Failure: Order {order['order_id']} failed ({pass_label})",
                        f"{msg}\n\nThis order needs to be resolved manually in Helm.",
                    )

    return len(orders)


def _verify_pregen_label_error_exists(page: Page) -> bool:
    locators = [
        page.get_by_text(
            re.compile(
                r"PregenLabel couldn't created by Pregenerated Labels Plugin",
                re.I,
            )
        ),
        page.get_by_text(
            re.compile(
                r"An error has occurred whilst getting the shipping label by "
                r"Pregenerated Labels Plugin",
                re.I,
            )
        ),
        page.locator("td").filter(
            has_text=re.compile(r"Pregenerated Labels Plugin", re.I)
        ),
        page.get_by_text(
            re.compile(
                r"Pregenerated Labels Plugin.*courier service cannot be selected",
                re.I,
            )
        ),
    ]
    for locator in locators:
        try:
            locator.first.wait_for(state="visible", timeout=5000)
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            continue
    return False


def _select_order_detail_shipping_service(
    page: Page,
    shipping_description: str,
    target_value: str,
    target_text: str,
    required_fragments: Sequence[str],
) -> None:
    shipping_method = page.locator("select[name='shipping_method_requested']")
    shipping_method.first.wait_for(state="visible", timeout=10000)
    selected_text = shipping_method.first.evaluate("""
        select => select.options[select.selectedIndex]?.textContent || ""
        """)
    print(f"[INFO] Existing shipping method: {selected_text.strip()}")
    try:
        shipping_method.first.select_option(value=target_value, timeout=5000)
    except PlaywrightError:
        pass

    selected_service = shipping_method.first.evaluate(
        """
        (select, {targetValue, targetText, requiredFragments}) => {
            const normalize = value => value
                .replace(/\\s+/g, " ")
                .replace(/[\\u2013\\u2014-]/g, "-")
                .replace(/\\s*-\\s*/g, "-")
                .trim();
            const target = normalize(targetText);
            const fragments = requiredFragments.map(normalize);
            const options = Array.from(select.options);
            const option = options.find(item => {
                const group = item.closest("optgroup");
                return group
                    && group.label === "All Courier Services"
                    && item.value === targetValue;
            }) || options.find(item => {
                const group = item.closest("optgroup");
                return group
                    && group.label === "All Courier Services"
                    && normalize(item.textContent || item.value || "") === target;
            }) || options.find(item => {
                const group = item.closest("optgroup");
                const text = normalize(item.textContent || item.value || "");
                return group
                    && group.label === "All Courier Services"
                    && fragments.every(fragment => text.includes(fragment));
            });

            if (!option) {
                throw new Error(`Could not find courier service: ${target}`);
            }

            select.selectedIndex = options.indexOf(option);
            select.dispatchEvent(new Event("input", { bubbles: true }));
            select.dispatchEvent(new Event("change", { bubbles: true }));
            if (typeof window.shippingMethodChange === "function") {
                window.shippingMethodChange(select);
            }

            const finalSelected = select.options[select.selectedIndex];
            const finalGroup = finalSelected.closest("optgroup")?.label || "";
            if (finalGroup !== "All Courier Services") {
                throw new Error(`Courier service was not selected. Selected group: ${finalGroup}`);
            }

            return {
                text: (finalSelected.textContent || "").replace(/\\s+/g, " ").trim(),
                value: finalSelected.value || "",
                group: finalGroup,
                index: select.selectedIndex,
            };
        }
        """,
        {
            "targetValue": target_value,
            "targetText": target_text,
            "requiredFragments": list(required_fragments),
        },
    )
    print(
        "[INFO] Selected courier service: "
        f"{selected_service['text']} ({selected_service['group']})"
    )
    _wait_for_network_idle(page)
    page.wait_for_timeout(1000)


def _select_evri_24_non_pod_order_detail(page: Page) -> None:
    _select_order_detail_shipping_service(
        page,
        "Evri 24 Non POD",
        "EvriCorporate ~ Evri 24 Non POD",
        "EvriCorporate - Evri 24 Non POD",
        ["EvriCorporate", "Evri 24 Non POD"],
    )


def _select_royal_mail_tracked_48_no_signature_order_detail(page: Page) -> None:
    _select_order_detail_shipping_service(
        page,
        "Royal Mail Tracked 48 No Signature",
        "RoyalMailClickAndDrop ~ RMCD Tracked 48 (TPS48)- No Signature",
        "RoyalMailClickAndDrop ~ RMCD Tracked 48 (TPS48) - No Signature",
        ["RoyalMailClickAndDrop", "RMCD Tracked 48", "TPS48", "No Signature"],
    )


def _select_royal_mail_tracked_48_with_signature(page: Page) -> None:
    _select_order_detail_shipping_service(
        page,
        "Royal Mail Tracked 48 With Signature",
        "RoyalMailClickAndDrop ~ RMCD Tracked 48 (TPS48)- With Signature",
        "RoyalMailClickAndDrop ~ RMCD Tracked 48 (TPS48) - With Signature",
        ["RoyalMailClickAndDrop", "RMCD Tracked 48", "TPS48", "With Signature"],
    )


def _click_visible_toggle_or_retry_shipping(page: Page, retry_select_shipping) -> None:
    toggle = (
        page.locator(".toggle-group")
        .filter(has=page.locator(".toggle-on", has_text=re.compile(r"\bOn\b", re.I)))
        .filter(has=page.locator(".toggle-off", has_text=re.compile(r"\bOff\b", re.I)))
    )

    try:
        toggle.first.wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError:
        retry_select_shipping(page)
        toggle.first.wait_for(state="visible", timeout=10000)

    if toggle.first.locator(".toggle-off.active").count() > 0:
        toggle.first.click(timeout=5000)
        _wait_for_network_idle(page)
    else:
        print("[INFO] Lock toggle is already ON — skipping click")


def _select_order_status_pregen(page: Page) -> None:
    status = page.locator("select[name='status_id']")
    status.first.wait_for(state="visible", timeout=10000)
    status.first.select_option("3003", timeout=5000)
    status.first.evaluate("""
        select => {
            select.dispatchEvent(new Event("input", { bubbles: true }));
            select.dispatchEvent(new Event("change", { bubbles: true }));
            if (typeof $ !== "undefined") {
                $(select).trigger("change");
            }
            if (typeof window.statusChange === "function") {
                window.statusChange(select);
            }
            if (typeof window.orderStatusChange === "function") {
                window.orderStatusChange(select);
            }
        }
    """)
    _wait_for_network_idle(page)


def _click_filters_button(page: Page) -> None:
    filters_btn = page.locator("button.dc-main-filters-trigger")
    filters_btn.first.wait_for(state="visible", timeout=10000)
    filters_btn.first.click(timeout=5000)
    _wait_for_network_idle(page)
    page.wait_for_timeout(500)


def _click_ship_by_date_filter(page: Page) -> None:
    trigger = page.locator(".custom-dropdown__trigger[data-dropdown='ship_by_date']")
    trigger.first.wait_for(state="visible", timeout=10000)
    trigger.first.click(timeout=5000)
    page.wait_for_timeout(500)


def _fill_ship_by_date_today(page: Page) -> None:
    today = datetime.date.today()
    date_str = f"{today.day} {today.strftime('%B %Y')}"
    page.evaluate(
        """(dateStr) => {
            const from = document.getElementById('ship_by_date-date-from');
            const to   = document.getElementById('ship_by_date-date-to');
            const full = document.getElementById('ship_by_date-date-full');
            if (!from || !to || !full) {
                throw new Error('Ship By Date filter inputs not found on page');
            }
            from.value = dateStr;
            to.value   = dateStr;
            full.value = dateStr + ',' + dateStr;
            full.dispatchEvent(new Event('change', { bubbles: true }));
            if (typeof $ !== 'undefined') {
                $(full).trigger('change');
            }
        }""",
        date_str,
    )
    print(f"[INFO] Ship By Date set to: {date_str}")
    page.wait_for_timeout(500)


def _click_apply_filters(page: Page) -> None:
    apply_btn = page.locator("button#apply-button")
    apply_btn.first.wait_for(state="visible", timeout=10000)
    apply_btn.first.click(timeout=5000)
    page.wait_for_load_state("domcontentloaded")
    _wait_for_network_idle(page)


def _get_filtered_record_count(page: Page) -> int:
    try:
        span = page.locator("span.check-filtered.table-select-all")
        span.first.wait_for(state="visible", timeout=10000)
        text = span.first.inner_text(timeout=5000)
        match = re.search(r"/\s*(\d+)\s*records", text)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 0


def _select_all_orders_on_page(page: Page, force_reselect: bool = False) -> None:
    checkbox = page.locator("input.check-all-on-page.processible[type='checkbox']")
    checkbox.first.wait_for(state="visible", timeout=5000)
    checkbox.first.scroll_into_view_if_needed(timeout=5000)
    if force_reselect and checkbox.first.is_checked(timeout=5000):
        checkbox.first.click(timeout=5000)
        page.wait_for_timeout(500)
    if not checkbox.first.is_checked(timeout=5000):
        checkbox.first.click(timeout=5000)


def _open_bulk_action_dropdown(page: Page) -> None:
    dropdown = page.locator(
        ".custom-dropdown[data-dropdown='bulk-action']",
        has=page.locator("button.custom-dropdown__trigger"),
    )
    _click_first_visible(
        [
            dropdown.locator("button.custom-dropdown__trigger"),
            page.get_by_role("button", name=re.compile(r"Select Bulk Action", re.I)),
        ],
        "Select Bulk Action dropdown",
    )
    dropdown.locator(".custom-dropdown__content").first.wait_for(
        state="visible",
        timeout=5000,
    )


def _select_set_shipping_bulk_action(page: Page) -> None:
    bulk_action = page.locator("select[name='bulk_action']")
    bulk_action.first.wait_for(state="visible", timeout=5000)
    bulk_action.first.select_option("set_shipping", timeout=5000)
    page.locator("#shippingMethodRequested").first.wait_for(
        state="visible",
        timeout=5000,
    )


def _select_evri_24_non_pod_shipping(page: Page) -> None:
    shipping_service = page.locator("select[name='set_shipping_method_requested']")
    shipping_service.first.wait_for(state="visible", timeout=5000)
    try:
        shipping_service.first.select_option(
            label="EvriCorporate - Evri 24 Non POD | Evri 24 Non POD",
            timeout=5000,
        )
    except PlaywrightError:
        shipping_service.first.select_option("28", timeout=5000)


def _select_royal_mail_tracked_48_no_signature(page: Page) -> None:
    shipping_service = page.locator("select[name='set_shipping_method_requested']")
    shipping_service.first.wait_for(state="visible", timeout=5000)
    shipping_service.first.evaluate("""
        select => {
            const target = "RoyalMailClickAndDrop \\u2013 RMCD Tracked 48 (TPS48)- No Signature | RMCD Tracked 48 (TPS48)- No Signature";
            const normalize = value => value
                .replace(/[\\u2013\\u2014-]/g, "-")
                .replace(/\\s+/g, " ")
                .trim();
            const options = Array.from(select.options);
            const option = options.find(item => normalize(item.textContent) === normalize(target));
            if (!option) {
                throw new Error(`Could not find shipping service: ${target}`);
            }
            select.selectedIndex = options.indexOf(option);
            select.dispatchEvent(new Event("input", { bubbles: true }));
            select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        """)


def _wait_for_progress_loader(page: Page, timeout_ms: int = 10 * 60 * 1000) -> None:
    processing_modal = page.get_by_text(
        re.compile(r"Selected orders are processing", re.I)
    )
    loader_selector = ", ".join(
        [
            ".progress",
            ".progress-bar",
            ".progress-striped",
            ".loading",
            ".loader",
            ".preloader",
            ".spinner",
            ".fa-spinner",
            ".blockUI",
            ".blockOverlay",
            "[class*='loader']",
            "[class*='loading']",
            "[class*='progress']",
            "[id*='loader']",
            "[id*='loading']",
            "[id*='progress']",
        ]
    )

    try:
        processing_modal.first.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        try:
            page.locator(loader_selector).first.wait_for(
                state="visible",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            page.wait_for_timeout(2000)
            return

    try:
        processing_modal.first.wait_for(state="hidden", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        raise RuntimeError(
            "Timed out waiting for the selected orders processing modal to finish."
        )

    page.wait_for_function(
        """
        selector => !Array.from(document.querySelectorAll(selector)).some(element => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity) !== 0
                && rect.width > 0
                && rect.height > 0;
        })
        """,
        arg=loader_selector,
        timeout=timeout_ms,
    )


def _submit_bulk_action(page: Page) -> None:
    dropdown = page.locator(".custom-dropdown[data-dropdown='bulk-action']")
    _click_first_visible(
        [
            dropdown.get_by_role("button", name=re.compile(r"Submit Action", re.I)),
            dropdown.locator("button[onclick='startBulkAction()']"),
        ],
        "Submit Action button",
    )
    _wait_for_progress_loader(page)
    _wait_for_network_idle(page)


def _set_status_as_pregen(page: Page) -> None:
    dropdown = page.locator(".custom-dropdown[data-dropdown='set_status']")
    _click_first_visible(
        [
            dropdown.locator("button.custom-dropdown__trigger"),
            page.get_by_role("button", name=re.compile(r"Set Status", re.I)),
        ],
        "Set Status dropdown",
    )
    dropdown.locator(".custom-dropdown__content").first.wait_for(
        state="visible",
        timeout=5000,
    )
    set_pregen = dropdown.locator("a.set-status-button[data-status-id='3003']")
    try:
        set_pregen.first.wait_for(state="visible", timeout=2000)
        set_pregen.first.click(timeout=5000)
    except (PlaywrightTimeoutError, PlaywrightError):
        set_pregen.first.evaluate("element => element.click()")
    _wait_for_network_idle(page)


def _go_to_dashboard(page: Page) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            page.goto(
                "https://mybeautyandcareltd1.myhelm.app/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            _wait_for_network_idle(page)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error
            if page.locator("#status_id_3003").count() > 0:
                _wait_for_network_idle(page)
                return
            print(f"[INFO] Dashboard navigation attempt {attempt} failed; retrying...")
            page.wait_for_timeout(2000)
    raise RuntimeError("Could not navigate to dashboard after retries.") from last_error


def _click_dashboard_sidebar_link(page: Page) -> None:
    _click_first_visible(
        [
            page.locator("#widget-sidebar a[href='/']").filter(
                has_text=re.compile(r"\bDashboard\b", re.I)
            ),
            page.get_by_role("link", name=re.compile(r"\bDashboard\b", re.I)),
        ],
        "Dashboard sidebar link",
    )
    page.wait_for_load_state("domcontentloaded")
    _wait_for_network_idle(page)


def _status_count(page: Page, selector: str) -> int:
    try:
        raw_count = page.locator(selector).first.text_content(timeout=10000) or ""
    except (PlaywrightTimeoutError, PlaywrightError):
        status_names = {
            "#status_id_3003": "PreGen",
            "#status_id_3009": "PreGen Failure",
        }
        status_name = status_names.get(selector)
        if not status_name:
            raise
        return _dashboard_status_count_by_name(page, status_name)
    count = re.sub(r"[^\d]", "", raw_count)
    return int(count or "0")


def _dashboard_status_count_by_name(page: Page, status_name: str) -> int:
    return page.evaluate(
        """
        statusName => {
            const normalize = value => value.replace(/\\s+/g, " ").trim();
            const labels = Array.from(document.querySelectorAll("p, span, div"))
                .filter(element => normalize(element.textContent || "") === statusName);

            for (const label of labels) {
                let node = label.parentElement;
                for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                    const text = normalize(node.textContent || "");
                    if (!text.includes(statusName)) {
                        continue;
                    }
                    const numbers = text.match(/\\b\\d+\\b/g);
                    if (numbers && numbers.length > 0) {
                        return Number(numbers[0]);
                    }
                }
            }

            throw new Error(`Could not find dashboard status count for ${statusName}`);
        }
        """,
        status_name,
    )


def _wait_for_pregen_count_zero(
    page: Page,
    timeout_ms: int = 30 * 60 * 1000,
    poll_ms: int = 5000,
) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        try:
            _go_to_dashboard(page)
        except PlaywrightTimeoutError:
            print("[INFO] Dashboard load timed out; retrying...")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Timed out waiting for dashboard while checking PreGen count."
                )
            page.wait_for_timeout(poll_ms)
            continue
        pregen_count = _status_count(page, "#status_id_3003")
        print(f"[INFO] PreGen status count: {pregen_count}")
        if pregen_count == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for PreGen status count to become 0.")
        page.wait_for_timeout(poll_ms)


def _verify_pregen_failure_count_greater_than_zero(page: Page) -> int:
    _go_to_dashboard(page)
    pregen_failure_count = _status_count(page, "#status_id_3009")
    print(f"[INFO] Final PreGen Failure count: {pregen_failure_count}")
    return pregen_failure_count


def _check_remaining_pregen_failure_orders(page: Page, config: Any) -> int:
    _go_to_dashboard(page)
    pregen_failure_count = _status_count(page, "#status_id_3009")
    if pregen_failure_count > 0:
        warning = (
            f"{pregen_failure_count} PreGen Failure order(s) still remain. "
            "Click Start automation to run again."
        )
        print(f"[WARN] {warning}")
        _send_failure_email(
            config,
            f"PreGen Failure: {pregen_failure_count} order(s) need manual fix",
            f"{warning}\n\nPlease log in to Helm and resolve the remaining orders manually.",
        )
    else:
        print("[INFO] No PreGen Failure orders remain.")
    return pregen_failure_count


def _apply_today_ship_by_date_filter(page: Page, start_step: int) -> int:
    _click_filters_button(page)
    _log_step(f"Step {start_step}: Click Filters button")

    _click_ship_by_date_filter(page)
    _log_step(f"Step {start_step + 1}: Click Ship By Date filter")

    _fill_ship_by_date_today(page)
    _log_step(f"Step {start_step + 2}: Fill Ship By Date with today's date")

    _click_apply_filters(page)
    _log_step(f"Step {start_step + 3}: Click Apply Filters button")

    filtered_count = _get_filtered_record_count(page)
    print(f"[INFO] Filtered record count after Ship By Date filter: {filtered_count}")
    return filtered_count


def _run_pregen_failure_detail_pass(
    page: Page,
    config: Any,
    select_shipping,
    shipping_description: str,
    pass_label: str,
    step_prefix: str,
    wait_step: int,
    notify_on_order_failure: bool = True,
) -> None:
    processed_count = _process_current_pregen_failure_orders(
        page,
        config,
        select_shipping,
        shipping_description,
        pass_label,
        step_prefix,
        notify_on_order_failure,
    )
    print(f"[INFO] {pass_label}: processed {processed_count} order(s).")
    try:
        _wait_for_pregen_count_zero(page)
    except RuntimeError as exc:
        msg = f"{pass_label} (Step {wait_step}): Timed out waiting for PreGen queue to drain: {exc}"
        print(f"[WARN] {msg}")
        _send_failure_email(
            config,
            f"PreGen Failure: Queue Timeout ({pass_label} Step {wait_step})",
            msg,
        )
        raise
    _log_step(f"Step {wait_step}: Wait until PreGen status count is 0")


def _check_remaining_today_ship_by_date_pregen_failure_orders(
    page: Page,
    config: Any,
) -> int:
    _go_to_dashboard(page)
    pregen_failure_count = _status_count(page, "#status_id_3009")
    if pregen_failure_count <= 0:
        print("[INFO] No PreGen Failure orders remain.")
        return 0

    _click_pregen_failure(page)
    today_count = _apply_today_ship_by_date_filter(page, 42)
    if today_count <= 0:
        print(
            f"[INFO] {pregen_failure_count} PreGen Failure order(s) remain, "
            "but none match today's Ship By Date. No manual alert will be sent."
        )
        return 0

    warning = (
        f"{today_count} PreGen Failure order(s) with today's Ship By Date still remain. "
        "Click Start automation to run again."
    )
    print(f"[WARN] {warning}")
    _send_failure_email(
        config,
        f"PreGen Failure: {today_count} today ship-by-date order(s) need manual fix",
        f"{warning}\n\nPlease log in to Helm and resolve today's remaining orders manually.",
    )
    return today_count


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

    def verify(self, timeout_ms: int = 15000) -> None:
        self.page.wait_for_timeout(500)
        start = time.monotonic()
        while True:
            if self._app_is_visible():
                return

            if (time.monotonic() - start) * 1000 >= timeout_ms:
                break
            self.page.wait_for_timeout(250)

        error_msg = (
            "Login did not complete within the expected time. "
            "If credentials are correct, the site may require extra steps "
            "(e.g., CAPTCHA/2FA) or the page UI changed."
        )
        _send_failure_email(
            self.config, "PreGen Failure Automation: Login Failed", error_msg
        )
        raise SystemExit(error_msg)


@dataclass(frozen=True)
class Config:
    helm_url: str
    email: str
    password: str
    headless: bool
    debug: bool
    notify_from: Optional[str]
    notify_app_password: Optional[str]

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")

        return Config(
            helm_url=(
                "https://mybeautyandcareltd1.myhelm.app/login.php?type=standard"
            ).strip(),
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
            notify_from=os.getenv("NOTIFY_EMAIL_FROM", "").strip() or None,
            notify_app_password=os.getenv("NOTIFY_EMAIL_APP_PASSWORD", "").strip()
            or None,
        )


def run(config: Config) -> int:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"HELM_URL: {config.helm_url}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        email_sent = [False]
        try:
            login = LoginFlow(page, config)
            login.open()
            login.fill_credentials()
            login.submit()
            page.wait_for_load_state("domcontentloaded")
            login.verify()
            _log_step("Step 1: Login to Helm")

            initial_pregen_failure_count = _click_pregen_failure(
                page,
                allow_empty=True,
            )
            if initial_pregen_failure_count <= 0:
                _log_step(
                    "Step 2: No PreGen Failure orders found — skipping to Stage 1"
                )
                return 0
            _log_step("Step 2: Click Pregen failure")

            _run_pregen_failure_detail_pass(
                page,
                config,
                _select_evri_24_non_pod_order_detail,
                "Evri 24 Non POD",
                "Pass 1",
                "Step 3",
                4,
                notify_on_order_failure=False,
            )

            final_pregen_failure_count = _verify_pregen_failure_count_greater_than_zero(
                page
            )
            _log_step("Step 5: Check PreGen Failure count")
            if final_pregen_failure_count <= 0:
                print(
                    "[INFO] PreGen Failure count is 0 after Pass 1 - Stage 1 will now run"
                )
                time.sleep(2)
                return 0

            remaining_pregen_failure_count = _click_pregen_failure(page)
            if remaining_pregen_failure_count <= 0:
                return 0
            _log_step("Step 6: Click Pregen failure")

            _run_pregen_failure_detail_pass(
                page,
                config,
                _select_royal_mail_tracked_48_no_signature_order_detail,
                "Royal Mail 48 No Signature",
                "Pass 2",
                "Step 7",
                8,
                notify_on_order_failure=False,
            )

            final_pregen_failure_count = _verify_pregen_failure_count_greater_than_zero(
                page
            )
            _log_step("Step 9: Check PreGen Failure count")
            if final_pregen_failure_count <= 0:
                print(
                    "[INFO] PreGen Failure count is 0 after Pass 2 - Stage 1 will now run"
                )
                time.sleep(2)
                return 0

            remaining_pregen_failure_count = _click_pregen_failure(page)
            if remaining_pregen_failure_count <= 0:
                return 0
            _log_step("Step 10: Click Pregen failure")

            filtered_count = _apply_today_ship_by_date_filter(page, 11)
            if filtered_count == 0:
                print(
                    f"[INFO] {final_pregen_failure_count} PreGen Failure order(s) remain, "
                    "but none match today's ship date. Stage 1 will now run."
                )
                time.sleep(2)
                return 0

            _run_pregen_failure_detail_pass(
                page,
                config,
                _select_royal_mail_tracked_48_no_signature_order_detail,
                "Royal Mail 48 No Signature",
                "Pass 3",
                "Step 15",
                16,
                notify_on_order_failure=False,
            )

            final_pregen_failure_count = _verify_pregen_failure_count_greater_than_zero(
                page
            )
            _log_step("Step 17: Check PreGen Failure count")
            if final_pregen_failure_count <= 0:
                print(
                    "[INFO] PreGen Failure count is 0 after Pass 3 - Stage 1 will now run"
                )
                time.sleep(2)
                return 0

            remaining_pregen_failure_count = _click_pregen_failure(page)
            if remaining_pregen_failure_count <= 0:
                return 0
            _log_step("Step 18: Click Pregen failure")

            filtered_count = _apply_today_ship_by_date_filter(page, 19)
            if filtered_count == 0:
                print(
                    f"[INFO] {final_pregen_failure_count} PreGen Failure order(s) remain, "
                    "but none match today's ship date. Stage 1 will now run."
                )
                time.sleep(2)
                return 0

            _run_pregen_failure_detail_pass(
                page,
                config,
                _select_royal_mail_tracked_48_with_signature,
                "Royal Mail 48 With Signature",
                "Pass 4",
                "Step 23",
                24,
                notify_on_order_failure=True,
            )

            remaining_count = _check_remaining_today_ship_by_date_pregen_failure_orders(
                page, config
            )
            _log_step(
                "Step 25: Check remaining today's Ship By Date PreGen Failure orders"
            )
            if remaining_count > 0:
                print(
                    f"[WARN] Stage 0 finished with {remaining_count} unresolved "
                    "today Ship By Date PreGen Failure order(s). Stage 1 and Stage 2 will not run."
                )
                _send_manual_intervention_alert(config, remaining_count)
                return remaining_count

            print("[INFO] PreGen Failure count is 0 - Stage 1 will now run")
            time.sleep(2)
            return 0

            _select_all_orders_on_page(page)
            _log_step("Step 3: Click select all on page checkbox")

            _open_bulk_action_dropdown(page)
            _log_step("Step 4: Click Select Bulk Action")

            _select_set_shipping_bulk_action(page)
            _log_step("Step 5: Select Set Shipping")

            _select_evri_24_non_pod_shipping(page)
            _log_step("Step 6: Select EvriCorporate Evri 24 Non POD")

            try:
                _submit_bulk_action(page)
            except Exception as exc:
                msg = f"Pass 1 (Step 7): Bulk action submission failed or timed out: {exc}"
                print(f"[WARN] {msg}")
                _send_failure_email(
                    config, "PreGen Failure: Bulk Submit Failed (Pass 1 Step 7)", msg
                )
                email_sent[0] = True
                raise
            _log_step("Step 7: Click Submit Action")

            _select_all_orders_on_page(page, force_reselect=True)
            _log_step("Step 8: Click select all on page checkbox")

            _set_status_as_pregen(page)
            _log_step("Step 9: Click Set as PreGen")

            try:
                _wait_for_pregen_count_zero(page)
            except RuntimeError as exc:
                msg = f"Pass 1 (Step 10): Timed out waiting for PreGen queue to drain: {exc}"
                print(f"[WARN] {msg}")
                _send_failure_email(
                    config, "PreGen Failure: Queue Timeout (Pass 1 Step 10)", msg
                )
                email_sent[0] = True
                raise
            _log_step("Step 10: Wait until PreGen status count is 0")

            final_pregen_failure_count = _verify_pregen_failure_count_greater_than_zero(
                page
            )
            _log_step("Step 11: Check PreGen Failure count")

            if final_pregen_failure_count == 0:
                print(
                    "[INFO] PreGen Failure count is 0 after Pass 1 — Stage 1 will now run"
                )
            elif final_pregen_failure_count > 0:
                remaining_pregen_failure_count = _click_pregen_failure(page)
                if remaining_pregen_failure_count <= 0:
                    return 0
                _log_step("Step 12: Click Pregen failure")

                _click_filters_button(page)
                _log_step("Step 13: Click Filters button")

                _click_ship_by_date_filter(page)
                _log_step("Step 14: Click Ship By Date filter")

                _fill_ship_by_date_today(page)
                _log_step("Step 15: Fill Ship By Date with today's date")

                _click_apply_filters(page)
                _log_step("Step 16: Click Apply Filters button")

                filtered_count = _get_filtered_record_count(page)
                print(
                    f"[INFO] Filtered record count after Ship By Date filter: {filtered_count}"
                )
                if filtered_count == 0:
                    total_failures = _verify_pregen_failure_count_greater_than_zero(
                        page
                    )
                    if total_failures > 0:
                        print(
                            f"[WARN] {total_failures} PreGen Failure order(s) remain on "
                            "the dashboard but none match today's ship date — "
                            "Stage 1 will now run."
                        )
                    else:
                        print("[INFO] PreGen Failure count is 0 — Stage 1 will now run")
                    return 0

                _select_all_orders_on_page(page)
                _log_step("Step 17: Click select all on page checkbox")

                _open_bulk_action_dropdown(page)
                _log_step("Step 18: Click Select Bulk Action")

                _select_set_shipping_bulk_action(page)
                _log_step("Step 19: Select Set Shipping")

                _select_royal_mail_tracked_48_no_signature(page)
                _log_step("Step 20: Select RoyalMailClickAndDrop RMCD Tracked 48")

                try:
                    _submit_bulk_action(page)
                except Exception as exc:
                    msg = f"Pass 2 (Step 21): Bulk action submission failed or timed out: {exc}"
                    print(f"[WARN] {msg}")
                    _send_failure_email(
                        config,
                        "PreGen Failure: Bulk Submit Failed (Pass 2 Step 21)",
                        msg,
                    )
                    email_sent[0] = True
                    raise
                _log_step("Step 21: Click Submit Action")

                _select_all_orders_on_page(page, force_reselect=True)
                _log_step("Step 22: Click select all on page checkbox")

                _set_status_as_pregen(page)
                _log_step("Step 23: Click Set as PreGen")

                try:
                    _wait_for_pregen_count_zero(page)
                except RuntimeError as exc:
                    msg = f"Pass 2 (Step 24): Timed out waiting for PreGen queue to drain: {exc}"
                    print(f"[WARN] {msg}")
                    _send_failure_email(
                        config, "PreGen Failure: Queue Timeout (Pass 2 Step 24)", msg
                    )
                    email_sent[0] = True
                    raise
                _log_step("Step 24: Wait until PreGen status count is 0")

                _click_dashboard_sidebar_link(page)
                _log_step("Step 25: Click Dashboard sidebar link")

                if _click_pregen_failure_if_count_greater_than_zero(page):
                    _log_step("Step 26: Click Pregen failure")

                    _click_filters_button(page)
                    _log_step("Step 27: Click Filters button")

                    _click_ship_by_date_filter(page)
                    _log_step("Step 28: Click Ship By Date filter")

                    _fill_ship_by_date_today(page)
                    _log_step("Step 29: Fill Ship By Date with today's date")

                    _click_apply_filters(page)
                    _log_step("Step 30: Click Apply Filters button")

                    filtered_count = _get_filtered_record_count(page)
                    print(
                        f"[INFO] Filtered record count after Ship By Date filter: {filtered_count}"
                    )
                    if filtered_count == 0:
                        total_failures = _verify_pregen_failure_count_greater_than_zero(
                            page
                        )
                        if total_failures > 0:
                            print(
                                f"[WARN] {total_failures} PreGen Failure order(s) remain on "
                                "the dashboard but none match today's ship date — "
                                "Stage 1 will now run."
                            )
                        else:
                            print(
                                "[INFO] PreGen Failure count is 0 — Stage 1 will now run"
                            )
                        return 0

                    _select_all_orders_on_page(page)
                    _log_step("Step 31: Click select all on page checkbox")

                    _open_bulk_action_dropdown(page)
                    _log_step("Step 32: Click Select Bulk Action")

                    _select_set_shipping_bulk_action(page)
                    _log_step("Step 33: Select Set Shipping")

                    _select_royal_mail_tracked_48_no_signature(page)
                    _log_step("Step 34: Select RoyalMailClickAndDrop RMCD Tracked 48")

                    try:
                        _submit_bulk_action(page)
                    except Exception as exc:
                        msg = f"Pass 3 (Step 35): Bulk action submission failed or timed out: {exc}"
                        print(f"[WARN] {msg}")
                        _send_failure_email(
                            config,
                            "PreGen Failure: Bulk Submit Failed (Pass 3 Step 35)",
                            msg,
                        )
                        email_sent[0] = True
                        raise
                    _log_step("Step 35: Click Submit Action")

                    _select_all_orders_on_page(page, force_reselect=True)
                    _log_step("Step 36: Click select all on page checkbox")

                    _set_status_as_pregen(page)
                    _log_step("Step 37: Click Set as PreGen")

                    try:
                        _wait_for_pregen_count_zero(page)
                    except RuntimeError as exc:
                        msg = f"Pass 3 (Step 38): Timed out waiting for PreGen queue to drain: {exc}"
                        print(f"[WARN] {msg}")
                        _send_failure_email(
                            config,
                            "PreGen Failure: Queue Timeout (Pass 3 Step 38)",
                            msg,
                        )
                        email_sent[0] = True
                        raise
                    _log_step("Step 38: Wait until PreGen status count is 0")

                    _click_dashboard_sidebar_link(page)
                    _log_step("Step 39: Click Dashboard sidebar link")

                    if _click_pregen_failure_if_count_greater_than_zero(page):
                        _log_step("Step 40: Click Pregen failure")

                        try:
                            orders = _collect_order_links(page)
                        except RuntimeError as exc:
                            msg = f"Pass 4 (Step 41): Could not collect order links from the page: {exc}"
                            print(f"[WARN] {msg}")
                            _send_failure_email(
                                config,
                                "PreGen Failure: Cannot Collect Orders (Pass 4 Step 41)",
                                f"{msg}\n\nPlease open Helm and process the remaining PreGen Failure orders manually.",
                            )
                            orders = []
                        for index, order in enumerate(orders, start=1):
                            try:
                                _process_pregen_failure_order(
                                    page, order, index, config
                                )
                            except Exception as order_exc:
                                msg = (
                                    f"Pass 4 (Step 41[{index}]): "
                                    f"Order {order['order_id']} failed: {order_exc}"
                                )
                                print(f"[WARN] {msg}")
                                _send_failure_email(
                                    config,
                                    f"PreGen Failure: Order {order['order_id']} failed (Pass 4)",
                                    f"{msg}\n\nThis order needs to be resolved manually in Helm.",
                                )

                        remaining_count = _check_remaining_pregen_failure_orders(
                            page, config
                        )
                        _log_step("Step 41.5: Check remaining PreGen Failure orders")
                        if remaining_count > 0:
                            print(
                                f"[WARN] Stage 0 finished with {remaining_count} unresolved "
                                "PreGen Failure order(s). Stage 1 and Stage 2 will not run."
                            )
                            _send_manual_intervention_alert(config, remaining_count)
                            return remaining_count

            print("[INFO] PreGen Failure count is 0 — Stage 1 will now run")
            time.sleep(2)
            return 0
        except Exception as exc:
            error_msg = f"PreGen Failure automation stopped unexpectedly:\n{exc}"
            print(f"[WARN] {error_msg}")
            if not email_sent[0]:
                _send_failure_email(
                    config, "PreGen Failure Automation Error", error_msg
                )
            raise
        finally:
            try:
                context.close()
            finally:
                browser.close()

    return 0


if __name__ == "__main__":
    remaining = run(Config.load())
    if remaining > 0:
        sys.exit(1)
