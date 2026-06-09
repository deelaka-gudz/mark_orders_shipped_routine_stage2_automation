import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DOTENV_PATH = Path(__file__).resolve().with_name(".env")
AMAZON_HOME_URL = "https://www.amazon.co.uk/"


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


def _log_step(step: str) -> None:
    print(f"[DONE] {step}")


def _log_info(debug: bool, message: str) -> None:
    if debug:
        print(f"[INFO] {message}")


def _wait_for_network_idle(page, timeout_ms: int = 10000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        pass


def open_amazon_account_list_flyout(page) -> None:
    candidates = [
        page.locator("#nav-link-accountList"),
        page.locator("#nav-link-accountList .nav-flyout-button"),
        page.get_by_role("link", name=re.compile("account", re.I)),
        page.get_by_text(re.compile("Hello, sign in", re.I)),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=10000)
            target.hover(timeout=10000)
            try:
                page.locator(".nav-action-signin-button").first.wait_for(
                    state="visible",
                    timeout=5000,
                )
                return
            except PlaywrightTimeoutError:
                target.click(timeout=10000)
                page.locator(".nav-action-signin-button").first.wait_for(
                    state="visible",
                    timeout=10000,
                )
                return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not open Amazon Account & Lists flyout.") from last_error


def click_amazon_flyout_sign_in(page) -> None:
    candidates = [
        page.locator(".nav-action-signin-button[data-nav-role='signin']"),
        page.locator("#nav-flyout-ya-signin .nav-action-signin-button"),
        page.get_by_role("link", name=re.compile("^sign in$", re.I)),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=10000)
            target.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            _wait_for_network_idle(page)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not click Amazon flyout Sign in button.") from last_error


def fill_amazon_email(page, email: str) -> None:
    candidates = [
        page.locator("#ap_email_login"),
        page.locator("input[name='email']"),
        page.get_by_label(re.compile("mobile number|email", re.I)),
        page.locator("input[autocomplete='username']"),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=15000)
            field.click(timeout=10000)
            field.fill(email, timeout=10000)
            field.dispatch_event("input", timeout=5000)
            field.dispatch_event("change", timeout=5000)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not fill Amazon email input.") from last_error


def click_amazon_continue(page) -> None:
    candidates = [
        page.locator("#continue"),
        page.locator("input[type='submit'][aria-labelledby='continue-announce']"),
        page.get_by_role("button", name=re.compile("^continue$", re.I)),
        page.locator("span#continue input[type='submit']"),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            button = locator.first
            button.wait_for(state="visible", timeout=10000)
            button.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            _wait_for_network_idle(page)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not click Amazon Continue button.") from last_error


def fill_amazon_password(page, password: str) -> None:
    candidates = [
        page.locator("#ap_password"),
        page.locator("input[name='password']"),
        page.get_by_label(re.compile("^password$", re.I)),
        page.locator("input[autocomplete='current-password']"),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=15000)
            field.click(timeout=10000)
            field.fill(password, timeout=10000)
            field.dispatch_event("input", timeout=5000)
            field.dispatch_event("change", timeout=5000)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not fill Amazon password input.") from last_error


def click_amazon_sign_in_submit(page) -> None:
    candidates = [
        page.locator("#signInSubmit"),
        page.locator("#auth-signin-button input[type='submit']"),
        page.get_by_role("button", name=re.compile("^sign in$", re.I)),
        page.locator(
            "input[type='submit'][aria-labelledby='auth-signin-button-announce']"
        ),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            button = locator.first
            button.wait_for(state="visible", timeout=10000)
            button.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            _wait_for_network_idle(page)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not click Amazon Sign in submit button.") from last_error


def fill_amazon_otp(page, otp_code: str) -> None:
    candidates = [
        page.locator("#auth-mfa-otpcode"),
        page.locator("input[name='otpCode']"),
        page.get_by_label(re.compile("enter code", re.I)),
        page.locator("input[type='tel'][autocomplete='off']"),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=30000)
            field.click(timeout=10000)
            field.fill(otp_code, timeout=10000)
            field.dispatch_event("input", timeout=5000)
            field.dispatch_event("change", timeout=5000)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not fill Amazon OTP input.") from last_error


def click_amazon_remember_device(page) -> None:
    candidates = [
        page.locator("label[for='auth-mfa-remember-device']"),
        page.locator("#auth-mfa-remember-device"),
        page.get_by_label(re.compile("don't require code|do not require code", re.I)),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            target = locator.first
            target.wait_for(state="visible", timeout=10000)
            target.click(timeout=10000)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError(
        "Could not click Amazon remember-device checkbox."
    ) from last_error


def click_amazon_mfa_sign_in(page) -> None:
    candidates = [
        page.locator("input#auth-signin-button[name='mfaSubmit']"),
        page.locator("input[name='mfaSubmit']"),
        page.locator("#a-autoid-0 input[type='submit']"),
        page.get_by_role("button", name=re.compile("^sign in$", re.I)),
    ]

    last_error: Exception | None = None
    for locator in candidates:
        try:
            button = locator.first
            button.wait_for(state="visible", timeout=10000)
            button.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            _wait_for_network_idle(page)
            return
        except (PlaywrightTimeoutError, PlaywrightError) as error:
            last_error = error

    raise RuntimeError("Could not click Amazon MFA Sign in button.") from last_error


@dataclass(frozen=True)
class Config:
    amazon_url: str
    amazon_email: str
    amazon_password: str
    amazon_otp: str
    headless: bool
    debug: bool

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")
        return Config(
            amazon_url=AMAZON_HOME_URL,
            amazon_email=(
                os.getenv("AMAZON_EMAIL") or _require_env("HELM_EMAIL")
            ).strip(),
            amazon_password=(
                os.getenv("AMAZON_PASSWORD") or _require_env("HELM_PASSWORD")
            ),
            amazon_otp=_require_env("AMAZON_OTP").strip(),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
        )


def run(config: Config) -> None:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"Amazon URL: {config.amazon_url}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(config.amazon_url, wait_until="domcontentloaded")
            _wait_for_network_idle(page)
            _log_step("Step 1: Opened Amazon UK home page")
            open_amazon_account_list_flyout(page)
            _log_step("Step 2: Opened Amazon Account & Lists flyout")
            click_amazon_flyout_sign_in(page)
            _log_step("Step 3: Clicked Amazon flyout Sign in button")
            fill_amazon_email(page, config.amazon_email)
            _log_step("Step 4: Entered Amazon email")
            click_amazon_continue(page)
            _log_step("Step 5: Clicked Amazon Continue button")
            fill_amazon_password(page, config.amazon_password)
            _log_step("Step 6: Entered Amazon password")
            click_amazon_sign_in_submit(page)
            _log_step("Step 7: Clicked Amazon Sign in submit button")
            fill_amazon_otp(page, config.amazon_otp)
            _log_step("Step 8: Entered Amazon OTP code")
            click_amazon_remember_device(page)
            _log_step("Step 9: Selected Amazon remember-device checkbox")
            click_amazon_mfa_sign_in(page)
            _log_step("Step 10: Clicked Amazon MFA Sign in button")
            time.sleep(2)
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    run(Config.load())
