#!/usr/bin/env python3
"""Telegram bot that scrapes gofile.io folder links and returns file download URLs.

Environment variables required:
  TELEGRAM_TOKEN – Telegram bot token from @BotFather

The bot authenticates to a gofile.io account before scraping any folder link,
ensuring that private or account-protected content is accessible.
"""

import logging
import os
import re
import time
from urllib.parse import unquote

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import WebDriverException
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN: str = os.environ.get("TELEGRAM_TOKEN", "")

# Direct-login URL for the gofile.io account.
# Override via the GOFILE_LOGIN_URL environment variable to use a different account.
LOGIN_URL: str = os.environ.get(
    "GOFILE_LOGIN_URL",
    "https://gofile.io/login/xsL5YvfvbsZAcA4Zl5ducfnvQtqg2pq5",
)

GOFILE_FOLDER_RE = re.compile(r"https?://(?:www\.)?gofile\.io/d/(\w+)")
GOFILE_DOWNLOAD_RE = re.compile(
    r"https://[A-Za-z0-9\-]+\.gofile\.io/download/[^\s\"'<>]+"
)

# gofile.io website token embedded in their JS bundle (required for API calls).
# Override via GOFILE_WEBSITE_TOKEN if the default token stops working.
_GOFILE_WEBSITE_TOKEN: str = os.environ.get("GOFILE_WEBSITE_TOKEN", "4fd6sg89d7s6")

# How long (seconds) to wait after the login redirect for cookies to be set.
_LOGIN_WAIT_SECONDS: int = 6

# How long (seconds) to wait for the React SPA to fully render a folder page.
_PAGE_RENDER_WAIT_SECONDS: int = 5

# Timeout (seconds) for REST API requests.
_API_REQUEST_TIMEOUT: int = 30

# Maximum characters per Telegram message (Telegram limit is 4096;
# we use 4000 to leave a safe buffer for any formatting overhead).
_MAX_MESSAGE_LEN: int = 4000

# ---------------------------------------------------------------------------
# Browser / session state
# ---------------------------------------------------------------------------

_driver: "webdriver.Firefox | None" = None
_authenticated: bool = False


def _build_driver() -> webdriver.Firefox:
    """Create a headless Firefox WebDriver instance."""
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--width=1920")
    options.add_argument("--height=1080")
    return webdriver.Firefox(options=options)


def _get_driver() -> webdriver.Firefox:
    global _driver
    if _driver is None:
        logger.info("Starting Firefox browser…")
        _driver = _build_driver()
    return _driver


def _login() -> None:
    """Authenticate to gofile.io using the direct-login URL."""
    global _authenticated
    driver = _get_driver()
    logger.info("Authenticating with gofile.io via %s", LOGIN_URL)
    driver.get(LOGIN_URL)
    # Allow the login redirect and cookie-set to complete.
    time.sleep(_LOGIN_WAIT_SECONDS)
    logger.info("Authentication step completed. Current URL: %s", driver.current_url)
    _authenticated = True


def _ensure_auth() -> None:
    if not _authenticated:
        _login()


# ---------------------------------------------------------------------------
# File-extraction helpers
# ---------------------------------------------------------------------------


def _get_account_token() -> "str | None":
    """Extract the gofile.io account token from the browser's localStorage."""
    driver = _get_driver()
    try:
        token = driver.execute_script("return localStorage.getItem('accountToken')")
        if token:
            logger.info("Account token extracted from localStorage.")
            return str(token)
    except Exception as exc:
        logger.debug("localStorage access failed: %s", exc)
    return None


def _get_website_token() -> str:
    """Try to read the website token from the page; fall back to the known constant."""
    driver = _get_driver()
    try:
        wt = driver.execute_script(
            "return window._wt "
            "|| document.querySelector('meta[name=\"wt\"]')?.getAttribute('content')"
        )
        if wt:
            return str(wt)
    except Exception:
        pass
    return _GOFILE_WEBSITE_TOKEN


def _build_requests_session() -> requests.Session:
    """Build a requests.Session populated with browser cookies for gofile.io."""
    driver = _get_driver()
    session = requests.Session()
    try:
        for cookie in driver.get_cookies():
            if "gofile.io" in cookie.get("domain", ""):
                session.cookies.set(cookie["name"], cookie["value"])
    except Exception as exc:
        logger.debug("Could not transfer browser cookies: %s", exc)
    return session


def _scrape_via_api(folder_id: str) -> "list[dict]":
    """Use the gofile.io REST API to list folder contents (preferred method)."""
    token = _get_account_token()
    if not token:
        logger.info("No account token available; skipping API scrape.")
        return []

    wt = _get_website_token()
    session = _build_requests_session()

    url = f"https://api.gofile.io/contents/{folder_id}"
    params: dict = {"token": token, "wt": wt}

    try:
        resp = session.get(url, params=params, timeout=_API_REQUEST_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        logger.warning("API request failed: %s", exc)
        return []

    if body.get("status") != "ok":
        logger.warning("API returned status=%s", body.get("status"))
        return []

    contents = body.get("data", {}).get("contents") or {}
    files: list[dict] = []
    for item in contents.values():
        if item.get("type") != "file":
            continue
        files.append(
            {
                "name": item.get("name", ""),
                "url": item.get("link") or item.get("directLink", ""),
                "size": item.get("size"),
                "md5": item.get("md5"),
                "created": item.get("createTime"),
            }
        )
    return files


def _scrape_via_dom(url: str) -> "list[dict]":
    """Navigate to the folder page in the authenticated browser and scrape download links."""
    driver = _get_driver()
    logger.info("Loading folder page via browser: %s", url)
    driver.get(url)
    # Allow the React SPA to render the file list.
    time.sleep(_PAGE_RENDER_WAIT_SECONDS)

    files: list[dict] = []
    seen: set = set()

    # Pass 1 – collect <a> tags whose href contains a gofile download path.
    for element in driver.find_elements(By.TAG_NAME, "a"):
        try:
            href = element.get_attribute("href") or ""
        except Exception:
            continue
        if ".gofile.io/download" in href and href not in seen:
            seen.add(href)
            files.append({"name": unquote(href.rstrip("/").split("/")[-1]), "url": href})

    if files:
        return files

    # Pass 2 – regex-scan the raw page source as a fallback.
    try:
        for raw_url in GOFILE_DOWNLOAD_RE.findall(driver.page_source):
            clean = raw_url.rstrip("\"'\\")
            if clean not in seen:
                seen.add(clean)
                files.append(
                    {"name": unquote(clean.rstrip("/").split("/")[-1]), "url": clean}
                )
    except Exception as exc:
        logger.warning("Page-source scan failed: %s", exc)

    return files


def scrape_folder(folder_url: str) -> "list[dict]":
    """Return a list of file dicts for all files in the given gofile.io folder URL."""
    global _driver, _authenticated

    _ensure_auth()

    match = GOFILE_FOLDER_RE.search(folder_url)
    folder_id = match.group(1) if match else None

    # Preferred path: REST API (structured, reliable).
    if folder_id:
        files = _scrape_via_api(folder_id)
        if files:
            return files

    # Fallback: Selenium DOM scraping in the authenticated browser.
    try:
        files = _scrape_via_dom(folder_url)
        if not files and folder_id:
            # Session may have expired – re-authenticate once and retry.
            logger.info("No files found; re-authenticating and retrying…")
            _authenticated = False
            _login()
            files = _scrape_via_dom(folder_url)
        return files
    except WebDriverException as exc:
        logger.error("WebDriver error during scrape: %s", exc)
        # Tear down the broken driver so the next call starts fresh.
        try:
            if _driver:
                _driver.quit()
        except Exception:
            pass
        _driver = None
        _authenticated = False
        raise


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_size(size_bytes: "int | None") -> str:
    """Return a human-readable file size string."""
    if size_bytes is None:
        return ""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _build_reply(files: "list[dict]") -> str:
    """Format the file list into a plain-text Telegram message."""
    lines = [f"📁 Found {len(files)} file(s):\n"]
    for i, f in enumerate(files, 1):
        name = f.get("name") or "Unknown"
        url = f.get("url") or ""
        extras: list[str] = []
        if f.get("size"):
            extras.append(_fmt_size(f["size"]))
        if f.get("md5"):
            extras.append(f"MD5: {f['md5']}")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"{i}. 📄 {name}{extra_str}")
        lines.append(f"   ⬇️ {url}")
        lines.append("")
    return "\n".join(lines).strip()


async def _send_reply(
    update: Update, status_msg: "object", text: str
) -> None:
    """Edit the status message with the reply, splitting if it exceeds 4000 chars."""
    if len(text) <= _MAX_MESSAGE_LEN:
        await status_msg.edit_text(text)
        return

    # Split on newlines so we don't cut mid-line.
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > _MAX_MESSAGE_LEN and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))

    await status_msg.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Telegram message handler
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming Telegram messages; process gofile.io folder links."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = GOFILE_FOLDER_RE.search(text)
    if not match:
        return  # Ignore messages without a gofile.io folder link.

    folder_url = match.group(0)
    status_msg = await update.message.reply_text(f"🔍 Scanning {folder_url} …")

    try:
        files = scrape_folder(folder_url)
    except Exception as exc:
        logger.exception("Unhandled error while scraping %s", folder_url)
        await status_msg.edit_text(f"❌ Unexpected error: {str(exc)[:300]}")
        return

    if not files:
        await status_msg.edit_text(
            "❌ No files found.\n"
            "The folder may be empty, private, or the link is invalid."
        )
        return

    reply = _build_reply(files)
    await _send_reply(update, status_msg, reply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable is required")

    logger.info("Initializing gofile.io browser session…")
    _login()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is polling for messages.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
