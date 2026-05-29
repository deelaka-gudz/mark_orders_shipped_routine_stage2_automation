import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

from automation_stage01 import LoginFlow, _log_info, _log_step

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
            headless=_env_flag("HEADLESS", default=False),
            debug=_env_flag("DEBUG", default=False),
            helm_manual_login_fallback=_env_flag(
                "HELM_MANUAL_LOGIN_FALLBACK", default=True
            ),
            helm_manual_login_timeout_seconds=int(
                os.getenv("HELM_MANUAL_LOGIN_TIMEOUT_SECONDS") or "300"
            ),
        )


def run_stage2_steps(page: Page, config: Config) -> None:
    _log_info(config.debug, f"Stage 2 starting from: {page.url}")
    _log_step("Stage 2: Ready for next instructions")


def run(config: Config) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
            _log_info(config.debug, f"HELM_URL: {config.helm_url}")

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
