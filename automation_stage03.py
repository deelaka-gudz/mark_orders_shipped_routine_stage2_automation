import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DOTENV_PATH = Path(__file__).resolve().with_name(".env")
AMAZON_HOME_URL = "https://sellercentral.amazon.co.uk/"
OTP_REQUIRED_RETURN_CODE = 8
OTP_INPUT_TIMEOUT_SECONDS = 10


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


def click_seller_central_log_in(page) -> None:
    candidates = [
        page.locator("#SCUK_SOA_WP_LOGIN_N_1374680C a[href*='/signin']"),
        page.locator("a[href*='sellercentral.amazon.co.uk/signin']"),
        page.get_by_role("link", name=re.compile("^log in$", re.I)),
        page.get_by_text(re.compile("^log in$", re.I)),
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

    raise RuntimeError("Could not click Seller Central Log in link.") from last_error


def fill_amazon_email(page, email: str) -> None:
    candidates = [
        page.locator("#ap_email"),
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
        page.locator("input#continue[type='submit']"),
        page.locator("span#continue input[type='submit']"),
        page.locator("input[type='submit'][aria-labelledby='continue-announce']"),
        page.get_by_role("button", name=re.compile("^continue$", re.I)),
        page.locator("#continue"),
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
    if not re.fullmatch(r"\d{6}", otp_code):
        raise RuntimeError("Amazon OTP code must be exactly 6 digits.")

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


def amazon_otp_field_has_six_digits(page) -> bool:
    try:
        field = page.locator("#auth-mfa-otpcode").first
        field.wait_for(state="visible", timeout=1000)
        value = str(field.input_value(timeout=1000) or "").strip()
        return bool(re.fullmatch(r"\d{6}", value))
    except (PlaywrightTimeoutError, PlaywrightError):
        return False


def wait_for_manual_browser_otp(page, timeout_seconds: int) -> bool:
    print(
        f"[ACTION] Enter the 6-digit Amazon OTP in the browser within "
        f"{timeout_seconds}s, or type it in this terminal."
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if amazon_otp_field_has_six_digits(page):
            return True
        time.sleep(0.25)
    return False


def amazon_otp_or_prompt(config) -> str | None:
    otp_code = str(config.amazon_otp or "").strip()
    if re.fullmatch(r"\d{6}", otp_code):
        return otp_code

    if not config.headless and sys.stdin.isatty():
        return prompt_for_otp_with_timeout(OTP_INPUT_TIMEOUT_SECONDS)

    return None


def prompt_for_otp_with_timeout(timeout_seconds: int) -> str | None:
    if os.name == "nt":
        return prompt_for_otp_with_timeout_windows(timeout_seconds)

    print(
        f"Enter the current 6-digit Amazon OTP code within {timeout_seconds}s: ",
        end="",
        flush=True,
    )
    return None


def prompt_for_otp_with_timeout_windows(timeout_seconds: int) -> str | None:
    import msvcrt

    print(
        f"Enter the current 6-digit Amazon OTP code within {timeout_seconds}s: ",
        end="",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    digits: list[str] = []

    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue

        character = msvcrt.getwch()
        if character in {"\r", "\n"}:
            break
        if character == "\003":
            raise KeyboardInterrupt
        if character in {"\b", "\x7f"}:
            if digits:
                digits.pop()
                print("\b \b", end="", flush=True)
            continue
        if character.isdigit() and len(digits) < 6:
            digits.append(character)
            print(character, end="", flush=True)
            if len(digits) == 6:
                break

    print()
    otp_code = "".join(digits)
    if re.fullmatch(r"\d{6}", otp_code):
        return otp_code

    print("Amazon OTP was not entered within 10 seconds or was not 6 digits.")
    return None


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
            amazon_otp=str(os.getenv("AMAZON_OTP")).strip(),
            headless=_env_flag(
                "AUTOMATION_HEADLESS", default=_env_flag("HEADLESS", default=False)
            ),
            debug=_env_flag("DEBUG", default=False),
        )


def run(config: Config) -> int:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"Amazon URL: {config.amazon_url}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(config.amazon_url, wait_until="domcontentloaded")
            _wait_for_network_idle(page)
            _log_step("Step 1: Opened Amazon Seller Central UK")
            click_seller_central_log_in(page)
            _log_step("Step 2: Clicked Seller Central Log in link")
            fill_amazon_email(page, config.amazon_email)
            _log_step("Step 3: Entered Amazon email")
            click_amazon_continue(page)
            _log_step("Step 4: Clicked Amazon Continue button")
            fill_amazon_password(page, config.amazon_password)
            _log_step("Step 5: Entered Amazon password")
            click_amazon_sign_in_submit(page)
            _log_step("Step 6: Clicked Amazon Sign in submit button")
            amazon_otp = amazon_otp_or_prompt(config)
            if amazon_otp:
                fill_amazon_otp(page, amazon_otp)
                _log_step("Step 7: Entered Amazon OTP code")
            elif not config.headless and wait_for_manual_browser_otp(
                page,
                OTP_INPUT_TIMEOUT_SECONDS,
            ):
                _log_step("Step 7: Confirmed Amazon OTP code was entered manually")
            else:
                print(
                    "[OTP_REQUIRED] Step 7: Amazon OTP code is required. "
                    "Enter the current authenticator code in the Streamlit modal."
                )
                return OTP_REQUIRED_RETURN_CODE
            click_amazon_mfa_sign_in(page)
            _log_step("Step 8: Clicked Amazon MFA Sign in button")
            time.sleep(2)
            return 0
        finally:
            try:
                context.close()
            finally:
                browser.close()


if __name__ == "__main__":
    sys.exit(run(Config.load()))
