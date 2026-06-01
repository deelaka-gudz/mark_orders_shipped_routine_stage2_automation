import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

from automation_stage01 import (
    LoginFlow,
    _download_helm_export_url,
    _log_info,
    _log_step,
    _origin_url,
    _save_download,
    _wait_for_network_idle,
    open_reports_page,
)

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


@dataclass(frozen=True)
class Config:
    helm_url: str
    email: str
    password: str
    download_dir: Path
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
                os.getenv("HELM_URL") or "https://mybeautyandcareltd.myhelm.app/"
            ).strip(),
            email=_require_env("HELM_EMAIL").strip(),
            password=_require_env("HELM_PASSWORD"),
            download_dir=Path(os.getenv("HELM_REPORT_DOWNLOAD_DIR") or "downloads"),
            helm_report_ready_timeout_seconds=int(
                os.getenv("HELM_REPORT_READY_TIMEOUT_SECONDS") or "2400"
            ),
            headless=_env_flag("HEADLESS", default=False),
            debug=_env_flag("DEBUG", default=False),
            helm_manual_login_fallback=_env_flag(
                "HELM_MANUAL_LOGIN_FALLBACK", default=True
            ),
            helm_manual_login_timeout_seconds=int(
                os.getenv("HELM_MANUAL_LOGIN_TIMEOUT_SECONDS") or "300"
            ),
        )


def open_orders_reports_section(page: Page) -> None:
    page.locator("a[data-section='orders'], a[href='#orders']").first.click(timeout=10000)
    _wait_for_network_idle(page)


def download_full_orders_report(page: Page, config: Config) -> Path:
    config.download_dir.mkdir(parents=True, exist_ok=True)

    request_full_orders_report_export(page, config)

    return download_completed_full_orders_report_from_history(page, config)


def request_full_orders_report_export(page: Page, config: Config) -> None:
    reports_path = "/reports-new/download"
    if reports_path not in page.url:
        page.goto(
            f"{_origin_url(config.helm_url)}{reports_path}",
            wait_until="domcontentloaded",
        )
        _wait_for_network_idle(page)
        open_orders_reports_section(page)

    _log_step("Step 2.3: Click Full Orders Report download/request button")
    if not _click_full_orders_report_request(page):
        raise RuntimeError("Could not find the Full Orders Report export request button.")

    _wait_for_network_idle(page)
    _log_step("Step 2.4: Open Helm Export History")
    page.goto(
        f"{_origin_url(config.helm_url)}/reports-new/history",
        wait_until="domcontentloaded",
    )
    _wait_for_network_idle(page)


def _click_full_orders_report_request(page: Page) -> bool:
    return bool(page.evaluate("""() => {
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const exactButton = document.querySelector(
                    "form[data-report-name='dc_full_orders_report'] " +
                    "input[name='create_report'], " +
                    "form[data-report-name='dc_full_orders_report'] " +
                    "button[type='submit'], " +
                    "input[export-service-button='3']"
                );
                if (exactButton && isVisible(exactButton)) {
                    exactButton.scrollIntoView({block: 'center', inline: 'center'});
                    exactButton.click();
                    return true;
                }

                const candidates = Array.from(
                    document.querySelectorAll(
                        "input[name='create_report'], input[type='submit'], button, a"
                    )
                ).filter(isVisible);

                for (const candidate of candidates) {
                    const form = candidate.closest('form');
                    const panel = candidate.closest('.panel');
                    const haystack = [
                        candidate.innerText,
                        candidate.textContent,
                        candidate.value,
                        candidate.getAttribute('title'),
                        candidate.getAttribute('aria-label'),
                        candidate.getAttribute('name'),
                        form?.getAttribute('data-report-name'),
                        form?.getAttribute('action'),
                        panel?.innerText,
                        panel?.getAttribute('export-service-id')
                    ].filter(Boolean).join(' ');

                    const isFullOrders = /full\\s+orders\\s+report|dc_full_orders_report|full_order_export/i.test(haystack);
                    const canRequest = /create|request|export|download|report/i.test(haystack);

                    if (isFullOrders && canRequest) {
                        candidate.scrollIntoView({block: 'center', inline: 'center'});
                        candidate.click();
                        return true;
                    }
                }

                return false;
            }"""))


def download_completed_full_orders_report_from_history(
    page: Page,
    config: Config,
) -> Path:
    _log_step("Step 2.5: Check newest Full Orders Report status in Export History")
    deadline = time.monotonic() + config.helm_report_ready_timeout_seconds
    last_logged_status: str | None = None
    retry_count = 0
    max_retries = 3

    while time.monotonic() < deadline:
        status = _latest_full_orders_report_status(page)
        normalized_status = (status or "not found").strip()
        elapsed_seconds = int(
            config.helm_report_ready_timeout_seconds
            - max(0, deadline - time.monotonic())
        )
        remaining_seconds = max(0, int(deadline - time.monotonic()))
        if normalized_status != last_logged_status or config.debug:
            print(
                "[WAIT] Step 2.6: Full Orders Report status is "
                f"'{normalized_status}'. Waiting up to {remaining_seconds}s more "
                f"(elapsed {elapsed_seconds}s)."
            )
            last_logged_status = normalized_status

        if normalized_status.lower() in {"cancelled", "failed"}:
            if retry_count >= max_retries:
                raise RuntimeError(
                    f"Latest Full Orders Report status is '{normalized_status}' after "
                    f"{retry_count} retry attempts."
                )
            retry_count += 1
            print(
                "[WARN] Step 2.6: Full Orders Report export ended as "
                f"'{normalized_status}'. Requesting a fresh export "
                f"(attempt {retry_count}/{max_retries})."
            )
            request_full_orders_report_export(page, config)
            last_logged_status = None
            continue

        if status and status.lower() == "completed":
            _log_step("Step 2.7: Full Orders Report is completed and ready to download")
            download_url = _latest_full_orders_report_download_url(page)
            if download_url:
                downloaded_path = _download_helm_export_url(
                    page, download_url, config.download_dir
                )
                _log_step("Step 2.8: Downloaded Full Orders Report from History URL")
                return downloaded_path

            with page.expect_download(timeout=60000) as download_info:
                if not _click_latest_full_orders_report_download(page):
                    raise RuntimeError(
                        "Latest Full Orders Report is completed, but no download URL "
                        "or download action button/link was found."
                    )
            downloaded_path = _save_download(download_info.value, config.download_dir)
            _log_step("Step 2.8: Downloaded Full Orders Report using History button")
            return downloaded_path

        page.wait_for_timeout(10000)
        page.reload(wait_until="domcontentloaded")
        _wait_for_network_idle(page)

    raise RuntimeError("Timed out waiting for the Helm Full Orders Report export.")


def _latest_full_orders_report_status(page: Page) -> str | None:
    return page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
            const row = rows.find(el => {
                const cells = Array.from(el.querySelectorAll('td'));
                return cells.length >= 8 && /^Full Orders Report$/i.test(cells[1].innerText.trim());
            });
            if (!row) return null;
            const cells = Array.from(row.querySelectorAll('td'));
            return cells[3]?.innerText.trim() || null;
        }""")


def _latest_full_orders_report_download_url(page: Page) -> str | None:
    return page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
            const row = rows.find(el => {
                const cells = Array.from(el.querySelectorAll('td'));
                return cells.length >= 8 && /^Full Orders Report$/i.test(cells[1].innerText.trim());
            });
            if (!row) return null;

            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return null;

            const link = row.querySelector(
                "td:last-child a[download][href*='dc_full_orders_report'], " +
                "td:last-child a[href*='dc_full_orders_report'], " +
                "td:last-child a[download][href*='/full-orders'], " +
                "td:last-child a[download][href*='/orders-'], " +
                "td:last-child a[download]"
            );
            return link?.href || null;
        }""")


def _click_latest_full_orders_report_download(page: Page) -> bool:
    return bool(page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tbody tr, tr'));
            const row = rows.find(el => {
                const cells = Array.from(el.querySelectorAll('td'));
                return cells.length >= 8 && /^Full Orders Report$/i.test(cells[1].innerText.trim());
            });
            if (!row) return false;

            const cells = Array.from(row.querySelectorAll('td'));
            if (!/^Completed$/i.test(cells[3]?.innerText.trim() || '')) return false;

            const action = row.querySelector(
                "td:last-child a[download][href*='dc_full_orders_report'], " +
                "td:last-child a[href*='dc_full_orders_report'], " +
                "td:last-child a[download][href*='/full-orders'], " +
                "td:last-child a[download][href*='/orders-'], " +
                "td:last-child a[download]"
            );

            if (!action) return false;
            action.scrollIntoView({block: 'center', inline: 'center'});
            action.click();
            return true;
        }"""))


def run_stage2_steps(page: Page, config: Config) -> None:
    _log_info(config.debug, f"Stage 2 starting from: {page.url}")

    open_reports_page(page, config)
    _log_step("Step 2.1: Open Reports page")

    open_orders_reports_section(page)
    _log_step("Step 2.2: Open Orders reports section")

    downloaded_path = download_full_orders_report(page, config)
    _log_step(f"Step 3: Download Full Orders Report to {downloaded_path}")

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

            time.sleep(2)
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    run(Config.load())
